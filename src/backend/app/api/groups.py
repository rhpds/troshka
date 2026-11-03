"""Group discovery and administration without a global user directory."""

from typing import Annotated

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
            "member_count": db.query(ManagedGroupMember)
            .filter_by(group_id=g.id)
            .count(),
            "manager_count": db.query(ManagedGroupManager)
            .filter_by(group_id=g.id)
            .count(),
        }
        for g in groups
    ]


@router.post("", status_code=201)
def create_group(body: GroupCreate, user: AdminUser, db: DbSession):
    group = ManagedGroup(description=body.description)
    _set_name(group, body.name)
    db.add(group)
    _save(db)
    return group_detail(db, group)


@router.get("/{group_id}")
def get_group(group_id: str, user: CurrentUser, db: DbSession):
    return group_detail(db, require_managed_group(db, user, group_id))


@router.patch("/{group_id}")
def update_group(group_id: str, body: GroupUpdate, user: AdminUser, db: DbSession):
    group = require_managed_group(db, user, group_id, lock=True)
    if body.name is not None:
        _set_name(group, body.name)
    if body.description is not None:
        group.description = body.description
    _save(db)
    return group_detail(db, group)


@router.delete("/{group_id}", status_code=204)
def delete_group(group_id: str, user: AdminUser, db: DbSession):
    group = require_managed_group(db, user, group_id, lock=True)
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
    require_managed_group(db, user, group_id)
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
    if db.get(ManagedGroupManager, (group_id, target.id)) is None:
        db.add(ManagedGroupManager(group_id=group_id, user_id=target.id))
    _save(db)
    return group_detail(db, group)


@router.delete("/{group_id}/managers/{user_id}")
def remove_manager(group_id: str, user_id: str, user: CurrentUser, db: DbSession):
    group = require_managed_group(db, user, group_id, lock=True)
    prevent_last_manager_removal(db, group_id, user_id)
    db.query(ManagedGroupManager).filter_by(group_id=group_id, user_id=user_id).delete()
    db.commit()
    return group_detail(db, group)
