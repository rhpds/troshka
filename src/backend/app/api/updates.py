from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.core.auth import require_role
from app.models.user import User
from app.services import app_updater

router = APIRouter(prefix="/update", tags=["update"])

AdminUser = Annotated[User, Depends(require_role("admin"))]


class ApplyUpdateRequest(BaseModel):
    # Dev mode only: also restart RQ workers so deploy/job code reloads too.
    restart_workers: bool = True


@router.get("/status")
def get_update_status(user: AdminUser):
    return app_updater.get_status()


@router.post(
    "/apply", responses={400: {"description": "Updates disabled in this environment"}}
)
def apply_update(
    user: AdminUser, request: Request, body: ApplyUpdateRequest | None = None
):
    if app_updater.resolve_mode() == "disabled":
        raise HTTPException(
            status_code=400, detail="Updates are managed externally (ArgoCD)"
        )
    client_ip = request.client.host if request.client else None
    restart_workers = body.restart_workers if body else True
    return app_updater.apply_update(
        initiated_by=user.email,
        client_ip=client_ip,
        restart_workers=restart_workers,
    )
