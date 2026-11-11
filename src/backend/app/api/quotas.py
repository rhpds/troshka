"""Authorized policy and organization discovery endpoints."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.managed_group import ManagedGroup, ManagedGroupManager
from app.models.user import User
from app.schemas.kubevirt_storage import NativeStorageUsage
from app.schemas.quota import Limits, Scope, Usage
from app.services.kubevirt_storage_usage import read_native_usage
from app.services.quota_policy import authorize_policy, get_limits, set_limits
from app.services.quota_usage import read_usage

router = APIRouter(prefix="/quotas", tags=["quotas"])
CurrentUser = Annotated[User, Depends(get_current_user)]
DbSession = Annotated[Session, Depends(get_db)]


@router.get("/organizations")
def list_organizations(user: CurrentUser, db: DbSession):
    query = db.query(ManagedGroup).filter_by(is_organization=True)
    if user.role != "admin":
        managed = db.query(ManagedGroupManager.group_id).filter_by(user_id=user.id)
        query = query.filter(ManagedGroup.id.in_(managed))
    return [
        {"id": org.id, "name": org.name, "cost_center_id": org.cost_center_id}
        for org in query.order_by(ManagedGroup.name).all()
    ]


@router.get("/organizations/{organization_id}/members")
def organization_members(organization_id: str, user: CurrentUser, db: DbSession):
    from fastapi import HTTPException

    org = authorize_policy(db, user, "organization", organization_id)
    if user.role != "admin" and db.get(ManagedGroupManager, (org.id, user.id)) is None:
        raise HTTPException(403, "Organization administrator required")
    return [
        {
            "id": member.id,
            "email": member.email,
            "display_name": member.display_name,
            "organization_id": org.id,
        }
        for member in db.query(User)
        .filter_by(organization_id=org.id)
        .order_by(User.email)
        .all()
    ]


@router.get("/{scope}/{scope_id}", response_model=Limits)
def read_policy(scope: Scope, scope_id: str, user: CurrentUser, db: DbSession):
    target = authorize_policy(db, user, scope, scope_id)
    return get_limits(db, scope, target.id)


@router.get("/{scope}/{scope_id}/usage", response_model=Usage)
def scope_usage(scope: Scope, scope_id: str, user: CurrentUser, db: DbSession):
    result = read_usage(db, user, scope, scope_id, datetime.now(UTC))
    db.commit()
    return result


@router.get("/{scope}/{scope_id}/native-storage", response_model=NativeStorageUsage)
def native_storage_usage(scope: Scope, scope_id: str, user: CurrentUser, db: DbSession):
    from fastapi.responses import JSONResponse

    result = read_native_usage(db, user, scope, scope_id, datetime.now(UTC))
    # Nullable measured fields stay explicit; privileged pool fields are absent.
    excluded = (
        set()
        if result.unattributed_retained_projects is not None
        else {
            "platform_bytes",
            "unattributed_retained_projects",
            "unattributed_retained_known_bytes",
        }
    )
    return JSONResponse(result.model_dump(mode="json", exclude=excluded))


@router.patch("/{scope}/{scope_id}", response_model=Limits)
def patch_policy(
    scope: Scope, scope_id: str, body: Limits, user: CurrentUser, db: DbSession
):
    set_limits(db, user, scope, scope_id, body, datetime.now(UTC))
    db.commit()
    return get_limits(db, scope, scope_id)
