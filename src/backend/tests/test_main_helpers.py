"""Tests for startup helpers and admin endpoints in app.main."""

import uuid
from unittest.mock import MagicMock, patch

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
