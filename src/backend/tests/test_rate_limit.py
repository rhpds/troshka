"""NAT-safe rate limiting for public API (and shared helpers for tunnel)."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from starlette.requests import Request
from starlette.responses import Response

from app.core import rate_limit as rl


class _FakeRedis:
    def __init__(self):
        self.store: dict = {}
        self.zsets: dict[str, dict[str, float]] = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ex=None):
        self.store[key] = value

    def delete(self, key):
        self.store.pop(key, None)
        self.zsets.pop(key, None)

    def pipeline(self):
        return _FakePipe(self)

    def zadd(self, key, mapping):
        z = self.zsets.setdefault(key, {})
        z.update(mapping)

    def zremrangebyscore(self, key, min_s, max_s):
        z = self.zsets.setdefault(key, {})
        for m, score in list(z.items()):
            if min_s <= score <= max_s:
                del z[m]

    def zcard(self, key):
        return len(self.zsets.get(key, {}))

    def expire(self, key, _ttl):
        pass


class _FakePipe:
    def __init__(self, r: _FakeRedis):
        self.r = r
        self.ops = []

    def zadd(self, key, mapping):
        self.ops.append(("zadd", key, mapping))
        return self

    def zremrangebyscore(self, key, min_s, max_s):
        self.ops.append(("zrem", key, min_s, max_s))
        return self

    def zcard(self, key):
        self.ops.append(("zcard", key))
        return self

    def expire(self, key, ttl):
        self.ops.append(("expire", key, ttl))
        return self

    def execute(self):
        results = []
        for op in self.ops:
            if op[0] == "zadd":
                self.r.zadd(op[1], op[2])
                results.append(True)
            elif op[0] == "zrem":
                self.r.zremrangebyscore(op[1], op[2], op[3])
                results.append(0)
            elif op[0] == "zcard":
                results.append(self.r.zcard(op[1]))
            elif op[0] == "expire":
                self.r.expire(op[1], op[2])
                results.append(True)
        return results


@pytest.fixture
def fake_redis(monkeypatch):
    r = _FakeRedis()
    import app.core.redis as redis_mod

    monkeypatch.setattr(redis_mod, "get_redis", lambda: r)

    def _sliding(key, window=60, limit=100):
        n = int(r.store.get(key) or 0) + 1
        r.store[key] = str(n)
        return n <= limit, n

    monkeypatch.setattr(redis_mod, "sliding_window_rate", _sliding)
    return r


def test_auth_fail_does_not_ban_when_ip_has_recent_success(fake_redis, monkeypatch):
    monkeypatch.setattr(rl, "BAN_THRESHOLD", 3)
    rl.record_auth_success("10.0.0.1")
    for _ in range(5):
        rl.record_auth_failure("10.0.0.1")
    assert not rl.is_banned("10.0.0.1")


def test_auth_fail_bans_cold_attacker_ip_at_high_threshold(fake_redis, monkeypatch):
    monkeypatch.setattr(rl, "BAN_THRESHOLD", 3)
    for _ in range(3):
        rl.record_auth_failure("203.0.113.9")
    assert rl.is_banned("203.0.113.9")


def test_unauth_ip_ceiling_is_high_for_event_nat():
    assert rl.MAX_UNAUTH_REQUESTS_PER_IP_PER_MINUTE >= 2000
    assert rl.MAX_TUNNEL_OPENS_PER_IP_PER_MINUTE >= 1000
    assert rl.BAN_THRESHOLD >= 50


def test_middleware_rates_by_bearer_token_not_only_ip(fake_redis, monkeypatch):
    monkeypatch.setattr(rl, "MAX_REQUESTS_PER_MINUTE", 2)

    async def ok(_request):
        return Response(status_code=200)

    mw = rl.RateLimitMiddleware(app=MagicMock())
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/v1/projects",
        "raw_path": b"/api/v1/projects",
        "query_string": b"",
        "headers": [
            (b"authorization", b"Bearer trk_aaaaaaaa"),
            (b"x-forwarded-for", b"198.51.100.1"),
        ],
        "client": ("198.51.100.1", 12345),
        "server": ("test", 80),
    }

    async def _run():
        request = Request(scope)
        assert (await mw.dispatch(request, ok)).status_code == 200
        assert (await mw.dispatch(request, ok)).status_code == 200
        resp = await mw.dispatch(request, ok)
        assert resp.status_code == 429

    asyncio.run(_run())


def test_auth_credential_fingerprint_stable():
    a = rl._credential_fingerprint("Bearer trk_abc")
    b = rl._credential_fingerprint("Bearer trk_abc")
    c = rl._credential_fingerprint("Bearer trk_xyz")
    assert a and a == b and a != c
    assert rl._credential_fingerprint(None) is None
    assert rl._credential_fingerprint("Basic x") is None
