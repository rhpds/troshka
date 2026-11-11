"""Atomic reservations and evidence-based operation allocations.

All callers own transactions. Reserve before dispatch; a timeout never releases
anything. confirm's `now` is the trusted provider observation time, not receipt
time. It must identify the resources attributable to THIS operation. Partial
observations retain the unobserved reservation. Complete observations certify
the remaining capacity was not created and no unfenced work can create it later.
"""

from datetime import datetime
from decimal import Decimal
from hashlib import sha256

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.managed_group import ManagedGroup
from app.models.project import Project
from app.models.quota import QuotaAllocation, QuotaReservation
from app.models.user import User
from app.schemas.quota import AccountingUnavailable, QuotaDenied, Resources
from app.services import quota_accounting as accounting
from app.services.quota_locks import lock_project, lock_rows
from app.services.quota_policy import get_limits

PENDING = ("pending", "dispatched", "reconciling")
DIMENSIONS = tuple(Resources.model_fields)


class OperationConflict(ValueError):  # noqa: N818 -- public service contract
    """An idempotency identity or observed allocation conflicts with its contract."""


def lock_operations(db: Session, operation_ids) -> None:
    """Batch lock sorted operations BEFORE users/orgs/projects, never after them.

    Batch reconcilers call this once with every affected ID before calling confirm.
    Transfers may update ledger attribution under identity locks without taking
    operation advisory locks, but must not call admission services under those locks."""
    identities = sorted(set(operation_ids))
    if any(
        not isinstance(value, str) or not value or len(value) > 255
        for value in identities
    ):
        raise OperationConflict("Invalid operation identity")
    if db.get_bind().dialect.name == "postgresql":
        for operation_id in identities:
            # Serializes globally unique identities across different organizations.
            key = int.from_bytes(
                sha256(operation_id.encode()).digest()[:8], "big", signed=True
            )
            db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def _operation_lock(db, operation_id):
    lock_operations(db, (operation_id,))


def _vector_sum(vectors):
    values = dict.fromkeys(DIMENSIONS, 0)
    for vector in vectors:
        for name in DIMENSIONS:
            values[name] += vector.get(name, 0)
    return Resources(**values)


def usage(db: Session, scope: str, scope_id: str) -> tuple[Resources, Resources]:
    """Confirmed usage plus unobserved reserved remainder. Caller holds scope lock."""
    columns = {
        "project": "project_id",
        "user": "owner_id",
        "organization": "organization_id",
    }
    if scope not in columns:
        raise AccountingUnavailable("Unknown quota scope")
    # Locking live identities does not invalidate ledger objects retained in the
    # Session. Preserve this transaction's writes before replacing cached values.
    db.flush()
    column = columns[scope]
    allocated = (
        db.query(QuotaAllocation)
        .filter(
            getattr(QuotaAllocation, column) == scope_id,
            QuotaAllocation.state != "released",
        )
        .populate_existing()
        .all()
    )
    reservations = (
        db.query(QuotaReservation)
        .filter(
            getattr(QuotaReservation, column) == scope_id,
            QuotaReservation.state.in_(PENDING),
        )
        .populate_existing()
        .all()
    )
    remaining = [
        {
            name: max(0, row.requested[name] - row.observed.get(name, 0))
            for name in DIMENSIONS
        }
        for row in reservations
    ]
    return _vector_sum(row.observed for row in allocated), _vector_sum(remaining)


def _check_scope(db, scope, scope_id, increment, now, own_remainder=None):
    limits = get_limits(db, scope, scope_id)
    # Spend is refreshed for EVERY scope, even zero-delta starts/unlimited limits.
    spent = accounting.spend(db, scope, scope_id, now)
    if limits.spending_usd is not None and spent >= limits.spending_usd:
        raise QuotaDenied(
            scope=scope,
            scope_id=scope_id,
            dimension="spending_usd",
            used=spent,
            reserved=Decimal(0),
            requested=Decimal(0),
            limit=limits.spending_usd,
            reason="spending_limit",
        )
    used, reserved = usage(db, scope, scope_id)
    from app.services.quota_storage import scope_evidence

    scope_evidence(db, scope, scope_id, now)
    if own_remainder is not None:
        reserved = Resources(
            **{
                name: getattr(reserved, name) - getattr(own_remainder, name)
                for name in DIMENSIONS
            }
        )
    for name in DIMENSIONS:
        delta, limit = getattr(increment, name), getattr(limits, name)
        exhausted_storage = (
            name == "storage_bytes"
            and limit is not None
            and getattr(used, name) >= limit
        )
        if limit is not None and (
            exhausted_storage
            or (
                delta > 0
                and getattr(used, name) + getattr(reserved, name) + delta > limit
            )
        ):
            raise QuotaDenied(
                scope=scope,
                scope_id=scope_id,
                dimension=name,
                used=getattr(used, name),
                reserved=getattr(reserved, name),
                requested=delta,
                limit=limit,
                reason="resource_limit",
            )


def reserve(
    db: Session, project_id: str, operation_id: str, increment: Resources, now: datetime
) -> str:
    increment = Resources.model_validate(increment)
    _operation_lock(db, operation_id)
    project, owner, organization = lock_project(db, project_id)
    if organization is None:
        raise AccountingUnavailable("Project requires an assigned owner organization")
    previous = (
        db.query(QuotaReservation)
        .filter_by(operation_id=operation_id)
        .populate_existing()
        .first()
    )
    if previous is not None:
        if (
            previous.project_id != project_id
            or previous.requested != increment.model_dump()
        ):
            raise OperationConflict(
                "Operation identity has a different project or vector"
            )
        return previous.id
    accounting.require_ready(db)
    for scope, identity in (
        ("organization", organization.id),
        ("user", owner.id),
        ("project", project.id),
    ):
        _check_scope(db, scope, identity, increment, now)
    row = QuotaReservation(
        operation_id=operation_id,
        project_id=project_id,
        owner_id=owner.id,
        organization_id=organization.id,
        requested=increment.model_dump(),
        observed=Resources().model_dump(),
        state="pending",
        updated_at=now,
    )
    db.add(row)
    db.flush()
    return row.id


def revalidate(db: Session, operation_id: str, now: datetime) -> None:
    """Recheck pending growth before dispatch, excluding only its own remainder.

    This grants no permission to reissue an already dispatched provider action;
    workers must also claim/fence dispatch with durable job metadata under these
    locks. Never call after acquiring identity locks for another operation."""
    _operation_lock(db, operation_id)
    row = db.query(QuotaReservation).filter_by(operation_id=operation_id).first()
    if row is None:
        raise OperationConflict("Operation has no reservation")
    project, owner, organization = lock_project(db, row.project_id)
    if organization is None:
        raise AccountingUnavailable("Project requires an assigned owner organization")
    row = (
        db.query(QuotaReservation)
        .filter_by(operation_id=operation_id)
        .populate_existing()
        .one()
    )
    if row.state not in PENDING:
        raise OperationConflict("Completed operation cannot dispatch again")
    if (row.owner_id, row.organization_id) != (owner.id, organization.id):
        raise OperationConflict(
            "Operation attribution does not match current membership"
        )
    remaining = Resources(
        **{
            name: max(0, row.requested[name] - row.observed.get(name, 0))
            for name in DIMENSIONS
        }
    )
    accounting.require_ready(db)
    for scope, identity in (
        ("organization", organization.id),
        ("user", owner.id),
        ("project", project.id),
    ):
        _check_scope(db, scope, identity, remaining, now, own_remainder=remaining)


def _lock_retained_identity(db, row):
    """Confirm reductions even after metadata deletion; retained ledger owns usage."""
    if db.get(Project, row.project_id) is not None:
        # Cleanup/reductions remain possible after organization unassignment.
        lock_project(db, row.project_id, require_organization=False)
    else:
        lock_rows(
            db,
            user_ids=(row.owner_id,) if db.get(User, row.owner_id) else (),
            organization_ids=(row.organization_id,)
            if db.get(ManagedGroup, row.organization_id)
            else (),
        )


def _validate_observation(
    reservation, allocation, observed, observation_id, now, complete
):
    if (
        not isinstance(observation_id, str)
        or not observation_id
        or len(observation_id) > 255
    ):
        raise OperationConflict("Invalid observation identity")
    if reservation.observation_id == observation_id:
        if reservation.observed != observed.model_dump():
            raise OperationConflict("Observation identity has different resources")
        return False
    if allocation and accounting.utc(now) <= accounting.utc(allocation.observed_at):
        return False  # Old or ambiguous-time evidence cannot free capacity.
    ceiling = (
        reservation.requested if reservation.state in PENDING else reservation.observed
    )
    if reservation.dispatch_metadata.get("storage_measured_at"):
        # The physical adapter may observe guest writes while creation is still
        # pending. This preserves already measured bytes, not arbitrary growth.
        ceiling = {
            **ceiling,
            "storage_bytes": max(
                ceiling.get("storage_bytes", 0),
                reservation.observed.get("storage_bytes", 0),
            ),
        }
    if any(getattr(observed, name) > ceiling.get(name, 0) for name in DIMENSIONS):
        raise OperationConflict("Observed growth has no live reservation")
    if reservation.state not in PENDING and not complete:
        raise OperationConflict("Completed operation cannot become uncertain again")
    return True


def confirm(
    db: Session,
    operation_id: str,
    observed: Resources,
    observation_id: str,
    now: datetime,
    *,
    complete: bool = True,
) -> None:
    """Replace operation allocation, retaining unobserved capacity if incomplete.

    Monotonic trusted observation timestamps fence stale/replayed evidence. Reusing
    the latest ID with different contents is a conflict. Adapters must preserve each
    snapshot's original timestamp and ID; stamping receipt time is unsafe.
    After completion, growth requires a NEW operation reservation."""
    observed = Resources.model_validate(observed)
    _operation_lock(db, operation_id)
    row = db.query(QuotaReservation).filter_by(operation_id=operation_id).first()
    if row is None:
        raise OperationConflict("Operation has no reservation")
    _lock_retained_identity(db, row)
    row = (
        db.query(QuotaReservation)
        .filter_by(operation_id=operation_id)
        .populate_existing()
        .one()
    )
    allocation = (
        db.query(QuotaAllocation)
        .filter_by(operation_id=operation_id)
        .populate_existing()
        .first()
    )
    if not _validate_observation(
        row, allocation, observed, observation_id, now, complete
    ):
        return
    if allocation is None:
        allocation = QuotaAllocation(
            operation_id=operation_id,
            project_id=row.project_id,
            owner_id=row.owner_id,
            organization_id=row.organization_id,
            requested=row.requested,
        )
        db.add(allocation)
    vector = observed.model_dump()
    allocation.observed = row.observed = vector
    allocation.observation_id = row.observation_id = observation_id
    allocation.observed_at = now
    allocation.updated_at = row.updated_at = now
    allocation.state = (
        "reconciling"
        if not complete
        else ("active" if observed.cpu or observed.ram_bytes else "stopped")
    )
    if not any(vector.values()) and complete:
        allocation.state = "released"
    row.state = "confirmed" if complete else "reconciling"
    if complete:
        row.confirmed_at = now
    db.flush()
