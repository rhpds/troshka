import datetime
from unittest.mock import MagicMock, patch

from app.core.auth import (
    _parse_csv,
    create_jwt,
    decode_jwt,
    hash_password,
    role_for_email,
    verify_password,
)
from app.services.access_window import (
    access_window_denial_message,
    user_within_access_window,
)


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


def test_role_for_email_uses_config_lists():
    with patch("app.core.auth._admin_users", {"admin@test.com"}), patch(
        "app.core.auth._operator_users", {"op@test.com"}
    ):
        assert role_for_email("admin@test.com") == "admin"
        assert role_for_email("op@test.com") == "operator"
        assert role_for_email("user@test.com") == "user"


def test_access_window_unbounded_allows():
    user = MagicMock(access_starts_at=None, access_ends_at=None)
    assert user_within_access_window(user)


def test_access_window_before_start_denies():
    now = datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC)
    user = MagicMock(
        access_starts_at=datetime.datetime(2026, 7, 1, tzinfo=datetime.UTC),
        access_ends_at=None,
    )
    assert not user_within_access_window(user, now=now)
    assert "not started" in access_window_denial_message(user, now=now).lower()


def test_access_window_after_end_denies():
    now = datetime.datetime(2026, 8, 1, tzinfo=datetime.UTC)
    user = MagicMock(
        access_starts_at=None,
        access_ends_at=datetime.datetime(2026, 7, 1, tzinfo=datetime.UTC),
    )
    assert not user_within_access_window(user, now=now)
    assert "expired" in access_window_denial_message(user, now=now).lower()


def test_access_window_inside_range_allows():
    now = datetime.datetime(2026, 6, 15, tzinfo=datetime.UTC)
    user = MagicMock(
        access_starts_at=datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC),
        access_ends_at=datetime.datetime(2026, 7, 1, tzinfo=datetime.UTC),
    )
    assert user_within_access_window(user, now=now)
