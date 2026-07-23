"""WebSocket endpoints for real-time project state updates and console proxy."""

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


def _authenticate_ws(token: str | None, db, headers=None) -> User | None:
    if not config.auth.oauth_enabled and not token:
        from app.core.auth import _get_or_create_dev_user

        return _get_or_create_dev_user(db)

    # SSO mode: check X-Forwarded-Email header (from oauth-proxy)
    if headers:
        email = headers.get("x-forwarded-email")
        if email:
            from app.core.auth import _upsert_sso_user

            return _upsert_sso_user(email, headers.get("x-forwarded-user"), db)

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
    """Build initial WS snapshot from cached poller data only."""
    from app.services.deploy_service import _deploy_progress
    from app.services.ws_pubsub import get_cached_vm_states

    snapshot: dict = {
        "type": "snapshot",
        "project_state": project.state,
        "deploy_error": project.deploy_error,
        "deploy_progress": _deploy_progress.get(project.id),
        "vm_states": {},
        "vm_progress": {},
    }

    db.close()

    cached = get_cached_vm_states(project.id)
    if cached:
        snapshot["vm_states"] = cached.get("states", {})
        snapshot["vm_progress"] = cached.get("progress", {})

    return snapshot


@router.websocket("/api/v1/projects/{project_id}/ws")
async def project_websocket(websocket: WebSocket, project_id: str):
    token = websocket.query_params.get("token")

    db = SessionLocal()
    try:
        user = _authenticate_ws(token, db, headers=dict(websocket.headers))
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

        snapshot = await asyncio.to_thread(_build_snapshot, project, db)
        db = None  # _build_snapshot closes the session before troshkad calls
        await websocket.send_json(snapshot)

        # Keep alive: listen for client messages, send heartbeat pings
        while True:
            try:
                await asyncio.wait_for(
                    websocket.receive_text(), timeout=HEARTBEAT_INTERVAL
                )
            except TimeoutError:
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


@router.websocket("/api/v1/patterns/{pattern_id}/ws")
async def pattern_websocket(websocket: WebSocket, pattern_id: str):
    token = websocket.query_params.get("token")
    db = SessionLocal()
    try:
        user = _authenticate_ws(token, db, headers=dict(websocket.headers))
        if not user:
            await websocket.close(code=4001, reason="Unauthorized")
            return
        from app.models.pattern import Pattern

        pattern = db.query(Pattern).filter_by(id=pattern_id).first()
        if not pattern or (pattern.owner_id != user.id and user.role != "admin"):
            await websocket.close(code=4004, reason="Pattern not found")
            return
        db.close()
        db = None

        await websocket.accept()
        from app.services.ws_pubsub import subscribe_pattern, unsubscribe_pattern

        subscribe_pattern(pattern_id, websocket)
        try:
            while True:
                try:
                    await asyncio.wait_for(
                        websocket.receive_text(), timeout=HEARTBEAT_INTERVAL
                    )
                except TimeoutError:
                    if websocket.client_state == WebSocketState.CONNECTED:
                        await websocket.send_json({"type": "ping"})
                except WebSocketDisconnect:
                    break
        finally:
            unsubscribe_pattern(pattern_id, websocket)
    except Exception:
        logger.debug("Pattern WS error for %s", pattern_id[:8], exc_info=True)
    finally:
        if db:
            db.close()
