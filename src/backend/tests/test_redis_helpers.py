"""Tests for extracted helper functions in app.core.redis."""

import uuid
from unittest.mock import MagicMock, patch

from app.core.auth import hash_password
from app.models.project import Project
from app.models.user import User
from tests.conftest import TestSession

# ── Test fixtures ──

_db = TestSession()
_user = User(
    email="redis-helpers-test@example.com",
    display_name="Redis Helpers Test",
    role="user",
    auth_source="local",
    password_hash=hash_password("pass"),
)
_db.add(_user)
_db.commit()
_db.refresh(_user)
USER_ID = _user.id
_db.close()


def _create_project(name, **kwargs):
    db = TestSession()
    p = Project(name=name, owner_id=USER_ID, **kwargs)
    db.add(p)
    db.commit()
    db.refresh(p)
    pid = p.id
    db.close()
    return pid


def _cleanup_project(pid):
    db = TestSession()
    db.query(Project).filter_by(id=pid).delete()
    db.commit()
    db.close()


# ── _cleanup_host_locks tests ──


@patch("app.core.redis._release_stale_lock")
@patch("app.core.redis.record_deploy_end", create=True)
def test_cleanup_host_locks_with_host_id(mock_record_end, mock_release):
    """With host_id provided, releases deploy tracking and network lock."""
    from app.core.redis import _cleanup_host_locks

    host_id = str(uuid.uuid4())

    with patch("app.services.placement.record_deploy_end") as mock_rde:
        _cleanup_host_locks(host_id, None)
        mock_rde.assert_called_once_with(host_id)

    mock_release.assert_called_once_with(f"lock:network:{host_id}")


@patch("app.core.redis._release_stale_lock")
def test_cleanup_host_locks_with_project_id_looks_up_host(mock_release):
    """With project_id only, looks up host_id from DB and releases network lock."""
    from app.core.redis import _cleanup_host_locks

    host_id = str(uuid.uuid4())
    pid = _create_project("cleanup-proj-test", host_id=host_id)

    try:
        with patch("app.core.database.SessionLocal", TestSession):
            _cleanup_host_locks(None, pid)

        mock_release.assert_called_once_with(f"lock:network:{host_id}")
    finally:
        _cleanup_project(pid)


@patch("app.core.redis._release_stale_lock")
def test_cleanup_host_locks_with_both(mock_release):
    """With both host_id and project_id, cleans up using host_id directly."""
    from app.core.redis import _cleanup_host_locks

    host_id = str(uuid.uuid4())
    project_id = str(uuid.uuid4())

    with patch("app.services.placement.record_deploy_end"):
        _cleanup_host_locks(host_id, project_id)

    # Only host_id path runs (the function skips project lookup when host_id is present)
    mock_release.assert_called_once_with(f"lock:network:{host_id}")


@patch("app.core.redis._release_stale_lock")
def test_cleanup_host_locks_with_neither(mock_release):
    """With neither host_id nor project_id, does nothing."""
    from app.core.redis import _cleanup_host_locks

    _cleanup_host_locks(None, None)

    mock_release.assert_not_called()


@patch("app.core.redis._release_stale_lock")
def test_cleanup_host_locks_project_no_host(mock_release):
    """Project exists but has no host_id — no lock released."""
    from app.core.redis import _cleanup_host_locks

    pid = _create_project("cleanup-no-host", host_id=None)

    try:
        with patch("app.core.database.SessionLocal", TestSession):
            _cleanup_host_locks(None, pid)

        mock_release.assert_not_called()
    finally:
        _cleanup_project(pid)


# ── _set_project_error_state tests ──


def test_set_project_error_state_deploying():
    """Project in 'deploying' state transitions to 'error' with message."""
    from app.core.redis import _set_project_error_state

    pid = _create_project("error-state-test", state="deploying")

    try:
        with patch("app.core.database.SessionLocal", TestSession):
            _set_project_error_state(pid, ValueError("disk full"))

        db = TestSession()
        p = db.get(Project, pid)
        assert p.state == "error"
        assert "disk full" in p.deploy_error
        assert "Please retry" in p.deploy_error
        db.close()
    finally:
        _cleanup_project(pid)


def test_set_project_error_state_stopping():
    """Project in 'stopping' state also transitions to 'error'."""
    from app.core.redis import _set_project_error_state

    pid = _create_project("error-stopping-test", state="stopping")

    try:
        with patch("app.core.database.SessionLocal", TestSession):
            _set_project_error_state(pid, RuntimeError("timeout"))

        db = TestSession()
        p = db.get(Project, pid)
        assert p.state == "error"
        assert "timeout" in p.deploy_error
        db.close()
    finally:
        _cleanup_project(pid)


def test_set_project_error_state_active_unchanged():
    """Project in 'active' state is NOT changed (only transitional states are)."""
    from app.core.redis import _set_project_error_state

    pid = _create_project("error-active-test", state="active")

    try:
        with patch("app.core.database.SessionLocal", TestSession):
            _set_project_error_state(pid, ValueError("oops"))

        db = TestSession()
        p = db.get(Project, pid)
        assert p.state == "active"
        db.close()
    finally:
        _cleanup_project(pid)


def test_set_project_error_state_not_found():
    """Non-existent project_id does not crash."""
    from app.core.redis import _set_project_error_state

    fake_id = str(uuid.uuid4())

    with patch("app.core.database.SessionLocal", TestSession):
        # Should not raise
        _set_project_error_state(fake_id, ValueError("gone"))


def test_set_project_error_state_none_exc():
    """exc_value=None produces a generic error message."""
    from app.core.redis import _set_project_error_state

    pid = _create_project("error-none-exc-test", state="deploying")

    try:
        with patch("app.core.database.SessionLocal", TestSession):
            _set_project_error_state(pid, None)

        db = TestSession()
        p = db.get(Project, pid)
        assert p.state == "error"
        assert p.deploy_error == "Job failed unexpectedly. Please retry."
        db.close()
    finally:
        _cleanup_project(pid)


def test_set_project_error_state_starting():
    """Project in 'starting' state transitions to 'error'."""
    from app.core.redis import _set_project_error_state

    pid = _create_project("error-starting-test", state="starting")

    try:
        with patch("app.core.database.SessionLocal", TestSession):
            _set_project_error_state(pid, Exception("boot failed"))

        db = TestSession()
        p = db.get(Project, pid)
        assert p.state == "error"
        assert "boot failed" in p.deploy_error
        db.close()
    finally:
        _cleanup_project(pid)


def test_set_project_error_state_deleting():
    """Project in 'deleting' state transitions to 'error'."""
    from app.core.redis import _set_project_error_state

    pid = _create_project("error-deleting-test", state="deleting")

    try:
        with patch("app.core.database.SessionLocal", TestSession):
            _set_project_error_state(pid, Exception("destroy failed"))

        db = TestSession()
        p = db.get(Project, pid)
        assert p.state == "error"
        assert "destroy failed" in p.deploy_error
        db.close()
    finally:
        _cleanup_project(pid)


# ── _collect_pubsub_callbacks tests ──


def test_collect_pubsub_callbacks_exact_match():
    """Exact channel match returns registered callbacks."""
    from app.core.redis import (
        _collect_pubsub_callbacks,
        _pubsub_callbacks,
        _pubsub_lock,
    )

    cb1 = MagicMock()
    cb2 = MagicMock()

    with _pubsub_lock:
        original = dict(_pubsub_callbacks)
        _pubsub_callbacks["project:abc123"] = [cb1, cb2]

    try:
        result = _collect_pubsub_callbacks("project:abc123")
        assert cb1 in result
        assert cb2 in result
        assert len(result) == 2
    finally:
        with _pubsub_lock:
            _pubsub_callbacks.clear()
            _pubsub_callbacks.update(original)


def test_collect_pubsub_callbacks_wildcard_match():
    """Wildcard pattern 'project:*' matches 'project:xyz'."""
    from app.core.redis import (
        _collect_pubsub_callbacks,
        _pubsub_callbacks,
        _pubsub_lock,
    )

    cb_wild = MagicMock()
    cb_exact = MagicMock()

    with _pubsub_lock:
        original = dict(_pubsub_callbacks)
        _pubsub_callbacks["project:*"] = [cb_wild]
        _pubsub_callbacks["project:xyz"] = [cb_exact]

    try:
        result = _collect_pubsub_callbacks("project:xyz")
        assert cb_exact in result
        assert cb_wild in result
        assert len(result) == 2
    finally:
        with _pubsub_lock:
            _pubsub_callbacks.clear()
            _pubsub_callbacks.update(original)


def test_collect_pubsub_callbacks_no_match():
    """Channel with no subscriptions returns empty list."""
    from app.core.redis import (
        _collect_pubsub_callbacks,
        _pubsub_callbacks,
        _pubsub_lock,
    )

    with _pubsub_lock:
        original = dict(_pubsub_callbacks)
        _pubsub_callbacks.clear()

    try:
        result = _collect_pubsub_callbacks("nonexistent:channel")
        assert result == []
    finally:
        with _pubsub_lock:
            _pubsub_callbacks.clear()
            _pubsub_callbacks.update(original)


def test_collect_pubsub_callbacks_wildcard_no_match():
    """Wildcard 'pattern:*' does NOT match 'project:abc'."""
    from app.core.redis import (
        _collect_pubsub_callbacks,
        _pubsub_callbacks,
        _pubsub_lock,
    )

    cb = MagicMock()

    with _pubsub_lock:
        original = dict(_pubsub_callbacks)
        _pubsub_callbacks.clear()
        _pubsub_callbacks["pattern:*"] = [cb]

    try:
        result = _collect_pubsub_callbacks("project:abc")
        assert cb not in result
        assert result == []
    finally:
        with _pubsub_lock:
            _pubsub_callbacks.clear()
            _pubsub_callbacks.update(original)


def test_collect_pubsub_callbacks_returns_copy():
    """Returned list is a copy, not a reference to the internal list."""
    from app.core.redis import (
        _collect_pubsub_callbacks,
        _pubsub_callbacks,
        _pubsub_lock,
    )

    cb = MagicMock()

    with _pubsub_lock:
        original = dict(_pubsub_callbacks)
        _pubsub_callbacks["project:copy-test"] = [cb]

    try:
        result = _collect_pubsub_callbacks("project:copy-test")
        assert result == [cb]
        # Mutating the result should not affect internals
        result.append(MagicMock())
        with _pubsub_lock:
            assert len(_pubsub_callbacks["project:copy-test"]) == 1
    finally:
        with _pubsub_lock:
            _pubsub_callbacks.clear()
            _pubsub_callbacks.update(original)
