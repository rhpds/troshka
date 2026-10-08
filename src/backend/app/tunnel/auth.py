"""Short-lived DB auth for tunnel WebSockets (release session before dial)."""

from __future__ import annotations

import datetime
import logging

from fastapi import WebSocket

from app.core.config import config
from app.core.database import SessionLocal
from app.models.project import Project
from app.models.user import User

logger = logging.getLogger(__name__)


def _token_from_websocket(websocket: WebSocket) -> str | None:
    auth = websocket.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip() or None
    # Legacy query param (discouraged — appears in access logs).
    return websocket.query_params.get("token")


def authorize_tunnel_session(
    websocket: WebSocket, project_id: str, cluster_id: str
) -> dict:
    """Authenticate and resolve dial context; DB is closed before return.

    On success: ``{"ok": True, "user", "project_id", "cluster", "targets",
    "host", "provider"}``.
    On failure: ``{"ok": False, "code", "reason"}``.
    """
    from app.models.host import Host
    from app.models.provider import Provider
    from app.services.ocp import api_tunnel as tun

    db = SessionLocal()
    try:
        token = _token_from_websocket(websocket)
        user = _resolve_user(token, db, headers=dict(websocket.headers))
        if not user:
            return {"ok": False, "code": 4001, "reason": "Unauthorized"}

        project = db.query(Project).filter_by(id=project_id).first()
        if not project:
            return {"ok": False, "code": 4004, "reason": "Project not found"}
        if project.owner_id != user.id and user.role != "admin":
            return {"ok": False, "code": 4003, "reason": "Access denied"}

        cluster = tun.get_cluster_access(project, cluster_id)
        if not cluster or not cluster.kubeconfig_available:
            return {
                "ok": False,
                "code": 4004,
                "reason": "Cluster kubeconfig not available",
            }
        if not project.host_id:
            return {"ok": False, "code": 4004, "reason": "Project has no host"}

        host = db.query(Host).filter_by(id=project.host_id).first()
        if not host:
            return {"ok": False, "code": 4004, "reason": "Host not found"}

        try:
            targets = tun.resolve_dial_targets(project, cluster, host, db)
        except Exception as exc:
            logger.warning("tunnel dial resolve failed: %s", exc)
            return {"ok": False, "code": 1013, "reason": str(exc)[:120]}
        if not targets:
            return {"ok": False, "code": 1013, "reason": "No dial path for cluster"}

        provider = None
        if host.provider_id:
            provider = db.query(Provider).filter_by(id=host.provider_id).first()
            if provider:
                provider.get_credentials()
                db.expunge(provider)

        _ = (
            host.ip_address,
            host.agent_token,
            host.agent_cert_fingerprint,
            host.provider_id,
        )
        user_id = str(user.id)
        pid = str(project.id)
        db.expunge(host)
        db.commit()
        return {
            "ok": True,
            "user_id": user_id,
            "project_id": pid,
            "cluster": cluster,
            "targets": targets,
            "host": host,
            "provider": provider,
        }
    except Exception:
        logger.exception("tunnel auth failed")
        return {"ok": False, "code": 1011, "reason": "Auth failed"}
    finally:
        db.close()


def _resolve_user(token: str | None, db, headers: dict) -> User | None:
    if token and token.startswith("trk_"):
        from app.models.api_key import ApiKey, hash_key

        api_key = (
            db.query(ApiKey).filter_by(key_hash=hash_key(token), is_active=True).first()
        )
        if not api_key or api_key.is_scoped:
            return None
        if api_key.expires_at and api_key.expires_at < datetime.datetime.now(
            datetime.UTC
        ):
            return None
        api_key.last_used_at = datetime.datetime.now(datetime.UTC)
        return api_key.user

    if not config.auth.oauth_enabled and not token:
        from app.core.auth import _get_or_create_dev_user

        return _get_or_create_dev_user(db)

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
