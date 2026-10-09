"""MultiplexSession lifecycle — idle empty sessions release the dial/PF."""

from __future__ import annotations

import asyncio

from app.tunnel.session import MultiplexSession


class _FakeWebSocket:
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
        self.sent.append(data)

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append({"bytes": data})

    async def close(self) -> None:
        self.closed = True


def test_idle_without_streams_closes_session(monkeypatch):
    """Empty-stream idle should end the session so PF resources are freed;
    troshka-oc reconnects on the next local oc."""
    monkeypatch.setattr("app.tunnel.session.STREAM_IDLE_S", 0.05)

    ws = _FakeWebSocket([TimeoutError()])
    session = MultiplexSession(ws, project_id="p", host=None, provider=None, targets=[])

    asyncio.run(asyncio.wait_for(session._loop(), timeout=2.0))
    # Loop exited on idle (did not hang waiting for disconnect).
    assert session._streams == {}
