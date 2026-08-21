"""Tests for uncovered ws_pubsub functions to push coverage past 80%."""

from unittest.mock import MagicMock, patch


class TestNotifyProject:
    @patch("app.services.ws_pubsub._deliver_locally")
    def test_delivers_locally_and_publishes_to_redis(self, mock_deliver):
        from app.services.ws_pubsub import notify_project

        with patch("app.core.redis.is_redis_available", return_value=True):
            with patch("app.core.redis.publish") as mock_pub:
                notify_project("proj-1", {"type": "test"})
                mock_deliver.assert_called_once_with("proj-1", {"type": "test"})
                mock_pub.assert_called_once()

    @patch("app.services.ws_pubsub._deliver_locally")
    def test_redis_unavailable_still_delivers_locally(self, mock_deliver):
        from app.services.ws_pubsub import notify_project

        with patch("app.core.redis.is_redis_available", return_value=False):
            notify_project("proj-2", {"type": "test"})
            mock_deliver.assert_called_once()

    @patch("app.services.ws_pubsub._deliver_locally")
    def test_redis_exception_swallowed(self, mock_deliver):
        from app.services.ws_pubsub import notify_project

        with patch("app.core.redis.is_redis_available", side_effect=Exception("boom")):
            notify_project("proj-3", {"type": "test"})
            mock_deliver.assert_called_once()


class TestDeliverLocally:
    def test_no_loop_returns_immediately(self):
        from app.services import ws_pubsub

        old_loop = ws_pubsub._loop
        ws_pubsub._loop = None
        try:
            ws_pubsub._deliver_locally("proj-1", {"type": "test"})
        finally:
            ws_pubsub._loop = old_loop

    def test_no_subscribers_returns_immediately(self):
        from app.services import ws_pubsub

        old_loop = ws_pubsub._loop
        ws_pubsub._loop = MagicMock()
        try:
            ws_pubsub._deliver_locally("no-such-project", {"type": "test"})
        finally:
            ws_pubsub._loop = old_loop


class TestEvictStaleCacheEntries:
    def test_evicts_projects_not_in_set(self):
        from app.services import ws_pubsub

        ws_pubsub._last_states["stale-1"] = {"vm_states": {}}
        ws_pubsub._last_states["stale-2"] = {"vm_states": {}}
        ws_pubsub._last_states["keep-1"] = {"vm_states": {}}
        try:
            active = {"keep-1": MagicMock()}
            ws_pubsub._evict_stale_cache_entries(active)
            assert "keep-1" in ws_pubsub._last_states
            assert "stale-1" not in ws_pubsub._last_states
            assert "stale-2" not in ws_pubsub._last_states
        finally:
            ws_pubsub._last_states.pop("keep-1", None)


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
    @patch("app.services.ws_pubsub._batch_fetch_vm_states", return_value=({}, {}, {}))
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
    @patch("app.services.ws_pubsub._batch_fetch_vm_states", return_value=({}, {}, {}))
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
