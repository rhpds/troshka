"""Read-only employer money evidence, independent of quota/capacity readiness.

Observed source rows are not proof of full monetary coverage. Nonempty scopes
therefore expose known subtotals with null authoritative totals until a reviewed
monetary coverage contract exists. Owner documents never supply employer money.
"""

from bisect import bisect_left
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import String, and_, cast, or_
from sqlalchemy.orm import Session

from app.models.elastic_ip import ElasticIp
from app.models.managed_group import ManagedGroup
from app.models.metering import MeteringInterval, MonthlyStatement, ProjectInvoice
from app.models.project import Project
from app.models.quota import QuotaAllocation, QuotaCharge, QuotaReservation
from app.models.user import User
from app.schemas.quota import AccountingUnavailable
from app.services.metering_math import month_bounds
from app.services.quota_accounting import interval_amount, summarize, utc

COVERAGE_REASON = "Full monetary source coverage has not been verified"
STAMP_KEYS = {
    "project_id",
    "project_name",
    "owner_id",
    "owner_name",
    "organization_id",
    "organization_name",
}


def scoped_members(
    db: Session, group_id: str, owner_id: str | None = None
) -> list[User]:
    query = db.query(User).filter(User.organization_id == group_id)
    if owner_id is not None:
        query = query.filter(User.id == owner_id)
    members = query.order_by(User.email).populate_existing().all()
    if owner_id is not None and not members:
        raise HTTPException(404, "Group member not found")
    return members


def _source_snapshot(db, group_id, project_ids):
    """One MVCC statement for sources AND charges: never mix cursor generations.

    FULL JOIN includes sources without segments and retained/imported charges
    without sources. Identity-map refresh prevents a cached cursor from combining
    with freshly committed segments. No locks, accrual or provider polling.
    """
    pairs = (
        db.query(MeteringInterval, QuotaCharge)
        .select_from(MeteringInterval)
        .join(
            QuotaCharge,
            and_(
                QuotaCharge.source == "interval",
                QuotaCharge.source_id == cast(MeteringInterval.id, String),
            ),
            full=True,
        )
        .filter(
            or_(
                MeteringInterval.project_id.in_(project_ids),
                MeteringInterval.quota_attribution["organization_id"].as_string()
                == group_id,
                QuotaCharge.organization_id == group_id,
            )
        )
        .populate_existing()
        .all()
    )
    sources, charges = {}, {}
    for source, charge in pairs:
        if source is not None:
            sources[source.id] = source
        if charge is not None:
            charges[charge.id] = charge
    return list(sources.values()), list(charges.values())


def _valid_stamp(row):
    stamp = row.quota_attribution
    if not isinstance(stamp, dict) or set(stamp) != STAMP_KEYS:
        return False
    if not all(isinstance(value, str) and value for value in stamp.values()):
        return False
    return stamp["project_id"] == row.project_id


def _valid_source(row):
    if not _valid_stamp(row):
        return False
    values = [Decimal(str(value)) for value in (row.qty, row.unit_rate)]
    return all(value.is_finite() and value >= 0 for value in values)


def _overlapping_ids(spans):
    """A furthest-ending witness marks every nested/chain conflict once."""
    invalid = set()
    furthest = None
    for start, end, identity in sorted(spans):
        if furthest is not None and start < furthest[1]:
            invalid.update((furthest[2], identity))
        if furthest is None or end > furthest[1]:
            furthest = (start, end, identity)
    return invalid


def _resource_overlaps(rows, now):
    return _overlapping_ids(
        (utc(row.started_at), utc(row.ended_at or now), row.id) for row in rows
    )


def _summary_index(charges):
    """Per-project sorted starts and prefix maximum ends permit binary search."""
    projects = defaultdict(list)
    for charge in charges:
        if charge.source != "interval":
            projects[charge.project_id].append(
                (utc(charge.start_at), utc(charge.end_at))
            )
    index = {}
    for project_id, spans in projects.items():
        starts, ends = [], []
        for start, end in sorted(spans):
            starts.append(start)
            ends.append(max(end, ends[-1]) if ends else end)
        index[project_id] = (starts, ends)
    return index


def _summary_overlaps(project_id, start, end, summaries):
    bounds = summaries.get(project_id)
    if bounds is None:
        return False
    starts, ends = bounds
    position = bisect_left(starts, utc(end)) - 1
    return position >= 0 and ends[position] > utc(start)


def _ambiguous_sources(sources, summaries, now):
    """Reject overlapping detailed resources and materialized summary coverage."""
    invalid = set()
    resources = defaultdict(list)
    for row in sources:
        resources[(row.project_id, row.kind, row.resource_id)].append(row)
        if _summary_overlaps(
            row.project_id, row.started_at, row.ended_at or now, summaries
        ):
            invalid.add(row.id)
    for rows in resources.values():
        invalid.update(_resource_overlaps(rows, now))
    return invalid


def _cursor_valid(row, segments):
    cursor = utc(row.quota_accrued_through or row.started_at)
    position = utc(row.started_at)
    if cursor < position or (row.ended_at and cursor > utc(row.ended_at)):
        return False
    for segment in sorted(segments, key=lambda item: utc(item.start_at)):
        if utc(segment.start_at) != position or utc(segment.end_at) > cursor:
            return False
        if any(
            getattr(segment, key) != value
            for key, value in row.quota_attribution.items()
        ):
            return False
        if (
            segment.kind != row.kind
            or segment.quantity != row.qty
            or segment.rate != row.unit_rate
        ):
            return False
        position = utc(segment.end_at)
    return position == cursor


def _tails(row, now):
    """Transient charge-shaped projections share accrual's cumulative rounding."""
    start = utc(row.quota_accrued_through or row.started_at)
    end = min(utc(row.ended_at), now) if row.ended_at else now
    while start < end:
        month_start, month_end = month_bounds(start.year, start.month)
        through = min(end, month_end)
        anchor = max(month_start, utc(row.started_at))
        amount = interval_amount(
            row.qty, row.unit_rate, anchor, through
        ) - interval_amount(row.qty, row.unit_rate, anchor, start)
        yield QuotaCharge(
            **row.quota_attribution,
            source="interval",
            source_id=row.id,
            start_at=start,
            end_at=through,
            kind=row.kind,
            quantity=row.qty,
            rate=row.unit_rate,
            amount=amount,
            provenance={"interval_started_at": utc(row.started_at).isoformat()},
        )
        start = through


def _evidence(sources, charges, group_id, now):
    summaries = _summary_index(charges)
    invalid = _ambiguous_sources(sources, summaries, now)
    conflicts = _charge_conflicts(charges)
    invalid.update(row.source_id for row in charges if row.id in conflicts)
    rows = [
        row
        for row in charges
        if row.organization_id == group_id
        and row.id not in conflicts
        and not (row.source == "interval" and row.source_id in invalid)
        and not _covered_by_summary(row, summaries)
    ]
    segments = defaultdict(list)
    for charge in charges:
        if charge.source == "interval":
            segments[charge.source_id].append(charge)
    for row in sources:
        if not _valid_source(row) or row.id in invalid:
            continue
        if row.quota_attribution["organization_id"] != group_id:
            continue
        if _cursor_valid(row, segments[row.id]):
            rows.extend(_tails(row, now))
    return rows


def _charge_conflicts(charges):
    invalid = set()
    by_source = defaultdict(list)
    for row in charges:
        key = (
            (row.source, row.source_id)
            if row.source == "interval"
            else ("summary", row.project_id)
        )
        by_source[key].append(row)
    for rows in by_source.values():
        invalid.update(
            _overlapping_ids(
                (utc(row.start_at), utc(row.end_at), row.id) for row in rows
            )
        )
    return invalid


def _covered_by_summary(row, summaries):
    return row.source == "interval" and _summary_overlaps(
        row.project_id, row.start_at, row.end_at, summaries
    )


def _money(rows, start=None, end=None, *, empty=False):
    rows = [
        row
        for row in rows
        if (start is None or utc(row.end_at) > start)
        and (end is None or utc(row.start_at) < end)
    ]
    try:
        summary = summarize(rows, start, end)
    except AccountingUnavailable:
        # A coarse imported summary cannot be prorated without detailed evidence.
        rows = [
            row
            for row in rows
            if row.source == "interval"
            or (
                (start is None or utc(row.start_at) >= start)
                and (end is None or utc(row.end_at) <= end)
            )
        ]
        summary = summarize(rows, start, end)
        empty = False
    return {
        "known_subtotal_usd": format(summary["total"], "f"),
        "total_usd": "0" if empty else None,
        "accounting_status": "ready"
        if empty
        else ("partial" if rows else "unavailable"),
        "reason": None if empty else COVERAGE_REASON,
        "line_items": {
            "by_kind": [
                {"kind": kind, "subtotal": format(amount, "f")}
                for kind, amount in sorted(summary["by_kind"].items())
            ]
        },
    }


def _project_lines(rows, start, end):
    by_project = defaultdict(list)
    for row in rows:
        if utc(row.end_at) > start and utc(row.start_at) < end:
            by_project[row.project_id].append(row)
    lines = []
    for pid, project_rows in sorted(by_project.items()):
        money = _money(project_rows, start, end)
        lines.append(
            {
                "project_id": pid,
                "project_name": project_rows[0].project_name,
                "subtotal": money["known_subtotal_usd"],
                "known_subtotal_usd": money["known_subtotal_usd"],
                "total_usd": money["total_usd"],
                "accounting_status": money["accounting_status"],
                "by_kind": money["line_items"]["by_kind"],
            }
        )
    return lines


def _nonempty_owners(db, members, projects, sources, rows):
    """Positive absence, not an empty ledger, is required for a verified zero.

    Any owner history, deployment metadata, actual resources or ledger operation
    defeats the never-used/draft-only proof. Documents are a blocker only, never
    a monetary source or evidence of historical employer identity.
    """
    owner_ids = [member.id for member in members]
    projects_by_id = {project.id: project for project in projects}
    nonempty = {row.owner_id for row in rows}
    for source in sources:
        if source.project_id in projects_by_id:
            nonempty.add(projects_by_id[source.project_id].owner_id)
    for project in projects:
        if (
            project.state != "draft"
            or project.host_id
            or project.disks
            or project.vms
            or project.deployed_topology
            or project.deploy_started_at
            or project.host_assignments
        ):
            nonempty.add(project.owner_id)
    external_ips = db.query(ElasticIp.project_id).filter(
        ElasticIp.project_id.in_(projects_by_id)
    )
    nonempty.update(projects_by_id[row.project_id].owner_id for row in external_ips)
    for model in (ProjectInvoice, MonthlyStatement, QuotaCharge):
        nonempty.update(
            row.owner_id
            for row in db.query(model.owner_id).filter(model.owner_id.in_(owner_ids))
        )
    for model in (QuotaAllocation, QuotaReservation):
        ledger = db.query(model).filter(
            or_(model.owner_id.in_(owner_ids), model.project_id.in_(projects_by_id))
        )
        for row in ledger:
            nonempty.add(row.owner_id)
            if row.project_id in projects_by_id:
                nonempty.add(projects_by_id[row.project_id].owner_id)
    return nonempty


def _member_report(member, owner_id, rows, start, end, empty, historical_name):
    current = member is not None
    money = _money(rows, start, end, empty=empty)
    return {
        "owner_id": owner_id,
        "owner_email": member.email if current else None,
        "display_name": member.display_name if current else historical_name,
        "current_member": current,
        "status": "provisional"
        if money["accounting_status"] != "unavailable"
        else "unavailable",
        **{key: value for key, value in money.items() if key != "line_items"},
        "by_project": _project_lines(rows, start, end),
    }


def group_report(
    db: Session,
    group: ManagedGroup,
    members: list[User],
    year: int,
    month: int,
    now: datetime | None = None,
    owner_id: str | None = None,
) -> dict:
    now = utc(now or datetime.now(UTC))
    start, end = month_bounds(year, month)
    end_as_of = min(end, now)
    # Never trust a caller's stale or legacy reporting roster.
    members = scoped_members(db, group.id)
    projects = (
        db.query(Project)
        .filter(Project.owner_id.in_([m.id for m in members]))
        .populate_existing()
        .all()
    )
    sources, charges = _source_snapshot(db, group.id, [p.id for p in projects])
    rows = _evidence(sources, charges, group.id, now)
    current = {member.id: member for member in members}
    historical = {
        row.owner_id: row.owner_name
        for row in charges
        if row.organization_id == group.id
    }
    for source in sources:
        if (
            _valid_stamp(source)
            and source.quota_attribution["organization_id"] == group.id
        ):
            stamp = source.quota_attribution
            historical[stamp["owner_id"]] = stamp["owner_name"]
    owner_rows = defaultdict(list)
    for row in rows:
        owner_rows[row.owner_id].append(row)
    identities = set(current) | set(historical)
    if owner_id is not None and owner_id not in identities:
        raise HTTPException(404, "Group member not found")
    nonempty = _nonempty_owners(db, members, projects, sources, rows)
    nonempty.update(historical)
    selected = sorted(identities if owner_id is None else {owner_id})
    reports = [
        _member_report(
            current.get(oid),
            oid,
            owner_rows[oid],
            start,
            end_as_of,
            oid not in nonempty,
            historical.get(oid),
        )
        for oid in selected
    ]
    selected_rows = [row for row in rows if row.owner_id in selected]
    money = _money(
        selected_rows,
        start,
        end_as_of,
        empty=not any(oid in nonempty for oid in selected),
    )
    active = []
    for project in projects:
        if project.owner_id not in selected or project.state == "draft":
            continue
        active.append(
            {
                "id": project.id,
                "name": project.name,
                "state": project.state,
                "owner_id": project.owner_id,
                "owner_email": current[project.owner_id].email,
                **_money(
                    [row for row in selected_rows if row.project_id == project.id],
                    end=now,
                ),
            }
        )
    return {
        "group": {"id": group.id, "name": group.name},
        "as_of": now.isoformat(),
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "currency": "USD",
        "billing_view": "organization_attributed_usage",
        "capacity_state": "unverified",
        **{key: value for key, value in money.items() if key != "line_items"},
        "members": reports,
        "active_projects": active,
    }
