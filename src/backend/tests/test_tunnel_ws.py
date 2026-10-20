"""Tunnel failures must not copy client-controlled data into log records."""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

from app.tunnel import ws


def test_session_error_does_not_log_injected_lines(monkeypatch, caplog):
    socket = AsyncMock()
    socket.headers = {}
    monkeypatch.setattr(ws, "is_banned", lambda ip: False)
    monkeypatch.setattr(ws, "check_tunnel_open_rate", lambda ip: True)
    monkeypatch.setattr(ws, "record_auth_success", lambda ip: None)
    monkeypatch.setattr(
        ws,
        "authorize_tunnel_session",
        lambda *args: {
            "ok": True,
            "user_id": "u",
            "project_id": "p",
            "host": None,
            "provider": None,
            "targets": [],
        },
    )
    registry = MagicMock()
    registry.try_acquire.return_value = None
    monkeypatch.setattr(ws, "REGISTRY", registry)
    session = MagicMock()
    session.run = AsyncMock(side_effect=RuntimeError("untrusted\nFORGED LOG"))
    monkeypatch.setattr(ws, "MultiplexSession", MagicMock(return_value=session))
    with caplog.at_level(logging.INFO, logger=ws.__name__):
        asyncio.run(ws.cluster_api_tunnel(socket, "p\nFORGED", "c\nFORGED"))
    assert [record.getMessage() for record in caplog.records] == [
        "tunnel session failed (RuntimeError)"
    ]
    registry.release.assert_called_once_with("u", "p")
    socket.close.assert_awaited_once()
