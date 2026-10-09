"""Connection caps for the tunnel service (env-overridable)."""

from __future__ import annotations

import os
from threading import Lock


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


MAX_SESSIONS_PER_POD = _int_env("TROSHKA_TUNNEL__MAX_SESSIONS_PER_POD", 100)
MAX_STREAMS_PER_SESSION = _int_env("TROSHKA_TUNNEL__MAX_STREAMS_PER_SESSION", 32)
MAX_SESSIONS_PER_USER = _int_env("TROSHKA_TUNNEL__MAX_SESSIONS_PER_USER", 20)
MAX_SESSIONS_PER_PROJECT = _int_env("TROSHKA_TUNNEL__MAX_SESSIONS_PER_PROJECT", 10)
DIAL_TIMEOUT_S = _int_env("TROSHKA_TUNNEL__DIAL_TIMEOUT_S", 15)
# Receive wait / server→client ping interval while a session has no traffic.
# Must NOT close the session when this elapses with zero streams — multi-cluster
# troshka-oc parks idle cluster WebSockets for the daemon lifetime.
STREAM_IDLE_S = _int_env("TROSHKA_TUNNEL__STREAM_IDLE_S", 60)


class SessionRegistry:
    """In-process session accounting for caps."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._by_user: dict[str, int] = {}
        self._by_project: dict[str, int] = {}
        self._total = 0

    def try_acquire(self, user_id: str, project_id: str) -> str | None:
        """Return an error reason if the session cannot start, else None."""
        with self._lock:
            if self._total >= MAX_SESSIONS_PER_POD:
                return "tunnel pod at capacity"
            if self._by_user.get(user_id, 0) >= MAX_SESSIONS_PER_USER:
                return "too many tunnels for user"
            if self._by_project.get(project_id, 0) >= MAX_SESSIONS_PER_PROJECT:
                return "too many tunnels for project"
            self._total += 1
            self._by_user[user_id] = self._by_user.get(user_id, 0) + 1
            self._by_project[project_id] = self._by_project.get(project_id, 0) + 1
            return None

    def release(self, user_id: str, project_id: str) -> None:
        with self._lock:
            self._total = max(0, self._total - 1)
            u = self._by_user.get(user_id, 0) - 1
            if u <= 0:
                self._by_user.pop(user_id, None)
            else:
                self._by_user[user_id] = u
            p = self._by_project.get(project_id, 0) - 1
            if p <= 0:
                self._by_project.pop(project_id, None)
            else:
                self._by_project[project_id] = p


REGISTRY = SessionRegistry()
