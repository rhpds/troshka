"""MultiplexSession lifecycle — idle parked sessions must stay open."""

from __future__ import annotations

import asyncio

from app.tunnel.session import MultiplexSession


class _FakeWebSocket:
    """Minimal ASGI-ish websocket: scripted receives + recorded sends."""

    def __init__(self, receives: list):
        self._receives = list(receives)
        self.sent: list[dict] = []
        self.closed = False

    async def receive(self) -> dict:
        if not self._receives:
            await asyncio.sleep(3600)
            return {"type": "websocket.disconnect"}
        item = self._receives.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def send_json(self, data: dict) -> None:
        if self.closed:
            raise RuntimeError("closed")
        self.sent.append(data)

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append({"bytes": data})

    async def close(self) -> None:
        self.closed = True


def test_idle_without_streams_keeps_session_and_pings(monkeypatch):
    """Former bug: empty _streams + STREAM_IDLE timeout tore down the session,
    killing parked multi-cluster troshka-oc contexts after ~60s."""
    monkeypatch.setattr("app.tunnel.session.STREAM_IDLE_S", 0.05)

    ws = _FakeWebSocket(
        [
            TimeoutError(),
            TimeoutError(),
            {"type": "websocket.disconnect"},
        ]
    )
    session = MultiplexSession(ws, project_id="p", host=None, provider=None, targets=[])

    asyncio.run(asyncio.wait_for(session._loop(), timeout=2.0))

    pings = [m for m in ws.sent if m.get("type") == "ping"]
    assert len(pings) >= 2


def test_client_ping_gets_pong():
    ws = _FakeWebSocket(
        [
            {"text": '{"type":"ping"}'},
            {"type": "websocket.disconnect"},
        ]
    )
    session = MultiplexSession(ws, project_id="p", host=None, provider=None, targets=[])
    asyncio.run(asyncio.wait_for(session._loop(), timeout=2.0))
    assert {"type": "pong"} in ws.sent
