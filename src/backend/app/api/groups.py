"""Group discovery and administration without a global user directory."""

from datetime import UTC, datetime
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.auth import get_current_user, require_role
from app.core.database import get_db
from app.models.managed_group import (
    ManagedGroup,
    ManagedGroupManager,
    ManagedGroupMember,
)
from app.models.quota import QuotaAllocation, QuotaAudit, QuotaReservation
from app.models.user import User
from app.schemas.managed_group import GroupCreate, GroupUpdate, ManagerAssignment
from app.services.group_access import (
    lock_assignment_user,
    prevent_last_manager_removal,
    require_managed_group,
)

router = APIRouter(prefix="/groups", tags=["groups"])
CurrentUser = Annotated[User, Depends(get_current_user)]
AdminUser = Annotated[User, Depends(require_role("admin"))]
DbSession = Annotated[Session, Depends(get_db)]


def group_people(db: Session, group_id: str, association) -> list[dict]:
    group = db.get(ManagedGroup, group_id)
    if (
        association is ManagedGroupMember
        and group is not None
        and group.is_organization
    ):
        rows = (
            db.query(User)
            .filter_by(organization_id=group_id)
            .order_by(User.email)
            .all()
        )
        return [
            {"id": row.id, "email": row.email, "display_name": row.display_name}
            for row in rows
        ]
    rows = (
        db.query(User)
        .join(association, association.user_id == User.id)
        .filter(association.group_id == group_id)
        .order_by(User.email)
        .all()
    )
    return [
        {"id": row.id, "email": row.email, "display_name": row.display_name}
        for row in rows
    ]


def group_detail(db: Session, group: ManagedGroup) -> dict:
    return {
        "id": group.id,
        "name": group.name,
        "description": group.description,
        "is_organization": group.is_organization,
        "cost_center_id": group.cost_center_id,
        "members": group_people(db, group.id, ManagedGroupMember),
        "managers": group_people(db, group.id, ManagedGroupManager),
    }


def _save(db: Session) -> None:
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Group name or assignment already exists") from None


def _set_name(group: ManagedGroup, name: str) -> None:
    name = name.strip()
    if not name:
        raise HTTPException(422, "Group name is required")
    group.name = name
    group.normalized_name = name.casefold()


def _organization_snapshot(group: ManagedGroup) -> dict:
    return {
        "is_organization": group.is_organization,
        "cost_center_id": group.cost_center_id,
        "name": group.name,
        "description": group.description,
    }


def _audit_organization(
    db: Session, user: User, group: ManagedGroup, action: str, before: dict, after: dict
) -> None:
    db.add(
        QuotaAudit(
            actor_id=user.id,
            scope="organization",
            scope_id=group.id,
            action=action,
            before=before,
            after=after,
            effective_at=datetime.now(UTC),
        )
    )


def _reject_in_use_organization(db: Session, group: ManagedGroup) -> None:
    assigned = db.query(User).filter_by(organization_id=group.id).first()
    allocation = (
        db.query(QuotaAllocation)
        .filter(
            QuotaAllocation.organization_id == group.id,
            QuotaAllocation.state != "released",
        )
        .first()
    )
    pending = (
        db.query(QuotaReservation)
        .filter(
            QuotaReservation.organization_id == group.id,
            QuotaReservation.state.in_(("pending", "dispatched", "reconciling")),
        )
        .first()
    )
    if assigned or allocation or pending:
        raise HTTPException(
            409, "Organization has assigned users or quota ledger allocations"
        )


def _require_roster_authority(user: User, group: ManagedGroup) -> None:
    if group.is_organization and user.role != "admin":
        raise HTTPException(
            403, "Platform administrator required for organization managers"
        )


@router.get("")
def list_groups(user: CurrentUser, db: DbSession):
    query = db.query(ManagedGroup)
    if user.role != "admin":
        query = query.join(ManagedGroupManager).filter(
            ManagedGroupManager.user_id == user.id
        )
    groups = query.order_by(ManagedGroup.name).all()
    return [
        {
            "id": g.id,
            "name": g.name,
            "description": g.description,
            "is_organization": g.is_organization,
            "cost_center_id": g.cost_center_id,
            "member_count": (
                db.query(User).filter_by(organization_id=g.id).count()
                if g.is_organization
                else db.query(ManagedGroupMember).filter_by(group_id=g.id).count()
            ),
            "manager_count": db.query(ManagedGroupManager)
            .filter_by(group_id=g.id)
            .count(),
        }
        for g in groups
    ]


@router.post("", status_code=201)
def create_group(body: GroupCreate, user: AdminUser, db: DbSession):
    group = ManagedGroup(
        id=str(uuid4()),
        description=body.description,
        is_organization=body.is_organization,
        cost_center_id=body.cost_center_id,
    )
    _set_name(group, body.name)
    db.add(group)
    if group.is_organization:
        _audit_organization(
            db, user, group, "organization_created", {}, _organization_snapshot(group)
        )
    _save(db)
    return group_detail(db, group)


@router.get("/{group_id}")
def get_group(group_id: str, user: CurrentUser, db: DbSession):
    return group_detail(db, require_managed_group(db, user, group_id))


@router.patch("/{group_id}")
def update_group(group_id: str, body: GroupUpdate, user: AdminUser, db: DbSession):
    group = require_managed_group(db, user, group_id, lock=True)
    before = _organization_snapshot(group)
    if body.is_organization is False and group.is_organization:
        _reject_in_use_organization(db, group)
    if body.is_organization is not None:
        group.is_organization = body.is_organization
    if "cost_center_id" in body.model_fields_set:
        group.cost_center_id = body.cost_center_id
    if body.name is not None:
        _set_name(group, body.name)
    if body.description is not None:
        group.description = body.description
    if before["is_organization"] or group.is_organization:
        _audit_organization(
            db,
            user,
            group,
            "organization_updated",
            before,
            _organization_snapshot(group),
        )
    _save(db)
    return group_detail(db, group)


@router.delete("/{group_id}", status_code=204)
def delete_group(group_id: str, user: AdminUser, db: DbSession):
    group = require_managed_group(db, user, group_id, lock=True)
    _reject_in_use_organization(db, group)
    if group.is_organization:
        _audit_organization(
            db, user, group, "organization_deleted", _organization_snapshot(group), {}
        )
    for association in (ManagedGroupMember, ManagedGroupManager):
        db.query(association).filter_by(group_id=group_id).delete(
            synchronize_session=False
        )
    db.delete(group)
    db.commit()


@router.put("/{group_id}/members/{user_id}")
def add_member(group_id: str, user_id: str, user: AdminUser, db: DbSession):
    lock_assignment_user(db, user_id)
    group = require_managed_group(db, user, group_id, lock=True)
    if db.get(ManagedGroupMember, (group_id, user_id)) is None:
        db.add(ManagedGroupMember(group_id=group_id, user_id=user_id))
    _save(db)
    return group_detail(db, group)


@router.delete("/{group_id}/members/{user_id}")
def remove_member(group_id: str, user_id: str, user: AdminUser, db: DbSession):
    group = require_managed_group(db, user, group_id, lock=True)
    db.query(ManagedGroupMember).filter_by(group_id=group_id, user_id=user_id).delete()
    db.commit()
    return group_detail(db, group)


@router.post("/{group_id}/managers")
def add_manager(
    group_id: str, body: ManagerAssignment, user: CurrentUser, db: DbSession
):
    _require_roster_authority(user, require_managed_group(db, user, group_id))
    target = (
        db.query(User)
        .filter_by(email=body.email.strip())
        .with_for_update()
        .populate_existing()
        .first()
    )
    if target is None:
        raise HTTPException(404, "User not found")
    group = require_managed_group(db, user, group_id, lock=True)
    _require_roster_authority(user, group)
    if db.get(ManagedGroupManager, (group_id, target.id)) is None:
        before = {
            "manager_ids": [
                r["id"] for r in group_people(db, group.id, ManagedGroupManager)
            ]
        }
        db.add(ManagedGroupManager(group_id=group_id, user_id=target.id))
        if group.is_organization:
            _audit_organization(
                db,
                user,
                group,
                "organization_managers",
                before,
                {"manager_ids": [*before["manager_ids"], target.id]},
            )
    _save(db)
    return group_detail(db, group)


@router.delete("/{group_id}/managers/{user_id}")
def remove_manager(group_id: str, user_id: str, user: CurrentUser, db: DbSession):
    group = require_managed_group(db, user, group_id, lock=True)
    _require_roster_authority(user, group)
    prevent_last_manager_removal(db, group_id, user_id)
    before = {
        "manager_ids": [
            r["id"] for r in group_people(db, group.id, ManagedGroupManager)
        ]
    }
    if group.is_organization and user_id in before["manager_ids"]:
        _audit_organization(
            db,
            user,
            group,
            "organization_managers",
            before,
            {
                "manager_ids": [
                    identity
                    for identity in before["manager_ids"]
                    if identity != user_id
                ]
            },
        )
    db.query(ManagedGroupManager).filter_by(group_id=group_id, user_id=user_id).delete()
    db.commit()
    return group_detail(db, group)
