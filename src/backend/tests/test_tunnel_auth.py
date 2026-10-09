"""Tunnel WebSocket auth: API keys only (no SSO headers / JWT / query token)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.tunnel import auth as tunnel_auth


def _ws(*, headers: dict | None = None, query: dict | None = None):
    ws = MagicMock()
    ws.headers = headers or {}
    ws.query_params = query or {}
    return ws


def test_rejects_x_forwarded_email_without_api_key():
    ws = _ws(headers={"x-forwarded-email": "attacker@example.com"})
    with patch.object(tunnel_auth, "SessionLocal") as sl:
        sl.return_value = MagicMock()
        result = tunnel_auth.authorize_tunnel_session(ws, "proj", "cluster")
    assert result["ok"] is False
    assert result["code"] == 4001


def test_rejects_jwt_bearer():
    ws = _ws(headers={"authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.e30.sig"})
    with patch.object(tunnel_auth, "SessionLocal") as sl:
        sl.return_value = MagicMock()
        result = tunnel_auth.authorize_tunnel_session(ws, "proj", "cluster")
    assert result["ok"] is False
    assert result["code"] == 4001


def test_rejects_query_token():
    ws = _ws(query={"token": "trk_should_not_work_in_query"})
    with patch.object(tunnel_auth, "SessionLocal") as sl:
        sl.return_value = MagicMock()
        result = tunnel_auth.authorize_tunnel_session(ws, "proj", "cluster")
    assert result["ok"] is False
    assert result["code"] == 4001


def test_rejects_anonymous():
    ws = _ws()
    with patch.object(tunnel_auth, "SessionLocal") as sl:
        sl.return_value = MagicMock()
        result = tunnel_auth.authorize_tunnel_session(ws, "proj", "cluster")
    assert result["ok"] is False
    assert result["code"] == 4001


def test_token_from_websocket_bearer_only():
    ws = _ws(
        headers={"authorization": "Bearer trk_abc"},
        query={"token": "trk_ignored"},
    )
    assert tunnel_auth._token_from_websocket(ws) == "trk_abc"
    assert tunnel_auth._token_from_websocket(_ws(query={"token": "trk_x"})) is None


def test_accepts_unscoped_api_key_owner(monkeypatch):
    user = SimpleNamespace(id="user-1")
    project = SimpleNamespace(id="p1", owner_id="user-1", host_id="h1")
    cluster = SimpleNamespace(kubeconfig_available=True)
    host = SimpleNamespace(
        id="h1",
        ip_address="10.0.0.1",
        agent_token="tok",
        agent_cert_fingerprint=None,
        provider_id=None,
    )
    monkeypatch.setattr(
        tunnel_auth, "_resolve_user", lambda token, db: user if token else None
    )

    session = MagicMock()
    session.query.return_value.filter_by.return_value.first.side_effect = [
        project,
        host,
    ]
    ws = _ws(headers={"authorization": "Bearer trk_goodkey"})
    with (
        patch.object(tunnel_auth, "SessionLocal", return_value=session),
        patch(
            "app.services.ocp.api_tunnel.get_cluster_access",
            return_value=cluster,
        ),
        patch(
            "app.services.ocp.api_tunnel.resolve_dial_targets",
            return_value=[{"via": "test"}],
        ),
    ):
        result = tunnel_auth.authorize_tunnel_session(ws, "p1", "c1")
    assert result["ok"] is True
    assert result["user_id"] == "user-1"
