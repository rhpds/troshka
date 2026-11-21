from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.organization_assignments import commit_assignment
from app.core.auth import require_role
from app.core.database import get_db
from app.models.managed_group import ManagedGroupManager, ManagedGroupMember
from app.models.project import Project
from app.models.user import User
from app.schemas.user import (
    OrganizationAssignment,
    UserCreate,
    UserResponse,
    UserUpdate,
)
from app.services.access_window import normalize_access_bounds
from app.services.database_access import guard_account_change
from app.services.group_access import protect_manager_user_deletion

router = APIRouter(prefix="/users", tags=["users"])

VALID_ROLES = {"user", "operator", "admin"}

AdminUser = Annotated[User, Depends(require_role("admin"))]
DbSession = Annotated[Session, Depends(get_db)]


def _apply_access_window(
    user: User, starts_at, ends_at, *, fields_set: set[str]
) -> None:
    if "access_starts_at" not in fields_set and "access_ends_at" not in fields_set:
        return
    next_start = (
        starts_at if "access_starts_at" in fields_set else user.access_starts_at
    )
    next_end = ends_at if "access_ends_at" in fields_set else user.access_ends_at
    start, end = normalize_access_bounds(next_start, next_end)
    if "access_starts_at" in fields_set:
        user.access_starts_at = start
    if "access_ends_at" in fields_set:
        user.access_ends_at = end


@router.get("/", response_model=list[UserResponse])
def list_users(user: AdminUser, db: DbSession):
    return db.query(User).order_by(User.email).all()


@router.put("/{user_id}/organization")
def assign_organization(
    user_id: UUID, body: OrganizationAssignment, user: AdminUser, db: DbSession
):
    return commit_assignment(
        db,
        user,
        str(user_id),
        str(body.organization_id) if body.organization_id else None,
    )


@router.post(
    "/",
    response_model=UserResponse,
    status_code=201,
    responses={400: {"description": "Bad request"}, 409: {"description": "Conflict"}},
)
def create_user(
    body: UserCreate,
    current_user: AdminUser,
    db: DbSession,
):
    email = body.email.strip().lower()
    if not email:
        raise HTTPException(400, "Email is required")
    if body.role not in VALID_ROLES:
        raise HTTPException(
            400,
            f"Invalid role: {body.role}. Must be one of: {', '.join(sorted(VALID_ROLES))}",
        )
    if db.query(User).filter(func.lower(User.email) == email).first():
        raise HTTPException(409, "A user with this email already exists")
    start, end = normalize_access_bounds(body.access_starts_at, body.access_ends_at)
    user = User(
        email=email,
        display_name=body.display_name or email.split("@")[0],
        role=body.role,
        auth_source="invited",
        access_enabled=True,
        access_starts_at=start,
        access_ends_at=end,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.patch(
    "/{user_id}",
    response_model=UserResponse,
    responses={400: {"description": "Bad request"}, 404: {"description": "Not found"}},
)
def update_user(
    user_id: str,
    body: UserUpdate,
    current_user: AdminUser,
    db: DbSession,
):
    if "quota_overrides" in body.model_fields_set:
        raise HTTPException(422, "Use the quota policy API to update limits")
    user = db.query(User).filter_by(id=user_id).first()
    if not user:
        raise HTTPException(404, "User not found")
    if user_id == current_user.id and body.role is not None:
        raise HTTPException(400, "Cannot change your own role")
    if user_id == current_user.id and body.access_enabled is False:
        raise HTTPException(400, "Cannot disable your own access")
    guard_account_change(
        db, current_user, user, role=body.role, access_enabled=body.access_enabled
    )
    if body.role is not None:
        if body.role not in VALID_ROLES:
            raise HTTPException(
                400,
                f"Invalid role: {body.role}. Must be one of: {', '.join(sorted(VALID_ROLES))}",
            )
        if user_id == current_user.id:
            raise HTTPException(400, "Cannot change your own role")
        user.role = body.role
    if body.display_name is not None:
        user.display_name = body.display_name
    if body.access_enabled is not None:
        if user_id == current_user.id and not body.access_enabled:
            raise HTTPException(400, "Cannot disable your own access")
        user.access_enabled = body.access_enabled
    _apply_access_window(
        user,
        body.access_starts_at,
        body.access_ends_at,
        fields_set=body.model_fields_set,
    )
    db.commit()
    db.refresh(user)
    return user


@router.delete(
    "/{user_id}",
    status_code=204,
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
    },
)
def delete_user(
    user_id: str,
    current_user: AdminUser,
    db: DbSession,
):
    user = db.query(User).filter_by(id=user_id).first()
    if not user:
        raise HTTPException(404, "User not found")
    if user_id == current_user.id:
        raise HTTPException(400, "Cannot delete yourself")
    guard_account_change(db, current_user, user, access_enabled=False)
    project_count = db.query(Project).filter_by(owner_id=user_id).count()
    if project_count > 0:
        raise HTTPException(
            409,
            f"User has {project_count} project(s) — reassign or delete them first",
        )
    protect_manager_user_deletion(db, user_id, current_user)
    for association in (ManagedGroupMember, ManagedGroupManager):
        db.query(association).filter_by(user_id=user_id).delete(
            synchronize_session=False
        )
    db.delete(user)
    db.commit()
