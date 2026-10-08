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


def _is_scoped_api_key(token: str | None, db) -> bool:
    """True when the token is a project-scoped API key.

    Websockets bypass HTTP dependencies (including the global scoped-key
    default-deny in get_current_user), so the /ws route must reject scoped
    ops-pod keys explicitly. Scoped keys are limited to REST topology-read +
    vm-exec; they must never open a project WS.
    """
    if not token or not token.startswith("trk_"):
        return False
    from app.models.api_key import ApiKey, hash_key

    api_key = (
        db.query(ApiKey).filter_by(key_hash=hash_key(token), is_active=True).first()
    )
    return bool(api_key and api_key.is_scoped)


def _authenticate_ws_api_key(token: str, db) -> User | None:
    """Resolve an unscoped ``trk_`` API key to its user (None if missing/scoped)."""
    if not token.startswith("trk_"):
        return None
    import datetime

    from app.models.api_key import ApiKey, hash_key

    api_key = (
        db.query(ApiKey).filter_by(key_hash=hash_key(token), is_active=True).first()
    )
    if not api_key or api_key.is_scoped:
        return None
    if api_key.expires_at and api_key.expires_at < datetime.datetime.now(datetime.UTC):
        return None
    api_key.last_used_at = datetime.datetime.now(datetime.UTC)
    return api_key.user


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

    api_user = _authenticate_ws_api_key(token, db)
    if api_user:
        return api_user

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
    from app.services.deploy_service import _get_deploy_progress_data
    from app.services.ws_pubsub import get_cached_vm_states

    dp = _get_deploy_progress_data(project.id)
    if not dp and project.state == "deploying":
        from app.core.redis import get_job_info

        job_info = get_job_info(project.id)
        if job_info and job_info.get("status") == "queued":
            dp = {
                "step": "queued",
                "detail": f"#{job_info['queue_position']} of {job_info['queue_length']}",
                "queue_position": job_info["queue_position"],
                "queue_length": job_info["queue_length"],
            }

    snapshot: dict = {
        "type": "snapshot",
        "project_state": project.state,
        "deploy_error": project.deploy_error,
        "deploy_progress": dp,
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
        if _is_scoped_api_key(token, db):
            await websocket.close(
                code=4003, reason="Scoped API keys cannot open websockets"
            )
            return

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


def _authorize_project_ws(websocket: WebSocket, project_id: str, db):
    """AuthN/AuthZ for project-scoped websockets. Returns (user, project) or None."""
    token = websocket.query_params.get("token")
    if _is_scoped_api_key(token, db):
        return None, None, 4003, "Scoped API keys cannot open websockets"
    user = _authenticate_ws(token, db, headers=dict(websocket.headers))
    if not user:
        return None, None, 4001, "Unauthorized"
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        return None, None, 4004, "Project not found"
    if project.owner_id != user.id and user.role != "admin":
        return None, None, 4003, "Access denied"
    return user, project, None, None


@router.websocket("/api/v1/projects/{project_id}/clusters/{cluster_id}/api-tunnel")
async def cluster_api_tunnel(websocket: WebSocket, project_id: str, cluster_id: str):
    """Binary WebSocket TCP tunnel to a nested OCP API (owner/admin only)."""
    from app.models.host import Host
    from app.services.ocp import api_tunnel as tun

    db = SessionLocal()
    ssock = None
    writer = None
    try:
        _user, project, code, reason = _authorize_project_ws(websocket, project_id, db)
        if code or project is None:
            await websocket.close(code=code or 4001, reason=reason or "Unauthorized")
            return

        cluster = tun.get_cluster_access(project, cluster_id)
        if not cluster or not cluster.kubeconfig_available:
            await websocket.close(code=4004, reason="Cluster kubeconfig not available")
            return
        if not project.host_id:
            await websocket.close(code=4004, reason="Project has no host")
            return
        host = db.query(Host).filter_by(id=project.host_id).first()
        if not host:
            await websocket.close(code=4004, reason="Host not found")
            return

        try:
            targets = tun.resolve_dial_targets(project, cluster, host, db)
        except Exception as exc:
            logger.warning("API tunnel dial resolve failed: %s", exc)
            await websocket.close(code=1013, reason=str(exc)[:120])
            return
        if not targets:
            await websocket.close(code=1013, reason="No dial path for cluster")
            return

        await websocket.accept()

        last_err: Exception | None = None
        provider = None
        if host.provider_id:
            from app.models.provider import Provider

            provider = db.query(Provider).filter_by(id=host.provider_id).first()

        for candidate in targets:
            try:
                if candidate.via == "troshkad":
                    ssock = await asyncio.wait_for(
                        asyncio.to_thread(
                            tun.open_troshkad_tunnel_socket,
                            host,
                            project.id,
                            candidate.host,
                            candidate.port,
                        ),
                        timeout=15,
                    )
                elif candidate.via in ("kubevirt-pf", "kubevirt-exec"):
                    if not provider:
                        raise RuntimeError("No provider for kubevirt tunnel")
                    opener = (
                        tun.open_kubevirt_portforward_socket
                        if candidate.via == "kubevirt-pf"
                        else tun.open_kubevirt_exec_relay
                    )
                    ssock = await asyncio.wait_for(
                        asyncio.to_thread(opener, provider, candidate),
                        timeout=30,
                    )
                else:
                    reader, writer = await asyncio.wait_for(
                        tun.open_direct_connection(candidate.host, candidate.port),
                        timeout=3,
                    )
                    await websocket.send_json(
                        {
                            "type": "ready",
                            "via": candidate.via,
                            "tls_server_name": candidate.tls_server_name,
                            "force_insecure": candidate.force_insecure,
                        }
                    )
                    await tun.bridge_websocket_to_tcp(websocket, reader, writer)
                    writer = None
                    return

                await websocket.send_json(
                    {
                        "type": "ready",
                        "via": candidate.via,
                        "tls_server_name": candidate.tls_server_name,
                        "force_insecure": candidate.force_insecure,
                    }
                )
                await tun.bridge_websocket_to_socket(websocket, ssock)
                ssock = None
                return
            except Exception as exc:
                last_err = exc
                logger.info(
                    "API tunnel dial %s:%s via=%s failed (%s); trying next",
                    candidate.host,
                    candidate.port,
                    candidate.via,
                    exc,
                )
                if writer is not None:
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except Exception:
                        pass
                    writer = None
                if ssock is not None:
                    try:
                        ssock.close()
                    except Exception:
                        pass
                    ssock = None

        reason = f"All dial paths failed: {last_err}"[:120]
        logger.warning(
            "API tunnel exhausted for %s/%s: %s",
            project_id[:8],
            cluster_id,
            reason,
        )
        try:
            await websocket.send_json({"type": "error", "error": reason})
        except Exception:
            pass
        await websocket.close(code=1013, reason=reason)
    except Exception:
        logger.debug(
            "API tunnel error for %s/%s", project_id[:8], cluster_id, exc_info=True
        )
        try:
            await websocket.close(code=1011, reason="Tunnel failed")
        except Exception:
            pass
    finally:
        if writer is not None:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
        if ssock is not None:
            try:
                ssock.close()
            except Exception:
                pass
        db.close()


@router.websocket("/api/v1/patterns/{pattern_id}/ws")
async def pattern_websocket(websocket: WebSocket, pattern_id: str):
    token = websocket.query_params.get("token")
    db = SessionLocal()
    try:
        if _is_scoped_api_key(token, db):
            await websocket.close(
                code=4003, reason="Scoped API keys cannot open websockets"
            )
            return

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
