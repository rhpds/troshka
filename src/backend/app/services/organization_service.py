"""Platform-only atomic organization transfers; callers own transactions.

Identity locks serialize with admission, workers and metering. Never acquire an
operation advisory lock or invoke admission after acquiring these identities.
"""

from datetime import datetime

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
    for model, states in (
        (QuotaAllocation, ("active", "stopped", "reconciling")),
        (QuotaReservation, PENDING),
    ):
        rows = (
            db.query(model)
            .filter(model.owner_id == owner.id, model.state.in_(states))
            .populate_existing()
            .all()
        )
        if any(
            row.project_id not in project_ids
            or row.organization_id != owner.organization_id
            for row in rows
        ):
            raise AccountingUnavailable(
                "Live allocation identity requires reconciliation before transfer"
            )


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


def _split_sources(db, owner, projects, destination, now):
    if owner.organization_id is None:
        return  # Draft assignment does not need or invent historical attribution.
    for project in projects:
        rows = (
            db.query(MeteringInterval)
            .filter_by(project_id=project.id, ended_at=None)
            .all()
        )
        if any(
            accounting.utc(row.quota_accrued_through or row.started_at) > now
            for row in rows
        ):
            raise AccountingUnavailable("Transfer boundary precedes accrued metering")
        if destination is None:
            accounting.accrue(db, project.id, now)
        else:
            accounting.split_attribution(db, project.id, now, destination)


def _move_ledger(db, owner_id, destination, now):
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
            QuotaReservation.owner_id == owner_id,
            (
                QuotaReservation.state.in_(PENDING)
                | QuotaReservation.operation_id.in_(operations)
            ),
        )
        .populate_existing()
        .all()
    )
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


def transfer_user(
    db: Session, actor: User, user_id: str, organization_id: str | None, now: datetime
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
    result = {
        "user_id": owner.id,
        "organization_id": organization_id,
        "changed": source != organization_id,
        "effective_at": now.isoformat(),
        "spending_state": "unchanged",
    }
    if source == organization_id:
        return result
    live = _has_infrastructure(db, owner, projects)
    if organization_id is None and live:
        raise HTTPException(
            409, "Cannot unassign a user with live infrastructure or pending work"
        )
    if live:
        if source is None:
            raise AccountingUnavailable(
                "Live unassigned resources require baseline reconciliation"
            )
        _validate_live_ledger(db, owner, projects)
        accounting.require_ready(db)
    if organization_id:
        _check_capacity(db, owner, organization_id)
        # Refresh the destination while membership still identifies the source.
        result["spending_state"] = _destination_spending(db, organization_id, now, live)
    _split_sources(db, owner, projects, organization_id, now)
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
