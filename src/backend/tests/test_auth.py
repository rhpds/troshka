from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.core.auth import (
    _enforce_access,
    _get_user_groups,
    _parse_csv,
    _resolve_role,
    _role_for_groups,
    create_jwt,
    decode_jwt,
    hash_password,
    verify_password,
)
from app.main import app

client = TestClient(app)


def test_parse_csv_handles_empty():
    assert _parse_csv("") == set()
    assert _parse_csv(None) == set()


def test_parse_csv_handles_values():
    result = _parse_csv("Alice@Example.com, bob@test.com")
    assert result == {"alice@example.com", "bob@test.com"}


def test_allowed_users_blocks_unauthorized_sso_user():
    """When allowed_users is set, users not in the list get 403."""
    mock_config = MagicMock()
    mock_config.auth.oauth_enabled = True
    mock_config.auth.admin_users = "allowed@test.com"
    mock_config.auth.operator_users = ""
    mock_config.auth.allowed_users = "allowed@test.com"
    mock_config.auth.jwt_secret = "test-secret"
    mock_config.auth.jwt_algorithm = "HS256"
    mock_config.auth.jwt_expiry_hours = 24

    with patch("app.core.auth.config", mock_config):
        # Reimport to pick up new config
        from app.core.auth import _parse_csv

        allowed = _parse_csv("allowed@test.com")
        assert "allowed@test.com" in allowed
        assert "blocked@test.com" not in allowed


def test_allowed_users_empty_allows_all():
    """When allowed_users is empty, all authenticated users are allowed."""
    from app.core.auth import _parse_csv

    allowed = _parse_csv("")
    assert len(allowed) == 0


def test_password_hashing():
    hashed = hash_password("secret123")
    assert hashed != "secret123"
    assert verify_password("secret123", hashed)
    assert not verify_password("wrong", hashed)


def test_jwt_roundtrip():
    token = create_jwt(user_id="abc-123", email="test@example.com", role="user")
    payload = decode_jwt(token)
    assert payload["sub"] == "abc-123"
    assert payload["email"] == "test@example.com"
    assert payload["role"] == "user"


def test_jwt_invalid_token():
    payload = decode_jwt("garbage.token.here")
    assert payload is None


# --- Group support tests ---

_MOCK_GROUPS = [
    {
        "metadata": {"name": "rhpds-admins"},
        "users": ["prutledg", "admin2"],
    },
    {
        "metadata": {"name": "troshka-operators"},
        "users": ["operator1", "prutledg"],
    },
    {
        "metadata": {"name": "troshka-users"},
        "users": ["user1", "user2"],
    },
]


@patch("app.core.auth._fetch_openshift_groups", return_value=_MOCK_GROUPS)
def test_get_user_groups(mock_fetch):
    groups = _get_user_groups("prutledg")
    assert groups == {"rhpds-admins", "troshka-operators"}


@patch("app.core.auth._fetch_openshift_groups", return_value=_MOCK_GROUPS)
def test_get_user_groups_unknown_user(mock_fetch):
    groups = _get_user_groups("nobody")
    assert groups == set()


@patch("app.core.auth._fetch_openshift_groups", return_value=_MOCK_GROUPS)
def test_role_for_groups_admin(mock_fetch):
    with patch("app.core.auth._admin_groups", {"rhpds-admins"}), patch(
        "app.core.auth._operator_groups", {"troshka-operators"}
    ), patch("app.core.auth._allowed_groups", {"troshka-users"}):
        assert _role_for_groups("prutledg") == "admin"


@patch("app.core.auth._fetch_openshift_groups", return_value=_MOCK_GROUPS)
def test_role_for_groups_operator(mock_fetch):
    with patch("app.core.auth._admin_groups", set()), patch(
        "app.core.auth._operator_groups", {"troshka-operators"}
    ), patch("app.core.auth._allowed_groups", {"troshka-users"}):
        assert _role_for_groups("operator1") == "operator"


@patch("app.core.auth._fetch_openshift_groups", return_value=_MOCK_GROUPS)
def test_role_for_groups_user(mock_fetch):
    with patch("app.core.auth._admin_groups", set()), patch(
        "app.core.auth._operator_groups", set()
    ), patch("app.core.auth._allowed_groups", {"troshka-users"}):
        assert _role_for_groups("user1") == "user"


@patch("app.core.auth._fetch_openshift_groups", return_value=_MOCK_GROUPS)
def test_role_for_groups_none_when_not_in_any(mock_fetch):
    with patch("app.core.auth._admin_groups", {"rhpds-admins"}), patch(
        "app.core.auth._operator_groups", set()
    ), patch("app.core.auth._allowed_groups", set()):
        assert _role_for_groups("user1") is None


@patch("app.core.auth._fetch_openshift_groups", return_value=_MOCK_GROUPS)
def test_resolve_role_email_overrides_groups(mock_fetch):
    with patch("app.core.auth._admin_users", {"admin@test.com"}), patch(
        "app.core.auth._operator_users", set()
    ), patch("app.core.auth._admin_groups", set()), patch(
        "app.core.auth._operator_groups", {"troshka-operators"}
    ), patch(
        "app.core.auth._allowed_groups", {"troshka-users"}
    ):
        assert _resolve_role("admin@test.com", "user1") == "admin"


@patch("app.core.auth._fetch_openshift_groups", return_value=_MOCK_GROUPS)
def test_resolve_role_falls_back_to_groups(mock_fetch):
    with patch("app.core.auth._admin_users", set()), patch(
        "app.core.auth._operator_users", set()
    ), patch("app.core.auth._admin_groups", set()), patch(
        "app.core.auth._operator_groups", {"troshka-operators"}
    ), patch(
        "app.core.auth._allowed_groups", {"troshka-users"}
    ):
        assert _resolve_role("operator1@test.com", "operator1") == "operator"


@patch("app.core.auth._fetch_openshift_groups", return_value=_MOCK_GROUPS)
def test_enforce_access_group_allowed(mock_fetch):
    with patch("app.core.auth._allowed_groups", {"troshka-users"}), patch(
        "app.core.auth._admin_groups", set()
    ), patch("app.core.auth._operator_groups", set()), patch(
        "app.core.auth._allowed_users", set()
    ):
        _enforce_access("user1@test.com", "user1")


@patch("app.core.auth._fetch_openshift_groups", return_value=_MOCK_GROUPS)
def test_enforce_access_group_denied(mock_fetch):
    with patch("app.core.auth._allowed_groups", {"troshka-users"}), patch(
        "app.core.auth._admin_groups", set()
    ), patch("app.core.auth._operator_groups", set()), patch(
        "app.core.auth._allowed_users", set()
    ):
        with pytest.raises(HTTPException) as exc_info:
            _enforce_access("nobody@test.com", "nobody")
        assert exc_info.value.status_code == 403


@patch("app.core.auth._fetch_openshift_groups", return_value=_MOCK_GROUPS)
def test_enforce_access_email_fallback(mock_fetch):
    with patch("app.core.auth._allowed_groups", {"troshka-users"}), patch(
        "app.core.auth._admin_groups", set()
    ), patch("app.core.auth._operator_groups", set()), patch(
        "app.core.auth._allowed_users", {"special@test.com"}
    ):
        _enforce_access("special@test.com", "nobody")


def test_enforce_access_no_groups_configured():
    with patch("app.core.auth._allowed_groups", set()), patch(
        "app.core.auth._admin_groups", set()
    ), patch("app.core.auth._operator_groups", set()), patch(
        "app.core.auth._allowed_users", set()
    ):
        _enforce_access("anyone@test.com")
