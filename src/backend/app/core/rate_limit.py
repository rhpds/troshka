import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

BAN_WINDOW = 60
BAN_THRESHOLD = 10
BAN_DURATION = 300

EXEMPT_PATHS = {"/api/v1/health", "/api/v1/ws"}

# Per-user deploy concurrency defaults (overridable via config)
MAX_CONCURRENT_DEPLOYS_PER_USER = 20
MAX_REQUESTS_PER_MINUTE = 100


def _get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _get_user_id(request: Request) -> str | None:
    return getattr(request.state, "user_id", None)


def record_auth_failure(ip: str):
    try:
        from app.core.redis import get_redis

        r = get_redis()
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
                "Banned IP %s for %ds (%d failures in %ds)",
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

        ip = _get_client_ip(request)
        if is_banned(ip):
            return JSONResponse(status_code=403, content={"detail": "banned"})

        # Per-user request rate limiting
        user_id = _get_user_id(request)
        if user_id:
            try:
                from app.core.redis import sliding_window_rate

                allowed, count = sliding_window_rate(
                    f"ratelimit:requests:{user_id}",
                    window=60,
                    limit=MAX_REQUESTS_PER_MINUTE,
                )
                if not allowed:
                    return JSONResponse(
                        status_code=429,
                        content={"detail": "Too many requests"},
                        headers={"Retry-After": "60"},
                    )
            except Exception:
                pass

        response = await call_next(request)

        if response.status_code == 401:
            record_auth_failure(ip)

        return response
