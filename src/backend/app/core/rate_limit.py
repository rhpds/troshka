import logging
import threading
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

BAN_WINDOW = 60
BAN_THRESHOLD = 10
BAN_DURATION = 300

_fail_tracker: dict[str, list[float]] = {}
_banned_ips: dict[str, float] = {}
_lock = threading.Lock()

EXEMPT_PATHS = {"/api/v1/health"}


def _get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def record_auth_failure(ip: str):
    now = time.monotonic()
    with _lock:
        times = _fail_tracker.get(ip, [])
        cutoff = now - BAN_WINDOW
        times = [t for t in times if t > cutoff]
        times.append(now)
        _fail_tracker[ip] = times
        if len(times) >= BAN_THRESHOLD:
            _banned_ips[ip] = now + BAN_DURATION
            del _fail_tracker[ip]
            logger.warning(
                "Banned IP %s for %ds (%d failures in %ds)",
                ip,
                BAN_DURATION,
                len(times),
                BAN_WINDOW,
            )


def is_banned(ip: str) -> bool:
    with _lock:
        expiry = _banned_ips.get(ip)
        if expiry is None:
            return False
        if time.monotonic() > expiry:
            del _banned_ips[ip]
            return False
        return True


def cleanup():
    now = time.monotonic()
    with _lock:
        cutoff = now - BAN_WINDOW
        stale = [
            ip for ip, times in _fail_tracker.items() if not times or times[-1] < cutoff
        ]
        for ip in stale:
            del _fail_tracker[ip]
        expired = [ip for ip, exp in _banned_ips.items() if now > exp]
        for ip in expired:
            del _banned_ips[ip]


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in EXEMPT_PATHS:
            return await call_next(request)

        ip = _get_client_ip(request)
        if is_banned(ip):
            return JSONResponse(status_code=403, content={"detail": "banned"})

        response = await call_next(request)

        if response.status_code == 401:
            record_auth_failure(ip)

        return response
