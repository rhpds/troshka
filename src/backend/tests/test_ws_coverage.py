"""Tests for uncovered ws_pubsub functions to push coverage past 80%."""

from unittest.mock import MagicMock, patch


class TestGetCachedVmStates:
    def test_returns_none_when_empty(self):
        from app.services.ws_pubsub import get_cached_vm_states

        result = get_cached_vm_states("nonexistent-project")
        assert result is None

    def test_returns_cached_states(self):
        from app.services import ws_pubsub

        ws_pubsub._last_states["test-proj"] = {
            "vm_states": {"vm1": "running"},
            "container_states": {"c1": "running"},
            "vm_progress": {},
        }
        try:
            result = ws_pubsub.get_cached_vm_states("test-proj")
            assert result is not None
            assert result["states"]["vm1"] == "running"
            assert result["container_states"]["c1"] == "running"
        finally:
            ws_pubsub._last_states.pop("test-proj", None)


class TestPollActiveProjects:
    @patch("app.services.ws_pubsub._notify_all_projects")
    @patch("app.services.ws_pubsub._batch_fetch_vm_states", return_value=({}, {}))
    @patch("app.services.deploy_service._get_deploy_progress_data", return_value=None)
    @patch("app.core.database.SessionLocal")
    def test_fetches_and_notifies(self, mock_sl, mock_prog, mock_batch, mock_notify):
        from app.services.ws_pubsub import _poll_active_projects

        proj = MagicMock()
        proj.id = "p1"
        proj.host_id = "h1"
        proj.host_assignments = None
        proj.state = "active"

        db = MagicMock()
        mock_sl.return_value = db
        db.query.return_value.filter.return_value.all.return_value = [proj]

        _poll_active_projects()

        mock_batch.assert_called_once()
        mock_notify.assert_called_once()
        db.close.assert_called_once()

    @patch("app.core.database.SessionLocal")
    def test_returns_early_when_no_projects(self, mock_sl):
        from app.services.ws_pubsub import _poll_active_projects

        db = MagicMock()
        mock_sl.return_value = db
        db.query.return_value.filter.return_value.all.return_value = []

        _poll_active_projects()
        db.close.assert_called_once()

    @patch("app.services.ws_pubsub._notify_all_projects")
    @patch("app.services.ws_pubsub._batch_fetch_vm_states", return_value=({}, {}))
    @patch("app.services.deploy_service._get_deploy_progress_data")
    @patch("app.core.database.SessionLocal")
    def test_tracks_deploying_hosts(self, mock_sl, mock_prog, mock_batch, mock_notify):
        from app.services.ws_pubsub import _poll_active_projects

        proj = MagicMock()
        proj.id = "p1"
        proj.host_id = "h1"
        proj.host_assignments = {"h2": "h2"}
        proj.state = "active"
        mock_prog.return_value = {"step": "downloading"}

        db = MagicMock()
        mock_sl.return_value = db
        db.query.return_value.filter.return_value.all.return_value = [proj]

        _poll_active_projects()
        mock_batch.assert_called_once()


class TestStartStatePoller:
    @patch("app.services.ws_pubsub.threading.Thread")
    def test_starts_daemon_thread(self, mock_thread_cls):
        from app.services.ws_pubsub import start_state_poller

        mock_thread = MagicMock()
        mock_thread_cls.return_value = mock_thread

        result = start_state_poller()

        mock_thread_cls.assert_called_once()
        assert mock_thread_cls.call_args[1]["daemon"] is True
        mock_thread.start.assert_called_once()
        assert result is mock_thread
