"""Tests for startup helpers and admin endpoints in app.main."""

import json
import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.core.auth import create_jwt, hash_password
from app.core.config import config
from app.core.database import get_db
from app.main import app
from app.models.host import Host
from app.models.project import Project
from app.models.user import User
from tests.conftest import TestSession, get_test_db

app.dependency_overrides[get_db] = get_test_db
client = TestClient(app)

# Actual JWT secret from config — needed for mock configs in 403 tests
_JWT_SECRET = config.auth.jwt_secret
_JWT_ALGORITHM = config.auth.jwt_algorithm

# ---------------------------------------------------------------------------
# Admin user + JWT for protected endpoints
# ---------------------------------------------------------------------------
_db = TestSession()
_user = User(
    email="admin-main-helpers@test.com",
    role="admin",
    auth_source="local",
    password_hash=hash_password("pass"),
)
_db.add(_user)
_db.commit()
_db.refresh(_user)
OWNER_ID = _user.id
TOKEN = create_jwt(user_id=_user.id, email=_user.email, role=_user.role)
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

# Non-admin user for 403 tests
_regular = User(
    email="regular-main-helpers@test.com",
    role="user",
    auth_source="local",
    password_hash=hash_password("pass"),
)
_db.add(_regular)
_db.commit()
_db.refresh(_regular)
REGULAR_TOKEN = create_jwt(
    user_id=_regular.id, email=_regular.email, role=_regular.role
)
REGULAR_HEADERS = {"Authorization": f"Bearer {REGULAR_TOKEN}"}
_db.close()


def _mock_oauth_config():
    """Return a patched config that enables OAuth mode but keeps the real JWT secret."""
    mock_cfg = MagicMock()
    mock_cfg.auth.oauth_enabled = True
    mock_cfg.auth.jwt_secret = _JWT_SECRET
    mock_cfg.auth.jwt_algorithm = _JWT_ALGORITHM
    mock_cfg.auth.jwt_expiry_hours = 24
    mock_cfg.auth.admin_users = ""
    mock_cfg.auth.operator_users = ""
    mock_cfg.auth.allowed_users = ""
    mock_cfg.auth.allowed_groups = ""
    mock_cfg.auth.admin_groups = ""
    mock_cfg.auth.operator_groups = ""
    return mock_cfg


# ---------------------------------------------------------------------------
# 1. _startup_reset_stuck_projects
# ---------------------------------------------------------------------------
class TestStartupResetStuckProjects:
    def test_deploying_project_resets_to_error(self):
        """A project stuck in 'deploying' (no deploy_step) is reset to 'error'."""
        db = TestSession()
        proj = Project(
            name="stuck-deploying",
            owner_id=OWNER_ID,
            state="deploying",
            deploy_step=None,
        )
        db.add(proj)
        db.commit()
        pid = proj.id
        db.close()

        with patch("app.core.database.SessionLocal", TestSession):
            with patch("app.core.redis.is_redis_available", return_value=False):
                from app.main import _startup_reset_stuck_projects

                _startup_reset_stuck_projects()

        # Re-query with a fresh session since the function closed its own
        db2 = TestSession()
        proj = db2.get(Project, pid)
        assert proj.state == "error"
        assert "deploying" in (proj.deploy_error or "")

        db2.delete(proj)
        db2.commit()
        db2.close()

    def test_stopping_project_resets_to_error(self):
        """A project stuck in 'stopping' is reset to 'error'."""
        db = TestSession()
        proj = Project(
            name="stuck-stopping",
            owner_id=OWNER_ID,
            state="stopping",
        )
        db.add(proj)
        db.commit()
        pid = proj.id
        db.close()

        with patch("app.core.database.SessionLocal", TestSession):
            with patch("app.core.redis.is_redis_available", return_value=False):
                from app.main import _startup_reset_stuck_projects

                _startup_reset_stuck_projects()

        db2 = TestSession()
        proj = db2.get(Project, pid)
        assert proj.state == "error"
        assert "stopping" in (proj.deploy_error or "")

        db2.delete(proj)
        db2.commit()
        db2.close()

    def test_draft_project_not_touched(self):
        """A project in 'draft' state is NOT reset."""
        db = TestSession()
        proj = Project(
            name="draft-project",
            owner_id=OWNER_ID,
            state="draft",
        )
        db.add(proj)
        db.commit()
        pid = proj.id
        db.close()

        with patch("app.core.database.SessionLocal", TestSession):
            with patch("app.core.redis.is_redis_available", return_value=False):
                from app.main import _startup_reset_stuck_projects

                _startup_reset_stuck_projects()

        db2 = TestSession()
        proj = db2.get(Project, pid)
        assert proj.state == "draft"

        db2.delete(proj)
        db2.commit()
        db2.close()

    def test_deploying_with_deploy_step_resumes(self):
        """A project in 'deploying' with deploy_step is resumed, not reset."""
        db = TestSession()
        db.query(Project).filter(
            Project.state.in_(("deploying", "stopping", "starting")),
            Project.owner_id != OWNER_ID,
        ).delete(synchronize_session="fetch")
        db.commit()
        db.close()

        db = TestSession()
        proj = Project(
            name="deploying-with-step",
            owner_id=OWNER_ID,
            state="deploying",
            deploy_step="create_vms",
        )
        db.add(proj)
        db.commit()
        pid = proj.id
        db.close()

        mock_enqueue = MagicMock()
        with patch("app.core.database.SessionLocal", TestSession):
            with patch("app.core.redis.enqueue_job", mock_enqueue):
                from app.main import _startup_reset_stuck_projects

                _startup_reset_stuck_projects()

        # Should enqueue a resume, not reset to error
        mock_enqueue.assert_called_once()
        call_args = mock_enqueue.call_args
        assert call_args[0][1] == pid  # project id
        assert call_args[1]["resume_from"] == "create_vms"

        # State should still be deploying (the job will handle it)
        db2 = TestSession()
        proj = db2.get(Project, pid)
        assert proj.state == "deploying"

        db2.delete(proj)
        db2.commit()
        db2.close()


# ---------------------------------------------------------------------------
# 2. _startup_reset_stuck_hosts
# ---------------------------------------------------------------------------
class TestStartupResetStuckHosts:
    def test_installing_host_resets_to_disconnected(self):
        """A host stuck in 'installing' agent_status is reset to 'disconnected'."""
        db = TestSession()
        host = Host(
            id=str(uuid.uuid4()),
            state="active",
            agent_status="installing",
            host_type="shared",
        )
        db.add(host)
        db.commit()
        hid = host.id
        db.close()

        with patch("app.core.database.SessionLocal", TestSession):
            from app.main import _startup_reset_stuck_hosts

            _startup_reset_stuck_hosts()

        db2 = TestSession()
        host = db2.get(Host, hid)
        assert host.agent_status == "disconnected"

        db2.delete(host)
        db2.commit()
        db2.close()

    def test_waiting_ssh_host_resets(self):
        """A host stuck in 'waiting_ssh' is reset to 'disconnected'."""
        db = TestSession()
        host = Host(
            id=str(uuid.uuid4()),
            state="active",
            agent_status="waiting_ssh",
            host_type="shared",
        )
        db.add(host)
        db.commit()
        hid = host.id
        db.close()

        with patch("app.core.database.SessionLocal", TestSession):
            from app.main import _startup_reset_stuck_hosts

            _startup_reset_stuck_hosts()

        db2 = TestSession()
        host = db2.get(Host, hid)
        assert host.agent_status == "disconnected"

        db2.delete(host)
        db2.commit()
        db2.close()

    def test_install_failed_host_resets(self):
        """A host stuck in 'install_failed' is reset to 'disconnected'."""
        db = TestSession()
        host = Host(
            id=str(uuid.uuid4()),
            state="active",
            agent_status="install_failed",
            host_type="shared",
        )
        db.add(host)
        db.commit()
        hid = host.id
        db.close()

        with patch("app.core.database.SessionLocal", TestSession):
            from app.main import _startup_reset_stuck_hosts

            _startup_reset_stuck_hosts()

        db2 = TestSession()
        host = db2.get(Host, hid)
        assert host.agent_status == "disconnected"

        db2.delete(host)
        db2.commit()
        db2.close()

    def test_connected_host_not_touched(self):
        """A host with 'connected' agent_status is NOT reset."""
        db = TestSession()
        host = Host(
            id=str(uuid.uuid4()),
            state="active",
            agent_status="connected",
            host_type="shared",
        )
        db.add(host)
        db.commit()
        hid = host.id
        db.close()

        with patch("app.core.database.SessionLocal", TestSession):
            from app.main import _startup_reset_stuck_hosts

            _startup_reset_stuck_hosts()

        db2 = TestSession()
        host = db2.get(Host, hid)
        assert host.agent_status == "connected"

        db2.delete(host)
        db2.commit()
        db2.close()


# ---------------------------------------------------------------------------
# 3. _startup_clear_health_monitors
# ---------------------------------------------------------------------------
class TestStartupClearHealthMonitors:
    def test_clears_redis_key_when_available(self):
        """When Redis is available, the health monitor set key is deleted."""
        mock_redis = MagicMock()

        with patch("app.core.redis.is_redis_available", return_value=True):
            with patch("app.core.redis.get_redis", return_value=mock_redis):
                from app.main import _startup_clear_health_monitors

                _startup_clear_health_monitors()

        mock_redis.delete.assert_called_once_with("deploy:health_monitors")

    def test_skips_when_redis_unavailable(self):
        """When Redis is unavailable, the function returns without error."""
        with patch("app.core.redis.is_redis_available", return_value=False):
            from app.main import _startup_clear_health_monitors

            # Should not raise
            _startup_clear_health_monitors()

    def test_swallows_redis_exception(self):
        """If Redis raises an exception, it is silently swallowed."""
        mock_redis = MagicMock()
        mock_redis.delete.side_effect = Exception("connection refused")

        with patch("app.core.redis.is_redis_available", return_value=True):
            with patch("app.core.redis.get_redis", return_value=mock_redis):
                from app.main import _startup_clear_health_monitors

                # Should not raise
                _startup_clear_health_monitors()


# ---------------------------------------------------------------------------
# 4. Admin endpoints via TestClient
# ---------------------------------------------------------------------------
class TestAdminQueueStatus:
    def test_queue_status_returns_200_for_admin(self):
        """Admin user can access queue-status endpoint."""
        resp = client.get("/api/v1/admin/queue-status", headers=HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        # When Redis is unavailable, returns redis=False message
        assert "redis" in data

    def test_queue_status_returns_403_for_regular_user(self):
        """Non-admin user gets 403 on admin endpoint."""
        with patch("app.core.auth.config", _mock_oauth_config()):
            resp = client.get("/api/v1/admin/queue-status", headers=REGULAR_HEADERS)
            assert resp.status_code == 403

    def test_queue_status_returns_401_without_auth(self):
        """No auth headers + oauth mode -> 401."""
        with patch("app.core.auth.config", _mock_oauth_config()):
            resp = client.get("/api/v1/admin/queue-status")
            assert resp.status_code == 401


class TestDebugThreads:
    def test_debug_threads_returns_200(self):
        """Admin user can access debug/threads endpoint."""
        resp = client.get("/api/v1/debug/threads", headers=HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert "count" in data
        assert "threads" in data
        assert isinstance(data["threads"], list)
        assert data["count"] > 0

    def test_debug_threads_thread_structure(self):
        """Each thread entry has expected fields."""
        resp = client.get("/api/v1/debug/threads", headers=HEADERS)
        data = resp.json()
        for t in data["threads"]:
            assert "name" in t
            assert "daemon" in t
            assert "alive" in t

    def test_debug_threads_403_for_regular_user(self):
        """Non-admin user gets 403 on debug/threads."""
        with patch("app.core.auth.config", _mock_oauth_config()):
            resp = client.get("/api/v1/debug/threads", headers=REGULAR_HEADERS)
            assert resp.status_code == 403


class TestHealthCheck:
    def test_health_check_no_auth_required(self):
        """Health endpoint is accessible without authentication."""
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"


# ---------------------------------------------------------------------------
# 5. _is_stale_abandoned_job
# ---------------------------------------------------------------------------
class TestIsStaleAbandonedJob:
    def test_stale_job_returns_true(self):
        """A job that ended more than 1 hour ago is stale."""
        from datetime import UTC, datetime, timedelta

        from app.main import _is_stale_abandoned_job

        j = MagicMock()
        now = datetime.now(UTC)
        j.ended_at = now - timedelta(hours=2)
        assert _is_stale_abandoned_job(j, now) is True

    def test_recent_job_returns_false(self):
        """A job that ended less than 1 hour ago is NOT stale."""
        from datetime import UTC, datetime, timedelta

        from app.main import _is_stale_abandoned_job

        j = MagicMock()
        now = datetime.now(UTC)
        j.ended_at = now - timedelta(minutes=30)
        assert _is_stale_abandoned_job(j, now) is False

    def test_no_ended_at_returns_false(self):
        """A job with no ended_at is NOT stale."""
        from datetime import UTC, datetime

        from app.main import _is_stale_abandoned_job

        j = MagicMock()
        j.ended_at = None
        assert _is_stale_abandoned_job(j, datetime.now(UTC)) is False

    def test_naive_ended_at_treated_as_utc(self):
        """A timezone-naive ended_at is treated as UTC and compared correctly."""
        from datetime import UTC, datetime, timedelta

        from app.main import _is_stale_abandoned_job

        j = MagicMock()
        now = datetime.now(UTC)
        # naive datetime 2 hours ago
        j.ended_at = (now - timedelta(hours=2)).replace(tzinfo=None)
        assert _is_stale_abandoned_job(j, now) is True

    def test_exactly_one_hour_is_not_stale(self):
        """A job that ended exactly 3600 seconds ago is NOT stale (boundary)."""
        from datetime import UTC, datetime, timedelta

        from app.main import _is_stale_abandoned_job

        j = MagicMock()
        now = datetime.now(UTC)
        j.ended_at = now - timedelta(seconds=3600)
        assert _is_stale_abandoned_job(j, now) is False


# ---------------------------------------------------------------------------
# 6. _should_discard_for_project
# ---------------------------------------------------------------------------
class TestShouldDiscardForProject:
    def test_no_project_id_returns_false(self):
        """A job without project_id in meta is NOT discarded."""
        from app.main import _should_discard_for_project

        j = MagicMock()
        j.meta = {}
        db = MagicMock()
        assert _should_discard_for_project(j, db) is False

    def test_project_not_found_returns_true(self):
        """A job whose project no longer exists IS discarded."""
        from app.main import _should_discard_for_project

        j = MagicMock()
        j.meta = {"project_id": "gone-project-id"}
        db = MagicMock()
        db.get.return_value = None
        assert _should_discard_for_project(j, db) is True

    def test_project_in_deploying_returns_false(self):
        """A job whose project is still 'deploying' is NOT discarded."""
        from app.main import _should_discard_for_project

        j = MagicMock()
        j.meta = {"project_id": "some-pid"}
        proj = MagicMock()
        proj.state = "deploying"
        db = MagicMock()
        db.get.return_value = proj
        assert _should_discard_for_project(j, db) is False

    def test_project_in_deployed_returns_true(self):
        """A job whose project is 'deployed' (non-transient) IS discarded."""
        from app.main import _should_discard_for_project

        j = MagicMock()
        j.meta = {"project_id": "some-pid"}
        proj = MagicMock()
        proj.state = "deployed"
        db = MagicMock()
        db.get.return_value = proj
        assert _should_discard_for_project(j, db) is True

    def test_project_in_stopping_returns_false(self):
        """A job whose project is 'stopping' (transient) is NOT discarded."""
        from app.main import _should_discard_for_project

        j = MagicMock()
        j.meta = {"project_id": "some-pid"}
        proj = MagicMock()
        proj.state = "stopping"
        db = MagicMock()
        db.get.return_value = proj
        assert _should_discard_for_project(j, db) is False


# ---------------------------------------------------------------------------
# 7. _handle_abandoned_job
# ---------------------------------------------------------------------------
class TestHandleAbandonedJob:
    def test_non_abandoned_error_is_skipped(self):
        """A job without AbandonedJobError in exc_info is ignored."""
        from app.main import _handle_abandoned_job

        j = MagicMock()
        j.exc_info = "SomeOtherError: boom"
        registry = MagicMock()
        db = MagicMock()
        from datetime import UTC, datetime

        _handle_abandoned_job(j, registry, db, datetime.now(UTC))
        registry.remove.assert_not_called()
        j.delete.assert_not_called()
        j.requeue.assert_not_called()

    def test_none_exc_info_is_skipped(self):
        """A job with None exc_info is ignored."""
        from app.main import _handle_abandoned_job

        j = MagicMock()
        j.exc_info = None
        registry = MagicMock()
        db = MagicMock()
        from datetime import UTC, datetime

        _handle_abandoned_job(j, registry, db, datetime.now(UTC))
        registry.remove.assert_not_called()

    def test_stale_abandoned_job_is_deleted(self):
        """A stale abandoned job is removed from registry and deleted."""
        from datetime import UTC, datetime, timedelta

        from app.main import _handle_abandoned_job

        j = MagicMock()
        j.exc_info = "rq.exceptions.AbandonedJobError: ..."
        j.func_name = "app.services.deploy_service.deploy_project_async"
        j.id = "abcdef1234567890"
        now = datetime.now(UTC)
        j.ended_at = now - timedelta(hours=2)
        registry = MagicMock()
        db = MagicMock()

        _handle_abandoned_job(j, registry, db, now)
        registry.remove.assert_called_once_with(j)
        j.delete.assert_called_once()
        j.requeue.assert_not_called()

    def test_irrelevant_project_abandoned_job_is_deleted(self):
        """An abandoned job whose project is gone is removed and deleted."""
        from datetime import UTC, datetime

        from app.main import _handle_abandoned_job

        j = MagicMock()
        j.exc_info = "AbandonedJobError"
        j.func_name = "app.services.deploy_service.deploy_project_async"
        j.id = "abcdef1234567890"
        j.ended_at = None  # not stale
        j.meta = {"project_id": "gone-pid"}
        registry = MagicMock()
        db = MagicMock()
        db.get.return_value = None  # project gone
        now = datetime.now(UTC)

        _handle_abandoned_job(j, registry, db, now)
        registry.remove.assert_called_once_with(j)
        j.delete.assert_called_once()
        j.requeue.assert_not_called()

    def test_valid_abandoned_job_is_requeued(self):
        """An abandoned job with a valid transient project is re-queued."""
        from datetime import UTC, datetime

        from app.main import _handle_abandoned_job

        j = MagicMock()
        j.exc_info = "AbandonedJobError"
        j.func_name = "app.services.deploy_service.deploy_project_async"
        j.id = "abcdef1234567890"
        j.ended_at = None  # not stale
        j.meta = {"project_id": "active-pid"}
        proj = MagicMock()
        proj.state = "deploying"
        registry = MagicMock()
        db = MagicMock()
        db.get.return_value = proj
        now = datetime.now(UTC)

        _handle_abandoned_job(j, registry, db, now)
        registry.remove.assert_called_once_with(j)
        j.requeue.assert_called_once()
        j.delete.assert_not_called()


# ---------------------------------------------------------------------------
# 8. _collect_queue_info
# ---------------------------------------------------------------------------
class TestCollectQueueInfo:
    def test_returns_info_for_all_queues(self):
        """Returns an entry for each of the 3 expected queues."""
        from app.main import _collect_queue_info

        mock_q = MagicMock()
        mock_q.count = 5
        mock_q.started_job_registry.count = 2
        mock_q.failed_job_registry.count = 1
        mock_q.deferred_job_registry.count = 0

        with patch("rq.Queue", return_value=mock_q):
            result = _collect_queue_info(MagicMock())

        assert len(result) == 3
        names = [q["name"] for q in result]
        assert "project_lifecycle" in names
        assert "host_lifecycle" in names
        assert "default" in names

    def test_queue_counts_propagated(self):
        """Queue depth numbers are correctly propagated."""
        from app.main import _collect_queue_info

        mock_q = MagicMock()
        mock_q.count = 10
        mock_q.started_job_registry.count = 3
        mock_q.failed_job_registry.count = 7
        mock_q.deferred_job_registry.count = 2

        with patch("rq.Queue", return_value=mock_q):
            result = _collect_queue_info(MagicMock())

        entry = result[0]
        assert entry["queued"] == 10
        assert entry["started"] == 3
        assert entry["failed"] == 7
        assert entry["deferred"] == 2

    def test_queue_exception_returns_error(self):
        """When a queue raises, an error entry is returned instead."""
        from app.main import _collect_queue_info

        def _boom(*a, **kw):
            raise RuntimeError("redis gone")

        with patch("rq.Queue", side_effect=_boom):
            result = _collect_queue_info(MagicMock())

        assert len(result) == 3
        for entry in result:
            assert "error" in entry

    def test_mixed_success_and_failure(self):
        """One queue succeeds, another fails — both are reported."""
        from app.main import _collect_queue_info

        call_count = 0

        def _factory(name, connection=None):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("broken")
            q = MagicMock()
            q.count = 1
            q.started_job_registry.count = 0
            q.failed_job_registry.count = 0
            q.deferred_job_registry.count = 0
            return q

        with patch("rq.Queue", side_effect=_factory):
            result = _collect_queue_info(MagicMock())

        assert len(result) == 3
        errors = [e for e in result if "error" in e]
        successes = [e for e in result if "queued" in e]
        assert len(errors) == 1
        assert len(successes) == 2


# ---------------------------------------------------------------------------
# 9. _collect_worker_info
# ---------------------------------------------------------------------------
class TestCollectWorkerInfo:
    def test_returns_worker_list(self):
        """Returns formatted info for each worker."""
        from app.main import _collect_worker_info

        mock_worker = MagicMock()
        mock_worker.name = "worker-1"
        mock_worker.get_state.return_value = "busy"
        q = MagicMock()
        q.name = "project_lifecycle"
        mock_worker.queues = [q]
        mock_worker.get_current_job_id.return_value = "job-123"
        cj = MagicMock()
        cj.origin = "project_lifecycle"
        cj.func_name = "app.services.deploy_service.deploy_project_async"
        mock_worker.get_current_job.return_value = cj
        mock_worker.successful_job_count = 10
        mock_worker.failed_job_count = 1
        mock_worker.total_working_time = 3600.0

        with patch("rq.Worker") as MockWorker:
            MockWorker.all.return_value = [mock_worker]
            result = _collect_worker_info(MagicMock())

        assert len(result) == 1
        w = result[0]
        assert w["name"] == "worker-1"
        assert w["state"] == "busy"
        assert w["current_func"] == "deploy_project_async"

    def test_idle_worker_no_current_job(self):
        """An idle worker with no current job has empty current fields."""
        from app.main import _collect_worker_info

        mock_worker = MagicMock()
        mock_worker.name = "worker-idle"
        mock_worker.get_state.return_value = "idle"
        mock_worker.queues = []
        mock_worker.get_current_job_id.return_value = None
        mock_worker.get_current_job.return_value = None
        mock_worker.successful_job_count = 0
        mock_worker.failed_job_count = 0
        mock_worker.total_working_time = 0.0

        with patch("rq.Worker") as MockWorker:
            MockWorker.all.return_value = [mock_worker]
            result = _collect_worker_info(MagicMock())

        assert len(result) == 1
        w = result[0]
        assert w["current_job"] == ""
        assert w["current_queue"] == ""
        assert w["current_func"] == ""

    def test_no_workers_returns_empty(self):
        """When no workers exist, returns an empty list."""
        from app.main import _collect_worker_info

        with patch("rq.Worker") as MockWorker:
            MockWorker.all.return_value = []
            result = _collect_worker_info(MagicMock())

        assert result == []

    def test_exception_returns_empty(self):
        """When Worker.all() raises, returns an empty list."""
        from app.main import _collect_worker_info

        with patch("rq.Worker") as MockWorker:
            MockWorker.all.side_effect = RuntimeError("redis gone")
            result = _collect_worker_info(MagicMock())

        assert result == []


# ---------------------------------------------------------------------------
# 10. _collect_inflight_deploys
# ---------------------------------------------------------------------------
class TestCollectInflightDeploys:
    def test_returns_host_counts(self):
        """Returns dict of truncated host_id -> count for positive counts."""
        from app.main import _collect_inflight_deploys

        r = MagicMock()
        r.scan_iter.return_value = [
            "inflight:deploys:aaaa1111-bbbb-cccc-dddd-eeeeeeeeeeee",
            "inflight:deploys:ffff2222-bbbb-cccc-dddd-eeeeeeeeeeee",
        ]
        r.get.side_effect = ["3", "1"]

        result = _collect_inflight_deploys(r)
        assert result == {"aaaa1111": 3, "ffff2222": 1}

    def test_zero_count_excluded(self):
        """Hosts with count 0 are excluded from the result."""
        from app.main import _collect_inflight_deploys

        r = MagicMock()
        r.scan_iter.return_value = ["inflight:deploys:aaaa1111-rest"]
        r.get.return_value = "0"

        result = _collect_inflight_deploys(r)
        assert result == {}

    def test_empty_scan_returns_empty(self):
        """No inflight keys returns an empty dict."""
        from app.main import _collect_inflight_deploys

        r = MagicMock()
        r.scan_iter.return_value = []

        result = _collect_inflight_deploys(r)
        assert result == {}

    def test_exception_returns_empty(self):
        """Redis errors are swallowed, returns empty dict."""
        from app.main import _collect_inflight_deploys

        r = MagicMock()
        r.scan_iter.side_effect = RuntimeError("connection lost")

        result = _collect_inflight_deploys(r)
        assert result == {}

    def test_none_get_treated_as_zero(self):
        """When r.get() returns None, it is treated as 0 and excluded."""
        from app.main import _collect_inflight_deploys

        r = MagicMock()
        r.scan_iter.return_value = ["inflight:deploys:aaaa1111-rest"]
        r.get.return_value = None

        result = _collect_inflight_deploys(r)
        assert result == {}


# ---------------------------------------------------------------------------
# 11. _resume_creating_pools
# ---------------------------------------------------------------------------
class TestResumeCreatingPools:
    def test_pool_with_fsx_id_is_requeued(self):
        """A pool in 'creating' with an FSx ID gets its poller re-enqueued."""
        from app.main import _resume_creating_pools

        pool = MagicMock()
        pool.name = "test-pool"
        pool.id = "pool-id-1"
        pool.fsx_filesystem_id = "fs-12345"
        pool.provider_id = "prov-1"
        pool.status = "creating"

        provider = MagicMock()
        provider.get_credentials.return_value = {"key": "val"}
        provider.default_region = "us-east-1"

        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = [pool]
        db.get.return_value = provider

        mock_enqueue = MagicMock()
        provider_model = MagicMock()

        with patch(
            "app.main._poll_fsx_until_available",
            create=True,
        ):
            _resume_creating_pools(db, mock_enqueue, provider_model)

        mock_enqueue.assert_called_once()
        call_kwargs = mock_enqueue.call_args[1]
        assert call_kwargs["queue_name"] == "host_lifecycle"

    def test_pool_without_fsx_id_marked_error(self):
        """A pool in 'creating' with no FSx ID is marked as error."""
        from app.main import _resume_creating_pools

        pool = MagicMock()
        pool.name = "broken-pool"
        pool.id = "pool-id-2"
        pool.fsx_filesystem_id = None
        pool.status = "creating"

        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = [pool]

        mock_enqueue = MagicMock()
        provider_model = MagicMock()

        _resume_creating_pools(db, mock_enqueue, provider_model)

        assert pool.status == "error"
        mock_enqueue.assert_not_called()

    def test_no_creating_pools_is_noop(self):
        """When no pools are in 'creating', nothing happens."""
        from app.main import _resume_creating_pools

        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = []

        mock_enqueue = MagicMock()
        _resume_creating_pools(db, mock_enqueue, MagicMock())
        mock_enqueue.assert_not_called()

    def test_provider_not_found_skips(self):
        """A pool whose provider no longer exists is skipped."""
        from app.main import _resume_creating_pools

        pool = MagicMock()
        pool.name = "orphan-pool"
        pool.fsx_filesystem_id = "fs-999"
        pool.provider_id = "gone-prov"

        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = [pool]
        db.get.return_value = None

        mock_enqueue = MagicMock()
        _resume_creating_pools(db, mock_enqueue, MagicMock())
        mock_enqueue.assert_not_called()


# ---------------------------------------------------------------------------
# 12. _retry_stuck_pattern_buffer_installs
# ---------------------------------------------------------------------------
class TestRetryStuckPatternBufferInstalls:
    def test_disconnected_active_pb_host_retried(self):
        """An active but disconnected pattern buffer host is retried."""
        from app.main import _retry_stuck_pattern_buffer_installs

        pool = MagicMock()
        pool.worker_host_id = "pb-host-1"
        pool.name = "pool-a"

        pb_host = MagicMock()
        pb_host.id = "pb-host-1-full-uuid"
        pb_host.state = "active"
        pb_host.agent_status = "disconnected"

        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = [pool]

        host_model = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = pb_host

        mock_enqueue = MagicMock()

        _retry_stuck_pattern_buffer_installs(db, mock_enqueue, host_model)

        mock_enqueue.assert_called_once()
        call_args = mock_enqueue.call_args
        assert call_args[0][1] == "pb-host-1-full-uuid"
        assert call_args[0][2] == pool.id
        assert call_args[1]["queue_name"] == "host_lifecycle"

    def test_connected_pb_host_not_retried(self):
        """An already connected pattern buffer host is NOT retried."""
        from app.main import _retry_stuck_pattern_buffer_installs

        pool = MagicMock()
        pool.worker_host_id = "pb-host-2"

        pb_host = MagicMock()
        pb_host.state = "active"
        pb_host.agent_status = "connected"

        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = [pool]

        host_model = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = pb_host

        mock_enqueue = MagicMock()
        _retry_stuck_pattern_buffer_installs(db, mock_enqueue, host_model)
        mock_enqueue.assert_not_called()

    def test_no_pb_pools_is_noop(self):
        """When no pools have worker_host_id, nothing happens."""
        from app.main import _retry_stuck_pattern_buffer_installs

        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = []

        mock_enqueue = MagicMock()
        _retry_stuck_pattern_buffer_installs(db, mock_enqueue, MagicMock())
        mock_enqueue.assert_not_called()

    def test_pb_host_not_found_skips(self):
        """A pool referencing a missing host is silently skipped."""
        from app.main import _retry_stuck_pattern_buffer_installs

        pool = MagicMock()
        pool.worker_host_id = "missing-host"

        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = [pool]

        host_model = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = None

        mock_enqueue = MagicMock()
        _retry_stuck_pattern_buffer_installs(db, mock_enqueue, host_model)
        mock_enqueue.assert_not_called()

    def test_inactive_pb_host_not_retried(self):
        """A pattern buffer host that is not 'active' is NOT retried."""
        from app.main import _retry_stuck_pattern_buffer_installs

        pool = MagicMock()
        pool.worker_host_id = "pb-host-3"

        pb_host = MagicMock()
        pb_host.state = "terminated"
        pb_host.agent_status = "disconnected"

        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = [pool]

        host_model = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = pb_host

        mock_enqueue = MagicMock()
        _retry_stuck_pattern_buffer_installs(db, mock_enqueue, host_model)
        mock_enqueue.assert_not_called()


# ── _sync_shared_pool_sg_rules tests ──


class TestSyncSharedPoolSgRules:
    def test_calls_add_sg_rules_for_available_pools(self):
        from app.main import _sync_shared_pool_sg_rules

        pool = MagicMock()
        pool.status = "available"
        pool.mode = "shared-fsx"
        pool.provider_id = "prov1"
        pool.name = "test-pool"

        provider = MagicMock()
        provider.security_group_id = "sg-123"
        provider.default_region = "us-east-1"
        provider.get_credentials.return_value = {"key": "val"}

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = [pool]
        mock_db.get.return_value = provider

        with patch(
            "app.services.storage_pool_service.add_sg_rules_for_shared_storage"
        ) as mock_add:
            _sync_shared_pool_sg_rules(mock_db, MagicMock)
            mock_add.assert_called_once_with({"key": "val"}, "us-east-1", "sg-123")

    def test_skips_pool_without_provider(self):
        from app.main import _sync_shared_pool_sg_rules

        pool = MagicMock()
        pool.status = "available"
        pool.mode = "shared-fsx"
        pool.provider_id = "prov1"
        pool.name = "test-pool"

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = [pool]
        mock_db.get.return_value = None

        with patch(
            "app.services.storage_pool_service.add_sg_rules_for_shared_storage"
        ) as mock_add:
            _sync_shared_pool_sg_rules(mock_db, MagicMock)
            mock_add.assert_not_called()

    def test_skips_pool_without_security_group(self):
        from app.main import _sync_shared_pool_sg_rules

        pool = MagicMock()
        pool.status = "available"
        pool.mode = "shared-fsx"
        pool.provider_id = "prov1"
        pool.name = "test-pool"

        provider = MagicMock()
        provider.security_group_id = None

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = [pool]
        mock_db.get.return_value = provider

        with patch(
            "app.services.storage_pool_service.add_sg_rules_for_shared_storage"
        ) as mock_add:
            _sync_shared_pool_sg_rules(mock_db, MagicMock)
            mock_add.assert_not_called()

    def test_logs_warning_on_failure(self):
        from app.main import _sync_shared_pool_sg_rules

        pool = MagicMock()
        pool.status = "available"
        pool.mode = "shared-fsx"
        pool.provider_id = "prov1"
        pool.name = "test-pool"

        provider = MagicMock()
        provider.security_group_id = "sg-123"
        provider.default_region = "us-east-1"
        provider.get_credentials.return_value = {"key": "val"}

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = [pool]
        mock_db.get.return_value = provider

        with patch(
            "app.services.storage_pool_service.add_sg_rules_for_shared_storage",
            side_effect=Exception("sg fail"),
        ):
            # Should not raise
            _sync_shared_pool_sg_rules(mock_db, MagicMock)


# ── _startup_resume_pattern_captures tests ──


class TestStartupResumePatternCaptures:
    @patch("app.core.database.SessionLocal")
    def test_resumes_capturing_patterns(self, mock_session_cls):
        from app.main import _startup_resume_pattern_captures

        pat = MagicMock()
        pat.id = "pat-111"
        pat.name = "test-pattern"
        pat.state = "capturing"
        pat.source_project_id = "proj-111"

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = [pat]
        mock_session_cls.return_value = mock_db

        with patch("app.core.redis.enqueue_job") as mock_enqueue:
            _startup_resume_pattern_captures()
            mock_enqueue.assert_called_once()
            args = mock_enqueue.call_args[0]
            assert args[1] == "pat-111"
            assert args[2] == "proj-111"

    @patch("app.core.database.SessionLocal")
    def test_no_stuck_patterns(self, mock_session_cls):
        from app.main import _startup_resume_pattern_captures

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = []
        mock_session_cls.return_value = mock_db

        with patch("app.core.redis.enqueue_job") as mock_enqueue:
            _startup_resume_pattern_captures()
            mock_enqueue.assert_not_called()


# ── _startup_recover_abandoned_jobs tests ──


class TestStartupRecoverAbandonedJobs:
    @patch("app.core.redis.is_redis_available", return_value=False)
    def test_noop_when_redis_unavailable(self, mock_redis):
        from app.main import _startup_recover_abandoned_jobs

        _startup_recover_abandoned_jobs()

    @patch("app.core.redis.is_redis_available", return_value=True)
    @patch("app.core.redis.get_redis_raw", side_effect=Exception("fail"))
    def test_handles_redis_error(self, mock_raw, mock_avail):
        from app.main import _startup_recover_abandoned_jobs

        # Should not raise
        _startup_recover_abandoned_jobs()


# ── list_failed_jobs / retry / delete endpoint tests ──


class TestFailedJobEndpoints:
    def test_list_failed_jobs_no_redis(self):
        with patch("app.core.redis.is_redis_available", return_value=False):
            resp = client.get("/api/v1/admin/failed-jobs", headers=HEADERS)
            assert resp.status_code == 200
            assert resp.json() == {"jobs": []}

    def test_retry_failed_job_no_redis(self):
        with patch("app.core.redis.is_redis_available", return_value=False):
            resp = client.post(
                "/api/v1/admin/failed-jobs/job-123/retry",
                headers=HEADERS,
            )
            assert resp.status_code == 400

    def test_delete_failed_job_no_redis(self):
        with patch("app.core.redis.is_redis_available", return_value=False):
            resp = client.delete(
                "/api/v1/admin/failed-jobs/job-123",
                headers=HEADERS,
            )
            assert resp.status_code == 400


# ── ocp_versions endpoint test ──


class TestOcpVersionsEndpoint:
    def test_ocp_versions_returns_channels(self):
        mock_data = {
            "nodes": [
                {"version": "4.18.1"},
                {"version": "4.18.2"},
            ]
        }

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(mock_data).encode()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp

            resp = client.get("/api/v1/ocp/versions")
            assert resp.status_code == 200
            data = resp.json()
            assert isinstance(data, list)

    def test_ocp_versions_handles_errors(self):
        with patch("urllib.request.urlopen", side_effect=Exception("timeout")):
            resp = client.get("/api/v1/ocp/versions")
            assert resp.status_code == 200
            assert resp.json() == []


# ── _startup_resume_storage_pools tests ──


class TestStartupResumeStoragePools:
    def test_calls_all_three_sub_functions(self):
        from app.main import _startup_resume_storage_pools

        mock_db = MagicMock()

        with patch("app.core.database.SessionLocal", return_value=mock_db), patch(
            "app.main._resume_creating_pools"
        ) as mock_resume, patch(
            "app.main._sync_shared_pool_sg_rules"
        ) as mock_sync, patch(
            "app.main._retry_stuck_pattern_buffer_installs"
        ) as mock_retry:
            _startup_resume_storage_pools()

            mock_resume.assert_called_once()
            mock_sync.assert_called_once()
            mock_retry.assert_called_once()
            mock_db.commit.assert_called_once()
            mock_db.close.assert_called_once()

    def test_closes_db_on_exception(self):
        from app.main import _startup_resume_storage_pools

        mock_db = MagicMock()

        with patch("app.core.database.SessionLocal", return_value=mock_db), patch(
            "app.main._resume_creating_pools",
            side_effect=Exception("db error"),
        ), patch("app.main._sync_shared_pool_sg_rules"), patch(
            "app.main._retry_stuck_pattern_buffer_installs"
        ):
            try:
                _startup_resume_storage_pools()
            except Exception:
                pass

            # DB session is always closed (finally block)
            mock_db.close.assert_called_once()


# ── Failed-jobs 403 tests for non-admin users ──


class TestFailedJobEndpoints403:
    def test_list_failed_jobs_403_for_regular_user(self):
        with patch("app.core.auth.config", _mock_oauth_config()):
            resp = client.get("/api/v1/admin/failed-jobs", headers=REGULAR_HEADERS)
            assert resp.status_code == 403

    def test_retry_failed_job_403_for_regular_user(self):
        with patch("app.core.auth.config", _mock_oauth_config()):
            resp = client.post(
                "/api/v1/admin/failed-jobs/job-123/retry",
                headers=REGULAR_HEADERS,
            )
            assert resp.status_code == 403

    def test_delete_failed_job_403_for_regular_user(self):
        with patch("app.core.auth.config", _mock_oauth_config()):
            resp = client.delete(
                "/api/v1/admin/failed-jobs/job-123",
                headers=REGULAR_HEADERS,
            )
            assert resp.status_code == 403


# ── Redis-available paths for admin endpoints ──


class TestQueueStatusWithRedis:
    def test_returns_full_data_when_redis_available(self):
        """Test queue-status returns structured data when Redis is available.

        Patches are scoped to avoid interfering with the rate-limit middleware
        which also calls get_redis().
        """
        from app.main import queue_status

        mock_r_raw = MagicMock()
        mock_r_str = MagicMock()
        mock_user = MagicMock()
        mock_user.role = "admin"

        with patch("app.core.redis.is_redis_available", return_value=True), patch(
            "app.core.redis.get_redis_raw", return_value=mock_r_raw
        ), patch("app.core.redis.get_redis", return_value=mock_r_str), patch(
            "app.main._collect_queue_info",
            return_value=[{"name": "deploy", "queued": 2}],
        ), patch(
            "app.main._collect_worker_info",
            return_value=[{"name": "w1", "state": "busy"}],
        ), patch(
            "app.main._collect_inflight_deploys",
            return_value={"abc12345": 3},
        ):
            # Call the function directly to avoid middleware interference
            data = queue_status(user=mock_user)
            assert data["redis"] is True
            assert data["queues"] == [{"name": "deploy", "queued": 2}]
            assert data["worker_count"] == 1
            assert data["workers"] == [{"name": "w1", "state": "busy"}]
            assert data["inflight_deploys"] == {"abc12345": 3}


class TestFailedJobsWithRedis:
    """Test admin endpoints by calling functions directly to avoid
    rate-limit middleware interference from patched Redis."""

    def test_list_failed_jobs_returns_job_list(self):
        from app.main import list_failed_jobs

        mock_user = MagicMock()
        mock_user.role = "admin"
        mock_job = MagicMock()
        mock_job.func_name = "app.services.deploy.deploy_project"
        mock_job.args = ["proj-123"]
        mock_job.exc_info = "ValueError: bad"
        mock_job.enqueued_at = None
        mock_job.ended_at = None

        mock_queue = MagicMock()
        mock_queue.failed_job_registry.get_job_ids.return_value = ["job-abc"]

        with patch("app.core.redis.is_redis_available", return_value=True), patch(
            "app.core.redis.get_redis_raw", return_value=MagicMock()
        ), patch("rq.Queue", return_value=mock_queue), patch(
            "rq.job.Job.fetch", return_value=mock_job
        ):
            data = list_failed_jobs(user=mock_user, queue_name="deploy")
            assert data["queue"] == "deploy"
            assert data["count"] == 1
            assert len(data["jobs"]) == 1
            assert data["jobs"][0]["id"] == "job-abc"
            assert "deploy_project" in data["jobs"][0]["func"]

    def test_list_failed_jobs_handles_fetch_error(self):
        from app.main import list_failed_jobs

        mock_user = MagicMock()
        mock_user.role = "admin"
        mock_queue = MagicMock()
        mock_queue.failed_job_registry.get_job_ids.return_value = ["bad-job"]

        with patch("app.core.redis.is_redis_available", return_value=True), patch(
            "app.core.redis.get_redis_raw", return_value=MagicMock()
        ), patch("rq.Queue", return_value=mock_queue), patch(
            "rq.job.Job.fetch", side_effect=Exception("no such job")
        ):
            data = list_failed_jobs(user=mock_user)
            assert data["jobs"][0]["id"] == "bad-job"
            assert data["jobs"][0]["error"] == "could not fetch"

    def test_retry_failed_job_success(self):
        from app.main import retry_failed_job

        mock_user = MagicMock()
        mock_user.role = "admin"
        mock_job = MagicMock()

        with patch("app.core.redis.is_redis_available", return_value=True), patch(
            "app.core.redis.get_redis_raw", return_value=MagicMock()
        ), patch("rq.job.Job.fetch", return_value=mock_job):
            data = retry_failed_job(job_id="job-abc", user=mock_user)
            assert data["status"] == "requeued"
            assert data["job_id"] == "job-abc"
            mock_job.requeue.assert_called_once()

    def test_retry_failed_job_fetch_error(self):
        from app.main import retry_failed_job

        mock_user = MagicMock()
        mock_user.role = "admin"

        with patch("app.core.redis.is_redis_available", return_value=True), patch(
            "app.core.redis.get_redis_raw", return_value=MagicMock()
        ), patch("rq.job.Job.fetch", side_effect=Exception("not found")):
            with pytest.raises(HTTPException) as exc:
                retry_failed_job(job_id="job-xyz", user=mock_user)
            assert exc.value.status_code == 400
            assert "not found" in str(exc.value.detail)

    def test_delete_failed_job_success(self):
        from app.main import delete_failed_job

        mock_user = MagicMock()
        mock_user.role = "admin"
        mock_job = MagicMock()

        with patch("app.core.redis.is_redis_available", return_value=True), patch(
            "app.core.redis.get_redis_raw", return_value=MagicMock()
        ), patch("rq.job.Job.fetch", return_value=mock_job):
            data = delete_failed_job(job_id="job-abc", user=mock_user)
            assert data["status"] == "deleted"
            assert data["job_id"] == "job-abc"
            mock_job.delete.assert_called_once()

    def test_delete_failed_job_fetch_error(self):
        from app.main import delete_failed_job

        mock_user = MagicMock()
        mock_user.role = "admin"

        with patch("app.core.redis.is_redis_available", return_value=True), patch(
            "app.core.redis.get_redis_raw", return_value=MagicMock()
        ), patch("rq.job.Job.fetch", side_effect=Exception("not found")):
            with pytest.raises(HTTPException) as exc:
                delete_failed_job(job_id="job-xyz", user=mock_user)
            assert exc.value.status_code == 400
            assert "not found" in str(exc.value.detail)


# ---------------------------------------------------------------------------
# _startup_recover_abandoned_jobs inner loop (lines 131-145)
# ---------------------------------------------------------------------------


class TestRecoverAbandonedJobsInnerLoop:
    """Test the inner loop of _startup_recover_abandoned_jobs that iterates
    queues, fetches jobs, and calls _handle_abandoned_job."""

    @patch("app.core.redis.is_redis_available", return_value=True)
    def test_iterates_queues_and_processes_jobs(self, _mock_avail):
        from app.main import _startup_recover_abandoned_jobs

        mock_registry = MagicMock()
        mock_registry.get_job_ids.return_value = ["job-1", "job-2"]

        mock_queue = MagicMock()
        mock_queue.failed_job_registry = mock_registry

        mock_job = MagicMock()
        mock_job.exc_info = "AbandonedJobError"

        mock_db = MagicMock()

        with patch("app.core.database.SessionLocal", return_value=mock_db), patch(
            "app.core.redis.get_redis_raw", return_value=MagicMock()
        ), patch("rq.Queue", return_value=mock_queue), patch(
            "rq.job.Job.fetch", return_value=mock_job
        ), patch(
            "app.main._handle_abandoned_job"
        ) as mock_handle:
            _startup_recover_abandoned_jobs()
            # 3 queues x 2 jobs = 6 calls
            assert mock_handle.call_count == 6

    @patch("app.core.redis.is_redis_available", return_value=True)
    def test_inner_loop_swallows_job_fetch_exceptions(self, _mock_avail):
        from app.main import _startup_recover_abandoned_jobs

        mock_registry = MagicMock()
        mock_registry.get_job_ids.return_value = ["bad-job"]

        mock_queue = MagicMock()
        mock_queue.failed_job_registry = mock_registry

        mock_db = MagicMock()

        with patch("app.core.database.SessionLocal", return_value=mock_db), patch(
            "app.core.redis.get_redis_raw", return_value=MagicMock()
        ), patch("rq.Queue", return_value=mock_queue), patch(
            "rq.job.Job.fetch", side_effect=Exception("gone")
        ), patch(
            "app.main._handle_abandoned_job"
        ) as mock_handle:
            # Should not raise
            _startup_recover_abandoned_jobs()
            mock_handle.assert_not_called()

    @patch("app.core.redis.is_redis_available", return_value=True)
    def test_inner_loop_closes_db(self, _mock_avail):
        from app.main import _startup_recover_abandoned_jobs

        mock_registry = MagicMock()
        mock_registry.get_job_ids.return_value = []

        mock_queue = MagicMock()
        mock_queue.failed_job_registry = mock_registry

        mock_db = MagicMock()

        with patch("app.core.database.SessionLocal", return_value=mock_db), patch(
            "app.core.redis.get_redis_raw", return_value=MagicMock()
        ), patch("rq.Queue", return_value=mock_queue):
            _startup_recover_abandoned_jobs()
            mock_db.close.assert_called_once()


# ---------------------------------------------------------------------------
# _retry_pb_agent_install (lines 429-516)
# ---------------------------------------------------------------------------


class TestRetryPbAgentInstall:
    """Test _retry_pb_agent_install with various conditions."""

    def test_host_not_found_returns_silently(self):
        from app.main import _retry_pb_agent_install

        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = None

        with patch("app.core.database.SessionLocal", return_value=mock_db), patch(
            "app.services.agent_deployer.deploy_agent"
        ) as mock_deploy:
            _retry_pb_agent_install("no-such-host", "no-such-pool")
            mock_deploy.assert_not_called()
            mock_db.close.assert_called_once()

    def test_pool_not_found_returns_silently(self):
        from app.main import _retry_pb_agent_install

        mock_host = MagicMock()
        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.side_effect = [
            mock_host,
            None,
        ]

        with patch("app.core.database.SessionLocal", return_value=mock_db), patch(
            "app.services.agent_deployer.deploy_agent"
        ) as mock_deploy:
            _retry_pb_agent_install("host-1", "no-pool")
            mock_deploy.assert_not_called()

    def test_pool_no_provider_returns_silently(self):
        from app.main import _retry_pb_agent_install

        mock_host = MagicMock()
        mock_pool = MagicMock()
        mock_pool.provider = None

        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.side_effect = [
            mock_host,
            mock_pool,
        ]

        with patch("app.core.database.SessionLocal", return_value=mock_db), patch(
            "app.services.agent_deployer.deploy_agent"
        ) as mock_deploy:
            _retry_pb_agent_install("host-1", "pool-1")
            mock_deploy.assert_not_called()

    def test_ssh_not_available_returns_silently(self):
        from app.main import _retry_pb_agent_install

        mock_host = MagicMock()
        mock_host.ip_address = "10.0.0.1"
        mock_host.private_key = "key-data"

        mock_provider = MagicMock()
        mock_provider.type = "ec2"

        mock_pool = MagicMock()
        mock_pool.provider = mock_provider
        mock_pool.nfs_endpoint = None
        mock_pool.fsx_dns_name = None
        mock_pool.azure_file_share_url = None
        mock_pool.ca_cert = None
        mock_pool.ca_key = None

        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.side_effect = [
            mock_host,
            mock_pool,
        ]

        with patch("app.core.database.SessionLocal", return_value=mock_db), patch(
            "app.services.agent_deployer.wait_for_ssh", return_value=False
        ), patch(
            "app.services.agent_deployer.get_provider_ssh_user",
            return_value="ec2-user",
        ), patch(
            "app.services.agent_deployer.get_provider_ssh_port", return_value=22
        ), patch(
            "app.services.agent_deployer.deploy_agent"
        ) as mock_deploy:
            _retry_pb_agent_install("host-1", "pool-1")
            mock_deploy.assert_not_called()

    def test_successful_install_calls_deploy_agent(self):
        from app.main import _retry_pb_agent_install

        mock_host = MagicMock()
        mock_host.ip_address = "10.0.0.1"
        mock_host.private_key = "key-data"
        mock_host.private_ip = "10.0.0.2"
        mock_host.id = "host-abcd1234"

        mock_provider = MagicMock()
        mock_provider.type = "ec2"

        mock_pool = MagicMock()
        mock_pool.provider = mock_provider
        mock_pool.nfs_endpoint = None
        mock_pool.fsx_dns_name = None
        mock_pool.azure_file_share_url = None
        mock_pool.ca_cert = None
        mock_pool.ca_key = None
        mock_pool.nfs_port = 0

        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.side_effect = [
            mock_host,
            mock_pool,
        ]

        with patch("app.core.database.SessionLocal", return_value=mock_db), patch(
            "app.services.agent_deployer.wait_for_ssh", return_value=True
        ), patch(
            "app.services.agent_deployer.get_provider_ssh_user",
            return_value="ec2-user",
        ), patch(
            "app.services.agent_deployer.get_provider_ssh_port", return_value=22
        ), patch(
            "app.services.agent_deployer.get_provider_data_disk",
            return_value="/dev/sdb",
        ), patch(
            "app.services.agent_ca_service.get_agent_ca_cert", return_value="ca-pem"
        ), patch(
            "app.services.agent_deployer.deploy_agent"
        ) as mock_deploy:
            _retry_pb_agent_install("host-abcd1234", "pool-1")
            mock_deploy.assert_called_once()
            call_kwargs = mock_deploy.call_args
            assert call_kwargs[0][0] == "10.0.0.1"  # ssh_host
            assert call_kwargs[1]["config"].storage_mode == "local"

    def test_fsx_pool_sets_shared_storage_mode(self):
        from app.main import _retry_pb_agent_install

        mock_host = MagicMock()
        mock_host.ip_address = "10.0.0.1"
        mock_host.private_key = "key-data"
        mock_host.private_ip = "10.0.0.2"
        mock_host.id = "host-abcd1234"

        mock_provider = MagicMock()
        mock_provider.type = "ec2"

        mock_pool = MagicMock()
        mock_pool.provider = mock_provider
        mock_pool.fsx_dns_name = "fs-abc.fsx.us-east-1.amazonaws.com"
        mock_pool.nfs_endpoint = None
        mock_pool.azure_file_share_url = None
        mock_pool.ca_cert = "CA"
        mock_pool.ca_key = "KEY"
        mock_pool.nfs_port = 0

        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.side_effect = [
            mock_host,
            mock_pool,
        ]

        with patch("app.core.database.SessionLocal", return_value=mock_db), patch(
            "app.services.agent_deployer.wait_for_ssh", return_value=True
        ), patch(
            "app.services.agent_deployer.get_provider_ssh_user",
            return_value="ec2-user",
        ), patch(
            "app.services.agent_deployer.get_provider_ssh_port", return_value=22
        ), patch(
            "app.services.agent_deployer.get_provider_data_disk",
            return_value="/dev/sdb",
        ), patch(
            "app.services.agent_ca_service.get_agent_ca_cert", return_value="ca-pem"
        ), patch(
            "app.services.storage_pool_service.sign_host_cert",
            return_value=("cert", "key"),
        ), patch(
            "app.services.agent_deployer.deploy_agent"
        ) as mock_deploy:
            _retry_pb_agent_install("host-abcd1234", "pool-1")
            mock_deploy.assert_called_once()
            call_kwargs = mock_deploy.call_args
            assert call_kwargs[1]["config"].storage_mode == "shared"
            assert (
                call_kwargs[1]["config"].nfs_server
                == "fs-abc.fsx.us-east-1.amazonaws.com"
            )
            assert call_kwargs[1]["config"].nfs_path == "/fsx"

    def test_exception_is_caught_and_db_closed(self):
        from app.main import _retry_pb_agent_install

        mock_host = MagicMock()
        mock_host.ip_address = "10.0.0.1"
        mock_host.private_key = "key-data"
        mock_host.id = "host-abcd1234"

        mock_provider = MagicMock()
        mock_provider.type = "ec2"

        mock_pool = MagicMock()
        mock_pool.provider = mock_provider
        mock_pool.nfs_endpoint = None
        mock_pool.fsx_dns_name = None
        mock_pool.azure_file_share_url = None
        mock_pool.ca_cert = None
        mock_pool.ca_key = None
        mock_pool.nfs_port = 0

        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.side_effect = [
            mock_host,
            mock_pool,
        ]

        with patch("app.core.database.SessionLocal", return_value=mock_db), patch(
            "app.services.agent_deployer.wait_for_ssh", return_value=True
        ), patch(
            "app.services.agent_deployer.get_provider_ssh_user",
            return_value="ec2-user",
        ), patch(
            "app.services.agent_deployer.get_provider_ssh_port", return_value=22
        ), patch(
            "app.services.agent_deployer.get_provider_data_disk",
            side_effect=RuntimeError("boom"),
        ):
            # Should not raise -- exception caught internally
            _retry_pb_agent_install("host-abcd1234", "pool-1")
            mock_db.close.assert_called_once()


# ---------------------------------------------------------------------------
# lifespan async context manager (lines 376-426)
# ---------------------------------------------------------------------------


class TestLifespan:
    """Test the lifespan async context manager calls all startup functions."""

    def test_lifespan_calls_startup_functions_and_yields(self):
        import asyncio

        from app.main import lifespan

        mock_app = MagicMock()

        async def _run():
            with patch("app.services.ws_pubsub.set_event_loop"), patch(
                "app.core.redis.get_redis"
            ) as mock_redis, patch(
                "app.services.agent_ca_service.ensure_agent_ca"
            ), patch(
                "app.services.health_poller.start_health_poller"
            ) as mock_hp, patch(
                "app.services.project_timer.start_project_timer"
            ) as mock_pt, patch(
                "app.services.ws_pubsub.start_state_poller"
            ) as mock_sp, patch(
                "app.services.ws_pubsub.start_redis_listener"
            ) as mock_rl, patch(
                "app.services.operator_updater.start_operator_updater"
            ) as mock_ou, patch(
                "app.main._startup_clear_health_monitors"
            ) as mock_chm, patch(
                "app.main._startup_recover_abandoned_jobs"
            ) as mock_raj, patch(
                "app.main._startup_reset_stuck_projects"
            ) as mock_rsp, patch(
                "app.main._startup_reset_stuck_hosts"
            ) as mock_rsh, patch(
                "app.main._startup_resume_pattern_captures"
            ) as mock_rpc, patch(
                "app.main._startup_resume_storage_pools"
            ) as mock_rsp2, patch(
                "app.core.redis.close_redis"
            ) as mock_close:
                mock_redis.return_value.ping.return_value = True

                async with lifespan(mock_app):
                    mock_hp.assert_called_once()
                    mock_pt.assert_called_once()
                    mock_sp.assert_called_once()
                    mock_rl.assert_called_once()
                    mock_ou.assert_called_once()
                    mock_chm.assert_called_once()
                    mock_raj.assert_called_once()
                    mock_rsp.assert_called_once()
                    mock_rsh.assert_called_once()
                    mock_rpc.assert_called_once()
                    mock_rsp2.assert_called_once()

                mock_close.assert_called_once()

        asyncio.run(_run())

    def test_lifespan_handles_redis_unavailable(self):
        import asyncio

        from app.main import lifespan

        mock_app = MagicMock()

        async def _run():
            with patch("app.services.ws_pubsub.set_event_loop"), patch(
                "app.core.redis.get_redis",
                side_effect=Exception("no redis"),
            ), patch("app.services.agent_ca_service.ensure_agent_ca"), patch(
                "app.services.health_poller.start_health_poller"
            ), patch(
                "app.services.project_timer.start_project_timer"
            ), patch(
                "app.services.ws_pubsub.start_state_poller"
            ), patch(
                "app.services.ws_pubsub.start_redis_listener"
            ), patch(
                "app.services.operator_updater.start_operator_updater"
            ), patch(
                "app.main._startup_clear_health_monitors"
            ), patch(
                "app.main._startup_recover_abandoned_jobs"
            ), patch(
                "app.main._startup_reset_stuck_projects"
            ), patch(
                "app.main._startup_reset_stuck_hosts"
            ), patch(
                "app.main._startup_resume_pattern_captures"
            ), patch(
                "app.main._startup_resume_storage_pools"
            ), patch(
                "app.core.redis.close_redis"
            ):
                async with lifespan(mock_app):
                    pass

        asyncio.run(_run())
