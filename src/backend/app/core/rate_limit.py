"""Abuse controls for public API (and shared helpers for troshka-tunnel).

Event NAT: hundreds of users may share 1–2 egress IPs. Prefer per-credential
limits after auth; keep per-IP ceilings very high; only ban cold attacker IPs
that never successfully authenticate.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)


def _enforcement_enabled() -> bool:
    """Skip Redis rate limits during pytest (shared TestClient IP blows the ceiling).

    Set ``TROSHKA_RATE_LIMIT_IN_TESTS=1`` to exercise middleware under pytest.
    """
    if os.environ.get("TROSHKA_RATE_LIMIT_DISABLED") == "1":
        return False
    if (
        os.environ.get("PYTEST_CURRENT_TEST")
        and os.environ.get("TROSHKA_RATE_LIMIT_IN_TESTS") != "1"
    ):
        return False
    return True


BAN_WINDOW = 60
# High on purpose: shared event NATs must not trip on a few bad logins.
BAN_THRESHOLD = 100
BAN_DURATION = 300

EXEMPT_PATHS = {"/api/v1/health", "/api/v1/ws"}

# Per authenticated credential (API key or JWT), not per NAT IP.
MAX_REQUESTS_PER_MINUTE = 100
# Loose flood break for requests with no Bearer credential (event-safe).
MAX_UNAUTH_REQUESTS_PER_IP_PER_MINUTE = 3000
# Tunnel WebSocket opens per NAT IP (dual-context use + idle re-dial).
MAX_TUNNEL_OPENS_PER_IP_PER_MINUTE = 2000

# Per-user deploy concurrency defaults (overridable via config)
MAX_CONCURRENT_DEPLOYS_PER_USER = 20


def _get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _credential_fingerprint(authorization: str | None) -> str | None:
    """Stable id for rate limiting without storing the secret."""
    if not authorization:
        return None
    if not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:32]


def client_ip_from_headers(headers: dict, fallback: str = "unknown") -> str:
    forwarded = headers.get("x-forwarded-for") or headers.get("X-Forwarded-For")
    if forwarded:
        return str(forwarded).split(",")[0].strip()
    return fallback


def record_auth_success(ip: str) -> None:
    """Mark IP as having a recent successful auth (blocks NAT-wide bans)."""
    if not ip or ip == "unknown":
        return
    try:
        from app.core.redis import get_redis

        get_redis().set(f"ratelimit:auth_ok:{ip}", "1", ex=BAN_WINDOW * 5)
    except Exception:
        pass


def record_auth_failure(ip: str) -> None:
    """Count auth failures; ban only cold attacker IPs (no recent success)."""
    if not ip or ip == "unknown":
        return
    try:
        from app.core.redis import get_redis

        r = get_redis()
        if r.get(f"ratelimit:auth_ok:{ip}"):
            # Shared egress (events/office): do not escalate to IP ban.
            return

        key = f"ratelimit:auth_fail:{ip}"
        pipe = r.pipeline()
        now = time.time()
        pipe.zadd(key, {str(now): now})
        pipe.zremrangebyscore(key, 0, now - BAN_WINDOW)
        pipe.zcard(key)
        pipe.expire(key, BAN_WINDOW + 1)
        results = pipe.execute()
        count = results[2]

        if count >= BAN_THRESHOLD:
            r.set(f"ratelimit:banned:{ip}", "1", ex=BAN_DURATION)
            r.delete(key)
            logger.warning(
                "Banned IP %s for %ds (%d failures in %ds, no recent auth success)",
                ip,
                BAN_DURATION,
                count,
                BAN_WINDOW,
            )
    except Exception:
        pass


def is_banned(ip: str) -> bool:
    try:
        from app.core.redis import get_redis

        return get_redis().get(f"ratelimit:banned:{ip}") is not None
    except Exception:
        return False


def check_request_rate(*, authorization: str | None, ip: str) -> tuple[bool, str]:
    """Return (allowed, reason). Per-credential when Bearer present, else per-IP."""
    if not _enforcement_enabled():
        return True, ""
    try:
        from app.core.redis import sliding_window_rate

        fp = _credential_fingerprint(authorization)
        if fp:
            allowed, _ = sliding_window_rate(
                f"ratelimit:cred:{fp}",
                window=60,
                limit=MAX_REQUESTS_PER_MINUTE,
            )
            return (allowed, "credential rate limit" if not allowed else "")
        allowed, _ = sliding_window_rate(
            f"ratelimit:unauth_ip:{ip}",
            window=60,
            limit=MAX_UNAUTH_REQUESTS_PER_IP_PER_MINUTE,
        )
        return (allowed, "unauthenticated IP rate limit" if not allowed else "")
    except Exception:
        return True, ""


def check_tunnel_open_rate(ip: str) -> bool:
    """Per-IP tunnel WebSocket open rate (high ceiling for event NAT)."""
    if not _enforcement_enabled():
        return True
    try:
        from app.core.redis import sliding_window_rate

        allowed, _ = sliding_window_rate(
            f"ratelimit:tunnel_open:{ip}",
            window=60,
            limit=MAX_TUNNEL_OPENS_PER_IP_PER_MINUTE,
        )
        return allowed
    except Exception:
        return True


def check_deploy_rate(user_id: str) -> tuple[bool, int]:
    """Check if user has exceeded concurrent deploy limit.
    Returns (allowed, current_count)."""
    try:
        from app.core.redis import get_redis

        r = get_redis()
        key = f"ratelimit:deploys:{user_id}"
        count = int(r.get(key) or 0)
        return count < MAX_CONCURRENT_DEPLOYS_PER_USER, count
    except Exception:
        return True, 0


def increment_deploy_count(user_id: str):
    try:
        from app.core.redis import increment_counter

        increment_counter(f"ratelimit:deploys:{user_id}", ttl=7200)
    except Exception:
        pass


def decrement_deploy_count(user_id: str):
    try:
        from app.core.redis import decrement_counter

        decrement_counter(f"ratelimit:deploys:{user_id}")
    except Exception:
        pass


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in EXEMPT_PATHS:
            return await call_next(request)
        if not _enforcement_enabled():
            return await call_next(request)

        ip = _get_client_ip(request)
        if is_banned(ip):
            return JSONResponse(status_code=403, content={"detail": "banned"})

        auth = request.headers.get("authorization")
        allowed, _reason = check_request_rate(authorization=auth, ip=ip)
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests"},
                headers={"Retry-After": "60"},
            )

        response = await call_next(request)

        if response.status_code == 401:
            record_auth_failure(ip)
        elif 200 <= response.status_code < 400 and _credential_fingerprint(auth):
            record_auth_success(ip)

        return response
