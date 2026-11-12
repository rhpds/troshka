"""Fenced, atomic fresh accounting conversion. Never polls providers or Redis.

QuotaReadiness has a durable singleton namespace: the immutable receipt is NOT
the baseline row, and never certifies physical capacity or monetary coverage.
"""

import json
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256

from sqlalchemy import select, text

from app.core.database import Base
from app.models.managed_group import ManagedGroup, ManagedGroupMember
from app.models.metering import MeteringInterval
from app.models.project import Project
from app.models.quota import (
    QuotaAction,
    QuotaAllocation,
    QuotaCharge,
    QuotaReadiness,
    QuotaReservation,
)
from app.models.user import User
from app.models.workload_run import WorkloadRun
from app.schemas.quota import AccountingUnavailable
from app.services import quota_accounting as accounting

RECEIPT_ID = "unified-groups-v1"


def _digest(value):
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _snapshot(db, *, preserved=False):
    result = {}
    excluded = {"metering_intervals", "quota_readiness"} if preserved else set()
    for name, table in sorted(Base.metadata.tables.items()):
        if name in excluded:
            continue
        rows = [dict(row) for row in db.execute(select(table)).mappings()]
        # Tunnel authentication touches this unrelated timestamp during a fence.
        # Every key owner/project/scope/expiry/active flag remains fingerprinted.
        ignored = {"last_used_at"} if name == "api_keys" else set()
        if preserved:
            ignored |= {
                "users": {"organization_id"},
                "managed_groups": {"is_organization"},
                "quota_allocations": {"organization_id", "updated_at"},
                "quota_reservations": {"organization_id", "updated_at"},
            }.get(name, set())
        rows = [
            {key: value for key, value in row.items() if key not in ignored}
            for row in rows
        ]
        # Only counts and SHA256 leave this function; secrets/topologies never do.
        result[name] = {
            "count": len(rows),
            "digest": _digest(sorted(rows, key=_digest)),
        }
    return result


def preservation(db):
    """All asset rows, relationships, paths, ACLs, policies and user flags."""
    return _snapshot(db, preserved=True)


def inventory(db):
    snapshot = _snapshot(db)
    return {
        "inventory_digest": _digest(snapshot),
        "tables": snapshot,
        "preservation": preservation(db),
    }


def _boundary(db):
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        tables = ", ".join('"' + name + '"' for name in sorted(Base.metadata.tables))
        db.execute(text(f"LOCK TABLE {tables} IN EXCLUSIVE MODE NOWAIT"))
    elif dialect == "sqlite":
        connection = db.connection()
        if not connection.connection.driver_connection.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
    else:
        raise AccountingUnavailable(
            "Fresh conversion requires PostgreSQL (SQLite rehearsal only)"
        )


def _mapping(db):
    from app.services.quota_baseline import DEFAULT_ORGANIZATION_ID

    groups = {group.id: group for group in db.query(ManagedGroup)}
    default = groups.get(DEFAULT_ORGANIZATION_ID)
    if default is None or not default.is_organization:
        raise AccountingUnavailable(
            "Fresh conversion requires the foundation Default organization identity"
        )
    legacy = {}
    for row in db.query(ManagedGroupMember).order_by(
        ManagedGroupMember.user_id, ManagedGroupMember.group_id
    ):
        if row.user_id in legacy:
            raise AccountingUnavailable(
                "Overlapping legacy group assignments require review"
            )
        if row.group_id not in groups:
            raise AccountingUnavailable("Legacy roster has missing group identity")
        legacy[row.user_id] = row.group_id
    mapping = {}
    for user in db.query(User).order_by(User.id):
        target = legacy.get(user.id, user.organization_id)
        if user.organization_id not in (None, default.id, target):
            raise AccountingUnavailable(
                "Conflicting nondefault authoritative membership"
            )
        if target is not None and target not in groups:
            raise AccountingUnavailable("Missing authoritative group identity")
        mapping[user.id] = target
    if set(legacy) - set(mapping):
        raise AccountingUnavailable("Legacy roster has missing user identity")
    return mapping


def _validate_work(db):
    checks = (
        db.query(QuotaReservation).filter(
            QuotaReservation.state.in_(("pending", "dispatched", "reconciling"))
        ),
        db.query(QuotaAllocation).filter_by(state="reconciling"),
        db.query(QuotaAction).filter(
            QuotaAction.state.in_(("pending", "issued", "failed"))
        ),
        db.query(WorkloadRun).filter(WorkloadRun.status.in_(("pending", "running"))),
        db.query(Project).filter(
            Project.state.in_(("deploying", "destroying", "starting", "stopping"))
        ),
    )
    if any(query.first() is not None for query in checks):
        raise AccountingUnavailable(
            "Fresh conversion rejects pending actions or ambiguous in-flight work"
        )


def _valid_source_boundary(row, start):
    began = accounting.utc(row.started_at)
    ended = accounting.utc(row.ended_at) if row.ended_at else start
    cursor = accounting.utc(row.quota_accrued_through or row.started_at)
    if not began <= cursor <= ended <= start:
        raise AccountingUnavailable(
            "Accounting start conflicts with existing source evidence or cursor"
        )


def _sources(db, start):
    if db.query(QuotaCharge).filter(QuotaCharge.end_at > start).first() is not None:
        raise AccountingUnavailable(
            "Fresh accounting start precedes existing calculated charges"
        )
    rows = db.query(MeteringInterval).order_by(MeteringInterval.id).all()
    open_sources, keys = [], set()
    for row in rows:
        _valid_source_boundary(row, start)
        if row.ended_at is not None:
            continue
        key = (row.project_id, row.kind, row.resource_id)
        if key in keys:
            raise AccountingUnavailable(
                "Overlapping open source observations require reconciliation"
            )
        keys.add(key)
        values = [Decimal(str(value)) for value in (row.qty, row.unit_rate)]
        if any(not value.is_finite() or value < 0 for value in values):
            raise AccountingUnavailable("Invalid observed source quantity or rate")
        open_sources.append(row)
    return open_sources


def _validate_ledger_identities(db):
    # Orphaned live operations cannot hide outside every current owner's closure.
    for row in db.query(QuotaAllocation).filter(
        QuotaAllocation.state.in_(("active", "stopped"))
    ):
        project = db.get(Project, row.project_id)
        owner = db.get(User, row.owner_id)
        if project is None or owner is None or project.owner_id != owner.id:
            raise AccountingUnavailable(
                "Live ledger has missing or conflicting owner/project identity"
            )


def _convert(db, mapping, rows, start):
    from app.services.organization_service import _move_ledger, _validate_live_ledger

    # Validate the old live ledger before changing any identity.
    _validate_ledger_identities(db)
    for user in db.query(User).order_by(User.id):
        projects = db.query(Project).filter_by(owner_id=user.id).all()
        _validate_live_ledger(db, user, projects)
    for group in db.query(ManagedGroup):
        group.is_organization = True
    for row in rows:
        row.ended_at = start  # No historical accrual or imported costs.
    for user in db.query(User).order_by(User.id):
        target = mapping[user.id]
        projects = db.query(Project).filter_by(owner_id=user.id).all()
        if target is None and any(
            p.state != "draft" or p.host_id or p.disks for p in projects
        ):
            raise AccountingUnavailable("Live resources require an authoritative group")
        if target:
            _move_ledger(db, user.id, target, start, [p.id for p in projects])
        user.organization_id = target
    db.flush()
    for row in rows:
        project = db.get(Project, row.project_id)
        stamp = accounting.attribution(db, project)
        db.add(
            MeteringInterval(
                project_id=row.project_id,
                kind=row.kind,
                resource_id=row.resource_id,
                qty=row.qty,
                unit_rate=row.unit_rate,
                host_id=row.host_id,
                provider_type=row.provider_type,
                started_at=start,
                quota_attribution=stamp,
            )
        )
    db.flush()


def _preflight(db, start, expected_digest, apply, writers_fenced):
    if db.new or db.dirty or db.deleted:
        raise AccountingUnavailable(
            "Fresh conversion requires a clean dedicated session"
        )
    if accounting.quotas_enabled():
        raise AccountingUnavailable("Fresh conversion requires enforcement disabled")
    if apply and (not writers_fenced or not expected_digest or start is None):
        raise AccountingUnavailable(
            "Apply requires writer fence, expected digest and explicit accounting start"
        )
    start = accounting.utc(start or datetime.now(UTC))
    if start > datetime.now(UTC):
        raise AccountingUnavailable("Fresh accounting start cannot be in the future")
    return start


def _record_receipt(db, result, start):
    db.add(QuotaReadiness(id=RECEIPT_ID, ready=False, details=result, checked_at=start))
    baseline = db.get(QuotaReadiness, "baseline")
    if baseline is None:
        baseline = QuotaReadiness(id="baseline")
        db.add(baseline)
    baseline.ready = False
    baseline.details = {
        "accounting_ready": False,
        "capacity_ready": False,
        "accounting_started_at": start.isoformat(),
        "fresh_receipt_id": RECEIPT_ID,
    }
    baseline.checked_at = start
    db.flush()


def run(
    db,
    *,
    accounting_start=None,
    expected_digest=None,
    apply=False,
    writers_fenced=False,
):
    """Own a dedicated clean session. Every error and dry run rolls back completely.

    Apply requires a caller attestation about *all* writers/external operations;
    table locks cannot establish that operational fact. Exact replay returns the
    saved receipt even after ordinary post-start accounting changes.
    """
    try:
        start = _preflight(db, accounting_start, expected_digest, apply, writers_fenced)
        _boundary(db)
        receipt = db.get(QuotaReadiness, RECEIPT_ID)
        if receipt:
            saved = receipt.details
            if apply and (
                expected_digest != saved["inventory_digest"]
                or start.isoformat() != saved["accounting_started_at"]
            ):
                raise AccountingUnavailable("Conflicting fresh conversion replay")
            db.rollback()
            return saved
        before = inventory(db)
        if (
            expected_digest is not None
            and expected_digest != before["inventory_digest"]
        ):
            raise AccountingUnavailable(
                "Fresh conversion inventory changed; re-inventory"
            )
        mapping = _mapping(db)
        _validate_work(db)
        rows = _sources(db, start)
        _convert(db, mapping, rows, start)
        if preservation(db) != before["preservation"]:
            raise AccountingUnavailable("Fresh conversion asset preservation mismatch")
        result = {
            "receipt_id": RECEIPT_ID,
            "accounting_started_at": start.isoformat(),
            "inventory_digest": before["inventory_digest"],
            "mapping": mapping,
            "mapping_digest": _digest(mapping),
            "preservation": before["preservation"],
            "successor_count": len(rows),
            "capacity_state": "unverified",
            "enforcement_state": "inactive",
        }
        _record_receipt(db, result, start)
        if apply:
            db.commit()
        else:
            db.rollback()
        return result
    except BaseException:
        db.rollback()
        raise
