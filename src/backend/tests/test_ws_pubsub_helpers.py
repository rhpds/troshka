"""Tests for extracted helpers in ws_pubsub.py."""

import json
import logging
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# _normalize_vm_state
# ---------------------------------------------------------------------------
class TestNormalizeVmState:
    def test_shut_off_maps_to_stopped(self):
        from app.services.ws_pubsub import _normalize_vm_state

        assert _normalize_vm_state("shut_off") == "stopped"

    def test_shutting_down_maps_to_stopped(self):
        from app.services.ws_pubsub import _normalize_vm_state

        assert _normalize_vm_state("shutting_down") == "stopped"

    def test_crashed_maps_to_stopped(self):
        from app.services.ws_pubsub import _normalize_vm_state

        assert _normalize_vm_state("crashed") == "stopped"

    def test_suspended_maps_to_stopped(self):
        from app.services.ws_pubsub import _normalize_vm_state

        assert _normalize_vm_state("suspended") == "stopped"

    def test_paused_maps_to_stopped(self):
        from app.services.ws_pubsub import _normalize_vm_state

        assert _normalize_vm_state("paused") == "stopped"

    def test_kubevirt_stopped_maps_to_stopped(self):
        from app.services.ws_pubsub import _normalize_vm_state

        assert _normalize_vm_state("Stopped") == "stopped"

    def test_running_maps_to_running(self):
        from app.services.ws_pubsub import _normalize_vm_state

        assert _normalize_vm_state("Running") == "running"

    def test_unknown_state_passes_through(self):
        from app.services.ws_pubsub import _normalize_vm_state

        assert _normalize_vm_state("running") == "running"
        assert _normalize_vm_state("migrating") == "migrating"
        assert _normalize_vm_state("redeploying") == "redeploying"

    def test_empty_string_passes_through(self):
        from app.services.ws_pubsub import _normalize_vm_state

        assert _normalize_vm_state("") == ""


# ---------------------------------------------------------------------------
# _dispatch_pubsub_message
# ---------------------------------------------------------------------------
class TestDispatchPubsubMessage:
    @patch("app.services.ws_pubsub._deliver_locally")
    def test_ignores_non_pmessage(self, mock_deliver):
        from app.services.ws_pubsub import _dispatch_pubsub_message

        _dispatch_pubsub_message({"type": "subscribe", "channel": "x", "data": "{}"})
        mock_deliver.assert_not_called()

    @patch("app.services.ws_pubsub._deliver_locally")
    def test_ignores_non_project_channel(self, mock_deliver):
        from app.services.ws_pubsub import _dispatch_pubsub_message

        _dispatch_pubsub_message(
            {"type": "pmessage", "channel": "other:abc", "data": "{}"}
        )
        mock_deliver.assert_not_called()

    @patch("app.services.ws_pubsub._deliver_locally")
    def test_delivers_valid_project_message(self, mock_deliver):
        from app.services.ws_pubsub import _dispatch_pubsub_message

        data = json.dumps({"type": "vm-state", "states": {}})
        _dispatch_pubsub_message(
            {"type": "pmessage", "channel": "project:proj-123", "data": data}
        )
        mock_deliver.assert_called_once_with(
            "proj-123", {"type": "vm-state", "states": {}}
        )

    @patch("app.services.ws_pubsub._deliver_locally")
    def test_handles_bytes_channel(self, mock_deliver):
        from app.services.ws_pubsub import _dispatch_pubsub_message

        data = json.dumps({"type": "project-state"})
        _dispatch_pubsub_message(
            {"type": "pmessage", "channel": b"project:proj-456", "data": data}
        )
        mock_deliver.assert_called_once_with("proj-456", {"type": "project-state"})

    @patch("app.services.ws_pubsub._deliver_locally")
    def test_ignores_invalid_json_data(self, mock_deliver):
        from app.services.ws_pubsub import _dispatch_pubsub_message

        _dispatch_pubsub_message(
            {"type": "pmessage", "channel": "project:proj-1", "data": "not json{"}
        )
        mock_deliver.assert_not_called()

    @patch("app.services.ws_pubsub._deliver_locally")
    def test_handles_pattern_channel(self, mock_deliver):
        from app.services.ws_pubsub import _dispatch_pubsub_message

        data = json.dumps({"type": "capture-progress"})
        _dispatch_pubsub_message(
            {
                "type": "pmessage",
                "channel": "project:pattern:pat-789",
                "data": data,
            }
        )
        mock_deliver.assert_called_once_with(
            "pattern:pat-789", {"type": "capture-progress"}
        )


# ---------------------------------------------------------------------------
# _batch_fetch_vm_states
# ---------------------------------------------------------------------------
class TestBatchFetchVmStates:
    @patch("app.services.ws_pubsub._fetch_troshkad_host_states")
    @patch("app.services.ws_pubsub._fetch_kubevirt_vm_states")
    def test_skips_deploying_hosts(self, mock_kv, mock_troshkad):
        from app.services.ws_pubsub import _batch_fetch_vm_states

        proj = MagicMock()
        proj.id = "p1"
        proj.host_id = "h1"
        projects = {"p1": proj}
        deploying = {"h1"}
        db = MagicMock()

        host_states, proj_states, container_states = _batch_fetch_vm_states(
            projects, deploying, db
        )
        assert host_states == {}
        assert proj_states == {}
        assert container_states == {}
        mock_troshkad.assert_not_called()
        mock_kv.assert_not_called()

    @patch("app.services.ws_pubsub._fetch_troshkad_host_states")
    @patch("app.services.ws_pubsub._fetch_kubevirt_vm_states", return_value=None)
    def test_kubevirt_cluster_uses_kv_fetcher(self, mock_kv, mock_troshkad):
        from app.services.ws_pubsub import _batch_fetch_vm_states

        proj = MagicMock()
        proj.id = "p1"
        proj.host_id = "h1"
        proj.host_assignments = None
        projects = {"p1": proj}

        host = MagicMock()
        host.ip_address = "10.0.0.1"
        host.host_type = "kubevirt-cluster"
        host.provider_id = "prov1"
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = host

        host_states, proj_states, container_states = _batch_fetch_vm_states(
            projects, set(), db
        )
        mock_kv.assert_called_once()
        mock_troshkad.assert_not_called()

    @patch("app.services.ws_pubsub._fetch_troshkad_host_states")
    def test_troshkad_host_uses_troshkad_fetcher(self, mock_troshkad):
        from app.services.ws_pubsub import _batch_fetch_vm_states

        proj = MagicMock()
        proj.id = "p1"
        proj.host_id = "h1"
        proj.host_assignments = None
        projects = {"p1": proj}

        host = MagicMock()
        host.id = "h1"
        host.ip_address = "10.0.0.1"
        host.host_type = "ec2"
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = host

        _batch_fetch_vm_states(projects, set(), db)
        mock_troshkad.assert_called_once()

    @patch("app.services.ws_pubsub._fetch_troshkad_host_states")
    def test_skips_host_without_ip(self, mock_troshkad):
        from app.services.ws_pubsub import _batch_fetch_vm_states

        proj = MagicMock()
        proj.id = "p1"
        proj.host_id = "h1"
        projects = {"p1": proj}

        host = MagicMock()
        host.ip_address = None
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = host

        _batch_fetch_vm_states(projects, set(), db)
        mock_troshkad.assert_not_called()

    @patch("app.services.ws_pubsub._fetch_troshkad_host_states")
    def test_skips_when_host_not_found(self, mock_troshkad):
        from app.services.ws_pubsub import _batch_fetch_vm_states

        proj = MagicMock()
        proj.id = "p1"
        proj.host_id = "h1"
        projects = {"p1": proj}

        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = None

        _batch_fetch_vm_states(projects, set(), db)
        mock_troshkad.assert_not_called()


# ---------------------------------------------------------------------------
# _notify_all_projects
# ---------------------------------------------------------------------------
class TestNotifyAllProjects:
    @patch("app.services.ws_pubsub._check_and_notify_project_changes")
    @patch("app.services.ws_pubsub._map_vm_states_for_project")
    @patch("app.services.deploy_service._get_deploy_progress_data", return_value=None)
    def test_active_project_maps_states(self, mock_dp, mock_map, mock_notify):
        from app.services.ws_pubsub import _last_states, _notify_all_projects

        mock_map.return_value = ({"vm1": "running"}, {}, {})
        proj = MagicMock()
        proj.id = "p1"
        proj.host_id = "h1"
        proj.state = "active"
        projects = {"p1": proj}

        _notify_all_projects(projects, {"h1": {"dom": "running"}}, {})
        mock_map.assert_called_once()
        mock_notify.assert_called_once()
        # cleanup
        _last_states.pop("p1", None)

    @patch("app.services.ws_pubsub._check_and_notify_project_changes")
    @patch("app.services.ws_pubsub._map_vm_states_for_project")
    @patch("app.services.deploy_service._get_deploy_progress_data", return_value=None)
    def test_deploying_project_skips_vm_states(self, mock_dp, mock_map, mock_notify):
        from app.services.ws_pubsub import _last_states, _notify_all_projects

        proj = MagicMock()
        proj.id = "p1"
        proj.host_id = "h1"
        proj.state = "deploying"
        projects = {"p1": proj}

        _notify_all_projects(projects, {}, {})
        mock_map.assert_not_called()
        # Still notified with empty states
        mock_notify.assert_called_once()
        args = mock_notify.call_args[0]
        assert args[3] == {}  # vm_states empty
        # cleanup
        _last_states.pop("p1", None)

    @patch("app.services.ws_pubsub._check_and_notify_project_changes")
    @patch("app.services.ws_pubsub._map_vm_states_for_project")
    @patch("app.services.deploy_service._get_deploy_progress_data", return_value=None)
    def test_evicts_stale_cache(self, mock_dp, mock_map, mock_notify):
        from app.services.ws_pubsub import _last_states, _notify_all_projects

        # Pre-populate with a stale entry
        _last_states["stale-project"] = {"project_state": "active"}
        mock_map.return_value = ({}, {}, {})

        proj = MagicMock()
        proj.id = "p1"
        proj.host_id = "h1"
        proj.state = "active"
        projects = {"p1": proj}

        _notify_all_projects(projects, {}, {})
        assert "stale-project" not in _last_states
        # cleanup
        _last_states.pop("p1", None)


# ---------------------------------------------------------------------------
# _log_vm_state_changes
# ---------------------------------------------------------------------------
class TestLogVmStateChanges:
    def test_logs_state_transition(self, caplog):
        from app.services.ws_pubsub import _log_vm_state_changes

        project = MagicMock()
        project.name = "test-project"
        project.topology = {
            "nodes": [{"id": "vm1", "type": "vmNode", "data": {"label": "bastion"}}]
        }
        with caplog.at_level(logging.INFO, logger="app.services.ws_pubsub"):
            _log_vm_state_changes(project, {"vm1": "running"}, {"vm1": "stopped"})
        assert "stopped" in caplog.text
        assert "running" in caplog.text
        assert "bastion" in caplog.text

    def test_no_log_when_same_state(self, caplog):
        from app.services.ws_pubsub import _log_vm_state_changes

        project = MagicMock()
        project.name = "test-project"
        project.topology = {
            "nodes": [{"id": "vm1", "type": "vmNode", "data": {"label": "x"}}]
        }
        with caplog.at_level(logging.INFO, logger="app.services.ws_pubsub"):
            _log_vm_state_changes(project, {"vm1": "running"}, {"vm1": "running"})
        assert caplog.text == ""

    def test_no_log_when_no_previous_state(self, caplog):
        from app.services.ws_pubsub import _log_vm_state_changes

        project = MagicMock()
        project.name = "test-project"
        project.topology = {
            "nodes": [{"id": "vm1", "type": "vmNode", "data": {"label": "x"}}]
        }
        with caplog.at_level(logging.INFO, logger="app.services.ws_pubsub"):
            _log_vm_state_changes(project, {"vm1": "running"}, {})
        assert caplog.text == ""

    def test_handles_missing_node_in_topology(self, caplog):
        from app.services.ws_pubsub import _log_vm_state_changes

        project = MagicMock()
        project.name = "test-project"
        project.topology = {"nodes": []}
        with caplog.at_level(logging.INFO, logger="app.services.ws_pubsub"):
            _log_vm_state_changes(project, {"vm1": "running"}, {"vm1": "stopped"})
        # Should still log with truncated vm_id
        assert "stopped" in caplog.text
        assert "running" in caplog.text


# ── get_cached_vm_states tests ──


class TestGetCachedVmStates:
    def test_returns_none_when_not_cached(self):
        from app.services.ws_pubsub import _last_states, get_cached_vm_states

        _last_states.pop("proj-missing", None)
        result = get_cached_vm_states("proj-missing")
        assert result is None

    def test_returns_cached_states(self):
        from app.services.ws_pubsub import _last_states, get_cached_vm_states

        _last_states["proj-1"] = {
            "vm_states": {"vm1": "running"},
            "container_states": {"c1": "running"},
            "vm_progress": {},
        }
        try:
            result = get_cached_vm_states("proj-1")
            assert result is not None
            assert result["states"]["vm1"] == "running"
            assert result["container_states"]["c1"] == "running"
        finally:
            _last_states.pop("proj-1", None)

    def test_returns_none_for_empty_dict(self):
        from app.services.ws_pubsub import _last_states, get_cached_vm_states

        _last_states["proj-empty"] = {}
        try:
            result = get_cached_vm_states("proj-empty")
            # Empty dict is falsy, so should return None
            assert result is None
        finally:
            _last_states.pop("proj-empty", None)


# ── notify_project tests ──


class TestNotifyProject:
    @patch("app.services.ws_pubsub._deliver_locally")
    def test_delivers_locally(self, mock_deliver):
        from app.services.ws_pubsub import notify_project

        notify_project("proj-1", {"type": "test"})
        mock_deliver.assert_called_once_with("proj-1", {"type": "test"})

    @patch("app.services.ws_pubsub._deliver_locally")
    @patch("app.core.redis.is_redis_available", return_value=True)
    @patch("app.core.redis.publish")
    def test_publishes_to_redis(self, mock_publish, mock_avail, mock_deliver):
        from app.services.ws_pubsub import notify_project

        notify_project("proj-1", {"type": "test"})
        mock_publish.assert_called_once_with("project:proj-1", {"type": "test"})

    @patch("app.services.ws_pubsub._deliver_locally")
    @patch("app.core.redis.is_redis_available", return_value=False)
    def test_skips_redis_when_unavailable(self, mock_avail, mock_deliver):
        from app.services.ws_pubsub import notify_project

        notify_project("proj-1", {"type": "test"})
        mock_deliver.assert_called_once()


# ── notify_pattern / subscribe_pattern / unsubscribe_pattern tests ──


class TestPatternPubSub:
    @patch("app.services.ws_pubsub.notify_project")
    def test_notify_pattern_delegates(self, mock_notify):
        from app.services.ws_pubsub import notify_pattern

        notify_pattern("pat-1", {"type": "progress"})
        mock_notify.assert_called_once_with("pattern:pat-1", {"type": "progress"})

    @patch("app.services.ws_pubsub.subscribe")
    def test_subscribe_pattern_delegates(self, mock_sub):
        from app.services.ws_pubsub import subscribe_pattern

        ws = MagicMock()
        subscribe_pattern("pat-1", ws)
        mock_sub.assert_called_once_with("pattern:pat-1", ws)

    @patch("app.services.ws_pubsub.unsubscribe")
    def test_unsubscribe_pattern_delegates(self, mock_unsub):
        from app.services.ws_pubsub import unsubscribe_pattern

        ws = MagicMock()
        unsubscribe_pattern("pat-1", ws)
        mock_unsub.assert_called_once_with("pattern:pat-1", ws)


# ── start_redis_listener tests ──


class TestStartRedisListener:
    def test_starts_only_once(self):
        import app.services.ws_pubsub as pubsub

        original = pubsub._redis_listener_started
        pubsub._redis_listener_started = False
        try:
            with patch("threading.Thread") as mock_thread:
                pubsub.start_redis_listener()
                mock_thread.assert_called_once()
                assert pubsub._redis_listener_started is True

                # Second call should be a no-op
                mock_thread.reset_mock()
                pubsub.start_redis_listener()
                mock_thread.assert_not_called()
        finally:
            pubsub._redis_listener_started = original


# ── _fetch_troshkad_host_states tests ──


class TestFetchTroshkadHostStates:
    def test_skips_disconnected_host(self):
        from app.services.ws_pubsub import _fetch_troshkad_host_states

        host = MagicMock()
        host.agent_status = "disconnected"
        host.id = "host-1"
        cache = {}
        ctr_cache = {}
        _fetch_troshkad_host_states(host, cache, ctr_cache)
        assert "host-1" not in cache
        assert "host-1" not in ctr_cache

    def test_skips_already_cached(self):
        from app.services.ws_pubsub import _fetch_troshkad_host_states

        host = MagicMock()
        host.agent_status = "connected"
        host.id = "host-1"
        cache = {"host-1": {"vm1": "running"}}
        ctr_cache = {"host-1": {"ctr1": {"state": "running"}}}
        _fetch_troshkad_host_states(host, cache, ctr_cache)
        # Should not overwrite
        assert cache["host-1"] == {"vm1": "running"}
        assert ctr_cache["host-1"] == {"ctr1": {"state": "running"}}

    @patch("app.services.troshkad_client.get_all_vm_states")
    def test_fetches_and_caches(self, mock_get):
        from app.services.ws_pubsub import _fetch_troshkad_host_states

        mock_get.return_value = {"vm1": "running", "vm2": "stopped"}
        host = MagicMock()
        host.agent_status = "connected"
        host.id = "host-2"
        cache = {}
        _fetch_troshkad_host_states(host, cache, {})
        assert cache["host-2"]["vm1"] == "running"

    @patch(
        "app.services.troshkad_client.get_all_vm_states", side_effect=Exception("fail")
    )
    def test_handles_fetch_error(self, mock_get):
        from app.services.ws_pubsub import _fetch_troshkad_host_states

        host = MagicMock()
        host.agent_status = "connected"
        host.id = "host-3"
        cache = {}
        # Should not raise
        _fetch_troshkad_host_states(host, cache, {})
        assert "host-3" not in cache

    @patch("app.services.troshkad_client.get_all_container_states")
    @patch("app.services.troshkad_client.get_all_vm_states")
    def test_fetches_and_caches_container_states(self, mock_vm, mock_ctr):
        from app.services.ws_pubsub import _fetch_troshkad_host_states

        mock_vm.return_value = {"vm1": "running"}
        mock_ctr.return_value = {"ctr1": {"state": "running", "ips": ["10.0.0.10"]}}
        host = MagicMock()
        host.agent_status = "connected"
        host.id = "host-4"
        host_cache = {}
        ctr_cache = {}
        _fetch_troshkad_host_states(host, host_cache, ctr_cache)
        assert ctr_cache["host-4"]["ctr1"]["state"] == "running"

    @patch(
        "app.services.troshkad_client.get_all_container_states",
        side_effect=Exception("fail"),
    )
    @patch("app.services.troshkad_client.get_all_vm_states", return_value={})
    def test_handles_container_fetch_error(self, mock_vm, mock_ctr):
        from app.services.ws_pubsub import _fetch_troshkad_host_states

        host = MagicMock()
        host.agent_status = "connected"
        host.id = "host-5"
        ctr_cache = {}
        # Should not raise
        _fetch_troshkad_host_states(host, {}, ctr_cache)
        assert "host-5" not in ctr_cache


# ── _maybe_scan_ocp_monitors tests ──


class TestMaybeScanOcpMonitors:
    @patch("app.core.database.SessionLocal")
    def test_scans_when_interval_elapsed(self, mock_session_cls):
        import app.services.ws_pubsub as pubsub

        old_last = pubsub._last_ocp_scan
        pubsub._last_ocp_scan = 0.0  # force elapsed
        try:
            MagicMock()
            proj_tuple = ("proj-1",)

            mock_db = MagicMock()
            mock_db.query.return_value.filter.return_value.all.return_value = [
                proj_tuple
            ]
            mock_session_cls.return_value = mock_db

            with patch(
                "app.services.deploy_service.maybe_start_ocp_health_monitor"
            ) as mock_start:
                pubsub._maybe_scan_ocp_monitors()
                mock_start.assert_called_once_with("proj-1")
        finally:
            pubsub._last_ocp_scan = old_last

    def test_skips_when_interval_not_elapsed(self):
        import time

        import app.services.ws_pubsub as pubsub

        old_last = pubsub._last_ocp_scan
        pubsub._last_ocp_scan = time.time()  # just happened
        try:
            with patch("app.core.database.SessionLocal") as mock_session_cls:
                pubsub._maybe_scan_ocp_monitors()
                mock_session_cls.assert_not_called()
        finally:
            pubsub._last_ocp_scan = old_last
