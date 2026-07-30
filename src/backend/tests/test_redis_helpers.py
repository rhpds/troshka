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


# ── _release_stale_lock tests ──


@patch("app.core.redis.get_redis")
def test_release_stale_lock_when_redis_available(mock_get_redis):
    """When redis is available, deletes the key."""
    import app.core.redis as redis_mod
    from app.core.redis import _release_stale_lock

    original = redis_mod._redis_available
    redis_mod._redis_available = True
    try:
        mock_client = MagicMock()
        mock_get_redis.return_value = mock_client

        _release_stale_lock("lock:network:host-123")

        mock_client.delete.assert_called_once_with("lock:network:host-123")
    finally:
        redis_mod._redis_available = original


def test_release_stale_lock_when_redis_unavailable():
    """When redis is not available, does nothing (no error)."""
    import app.core.redis as redis_mod
    from app.core.redis import _release_stale_lock

    original = redis_mod._redis_available
    redis_mod._redis_available = False
    try:
        # Should not raise, and should not attempt any Redis call
        _release_stale_lock("lock:network:host-456")
    finally:
        redis_mod._redis_available = original


@patch("app.core.redis.get_redis")
def test_release_stale_lock_swallows_exception(mock_get_redis):
    """Exception during delete is swallowed silently."""
    import app.core.redis as redis_mod
    from app.core.redis import _release_stale_lock

    original = redis_mod._redis_available
    redis_mod._redis_available = True
    try:
        mock_client = MagicMock()
        mock_client.delete.side_effect = Exception("connection lost")
        mock_get_redis.return_value = mock_client

        # Should not raise
        _release_stale_lock("lock:network:host-789")
    finally:
        redis_mod._redis_available = original


# ── _on_job_success tests ──


def test_on_job_success_with_host_id():
    """With host_id in meta, calls record_deploy_end."""
    from app.core.redis import _on_job_success

    job = MagicMock()
    job.meta = {"host_id": "host-abc"}
    connection = MagicMock()

    with patch("app.services.placement.record_deploy_end") as mock_rde:
        _on_job_success(job, connection, None)
        mock_rde.assert_called_once_with("host-abc")


def test_on_job_success_with_project_id():
    """With project_id in meta, deletes the job:project key from Redis."""
    from app.core.redis import _on_job_success

    job = MagicMock()
    job.meta = {"project_id": "proj-xyz"}
    connection = MagicMock()

    _on_job_success(job, connection, None)

    connection.delete.assert_called_once_with("job:project:proj-xyz")


def test_on_job_success_with_both():
    """With both host_id and project_id, does both cleanup actions."""
    from app.core.redis import _on_job_success

    job = MagicMock()
    job.meta = {"host_id": "host-111", "project_id": "proj-222"}
    connection = MagicMock()

    with patch("app.services.placement.record_deploy_end") as mock_rde:
        _on_job_success(job, connection, None)
        mock_rde.assert_called_once_with("host-111")

    connection.delete.assert_called_once_with("job:project:proj-222")


def test_on_job_success_with_neither():
    """With empty meta, does nothing and does not crash."""
    from app.core.redis import _on_job_success

    job = MagicMock()
    job.meta = {}
    connection = MagicMock()

    _on_job_success(job, connection, None)

    connection.delete.assert_not_called()


def test_on_job_success_swallows_deploy_end_error():
    """Exception in record_deploy_end is swallowed."""
    from app.core.redis import _on_job_success

    job = MagicMock()
    job.meta = {"host_id": "host-err"}
    connection = MagicMock()

    with patch(
        "app.services.placement.record_deploy_end",
        side_effect=Exception("redis down"),
    ):
        # Should not raise
        _on_job_success(job, connection, None)


def test_on_job_success_swallows_delete_error():
    """Exception in connection.delete is swallowed."""
    from app.core.redis import _on_job_success

    job = MagicMock()
    job.meta = {"project_id": "proj-err"}
    connection = MagicMock()
    connection.delete.side_effect = Exception("redis error")

    # Should not raise
    _on_job_success(job, connection, None)


# ── _on_job_failure tests ──


@patch("app.core.redis._set_project_error_state")
@patch("app.core.redis.delete_progress")
@patch("app.core.redis._cleanup_host_locks")
def test_on_job_failure_non_abandoned(mock_cleanup, mock_del_progress, mock_set_error):
    """Non-abandoned failure cleans up progress, deletes job key, sets error state."""
    from app.core.redis import _on_job_failure

    job = MagicMock()
    job.id = "job-12345678"
    job.meta = {"project_id": "proj-fail", "host_id": "host-fail"}
    connection = MagicMock()

    _on_job_failure(job, connection, ValueError, ValueError("disk full"), None)

    mock_cleanup.assert_called_once_with("host-fail", "proj-fail")
    mock_del_progress.assert_called_once_with("deploy:proj-fail")
    connection.delete.assert_called_once_with("job:project:proj-fail")
    mock_set_error.assert_called_once()
    assert mock_set_error.call_args[0][0] == "proj-fail"


@patch("app.core.redis._set_project_error_state")
@patch("app.core.redis.delete_progress")
@patch("app.core.redis._cleanup_host_locks")
def test_on_job_failure_abandoned(mock_cleanup, mock_del_progress, mock_set_error):
    """Abandoned job (AbandonedJobError) leaves project in transient state for recovery."""
    from app.core.redis import _on_job_failure

    # Create a fake AbandonedJobError type
    class AbandonedJobError(Exception):
        pass

    job = MagicMock()
    job.id = "job-abcd1234"
    job.meta = {"project_id": "proj-abandon", "host_id": "host-ab"}
    connection = MagicMock()

    _on_job_failure(
        job, connection, AbandonedJobError, AbandonedJobError("abandoned"), None
    )

    mock_cleanup.assert_called_once_with("host-ab", "proj-abandon")
    # Abandoned jobs should NOT delete progress or set error state
    mock_del_progress.assert_not_called()
    mock_set_error.assert_not_called()
    connection.delete.assert_not_called()


@patch("app.core.redis._set_project_error_state")
@patch("app.core.redis.delete_progress")
@patch("app.core.redis._cleanup_host_locks")
def test_on_job_failure_no_project_id(mock_cleanup, mock_del_progress, mock_set_error):
    """Without project_id, cleans up host locks but skips project-level cleanup."""
    from app.core.redis import _on_job_failure

    job = MagicMock()
    job.id = "job-noproj00"
    job.meta = {"host_id": "host-only"}
    connection = MagicMock()

    _on_job_failure(job, connection, RuntimeError, RuntimeError("oops"), None)

    mock_cleanup.assert_called_once_with("host-only", None)
    # No project_id means early return — no progress/error cleanup
    mock_del_progress.assert_not_called()
    mock_set_error.assert_not_called()


# ── close_redis tests ──


def test_close_redis_with_active_client():
    """Closes client and resets state."""
    import app.core.redis as redis_mod
    from app.core.redis import close_redis

    mock_client = MagicMock()
    original_client = redis_mod._client
    original_avail = redis_mod._redis_available
    redis_mod._client = mock_client
    redis_mod._redis_available = True
    try:
        close_redis()

        mock_client.close.assert_called_once()
        assert redis_mod._client is None
        assert redis_mod._redis_available is False
    finally:
        redis_mod._client = original_client
        redis_mod._redis_available = original_avail


def test_close_redis_with_no_client():
    """When no client exists, does nothing and does not crash."""
    import app.core.redis as redis_mod
    from app.core.redis import close_redis

    original_client = redis_mod._client
    original_avail = redis_mod._redis_available
    redis_mod._client = None
    redis_mod._redis_available = False
    try:
        close_redis()

        assert redis_mod._client is None
        assert redis_mod._redis_available is False
    finally:
        redis_mod._client = original_client
        redis_mod._redis_available = original_avail


def test_close_redis_swallows_close_error():
    """Exception during client.close() is swallowed."""
    import app.core.redis as redis_mod
    from app.core.redis import close_redis

    mock_client = MagicMock()
    mock_client.close.side_effect = Exception("already closed")
    original_client = redis_mod._client
    original_avail = redis_mod._redis_available
    redis_mod._client = mock_client
    redis_mod._redis_available = True
    try:
        close_redis()

        assert redis_mod._client is None
        assert redis_mod._redis_available is False
    finally:
        redis_mod._client = original_client
        redis_mod._redis_available = original_avail


# ── is_redis_available tests ──


def test_is_redis_available_when_already_connected():
    """Returns True when client exists and redis is available."""
    import app.core.redis as redis_mod
    from app.core.redis import is_redis_available

    original_client = redis_mod._client
    original_avail = redis_mod._redis_available
    redis_mod._client = MagicMock()  # pretend client exists
    redis_mod._redis_available = True
    try:
        assert is_redis_available() is True
    finally:
        redis_mod._client = original_client
        redis_mod._redis_available = original_avail


def test_is_redis_available_when_not_connected():
    """Returns False when client exists but redis is not available."""
    import app.core.redis as redis_mod
    from app.core.redis import is_redis_available

    original_client = redis_mod._client
    original_avail = redis_mod._redis_available
    redis_mod._client = MagicMock()  # pretend client exists
    redis_mod._redis_available = False
    try:
        assert is_redis_available() is False
    finally:
        redis_mod._client = original_client
        redis_mod._redis_available = original_avail


def test_is_redis_available_initializes_when_no_client():
    """When no client yet, calls get_redis() to initialize."""
    import app.core.redis as redis_mod
    from app.core.redis import is_redis_available

    original_client = redis_mod._client
    original_avail = redis_mod._redis_available
    redis_mod._client = None
    redis_mod._redis_available = False
    try:
        with patch("app.core.redis.get_redis") as mock_get:
            # Simulate get_redis setting _redis_available to False (no redis in test)
            result = is_redis_available()
            mock_get.assert_called_once()
            assert result is False
    finally:
        redis_mod._client = original_client
        redis_mod._redis_available = original_avail


def test_is_redis_available_handles_get_redis_exception():
    """When get_redis() raises, returns False without crashing."""
    import app.core.redis as redis_mod
    from app.core.redis import is_redis_available

    original_client = redis_mod._client
    original_avail = redis_mod._redis_available
    redis_mod._client = None
    redis_mod._redis_available = False
    try:
        with patch("app.core.redis.get_redis", side_effect=Exception("boom")):
            result = is_redis_available()
            assert result is False
    finally:
        redis_mod._client = original_client
        redis_mod._redis_available = original_avail
