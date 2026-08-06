"""Tests for ws.py — WebSocket API endpoints and helpers."""

import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.auth import create_jwt, hash_password
from app.core.database import get_db
from app.main import app
from app.models.pattern import Pattern
from app.models.project import Project
from app.models.user import User
from tests.conftest import TestSession, get_test_db

app.dependency_overrides[get_db] = get_test_db
client = TestClient(app)

# ---------------------------------------------------------------------------
# Test data setup
# ---------------------------------------------------------------------------
_db = TestSession()
_user = User(
    email="ws-api-test@example.com",
    display_name="WS API Tester",
    role="user",
    auth_source="local",
    password_hash=hash_password("pass"),
)
_db.add(_user)
_user_admin = User(
    email="ws-api-admin@example.com",
    display_name="WS API Admin",
    role="admin",
    auth_source="local",
    password_hash=hash_password("pass"),
)
_db.add(_user_admin)
_other_user = User(
    email="ws-api-other@example.com",
    display_name="WS API Other",
    role="user",
    auth_source="local",
    password_hash=hash_password("pass"),
)
_db.add(_other_user)
_db.commit()
_db.refresh(_user)
_db.refresh(_user_admin)
_db.refresh(_other_user)
TOKEN = create_jwt(user_id=_user.id, email=_user.email, role=_user.role)
ADMIN_TOKEN = create_jwt(
    user_id=_user_admin.id, email=_user_admin.email, role=_user_admin.role
)
OTHER_TOKEN = create_jwt(
    user_id=_other_user.id, email=_other_user.email, role=_other_user.role
)
USER_ID = _user.id
ADMIN_ID = _user_admin.id
OTHER_ID = _other_user.id
_db.close()


# ---------------------------------------------------------------------------
# Helper to create projects/patterns in the DB
# ---------------------------------------------------------------------------
def _create_project(owner_id: str, state: str = "deployed", **kwargs) -> str:
    db = TestSession()
    pid = str(uuid.uuid4())
    p = Project(id=pid, name="ws-test-proj", owner_id=owner_id, state=state, **kwargs)
    db.add(p)
    db.commit()
    db.close()
    return pid


def _create_pattern(owner_id: str, state: str = "available") -> str:
    db = TestSession()
    pid = str(uuid.uuid4())
    p = Pattern(
        id=pid,
        name="ws-test-pat",
        owner_id=owner_id,
        state=state,
        topology={"nodes": [], "edges": []},
    )
    db.add(p)
    db.commit()
    db.close()
    return pid


# ===================================================================
# 1. _authenticate_ws unit tests
# ===================================================================
class TestAuthenticateWs:
    """Unit tests for _authenticate_ws helper."""

    def test_dev_mode_no_token(self):
        """Dev mode (oauth_enabled=False) with no token returns dev user."""
        from app.api.ws import _authenticate_ws

        db = TestSession()
        user = _authenticate_ws(token=None, db=db)
        assert user is not None
        assert user.email == "local-dev@troshka"
        db.close()

    def test_sso_mode_with_forwarded_email(self):
        """SSO mode with x-forwarded-email header upserts and returns user."""
        from app.api.ws import _authenticate_ws

        db = TestSession()
        with patch("app.api.ws.config") as mock_config:
            mock_config.auth.oauth_enabled = True
            headers = {
                "x-forwarded-email": "sso-ws-test@example.com",
                "x-forwarded-user": "ssotester",
            }
            user = _authenticate_ws(token=None, db=db, headers=headers)
            assert user is not None
            assert user.email == "sso-ws-test@example.com"
        db.close()

    def test_no_token_not_dev_mode(self):
        """No token and oauth_enabled=True (no SSO headers) returns None."""
        from app.api.ws import _authenticate_ws

        db = TestSession()
        with patch("app.api.ws.config") as mock_config:
            mock_config.auth.oauth_enabled = True
            user = _authenticate_ws(token=None, db=db)
            assert user is None
        db.close()

    def test_valid_jwt_token(self):
        """Valid JWT token returns matching user."""
        from app.api.ws import _authenticate_ws

        db = TestSession()
        with patch("app.api.ws.config") as mock_config:
            mock_config.auth.oauth_enabled = True
            user = _authenticate_ws(token=TOKEN, db=db)
            assert user is not None
            assert user.email == "ws-api-test@example.com"
        db.close()

    def test_invalid_jwt_token(self):
        """Invalid JWT token returns None."""
        from app.api.ws import _authenticate_ws

        db = TestSession()
        with patch("app.api.ws.config") as mock_config:
            mock_config.auth.oauth_enabled = True
            user = _authenticate_ws(token="garbage.token.here", db=db)
            assert user is None
        db.close()

    def test_jwt_missing_email_and_sub(self):
        """JWT with no email or sub claim returns None."""
        from app.api.ws import _authenticate_ws

        db = TestSession()
        with patch("app.api.ws.config") as mock_config:
            mock_config.auth.oauth_enabled = True
            # Patch decode_jwt at the source — ws.py imports it locally
            with patch("app.core.auth.decode_jwt", return_value={"role": "user"}):
                user = _authenticate_ws(token="some-token", db=db)
                assert user is None
        db.close()


# ===================================================================
# 2. _build_snapshot unit tests
# ===================================================================
class TestBuildSnapshot:
    """Unit tests for _build_snapshot helper."""

    def test_with_deploy_progress(self):
        """Snapshot includes deploy progress data when available."""
        from app.api.ws import _build_snapshot

        pid = _create_project(USER_ID, state="deploying")
        db = TestSession()
        project = db.query(Project).filter_by(id=pid).first()

        progress = {"step": "creating-disks", "detail": "2/5"}
        with patch(
            "app.services.deploy_service._get_deploy_progress_data",
            return_value=progress,
        ), patch("app.services.ws_pubsub.get_cached_vm_states", return_value=None):
            snap = _build_snapshot(project, db)

        assert snap["type"] == "snapshot"
        assert snap["project_state"] == "deploying"
        assert snap["deploy_progress"]["step"] == "creating-disks"
        assert snap["vm_states"] == {}

    def test_deploying_with_queued_job(self):
        """Deploying state with queued job info synthesises queue progress."""
        from app.api.ws import _build_snapshot

        pid = _create_project(USER_ID, state="deploying")
        db = TestSession()
        project = db.query(Project).filter_by(id=pid).first()

        job_info = {"status": "queued", "queue_position": 3, "queue_length": 5}
        with patch(
            "app.services.deploy_service._get_deploy_progress_data",
            return_value=None,
        ), patch("app.core.redis.get_job_info", return_value=job_info), patch(
            "app.services.ws_pubsub.get_cached_vm_states", return_value=None
        ):
            snap = _build_snapshot(project, db)

        assert snap["deploy_progress"]["step"] == "queued"
        assert snap["deploy_progress"]["queue_position"] == 3
        assert snap["deploy_progress"]["queue_length"] == 5

    def test_deploying_no_job_info(self):
        """Deploying state with no progress and no job info returns None progress."""
        from app.api.ws import _build_snapshot

        pid = _create_project(USER_ID, state="deploying")
        db = TestSession()
        project = db.query(Project).filter_by(id=pid).first()

        with patch(
            "app.services.deploy_service._get_deploy_progress_data",
            return_value=None,
        ), patch("app.core.redis.get_job_info", return_value=None), patch(
            "app.services.ws_pubsub.get_cached_vm_states", return_value=None
        ):
            snap = _build_snapshot(project, db)

        assert snap["deploy_progress"] is None

    def test_with_cached_vm_states(self):
        """Snapshot includes cached VM states when available."""
        from app.api.ws import _build_snapshot

        pid = _create_project(USER_ID, state="deployed")
        db = TestSession()
        project = db.query(Project).filter_by(id=pid).first()

        cached = {
            "states": {"vm-1": "running", "vm-2": "stopped"},
            "progress": {"vm-1": {"download_pct": 50}},
        }
        with patch(
            "app.services.deploy_service._get_deploy_progress_data",
            return_value=None,
        ), patch("app.services.ws_pubsub.get_cached_vm_states", return_value=cached):
            snap = _build_snapshot(project, db)

        assert snap["vm_states"] == {"vm-1": "running", "vm-2": "stopped"}
        assert snap["vm_progress"] == {"vm-1": {"download_pct": 50}}

    def test_without_cached_vm_states(self):
        """Snapshot has empty dicts when no cached VM states."""
        from app.api.ws import _build_snapshot

        pid = _create_project(USER_ID, state="deployed")
        db = TestSession()
        project = db.query(Project).filter_by(id=pid).first()

        with patch(
            "app.services.deploy_service._get_deploy_progress_data",
            return_value=None,
        ), patch("app.services.ws_pubsub.get_cached_vm_states", return_value=None):
            snap = _build_snapshot(project, db)

        assert snap["vm_states"] == {}
        assert snap["vm_progress"] == {}

    def test_deploy_error_included(self):
        """Snapshot includes deploy_error from project model."""
        from app.api.ws import _build_snapshot

        pid = _create_project(USER_ID, state="error", deploy_error="Host unreachable")
        db = TestSession()
        project = db.query(Project).filter_by(id=pid).first()

        with patch(
            "app.services.deploy_service._get_deploy_progress_data",
            return_value=None,
        ), patch("app.services.ws_pubsub.get_cached_vm_states", return_value=None):
            snap = _build_snapshot(project, db)

        assert snap["deploy_error"] == "Host unreachable"


# ===================================================================
# 3. project_websocket route tests
# ===================================================================
class TestProjectWebSocket:
    """Integration tests for the project WebSocket endpoint."""

    def test_unauthorized_invalid_token(self):
        """Invalid token with oauth_enabled=True closes with 4001."""
        pid = _create_project(USER_ID)
        with patch("app.api.ws.config") as mock_config:
            mock_config.auth.oauth_enabled = True
            with pytest.raises(WebSocketDisconnect) as exc_info:
                with client.websocket_connect(
                    f"/api/v1/projects/{pid}/ws?token=bad-token"
                ):
                    pass
            assert exc_info.value.code == 4001

    def test_project_not_found(self):
        """Non-existent project closes with 4004."""
        fake_id = str(uuid.uuid4())
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(
                f"/api/v1/projects/{fake_id}/ws?token={TOKEN}"
            ):
                pass
        assert exc_info.value.code == 4004

    def test_access_denied_wrong_owner(self):
        """Non-owner, non-admin user gets 4003."""
        pid = _create_project(USER_ID)
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(
                f"/api/v1/projects/{pid}/ws?token={OTHER_TOKEN}"
            ):
                pass
        assert exc_info.value.code == 4003

    def test_success_owner_receives_snapshot(self):
        """Owner connects, receives initial snapshot, then disconnects."""
        pid = _create_project(USER_ID, state="deployed")
        with patch(
            "app.services.deploy_service._get_deploy_progress_data",
            return_value=None,
        ), patch("app.services.ws_pubsub.get_cached_vm_states", return_value=None):
            with client.websocket_connect(
                f"/api/v1/projects/{pid}/ws?token={TOKEN}"
            ) as ws:
                data = ws.receive_json()
                assert data["type"] == "snapshot"
                assert data["project_state"] == "deployed"
                assert data["vm_states"] == {}

    def test_success_admin_receives_snapshot(self):
        """Admin can connect to any project and receives snapshot."""
        pid = _create_project(USER_ID, state="deployed")
        with patch(
            "app.services.deploy_service._get_deploy_progress_data",
            return_value=None,
        ), patch("app.services.ws_pubsub.get_cached_vm_states", return_value=None):
            with client.websocket_connect(
                f"/api/v1/projects/{pid}/ws?token={ADMIN_TOKEN}"
            ) as ws:
                data = ws.receive_json()
                assert data["type"] == "snapshot"
                assert data["project_state"] == "deployed"

    def test_snapshot_with_progress(self):
        """Snapshot includes deploy progress when project is deploying."""
        pid = _create_project(USER_ID, state="deploying")
        progress = {"step": "starting-vms", "detail": "3/4"}
        with patch(
            "app.services.deploy_service._get_deploy_progress_data",
            return_value=progress,
        ), patch("app.services.ws_pubsub.get_cached_vm_states", return_value=None):
            with client.websocket_connect(
                f"/api/v1/projects/{pid}/ws?token={TOKEN}"
            ) as ws:
                data = ws.receive_json()
                assert data["deploy_progress"]["step"] == "starting-vms"

    def test_dev_mode_no_token(self):
        """Dev mode (oauth_enabled=False) connects without token."""
        pid = _create_project(USER_ID, state="deployed")
        # Dev mode returns the dev user (admin), who can access any project
        with patch(
            "app.services.deploy_service._get_deploy_progress_data",
            return_value=None,
        ), patch("app.services.ws_pubsub.get_cached_vm_states", return_value=None):
            with client.websocket_connect(f"/api/v1/projects/{pid}/ws") as ws:
                data = ws.receive_json()
                assert data["type"] == "snapshot"


# ===================================================================
# 4. pattern_websocket route tests
# ===================================================================
class TestPatternWebSocket:
    """Integration tests for the pattern WebSocket endpoint."""

    def test_unauthorized(self):
        """Invalid token with oauth_enabled=True closes with 4001."""
        pat_id = _create_pattern(USER_ID)
        with patch("app.api.ws.config") as mock_config:
            mock_config.auth.oauth_enabled = True
            with pytest.raises(WebSocketDisconnect) as exc_info:
                with client.websocket_connect(
                    f"/api/v1/patterns/{pat_id}/ws?token=bad-token"
                ):
                    pass
            assert exc_info.value.code == 4001

    def test_pattern_not_found(self):
        """Non-existent pattern closes with 4004."""
        fake_id = str(uuid.uuid4())
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(
                f"/api/v1/patterns/{fake_id}/ws?token={TOKEN}"
            ):
                pass
        assert exc_info.value.code == 4004

    def test_access_denied_wrong_owner(self):
        """Non-owner, non-admin gets 4004 (pattern WS merges not-found with access-denied)."""
        pat_id = _create_pattern(USER_ID)
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(
                f"/api/v1/patterns/{pat_id}/ws?token={OTHER_TOKEN}"
            ):
                pass
        assert exc_info.value.code == 4004

    def test_success_owner_connects(self):
        """Owner connects to pattern WS successfully."""
        pat_id = _create_pattern(USER_ID)
        with client.websocket_connect(
            f"/api/v1/patterns/{pat_id}/ws?token={TOKEN}"
        ) as ws:
            # Pattern WS does not send a snapshot; it stays open for notifications.
            # Send a text message to keep the connection alive, then close.
            ws.send_text("ping")

    def test_success_admin_connects(self):
        """Admin can connect to any pattern WS."""
        pat_id = _create_pattern(USER_ID)
        with client.websocket_connect(
            f"/api/v1/patterns/{pat_id}/ws?token={ADMIN_TOKEN}"
        ) as ws:
            ws.send_text("ping")

    def test_dev_mode_no_token(self):
        """Dev mode connects without token (dev user is admin)."""
        pat_id = _create_pattern(USER_ID)
        with client.websocket_connect(f"/api/v1/patterns/{pat_id}/ws") as ws:
            ws.send_text("ping")
