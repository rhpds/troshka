import logging
import time
import threading

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

BAN_WINDOW = 60
BAN_THRESHOLD = 10
BAN_DURATION = 300
PERMABAN_THRESHOLD = 3
PERMABAN_WINDOW = 3600

_fail_tracker: dict[str, list[float]] = {}
_banned_ips: dict[str, float] = {}
_permabanned_ips: set[str] = set()
_ban_history: dict[str, list[float]] = {}
_lock = threading.Lock()

EXEMPT_PATHS = {"/api/v1/health"}


def _get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _is_internal_ip(ip: str) -> bool:
    return (
        ip.startswith("10.")
        or ip.startswith("172.")
        or ip.startswith("127.")
        or ip == "unknown"
    )


def record_auth_failure(ip: str):
    now = time.monotonic()
    with _lock:
        if ip in _permabanned_ips:
            return
        times = _fail_tracker.get(ip, [])
        cutoff = now - BAN_WINDOW
        times = [t for t in times if t > cutoff]
        times.append(now)
        _fail_tracker[ip] = times
        if len(times) >= BAN_THRESHOLD:
            _banned_ips[ip] = now + BAN_DURATION
            del _fail_tracker[ip]
            history = _ban_history.get(ip, [])
            history_cutoff = now - PERMABAN_WINDOW
            history = [t for t in history if t > history_cutoff]
            history.append(now)
            _ban_history[ip] = history
            if len(history) >= PERMABAN_THRESHOLD:
                _permabanned_ips.add(ip)
                _banned_ips.pop(ip, None)
                _ban_history.pop(ip, None)
                logger.warning(
                    "Permanently banned IP %s (%d temp bans in %ds)",
                    ip,
                    len(history),
                    PERMABAN_WINDOW,
                )
            else:
                logger.warning(
                    "Banned IP %s for %ds (%d failures in %ds, strike %d/%d)",
                    ip,
                    BAN_DURATION,
                    len(times),
                    BAN_WINDOW,
                    len(history),
                    PERMABAN_THRESHOLD,
                )


def is_banned(ip: str) -> bool:
    with _lock:
        if ip in _permabanned_ips:
            return True
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
        history_cutoff = now - PERMABAN_WINDOW
        stale_history = [
            ip
            for ip, times in _ban_history.items()
            if not times or times[-1] < history_cutoff
        ]
        for ip in stale_history:
            del _ban_history[ip]


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in EXEMPT_PATHS:
            return await call_next(request)

        ip = _get_client_ip(request)
        if _is_internal_ip(ip):
            return await call_next(request)
        if is_banned(ip):
            return JSONResponse(status_code=403, content={"detail": "banned"})

        response = await call_next(request)

        if response.status_code == 401:
            record_auth_failure(ip)

        return response
