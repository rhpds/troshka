"""Durable calculated charges; callers own transaction boundaries.

Only charge segments contribute to totals. Invoices/statements are projections,
never additive sources. Accrual locks each source row and advances its cursor in
the same transaction as its segments. Readiness belongs to the rollout baseline.
"""

from datetime import UTC, datetime
from decimal import Decimal, localcontext

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import config
from app.models.managed_group import ManagedGroup
from app.models.metering import MeteringInterval
from app.models.project import Project
from app.models.quota import QuotaCharge, QuotaReadiness
from app.models.user import User
from app.schemas.quota import AccountingUnavailable, Scope
from app.services.metering_math import month_bounds
from app.services.quota_locks import lock_metering_project, lock_retained_project

PRECISION = Decimal("0.000000000001")


def quotas_enabled() -> bool:
    return bool(getattr(getattr(config, "quotas", None), "enabled", False))


def utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def require_ready(db: Session) -> None:
    baseline = db.get(QuotaReadiness, "baseline")
    if (
        not baseline
        or not baseline.ready
        or not (baseline.details or {}).get("accounting_ready")
    ):
        raise AccountingUnavailable("Accounting baseline has not been verified")
    if quotas_enabled():
        from app.services.quota_baseline import VERSION

        if baseline.baseline_version != VERSION:
            raise AccountingUnavailable(
                "Physical storage requires a new allocated-byte baseline; virtual baselines are unsupported"
            )
        from app.services.quota_operations import supported

        for project in db.query(Project).all():
            if project.state != "draft" or project.host_id:
                supported(db, project, deployed=True)


def attribution(
    db: Session, project: Project, organization_id: str | None = None
) -> dict:
    owner = db.get(User, project.owner_id) if project.owner_id else None
    oid = organization_id or (owner.organization_id if owner else None)
    organization = db.get(ManagedGroup, oid) if oid else None
    if not owner or not organization or not organization.is_organization:
        raise AccountingUnavailable(
            "Metering requires an authoritative owner and organization"
        )
    return {
        "project_id": project.id,
        "project_name": project.name,
        "owner_id": owner.id,
        "owner_name": owner.display_name or owner.email,
        "organization_id": organization.id,
        "organization_name": organization.name,
    }


def interval_amount(quantity, rate, start: datetime, end: datetime) -> Decimal:
    duration = utc(end) - utc(start)
    micros = (
        duration.days * 86400 + duration.seconds
    ) * 1000000 + duration.microseconds
    with localcontext() as ctx:
        ctx.prec = 70
        return (
            Decimal(str(quantity))
            * Decimal(str(rate))
            * Decimal(micros)
            / Decimal(3600000000)
        ).quantize(PRECISION)


def _accrue_interval(
    db: Session, row: MeteringInterval, project: Project, through: datetime
) -> None:
    start = utc(row.quota_accrued_through or row.started_at)
    end = min(utc(through), utc(row.ended_at)) if row.ended_at else utc(through)
    if end <= start:
        return
    if row.quota_attribution is None:
        raise AccountingUnavailable("Source attribution requires reviewed migration")
    quantity, rate = Decimal(str(row.qty)), Decimal(str(row.unit_rate))
    if not quantity.is_finite() or not rate.is_finite() or min(quantity, rate) < 0:
        raise AccountingUnavailable("Invalid metering quantity or snapshotted rate")
    while start < end:
        month_start, month_end = month_bounds(start.year, start.month)
        segment_end = min(end, month_end)
        # Differences of rounded cumulative costs prevent per-poll rounding drift.
        anchor = max(month_start, utc(row.started_at))
        amount = interval_amount(quantity, rate, anchor, segment_end) - interval_amount(
            quantity, rate, anchor, start
        )
        db.add(
            QuotaCharge(
                **row.quota_attribution,
                source="interval",
                source_id=row.id,
                start_at=start,
                end_at=segment_end,
                kind=row.kind,
                quantity=quantity,
                rate=rate,
                amount=amount,
                provenance={
                    "provider_type": row.provider_type,
                    "host_id": row.host_id,
                    "resource_id": row.resource_id,
                    "interval_started_at": utc(row.started_at).isoformat(),
                    "rate_source": "metering_interval_numeric_18_6",
                },
            )
        )
        start = segment_end
    row.quota_accrued_through = end


def accrue(
    db: Session,
    project_id: str,
    through: datetime,
    *,
    retained: bool = False,
    attributed_only: bool = False,
) -> None:
    """Accrue source stamps without polling/committing.

    Cleanup's retained mode permits missing current membership only for closed
    sources with their original attribution; it cannot invent a new parent.
    """
    try:
        db.flush()
        project = db.get(Project, project_id)
        if project is None:
            return  # Deleted identities retain their already accrued segments.
        if retained:
            project = lock_retained_project(db, project_id)
        else:
            project, _, _ = lock_metering_project(db, project_id)
        rows = (
            db.query(MeteringInterval)
            .filter_by(project_id=project_id)
            .order_by(MeteringInterval.id)
            .with_for_update()
            .populate_existing()
            .all()
        )
        for row in rows:
            if attributed_only and row.quota_attribution is None:
                continue  # Unknown legacy history stays available only in owner billing.
            if retained:
                _require_closed_attribution(db, project, row, through)
            _accrue_interval(db, row, project, through)
        db.flush()
    except (SQLAlchemyError, ArithmeticError, ValueError) as exc:
        raise AccountingUnavailable("Cannot accrue calculated charges") from exc


def _require_closed_attribution(db, project, row, through):
    if row.ended_at is None:
        raise AccountingUnavailable(
            "Retained cleanup accrual requires a closed source interval"
        )
    end = min(utc(through), utc(row.ended_at))
    start = utc(row.quota_accrued_through or row.started_at)
    if end > start and not row.quota_attribution:
        raise AccountingUnavailable("Source attribution requires reviewed migration")


def retain_attributed_history(db: Session, project_ids, through: datetime) -> None:
    """Retain known monetary evidence before a legacy metadata deletion batch.

    Identity tiers are acquired for the whole batch before any source is consumed.
    This never polls providers, assigns unknown sources, or asserts coverage/readiness.
    """
    from app.services.quota_locks import lock_metering_projects

    for project in lock_metering_projects(db, project_ids):
        accrue(db, project.id, through, attributed_only=True)


def split_attribution(
    db: Session,
    project_id: str,
    through: datetime,
    organization_id: str,
    *,
    attributed_only: bool = False,
) -> None:
    """Transfer hook: caller holds admission/membership locks, before changing membership."""
    accrue(db, project_id, through, attributed_only=attributed_only)
    project = db.get(Project, project_id)
    if project is None:
        raise AccountingUnavailable("Missing project during attribution transfer")
    new_attribution = attribution(db, project, organization_id)
    rows = (
        db.query(MeteringInterval)
        .filter_by(project_id=project_id, ended_at=None)
        .order_by(MeteringInterval.id)
        .with_for_update()
        .populate_existing()
        .all()
    )
    for row in rows:
        if utc(row.started_at) > utc(through):
            raise AccountingUnavailable(
                "Transfer boundary precedes current metering interval"
            )
        row.ended_at = utc(through)
        db.add(
            MeteringInterval(
                project_id=project_id,
                kind=row.kind,
                resource_id=row.resource_id,
                qty=row.qty,
                unit_rate=row.unit_rate,
                host_id=row.host_id,
                provider_type=row.provider_type,
                started_at=utc(through),
                quota_attribution=new_attribution,
            )
        )
    db.flush()


def _projects(db: Session, scope: Scope, scope_id: str):
    query = db.query(Project)
    if scope == "project":
        return query.filter(Project.id == scope_id).order_by(Project.id).all()
    if scope == "user":
        return query.filter(Project.owner_id == scope_id).order_by(Project.id).all()
    if scope == "organization":
        return (
            query.join(User, User.id == Project.owner_id)
            .filter(User.organization_id == scope_id)
            .order_by(Project.id)
            .all()
        )
    raise AccountingUnavailable("Unknown accounting scope")


def charge_rows(
    db: Session,
    scope: Scope,
    scope_id: str,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[QuotaCharge]:
    columns = {
        "project": QuotaCharge.project_id,
        "user": QuotaCharge.owner_id,
        "organization": QuotaCharge.organization_id,
    }
    if scope not in columns:
        raise AccountingUnavailable("Unknown accounting scope")
    query = db.query(QuotaCharge).filter(columns[scope] == scope_id)
    if start is not None:
        query = query.filter(QuotaCharge.end_at > utc(start))
    if end is not None:
        query = query.filter(QuotaCharge.start_at < utc(end))
    return query.order_by(QuotaCharge.start_at, QuotaCharge.id).all()


def refresh(db: Session, scope: Scope, scope_id: str, now: datetime) -> None:
    from app.services.metering_service import reconcile_project

    require_ready(db)
    models = {"project": Project, "user": User, "organization": ManagedGroup}
    if scope not in models:
        raise AccountingUnavailable("Unknown accounting scope")
    if db.get(models[scope], scope_id) is None and not charge_rows(db, scope, scope_id):
        raise AccountingUnavailable(
            "Scope has no authoritative identity or accounting history"
        )
    from app.services.quota_locks import lock_metering_projects

    projects = _projects(db, scope, scope_id)
    lock_metering_projects(db, [project.id for project in projects])
    for project in projects:
        project, _, _ = lock_metering_project(db, project.id)
        from app.services.quota_operations import known_empty

        if project.state in {"draft", "deploying"} and known_empty(db, project):
            accrue(db, project.id, now)
            continue
        # A draft with no retained infrastructure is a known zero observation.
        if project.state == "draft":
            if (
                project.host_id
                or project.disks
                or (
                    db.query(MeteringInterval)
                    .filter_by(project_id=project.id, ended_at=None)
                    .first()
                )
            ):
                raise AccountingUnavailable(
                    "Draft has unverified retained infrastructure"
                )
        else:
            reconcile_project(db, project, now=now, quota_refresh=True)
        accrue(db, project.id, now)


def spend(db: Session, scope: Scope, scope_id: str, now: datetime) -> Decimal:
    """Project lifetime or user/org UTC month total; unavailable never becomes zero."""
    try:
        now = utc(now)
        refresh(db, scope, scope_id, now)
        start, end = (
            (None, now) if scope == "project" else month_bounds(now.year, now.month)
        )
        rows = charge_rows(db, scope, scope_id, start, min(end, now))
        return summarize(rows, start, min(end, now))["total"]
    except SQLAlchemyError as exc:
        raise AccountingUnavailable("Cannot calculate current spend") from exc


def _clipped_amount(
    row: QuotaCharge, start: datetime | None, end: datetime | None
) -> Decimal:
    lower = max(utc(row.start_at), utc(start)) if start else utc(row.start_at)
    upper = min(utc(row.end_at), utc(end)) if end else utc(row.end_at)
    if upper <= lower:
        return Decimal(0)
    if lower == utc(row.start_at) and upper == utc(row.end_at):
        return row.amount
    if row.source != "interval":
        raise AccountingUnavailable(
            "Cannot prorate a historical summary without detailed usage"
        )
    origin = datetime.fromisoformat(
        row.provenance.get("interval_started_at", row.start_at.isoformat())
    )
    anchor = max(utc(origin), month_bounds(lower.year, lower.month)[0])
    return interval_amount(row.quantity, row.rate, anchor, upper) - interval_amount(
        row.quantity, row.rate, anchor, lower
    )


def summarize(
    rows: list[QuotaCharge], start: datetime | None = None, end: datetime | None = None
) -> dict:
    """One decimal projection for live views, invoices, statements and reports."""
    by_kind: dict[str, Decimal] = {}
    with localcontext() as ctx:
        ctx.prec = 70
        for row in rows:
            by_kind[row.kind] = by_kind.get(row.kind, Decimal(0)) + _clipped_amount(
                row, start, end
            )
        total = sum(by_kind.values(), Decimal(0))
    return {
        "total": total,
        "by_kind": by_kind,
        "line_items": {
            "by_kind": [
                {"kind": kind, "subtotal": str(amount)}
                for kind, amount in sorted(by_kind.items())
            ]
        },
    }


def project_view(db: Session, project: Project, now: datetime) -> dict:
    refresh(db, "project", project.id, now)
    rows = charge_rows(db, "project", project.id, end=now)
    summary = summarize(rows, end=now)
    return {
        "total_usd": str(summary["total"]),
        "by_kind": {kind: str(amount) for kind, amount in summary["by_kind"].items()},
        "line_items": summary["line_items"],
        "currency": "USD",
        "budget_usd": str(project.budget_usd)
        if project.budget_usd is not None
        else None,
        "budget_warned": bool(project.budget_warned),
        "budget_stopped": bool(project.budget_stopped),
        "accounting_status": "ready",
        "as_of": utc(now).isoformat(),
        "running_seconds": None,
        "running_since": None,
        "running_until": None,
    }


def import_legacy_charge(
    db: Session,
    *,
    project_id: str,
    source: str,
    source_id: str,
    start: datetime,
    end: datetime,
    amount: Decimal,
    identity: dict | None = None,
) -> None:
    """Backfill a disjoint project summary; prefer detailed intervals whenever present.

    Baseline importer must hold its rollout lock. Summary imports never infer
    organization from reporting membership; deleted projects supply identity.
    """
    if source not in {"invoice", "statement"} or utc(end) <= utc(start):
        raise AccountingUnavailable("Invalid legacy source coverage")
    source = f"legacy:{source}"
    existing = db.query(QuotaCharge).filter_by(source=source, source_id=source_id).all()
    if existing:
        if len(existing) == 1 and (
            utc(existing[0].start_at),
            utc(existing[0].end_at),
            existing[0].amount,
        ) == (utc(start), utc(end), amount):
            return
        raise AccountingUnavailable("Conflicting legacy import")
    overlap = (
        db.query(MeteringInterval)
        .filter(
            MeteringInterval.project_id == project_id,
            MeteringInterval.started_at < utc(end),
            (
                MeteringInterval.ended_at.is_(None)
                | (MeteringInterval.ended_at > utc(start))
            ),
        )
        .first()
    )
    if overlap or charge_rows(db, "project", project_id, start, end):
        raise AccountingUnavailable("Legacy summary overlaps existing charge coverage")
    if identity is None:
        project = db.get(Project, project_id)
        if project is None:
            raise AccountingUnavailable(
                "Deleted legacy project needs explicit identity provenance"
            )
        identity = attribution(db, project)
    # Summary imports must already be split by UTC month: arbitrary prorating is not evidence.
    if utc(end) > month_bounds(utc(start).year, utc(start).month)[1]:
        raise AccountingUnavailable(
            "Legacy summary spans months without detailed usage"
        )
    db.add(
        QuotaCharge(
            **identity,
            source=source,
            source_id=source_id,
            start_at=utc(start),
            end_at=utc(end),
            kind="legacy_summary",
            quantity=Decimal(1),
            rate=Decimal(0),
            amount=amount,
            provenance={"migration_attribution": True, "coverage": "disjoint_summary"},
        )
    )
    db.flush()
