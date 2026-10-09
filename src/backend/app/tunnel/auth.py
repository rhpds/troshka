"""Short-lived DB auth for tunnel WebSockets (API keys only).

Public tunnel Route is not behind oauth-proxy — never trust SSO headers or JWT.
"""

from __future__ import annotations

import datetime
import logging

from fastapi import WebSocket

from app.core.database import SessionLocal
from app.models.project import Project
from app.models.user import User

logger = logging.getLogger(__name__)


def _token_from_websocket(websocket: WebSocket) -> str | None:
    """Bearer trk_… only — no query-string tokens (access-log leakage)."""
    auth = websocket.headers.get("authorization") or ""
    if auth.lower().startswith("bearer trk_"):
        return auth.split(" ", 1)[1].strip() or None
    return None


def authorize_tunnel_session(
    websocket: WebSocket, project_id: str, cluster_id: str
) -> dict:
    """Authenticate and resolve dial context; DB is closed before return.

    On success: ``{"ok": True, "user_id", "project_id", "cluster", "targets",
    "host", "provider"}``.
    On failure: ``{"ok": False, "code", "reason"}``.
    """
    from app.models.host import Host
    from app.models.provider import Provider
    from app.services.ocp import api_tunnel as tun

    db = SessionLocal()
    try:
        token = _token_from_websocket(websocket)
        user = _resolve_user(token, db)
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


def _resolve_user(token: str | None, db) -> User | None:
    """Unscoped, active, non-expired Troshka API key only."""
    if not token or not token.startswith("trk_"):
        return None
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
