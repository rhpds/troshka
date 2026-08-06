"""Tests for uncovered paths in project_timer.py.

Covers:
  - _notify success and failure
  - _delete_project — active project, stopped project, EIP cleanup
  - start_project_timer
  - auto-delete warning path
  - non-dry-run auto-stop firing
"""

import datetime
from unittest.mock import MagicMock, patch

from app.core.auth import hash_password
from app.models.project import Project
from app.models.user import User
from app.services.project_timer import (
    _delete_project,
    _notify,
    start_project_timer,
)
from tests.conftest import TestSession

# ═══════════════════════════════════════════════════════════════════════════
# _notify
# ═══════════════════════════════════════════════════════════════════════════


class TestNotify:
    @patch("app.services.ws_pubsub.notify_project")
    def test_success(self, mock_ws):
        _notify("proj-12345678", {"type": "test"})
        mock_ws.assert_called_once_with("proj-12345678", {"type": "test"})

    @patch("app.services.ws_pubsub.notify_project", side_effect=Exception("ws down"))
    def test_exception_swallowed(self, mock_ws):
        # Should not raise
        _notify("proj-12345678", {"type": "test"})


# ═══════════════════════════════════════════════════════════════════════════
# _delete_project
# ═══════════════════════════════════════════════════════════════════════════


class TestDeleteProject:
    @patch("app.services.project_timer._notify")
    @patch("app.core.redis.enqueue_job")
    @patch("app.services.deploy_service.stop_project_async")
    def test_active_project_stops_first(self, mock_stop, mock_enqueue, mock_notify):
        s = MagicMock()
        project = MagicMock()
        project.id = "proj-del-active"
        project.state = "active"
        project.host_id = "host-1"
        project.vni_map = {}
        project.deployed_topology = {}
        project.topology = {}
        project.dns_provider_id = None
        project.domain = None

        # After stop_project_async, refresh sets state to stopped
        def _refresh(p):
            p.state = "stopped"

        s.refresh.side_effect = _refresh
        s.query.return_value.filter_by.return_value.all.return_value = []

        _delete_project(s, project)
        mock_stop.assert_called_once_with("proj-del-active")
        s.delete.assert_called_once_with(project)
        s.commit.assert_called()

    @patch("app.services.project_timer._notify")
    @patch("app.core.redis.enqueue_job")
    def test_stopped_project_enqueues_destroy(self, mock_enqueue, mock_notify):
        s = MagicMock()
        project = MagicMock()
        project.id = "proj-del-stopped"
        project.state = "stopped"
        project.host_id = "host-1"
        project.vni_map = {"n1": 100}
        project.deployed_topology = {"nodes": []}
        project.topology = None
        project.dns_provider_id = "dns-1"
        project.domain = "example.com"
        s.query.return_value.filter_by.return_value.all.return_value = []

        _delete_project(s, project)
        mock_enqueue.assert_called()
        s.delete.assert_called_once_with(project)

    @patch("app.services.project_timer._notify")
    @patch("app.services.eip_service.release_eip")
    def test_releases_eips(self, mock_release, mock_notify):
        s = MagicMock()
        project = MagicMock()
        project.id = "proj-del-eip"
        project.state = "draft"
        project.host_id = None
        eip = MagicMock()
        eip.public_ip = "1.2.3.4"
        s.query.return_value.filter_by.return_value.all.return_value = [eip]

        _delete_project(s, project)
        mock_release.assert_called_once_with(s, eip)

    @patch("app.services.project_timer._notify")
    @patch("app.services.eip_service.release_eip", side_effect=Exception("err"))
    def test_eip_release_failure_handled(self, mock_release, mock_notify):
        s = MagicMock()
        project = MagicMock()
        project.id = "proj-del-eip2"
        project.state = "draft"
        project.host_id = None
        eip = MagicMock()
        eip.public_ip = "5.6.7.8"
        s.query.return_value.filter_by.return_value.all.return_value = [eip]
        _delete_project(s, project)
        s.delete.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════
# start_project_timer
# ═══════════════════════════════════════════════════════════════════════════


class TestStartProjectTimer:
    @patch("app.services.project_timer._timer_loop")
    def test_returns_daemon_thread(self, mock_loop):
        mock_loop.side_effect = lambda: None
        thread = start_project_timer()
        assert thread.daemon is True
        assert thread.name == "project-timer"
        thread.join(timeout=1)


# ═══════════════════════════════════════════════════════════════════════════
# auto-delete warning path
# ═══════════════════════════════════════════════════════════════════════════


class TestAutoDeleteWarning:
    def test_auto_delete_warning_dry_run(self):
        """Projects within 5 min of lifetime expiry get warned."""
        from app.services.project_timer import _check_project_timers

        _db = TestSession()
        _user = _db.query(User).filter_by(email="timer-test@example.com").first()
        if not _user:
            _user = User(
                email="timer-test@example.com",
                display_name="Timer Test",
                role="user",
                auth_source="local",
                password_hash=hash_password("pass"),
            )
            _db.add(_user)
            _db.commit()
            _db.refresh(_user)
        user_id = _user.id

        now = datetime.datetime.now(datetime.UTC)
        p = Project(
            name="Auto Delete Warning Test",
            owner_id=user_id,
            state="stopped",
            auto_delete_minutes=60,
            auto_delete_started_at=now - datetime.timedelta(minutes=57),
            lifetime_expires_at=now + datetime.timedelta(minutes=3),
        )
        _db.add(p)
        _db.commit()
        _db.refresh(p)
        pid = p.id
        _db.close()

        result = _check_project_timers(_dry_run=True)
        assert pid in result["auto_delete_warned"]

        _db2 = TestSession()
        _db2.query(Project).filter_by(id=pid).delete()
        _db2.commit()
        _db2.close()


# ═══════════════════════════════════════════════════════════════════════════
# non-dry-run auto-stop
# ═══════════════════════════════════════════════════════════════════════════


class TestAutoStopNonDryRun:
    @patch("app.services.project_timer._spawn_stop")
    @patch("app.services.project_timer._notify")
    def test_fires_auto_stop(self, mock_notify, mock_spawn):
        from app.services.project_timer import _check_project_timers

        _db = TestSession()
        _user = _db.query(User).filter_by(email="timer-test@example.com").first()
        if not _user:
            _user = User(
                email="timer-test@example.com",
                display_name="Timer Test",
                role="user",
                auth_source="local",
                password_hash=hash_password("pass"),
            )
            _db.add(_user)
            _db.commit()
            _db.refresh(_user)
        user_id = _user.id

        now = datetime.datetime.now(datetime.UTC)
        p = Project(
            name="Auto Stop Non-Dry Run",
            owner_id=user_id,
            state="active",
            auto_stop_minutes=60,
            auto_stop_started_at=now - datetime.timedelta(hours=2),
            auto_stop_expires_at=now - datetime.timedelta(hours=1),
        )
        _db.add(p)
        _db.commit()
        _db.refresh(p)
        pid = p.id
        _db.close()

        result = _check_project_timers(_dry_run=False)
        assert pid in result["auto_stop"]
        mock_spawn.assert_called()

        # Verify state was changed
        _db2 = TestSession()
        proj = _db2.query(Project).filter_by(id=pid).first()
        assert proj.state == "stopping"
        assert proj.auto_stopped is True
        _db2.query(Project).filter_by(id=pid).delete()
        _db2.commit()
        _db2.close()
