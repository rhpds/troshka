"""Tests for project-scoped, limited-permission API keys.

Covers the ApiKey.is_scoped property and ApiKey.has_scope() helper that back
the per-project least-privilege ops-pod key (Plan 4, Task 1).
"""

import uuid

from app.models.api_key import ApiKey, generate_api_key, hash_key


def _make_key(project_id=None, scopes=None):
    key = generate_api_key()
    return ApiKey(
        id=str(uuid.uuid4()),
        user_id=str(uuid.uuid4()),
        name="test-key",
        key_hash=hash_key(key),
        key_prefix=key[:10],
        project_id=project_id,
        scopes=scopes,
    )


def test_unscoped_key_is_full_access():
    """A key with no project_id behaves like today's full-access user key."""
    key = _make_key(project_id=None, scopes=None)
    assert key.is_scoped is False
    # Full access: has_scope returns True for anything.
    assert key.has_scope("topology:read") is True
    assert key.has_scope("vm:exec") is True
    assert key.has_scope("anything:at:all") is True


def test_scoped_key_grants_only_listed_permissions():
    """A project-scoped key only grants the permissions in its scopes list."""
    key = _make_key(project_id="p1", scopes=["topology:read"])
    assert key.is_scoped is True
    assert key.has_scope("topology:read") is True
    assert key.has_scope("vm:exec") is False


def test_scoped_key_with_empty_scopes_grants_nothing():
    """A scoped key with an empty scopes list grants no permissions."""
    key = _make_key(project_id="p1", scopes=[])
    assert key.is_scoped is True
    assert key.has_scope("topology:read") is False


def test_scoped_key_with_none_scopes_grants_nothing():
    """A scoped key (project set) with scopes=None grants no permissions."""
    key = _make_key(project_id="p1", scopes=None)
    assert key.is_scoped is True
    assert key.has_scope("topology:read") is False
