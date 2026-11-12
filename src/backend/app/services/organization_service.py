"""Platform-only atomic organization transfers; callers own transactions.

Identity locks serialize with admission, workers and metering. Never acquire an
operation advisory lock or invoke admission after acquiring these identities.
"""

from datetime import datetime
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.managed_group import ManagedGroup
from app.models.metering import MeteringInterval
from app.models.project import Project
from app.models.quota import QuotaAllocation, QuotaAudit, QuotaPolicy, QuotaReservation
from app.models.user import User
from app.schemas.quota import AccountingUnavailable, QuotaDenied, Resources
from app.services import quota_accounting as accounting
from app.services.quota_admission import PENDING, usage
from app.services.quota_locks import lock_rows
from app.services.quota_policy import get_limits, request_reevaluation


def _lock_transfer(db, user_id, organization_id):
    owner = (
        db.query(User)
        .filter_by(id=user_id)
        .with_for_update()
        .populate_existing()
        .first()
    )
    if owner is None:
        raise HTTPException(404, "User not found")
    ids = sorted({value for value in (owner.organization_id, organization_id) if value})
    groups = db.query(ManagedGroup).filter(ManagedGroup.id.in_(ids)).all()
    destination = next((group for group in groups if group.id == organization_id), None)
    if organization_id and (destination is None or not destination.is_organization):
        raise HTTPException(404, "Organization not found")
    lock_rows(db, organization_ids=ids)
    if destination is not None and not destination.is_organization:
        raise HTTPException(404, "Organization not found")
    projects = db.query(Project).filter_by(owner_id=owner.id).order_by(Project.id).all()
    lock_rows(db, project_ids=[project.id for project in projects])
    return owner, projects


def _has_infrastructure(db, owner, projects):
    used, reserved = usage(db, "user", owner.id)
    pending = (
        db.query(QuotaReservation)
        .filter(
            QuotaReservation.owner_id == owner.id, QuotaReservation.state.in_(PENDING)
        )
        .first()
    )
    allocation = (
        db.query(QuotaAllocation)
        .filter(
            QuotaAllocation.owner_id == owner.id, QuotaAllocation.state != "released"
        )
        .first()
    )
    open_source = (
        db.query(MeteringInterval)
        .filter(
            MeteringInterval.project_id.in_([p.id for p in projects]),
            MeteringInterval.ended_at.is_(None),
        )
        .first()
    )
    metadata = any(p.state != "draft" or p.host_id or p.disks for p in projects)
    return (
        any(used.model_dump().values())
        or any(reserved.model_dump().values())
        or bool(allocation or pending or open_source or metadata)
    )


def _validate_live_ledger(db, owner, projects):
    project_ids = {project.id for project in projects}
    allocations, reservations = _live_ledger(db, owner.id)
    for rows in (allocations, reservations):
        if any(
            row.project_id not in project_ids
            or row.owner_id != owner.id
            or row.organization_id != owner.organization_id
            for row in rows
        ):
            raise AccountingUnavailable(
                "Live allocation identity requires reconciliation before transfer"
            )
    projects_by_operation = {row.operation_id: row.project_id for row in allocations}
    if any(
        row.operation_id in projects_by_operation
        and row.project_id != projects_by_operation[row.operation_id]
        for row in reservations
    ):
        raise AccountingUnavailable(
            "Operation identity requires reconciliation before transfer"
        )


def _validate_source(row, owner, now):
    start = accounting.utc(row.started_at)
    end = accounting.utc(row.ended_at) if row.ended_at else now
    cursor = accounting.utc(row.quota_accrued_through or row.started_at)
    if not start <= cursor <= end <= now:
        raise AccountingUnavailable("Transfer boundary conflicts with metering cursor")
    identity = row.quota_attribution
    if identity is None:
        if row.ended_at is None:
            raise AccountingUnavailable(
                "Open source attribution requires reviewed migration"
            )
        return
    if (
        not isinstance(identity, dict)
        or set(identity)
        != {
            "project_id",
            "project_name",
            "owner_id",
            "owner_name",
            "organization_id",
            "organization_name",
        }
        or not all(isinstance(value, str) and value for value in identity.values())
        or identity.get("project_id") != row.project_id
        or identity.get("owner_id") != owner.id
        or not identity.get("organization_id")
        or (
            row.ended_at is None
            and identity["organization_id"] != owner.organization_id
        )
    ):
        raise AccountingUnavailable("Metering source identity requires reconciliation")
    values = [Decimal(str(value)) for value in (row.qty, row.unit_rate)]
    if any(not value.is_finite() or value < 0 for value in values):
        raise AccountingUnavailable("Invalid metering quantity or snapshotted rate")


def _validate_sources(db, owner, projects, now):
    rows = (
        db.query(MeteringInterval)
        .filter(MeteringInterval.project_id.in_([p.id for p in projects]))
        .order_by(MeteringInterval.started_at, MeteringInterval.id)
        .with_for_update()
        .populate_existing()
        .all()
    )
    boundaries = {}
    for row in rows:
        _validate_source(row, owner, now)
        key = (row.project_id, row.kind, row.resource_id)
        if key in boundaries and boundaries[key] > accounting.utc(row.started_at):
            raise AccountingUnavailable("Ambiguous overlapping metering sources")
        boundaries[key] = accounting.utc(row.ended_at) if row.ended_at else now


def _check_capacity(db, owner, destination):
    used, reserved = usage(db, "organization", destination)
    moving_used, moving_reserved = usage(db, "user", owner.id)
    limits = get_limits(db, "organization", destination)
    for name in Resources.model_fields:
        increment = getattr(moving_used, name) + getattr(moving_reserved, name)
        limit = getattr(limits, name)
        if (
            increment
            and limit is not None
            and getattr(used, name) + getattr(reserved, name) + increment > limit
        ):
            raise QuotaDenied(
                scope="organization",
                scope_id=destination,
                dimension=name,
                used=getattr(used, name),
                reserved=getattr(reserved, name),
                requested=increment,
                limit=limit,
                reason="resource_limit",
            )


def _split_sources(db, owner, projects, destination, now, enabled):
    if owner.organization_id is None:
        return  # Draft assignment does not need or invent historical attribution.
    for project in projects:
        if destination is None:
            accounting.accrue(db, project.id, now, attributed_only=not enabled)
        else:
            accounting.split_attribution(
                db, project.id, now, destination, attributed_only=not enabled
            )


def _live_ledger(db, owner_id):
    allocations = (
        db.query(QuotaAllocation)
        .filter(
            QuotaAllocation.owner_id == owner_id, QuotaAllocation.state != "released"
        )
        .populate_existing()
        .all()
    )
    operations = [row.operation_id for row in allocations]
    reservations = (
        db.query(QuotaReservation)
        .filter(
            (
                (
                    (QuotaReservation.owner_id == owner_id)
                    & QuotaReservation.state.in_(PENDING)
                )
                | QuotaReservation.operation_id.in_(operations)
            ),
        )
        .populate_existing()
        .all()
    )
    return allocations, reservations


def _move_ledger(db, owner_id, destination, now):
    allocations, reservations = _live_ledger(db, owner_id)
    for row in [*allocations, *reservations]:
        row.organization_id = destination
        row.updated_at = now
    # IDs, claim, generation, dispatch tickets and observation identities survive.


def _destination_spending(db, destination, now, live):
    limits = get_limits(db, "organization", destination)
    policy = (
        db.query(QuotaPolicy)
        .filter_by(scope="organization", scope_id=destination)
        .first()
    )
    request_reevaluation(
        db, "organization", destination, policy.revision if policy else 1, now
    )
    try:
        spent = accounting.spend(db, "organization", destination, now)
    except AccountingUnavailable:
        if live:
            raise
        return "accounting_unavailable"
    if limits.spending_usd is not None and spent >= limits.spending_usd:
        return "budget_exceeded_shutdown_pending"
    return "within_budget"


def _validate_transfer(db, owner, projects, destination, now):
    live = _has_infrastructure(db, owner, projects)
    if destination is None and live:
        raise HTTPException(
            409, "Cannot unassign a user with live infrastructure or pending work"
        )
    if live:
        if owner.organization_id is None:
            raise AccountingUnavailable(
                "Live unassigned resources require baseline reconciliation"
            )
        _validate_live_ledger(db, owner, projects)
    _validate_sources(db, owner, projects, now)
    return live


def transfer_user(
    db: Session,
    actor: User,
    user_id: str,
    organization_id: str | None,
    now: datetime,
    *,
    expected_source: str | None = None,
) -> dict:
    """Move current capacity/future costs, retaining old charges and explicit caps.

    Exceptions require rollback of the entire caller transaction. An exhausted
    spending allowance moves successfully and requests immediate budget action.
    Unassignment needs positive absence of live infrastructure and pending work.
    """
    if actor.role != "admin":
        raise HTTPException(
            403, "Platform administrator required for organization assignment"
        )
    now = accounting.utc(now)
    owner, projects = _lock_transfer(db, user_id, organization_id)
    source = owner.organization_id
    if expected_source is not None and source != expected_source:
        raise HTTPException(409, "User no longer belongs to the expected group")
    enabled = accounting.quotas_enabled()
    result = {
        "user_id": owner.id,
        "organization_id": organization_id,
        "changed": source != organization_id,
        "effective_at": now.isoformat(),
        "spending_state": "unchanged",
        "enforcement_state": "active" if enabled else "inactive",
        "capacity_state": "unverified",
        "accounting_state": "unverified",
    }
    if source == organization_id:
        return result
    live = _validate_transfer(db, owner, projects, organization_id, now)
    if enabled and live:
        accounting.require_ready(db)
        result["capacity_state"] = result["accounting_state"] = "verified"
    if organization_id and enabled:
        _check_capacity(db, owner, organization_id)
        # Refresh the destination while membership still identifies the source.
        result["spending_state"] = _destination_spending(db, organization_id, now, live)
    elif not enabled:
        result["spending_state"] = "inactive"
    _split_sources(db, owner, projects, organization_id, now, enabled)
    if organization_id:
        _move_ledger(db, owner.id, organization_id, now)
    owner.organization_id = organization_id
    db.add(
        QuotaAudit(
            actor_id=actor.id,
            scope="user",
            scope_id=owner.id,
            action="organization_transfer",
            before={"organization_id": source},
            after={"organization_id": organization_id},
            effective_at=now,
        )
    )
    db.flush()
    return result
