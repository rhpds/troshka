import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.portal import ProjectPortalToken
from app.models.project import Project
from app.models.user import User

logger = logging.getLogger(__name__)
router = APIRouter(tags=["portal"])

ACCESS_LEVELS = {"readonly": 0, "power": 1, "console": 2, "manage": 3}
POWER_ACTIONS = {"start", "stop", "restart", "forcestop"}


class PortalTokenRequest(BaseModel):
    access_level: str = "readonly"
    expires_at: str | None = None


@router.post("/projects/{project_id}/portal-token", status_code=201)
def create_portal_token(
    project_id: str,
    body: PortalTokenRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(404, "Project not found")
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(403, "Not authorized")
    if body.access_level not in ("readonly", "power", "console", "manage"):
        raise HTTPException(400, f"Invalid access level: {body.access_level}")

    portal_token = ProjectPortalToken(
        project_id=project_id,
        access_level=body.access_level,
    )
    db.add(portal_token)
    db.commit()
    db.refresh(portal_token)

    base_url = str(request.base_url).rstrip("/")
    return {
        "token": portal_token.token,
        "access_level": portal_token.access_level,
        "portal_url": f"{base_url}/portal/{portal_token.token}",
    }


@router.get("/portal/{token}")
def get_portal(
    token: str,
    db: Session = Depends(get_db),
):
    """Public endpoint -- no authentication required. Token is the auth."""
    portal_token = db.query(ProjectPortalToken).filter_by(token=token).first()
    if not portal_token:
        raise HTTPException(404, "Invalid or expired portal token")

    project = db.query(Project).filter_by(id=portal_token.project_id).first()
    if not project:
        raise HTTPException(404, "Project not found")

    topology = project.topology or {}
    hidden = set(topology.get("hiddenNodeIds", []))
    if hidden:
        topology = {
            **topology,
            "nodes": [n for n in topology.get("nodes", []) if n["id"] not in hidden],
            "edges": [
                e
                for e in topology.get("edges", [])
                if e.get("source") not in hidden and e.get("target") not in hidden
            ],
        }

    return {
        "project_id": project.id,
        "project_name": project.name,
        "project_state": project.state,
        "access_level": portal_token.access_level,
        "topology": topology,
    }


@router.get("/portal/{token}/vm-states")
def portal_vm_states(
    token: str,
    db: Session = Depends(get_db),
):
    """Public endpoint — get live VM states via portal token."""
    portal_token = db.query(ProjectPortalToken).filter_by(token=token).first()
    if not portal_token:
        raise HTTPException(404, "Invalid or expired portal token")
    project = db.query(Project).filter_by(id=portal_token.project_id).first()
    if not project or not project.host_id:
        return {"states": {}}

    from app.models.host import Host
    from app.services.troshkad_client import get_vm_state as troshkad_get_vm_state

    host = db.query(Host).filter_by(id=project.host_id).first()
    if not host:
        return {"states": {}}

    states = {}
    for node in (project.topology or {}).get("nodes", []):
        if node.get("type") != "vmNode":
            continue
        dom_name = f"troshka-{project.id[:8]}-{node['id'][:8]}"
        state_info = troshkad_get_vm_state(host, dom_name)
        raw = (
            state_info.get("state", "unknown")
            if isinstance(state_info, dict)
            else "unknown"
        )
        if raw == "running":
            states[node["id"]] = "running"
        elif raw == "shut_off":
            states[node["id"]] = "stopped"
        else:
            states[node["id"]] = raw
    return {"states": states}


def _get_portal_token(
    token: str, db: Session, min_level: str = "readonly"
) -> tuple[ProjectPortalToken, Project]:
    portal_token = db.query(ProjectPortalToken).filter_by(token=token).first()
    if not portal_token:
        raise HTTPException(404, "Invalid or expired portal token")
    if ACCESS_LEVELS.get(portal_token.access_level, 0) < ACCESS_LEVELS.get(
        min_level, 0
    ):
        raise HTTPException(
            403,
            f"Access level '{portal_token.access_level}' insufficient, requires '{min_level}'",
        )
    project = db.query(Project).filter_by(id=portal_token.project_id).first()
    if not project:
        raise HTTPException(404, "Project not found")
    return portal_token, project


ACTION_MAP = {
    "start": ("/vms/start", 60),
    "stop": ("/vms/stop", 60),
    "restart": ("/vms/reboot", 60),
    "forcestop": ("/vms/force-off", 30),
}


@router.post("/portal/{token}/vms/{vm_id}/{action}")
def portal_vm_action(
    token: str,
    vm_id: str,
    action: str,
    db: Session = Depends(get_db),
):
    """Public endpoint -- token is the auth. Perform VM power actions via portal."""
    if action not in POWER_ACTIONS:
        raise HTTPException(400, f"Unknown action: {action}")
    _, project = _get_portal_token(token, db, min_level="power")
    if project.state not in ("active", "stopped"):
        raise HTTPException(
            400, f"Project is {project.state}, cannot perform VM actions"
        )
    if not project.host_id:
        raise HTTPException(400, "Project is not deployed")

    from app.models.host import Host
    from app.services.troshkad_client import TroshkadError, start_job, wait_for_job

    host = db.query(Host).filter_by(id=project.host_id).first()
    if not host:
        raise HTTPException(400, "Host is disconnected or unavailable")

    dom_name = f"troshka-{project.id[:8]}-{vm_id[:8]}"
    endpoint, timeout = ACTION_MAP[action]
    try:
        job_id = start_job(host, endpoint, {"domain_name": dom_name})
        wait_for_job(host, job_id, timeout=timeout, poll_interval=2)
        return {"action": action, "success": True}
    except TroshkadError as e:
        logger.error("Portal VM action %s failed for %s: %s", action, dom_name, e)
        return {"action": action, "success": False}
