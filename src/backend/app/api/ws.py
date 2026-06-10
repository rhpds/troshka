"""WebSocket endpoint for real-time project state updates."""
import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from app.core.config import config
from app.core.database import SessionLocal
from app.models.project import Project
from app.models.user import User
from app.services.ws_pubsub import subscribe, unsubscribe

logger = logging.getLogger(__name__)

router = APIRouter()

HEARTBEAT_INTERVAL = 30


def _authenticate_ws(token: str | None, db) -> User | None:
    if not config.auth.oauth_enabled and not token:
        from app.core.auth import _get_or_create_dev_user
        return _get_or_create_dev_user(db)

    if not token:
        return None

    from app.core.auth import decode_jwt
    payload = decode_jwt(token)
    if not payload:
        return None
    email = payload.get("email") or payload.get("sub")
    if not email:
        return None
    return db.query(User).filter_by(email=email).first()


def _build_snapshot(project: Project, db) -> dict:
    """Build initial WS snapshot. DB session is used then closed before troshkad calls."""
    from app.services.deploy_service import _deploy_progress
    from app.api.projects import _redeploy_progress, _domain_name
    from app.services.troshkad_client import get_vm_state as troshkad_get_vm_state
    from app.models.host import Host

    snapshot = {
        "type": "snapshot",
        "project_state": project.state,
        "deploy_error": project.deploy_error,
        "deploy_progress": _deploy_progress.get(project.id),
        "vm_states": {},
        "vm_progress": {},
    }

    if not project.host_id:
        return snapshot

    host = db.query(Host).filter_by(id=project.host_id).first()
    if not host or not host.ip_address:
        return snapshot

    # Collect all data we need from DB objects before closing the session
    project_id = project.id
    topology_nodes = (project.topology or {}).get("nodes", [])
    host_copy = type("H", (), {
        "ip_address": host.ip_address,
        "agent_token": host.agent_token,
        "agent_cert_fingerprint": host.agent_cert_fingerprint,
    })()

    # Close DB session before making network calls to troshkad
    db.close()

    for node in topology_nodes:
        if node.get("type") != "vmNode":
            continue
        dom_name = _domain_name(project_id, node["id"])
        if dom_name in _redeploy_progress:
            snapshot["vm_states"][node["id"]] = "redeploying"
            snapshot["vm_progress"][node["id"]] = _redeploy_progress[dom_name]
        else:
            try:
                vm_info = troshkad_get_vm_state(host_copy, dom_name, timeout=5)
                state = vm_info["state"]
                if state == "shut_off":
                    state = "stopped"
                snapshot["vm_states"][node["id"]] = state
            except Exception:
                snapshot["vm_states"][node["id"]] = "unknown"

    return snapshot


@router.websocket("/api/v1/projects/{project_id}/ws")
async def project_websocket(websocket: WebSocket, project_id: str):
    token = websocket.query_params.get("token")

    db = SessionLocal()
    try:
        user = _authenticate_ws(token, db)
        if not user:
            await websocket.close(code=4001, reason="Unauthorized")
            return

        project = db.query(Project).filter_by(id=project_id).first()
        if not project:
            await websocket.close(code=4004, reason="Project not found")
            return
        if project.owner_id != user.id and user.role != "admin":
            await websocket.close(code=4003, reason="Access denied")
            return

        await websocket.accept()
        subscribe(project_id, websocket)

        snapshot = _build_snapshot(project, db)
        db = None  # _build_snapshot closes the session before troshkad calls
        await websocket.send_json(snapshot)

        # Keep alive: listen for client messages, send heartbeat pings
        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=HEARTBEAT_INTERVAL)
            except asyncio.TimeoutError:
                if websocket.client_state == WebSocketState.CONNECTED:
                    await websocket.send_json({"type": "ping"})
            except WebSocketDisconnect:
                break

    except Exception:
        logger.debug("WebSocket error for project %s", project_id[:8], exc_info=True)
    finally:
        unsubscribe(project_id, websocket)
        if db:
            db.close()
