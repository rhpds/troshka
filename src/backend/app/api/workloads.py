"""Workload runs REST API — trigger, status, list."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.database import get_db
from app.core.redis import get_progress
from app.models.project import Project
from app.models.user import User
from app.models.workload_run import WorkloadRun
from app.services.workloads.run_service import _has_ocp, start_workload_run

router = APIRouter(tags=["workloads"])

CurrentUser = Annotated[User, Depends(get_current_user)]
DbSession = Annotated[Session, Depends(get_db)]

_PROJECT_NOT_FOUND = "Project not found"
_RUN_NOT_FOUND = "Workload run not found"
_ACCESS_DENIED = "Access denied"
_PROJECT_MUST_BE_ACTIVE = "Project must be active to run workloads"
_CLUSTER_NOT_WORKLOAD_READY = "Cluster not yet workload-ready (minimal control-plane-usable milestone not reached)"


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
class WorkloadRunRequest(BaseModel):
    kind: str
    catalog_item: str | None = None
    role_fqcn: str | None = None
    target_map: dict | None = None
    # AgnosticD-compatible collections/roles to install before the workload runs
    # (git-sourced), e.g. {"collections": [{"name": "https://github.com/rhpds/
    # core_workloads.git", "type": "git", "version": "main"}]}.
    requirements_content: dict | None = None
    ee_image: str | None = None


class WorkloadRunResponse(BaseModel):
    id: str
    status: str
    progress: dict | None = None


class WorkloadLogResponse(BaseModel):
    id: str
    status: str
    log: str


class WorkloadRunListItem(BaseModel):
    id: str
    project_id: str | None
    kind: str
    catalog_item: str | None
    role_fqcn: str | None
    status: str
    error: str | None
    created_at: str


class InventoryPreviewRequest(BaseModel):
    target_map: dict | None = None


class InventoryPreviewResponse(BaseModel):
    groups: dict[str, list[str]]
    errors: list[str]


# ---------------------------------------------------------------------------
# Helper: authorization guard
# ---------------------------------------------------------------------------
def _enforce_project_access(project: Project, user: User) -> None:
    """Raise 403 if user is not owner/admin."""
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail=_ACCESS_DENIED)


# ---------------------------------------------------------------------------
# POST /projects/{project_id}/workloads — trigger run
# ---------------------------------------------------------------------------
@router.post(
    "/projects/{project_id}/workloads",
    response_model=WorkloadRunResponse,
    status_code=202,
    responses={403: {}, 404: {}, 409: {}},
)
def trigger_workload_run(
    project_id: str,
    body: WorkloadRunRequest,
    user: CurrentUser,
    db: DbSession,
):
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail=_PROJECT_NOT_FOUND)

    _enforce_project_access(project, user)

    if project.state != "active":
        raise HTTPException(status_code=409, detail=_PROJECT_MUST_BE_ACTIVE)

    # OCP-targeting runs require the minimal control-plane-usable milestone
    if _has_ocp(project) and project.ocp_control_plane_usable_at is None:
        raise HTTPException(status_code=409, detail=_CLUSTER_NOT_WORKLOAD_READY)

    run = start_workload_run(
        db,
        project_id=project_id,
        kind=body.kind,
        catalog_item=body.catalog_item,
        role_fqcn=body.role_fqcn,
        target_map=body.target_map,
        requirements_content=body.requirements_content,
        ee_image=body.ee_image,
        owner_id=user.id,
    )

    return WorkloadRunResponse(
        id=run.id,
        status=run.status,
    )


# ---------------------------------------------------------------------------
# GET /projects/{project_id}/workloads — list runs
# ---------------------------------------------------------------------------
@router.get(
    "/projects/{project_id}/workloads",
    response_model=list[WorkloadRunListItem],
    responses={403: {}, 404: {}},
)
def list_workload_runs(
    project_id: str,
    user: CurrentUser,
    db: DbSession,
):
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail=_PROJECT_NOT_FOUND)

    _enforce_project_access(project, user)

    runs = (
        db.query(WorkloadRun)
        .filter_by(project_id=project_id)
        .order_by(WorkloadRun.created_at.desc())
        .all()
    )

    items = []
    for r in runs:
        created_at_str = ""
        if r.created_at and hasattr(r.created_at, "isoformat"):
            created_at_str = r.created_at.isoformat()  # type: ignore
        items.append(
            WorkloadRunListItem(
                id=r.id,
                project_id=r.project_id,
                kind=r.kind,
                catalog_item=r.catalog_item,
                role_fqcn=r.role_fqcn,
                status=r.status,
                error=r.error,
                created_at=created_at_str,
            )
        )
    return items


# ---------------------------------------------------------------------------
# GET /workloads/{run_id} — get run status/progress
# ---------------------------------------------------------------------------
@router.get(
    "/workloads/{run_id}",
    response_model=WorkloadRunResponse,
    responses={403: {}, 404: {}},
)
def get_workload_run_status(
    run_id: str,
    user: CurrentUser,
    db: DbSession,
):
    run = db.get(WorkloadRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail=_RUN_NOT_FOUND)

    # Load the run's project for authorization
    if run.project_id:
        project = db.get(Project, run.project_id)
        assert project is not None, f"Project {run.project_id} not found"
        _enforce_project_access(project, user)
    else:
        # Orphan run (project deleted) — admin-only
        if user.role != "admin":
            raise HTTPException(status_code=403, detail=_ACCESS_DENIED)

    # Redis-only progress (WorkloadRun has no progress column)
    progress = get_progress(f"workload:{run_id}")

    return WorkloadRunResponse(
        id=run.id,
        status=run.status,
        progress=progress,
    )


# ---------------------------------------------------------------------------
# GET /workloads/{run_id}/log — get run log
# ---------------------------------------------------------------------------
@router.get(
    "/workloads/{run_id}/log",
    response_model=WorkloadLogResponse,
    responses={403: {}, 404: {}},
)
def get_workload_run_log(run_id: str, user: CurrentUser, db: DbSession):
    run = db.get(WorkloadRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail=_RUN_NOT_FOUND)
    if run.project_id:
        project = db.get(Project, run.project_id)
        assert project is not None, f"Project {run.project_id} not found"
        _enforce_project_access(project, user)
    elif user.role != "admin":
        raise HTTPException(status_code=403, detail=_ACCESS_DENIED)

    from app.services.workloads.run_service import get_workload_log

    return WorkloadLogResponse(
        id=run.id, status=run.status, log=get_workload_log(db, run)
    )


# ---------------------------------------------------------------------------
# POST /projects/{project_id}/workloads/inventory-preview — preview inventory
# ---------------------------------------------------------------------------
@router.post(
    "/projects/{project_id}/workloads/inventory-preview",
    response_model=InventoryPreviewResponse,
    responses={403: {}, 404: {}},
)
def preview_workload_inventory(
    project_id: str,
    body: InventoryPreviewRequest,  # noqa: ARG001 — reserved for future scoping
    user: CurrentUser,
    db: DbSession,
):
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail=_PROJECT_NOT_FOUND)
    _enforce_project_access(project, user)

    from app.services.workloads.inventory import (
        InventoryError,
        preview_inventory,
        validate_ansible_groups,
    )

    topo = project.deployed_topology or project.topology or {}
    groups = preview_inventory(topo)
    errors: list[str] = []
    try:
        validate_ansible_groups(topo, require_bastion=(not _has_ocp(project)))
    except InventoryError as exc:
        errors.append(str(exc))
    return InventoryPreviewResponse(groups=groups, errors=errors)
