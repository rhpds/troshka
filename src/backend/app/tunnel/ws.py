"""WebSocket routes for the dedicated tunnel service."""

from __future__ import annotations

import logging

from fastapi import APIRouter, WebSocket

from app.tunnel.auth import authorize_tunnel_session
from app.tunnel.limits import REGISTRY
from app.tunnel.session import MultiplexSession

logger = logging.getLogger(__name__)

router = APIRouter()


@router.websocket("/api/v1/projects/{project_id}/clusters/{cluster_id}/api-tunnel")
async def cluster_api_tunnel(websocket: WebSocket, project_id: str, cluster_id: str):
    """Multiplexed TCP tunnel to a nested OCP API (owner/admin only)."""
    ctx = authorize_tunnel_session(websocket, project_id, cluster_id)
    if not ctx.get("ok"):
        await websocket.close(
            code=int(ctx.get("code") or 4001),
            reason=str(ctx.get("reason") or "Unauthorized")[:120],
        )
        return

    user_id = str(ctx["user_id"])
    pid = str(ctx["project_id"])
    reason = REGISTRY.try_acquire(user_id, pid)
    if reason:
        await websocket.close(code=1013, reason=reason[:120])
        return

    await websocket.accept()
    try:
        session = MultiplexSession(
            websocket,
            project_id=pid,
            host=ctx["host"],
            provider=ctx["provider"],
            targets=ctx["targets"],
        )
        await session.run()
    except Exception as exc:
        logger.info("tunnel session error %s/%s: %s", project_id[:8], cluster_id, exc)
        try:
            await websocket.send_json({"type": "error", "error": str(exc)[:200]})
        except Exception:
            pass
        try:
            await websocket.close(code=1013, reason=str(exc)[:120])
        except Exception:
            pass
    finally:
        REGISTRY.release(user_id, pid)
