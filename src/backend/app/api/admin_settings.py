"""Admin-tunable system settings API."""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth import require_role
from app.core.database import get_db
from app.models.user import User
from app.services.system_settings import (
    DEFAULT_INGRESS_CONTROLLER,
    get_ingress_controller_name,
    set_ingress_controller_name,
)

router = APIRouter(prefix="/admin/settings", tags=["admin-settings"])

AdminUser = Annotated[User, Depends(require_role("admin"))]
DbSession = Annotated[Session, Depends(get_db)]


class IngressSettings(BaseModel):
    ingress_controller: str = Field(
        default=DEFAULT_INGRESS_CONTROLLER,
        description=(
            "OpenShift IngressController name used for Troshka Routes "
            "(default → cluster apps domain; ingress-rhdp-net → per-cluster "
            "apps.<cluster>.rhdp.net)."
        ),
    )


class SystemSettingsResponse(BaseModel):
    ingress_controller: str


@router.get("", response_model=SystemSettingsResponse)
def get_system_settings(user: AdminUser, db: DbSession):
    return SystemSettingsResponse(
        ingress_controller=get_ingress_controller_name(db),
    )


@router.put("", response_model=SystemSettingsResponse)
def update_system_settings(
    body: IngressSettings,
    user: AdminUser,
    db: DbSession,
):
    name = set_ingress_controller_name(db, body.ingress_controller)
    return SystemSettingsResponse(ingress_controller=name)
