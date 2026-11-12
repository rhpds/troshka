"""Shared billing policy and serialized managed-group assignment rules."""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.managed_group import (
    ManagedGroup,
    ManagedGroupManager,
)
from app.models.quota import QuotaAudit
from app.models.user import User


def billing_owner_ids(db: Session, user: User) -> set[str] | None:
    if user.role == "admin":
        return None
    managed = db.query(ManagedGroupManager.group_id).filter_by(user_id=user.id)
    assigned = db.query(User.id).filter(User.organization_id.in_(managed)).all()
    return {user.id, *(row.id for row in assigned)}


def require_billing_owner(db: Session, user: User, owner_id: str | None) -> None:
    scope = billing_owner_ids(db, user)
    if scope is not None and owner_id not in scope:
        raise HTTPException(403, "Access denied")


def require_managed_group(
    db: Session, user: User, group_id: str, lock: bool = False
) -> ManagedGroup:
    try:
        group_id = str(UUID(group_id))
    except ValueError:
        raise HTTPException(404, "Group not found") from None
    query = db.query(ManagedGroup).filter_by(id=group_id)
    if lock:
        query = query.with_for_update().populate_existing()
    group = query.first()
    # Authorization is deliberately queried after the parent lock is acquired.
    assignment = (
        db.query(ManagedGroupManager)
        .filter_by(group_id=group_id, user_id=user.id)
        .populate_existing()
        .first()
    )
    if group is None or (user.role != "admin" and assignment is None):
        raise HTTPException(404, "Group not found")
    return group


def lock_assignment_user(db: Session, user_id: str) -> User:
    user = (
        db.query(User)
        .filter_by(id=user_id)
        .with_for_update()
        .populate_existing()
        .first()
    )
    if user is None:
        raise HTTPException(404, "User not found")
    return user


def prevent_last_manager_removal(db: Session, group_id: str, user_id: str) -> None:
    if db.get(ManagedGroupManager, (group_id, user_id)) is None:
        return
    if db.query(ManagedGroupManager).filter_by(group_id=group_id).count() <= 1:
        raise HTTPException(409, "Cannot remove the last group manager")


def protect_manager_user_deletion(db: Session, user_id: str, actor: User) -> None:
    # Assignment additions also lock this user first, preventing a newly added
    # sole-manager association from appearing after this group's snapshot.
    lock_assignment_user(db, user_id)
    ids = [
        r.group_id
        for r in db.query(ManagedGroupManager).filter_by(user_id=user_id).all()
    ]
    groups = (
        db.query(ManagedGroup)
        .filter(ManagedGroup.id.in_(ids))
        .order_by(ManagedGroup.id)
        .with_for_update()
        .all()
    )
    for group in groups:
        prevent_last_manager_removal(db, group.id, user_id)
        if group.is_organization:
            before = [
                row.user_id
                for row in db.query(ManagedGroupManager)
                .filter_by(group_id=group.id)
                .all()
            ]
            db.add(
                QuotaAudit(
                    actor_id=actor.id,
                    scope="organization",
                    scope_id=group.id,
                    action="organization_managers",
                    before={"manager_ids": before},
                    after={
                        "manager_ids": [
                            identity for identity in before if identity != user_id
                        ]
                    },
                    effective_at=datetime.now(UTC),
                )
            )
