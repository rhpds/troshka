"""Tests for extracted helper functions in ws_pubsub.py.

Covers _map_vm_states_for_project, _fetch_kubevirt_vm_states,
and _check_and_notify_project_changes.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.services.ws_pubsub import (
    _check_and_notify_project_changes,
    _container_domain_name,
    _fetch_kubevirt_vm_states,
    _last_states,
    _map_container_states_for_project,
    _map_vm_states_for_project,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_project(
    project_id="proj-1111-2222-3333",
    name="test-project",
    state="active",
    deploy_error=None,
    nodes=None,
):
    """Build a mock project with topology containing vmNodes."""
    if nodes is None:
        nodes = [
            {
                "id": "vm-aaaa-1111",
                "type": "vmNode",
                "data": {"id": "vm-aaaa-1111", "label": "bastion"},
            },
            {
                "id": "vm-bbbb-2222",
                "type": "vmNode",
                "data": {"id": "vm-bbbb-2222", "label": "worker"},
            },
            {
                "id": "net-cccc-3333",
                "type": "networkNode",
                "data": {"id": "net-cccc-3333"},
            },
        ]
    p = SimpleNamespace(
        id=project_id,
        name=name,
        state=state,
        deploy_error=deploy_error,
        topology={"nodes": nodes},
    )
    return p


def _domain_name(project_id, node_id):
    return f"troshka-{project_id[:8]}-{node_id[:8]}"


# ===========================================================================
# _map_vm_states_for_project
# ===========================================================================


class TestMapVmStatesForProject:
    @patch("app.api.projects._redeploy_progress", {})
    @patch("app.api.projects._domain_name", side_effect=_domain_name)
    def test_host_batch_maps_states(self, mock_dn):
        project = _make_project()
        dom_a = _domain_name(project.id, "vm-aaaa-1111")
        dom_b = _domain_name(project.id, "vm-bbbb-2222")
        host_batch = {dom_a: "running", dom_b: "shut_off"}

        vm_states, vm_progress, vm_boot_devs = _map_vm_states_for_project(
            project, host_batch, kv_batch=None
        )

        assert vm_states["vm-aaaa-1111"] == "running"
        # shut_off normalised to "stopped"
        assert vm_states["vm-bbbb-2222"] == "stopped"
        assert vm_progress == {}
        assert vm_boot_devs == {}

    @patch("app.api.projects._redeploy_progress", {})
    @patch("app.api.projects._domain_name", side_effect=_domain_name)
    def test_kubevirt_batch_maps_states(self, mock_dn):
        project = _make_project()
        kv_batch = {"vm-aaaa-1111": "Running", "vm-bbbb-2222": "Stopped"}

        vm_states, vm_progress, _ = _map_vm_states_for_project(
            project, host_batch=None, kv_batch=kv_batch
        )

        # Running normalised to "running"
        assert vm_states["vm-aaaa-1111"] == "running"
        # Stopped normalised to "stopped"
        assert vm_states["vm-bbbb-2222"] == "stopped"

    @patch("app.api.projects._redeploy_progress", {})
    @patch("app.api.projects._domain_name", side_effect=_domain_name)
    def test_kubevirt_batch_preferred_over_host_batch(self, mock_dn):
        """When kv_batch is provided, host_batch is ignored."""
        project = _make_project()
        host_batch = {_domain_name(project.id, "vm-aaaa-1111"): "running"}
        kv_batch = {"vm-aaaa-1111": "Stopped"}

        vm_states, _, _ = _map_vm_states_for_project(project, host_batch, kv_batch)

        # kv_batch wins
        assert vm_states["vm-aaaa-1111"] == "stopped"

    @patch("app.api.projects._redeploy_progress", {})
    @patch("app.api.projects._domain_name", side_effect=_domain_name)
    def test_both_batches_none_returns_empty(self, mock_dn):
        project = _make_project()
        vm_states, vm_progress, vm_boot_devs = _map_vm_states_for_project(
            project, host_batch=None, kv_batch=None
        )
        assert vm_states == {}
        assert vm_progress == {}
        assert vm_boot_devs == {}

    @patch("app.api.projects._redeploy_progress", {})
    @patch("app.api.projects._domain_name", side_effect=_domain_name)
    def test_not_found_domains_skipped(self, mock_dn):
        """VMs whose domain is not in the host batch are silently skipped."""
        project = _make_project()
        # Only one VM present in batch
        dom_a = _domain_name(project.id, "vm-aaaa-1111")
        host_batch = {dom_a: "running"}

        vm_states, _, _ = _map_vm_states_for_project(project, host_batch, kv_batch=None)

        assert "vm-aaaa-1111" in vm_states
        assert "vm-bbbb-2222" not in vm_states

    @patch("app.api.projects._domain_name", side_effect=_domain_name)
    def test_redeploying_state_from_redeploy_progress(self, mock_dn):
        """VM in _redeploy_progress gets 'redeploying' state + progress."""
        project = _make_project()
        dom_a = _domain_name(project.id, "vm-aaaa-1111")
        progress_data = {"step": "downloading", "pct": 42}

        with patch(
            "app.api.projects._redeploy_progress",
            {dom_a: progress_data},
        ):
            host_batch = {dom_a: "running"}
            vm_states, vm_progress, _ = _map_vm_states_for_project(
                project, host_batch, kv_batch=None
            )

        assert vm_states["vm-aaaa-1111"] == "redeploying"
        assert vm_progress["vm-aaaa-1111"] == progress_data

    @patch("app.api.projects._redeploy_progress", {})
    @patch("app.api.projects._domain_name", side_effect=_domain_name)
    def test_all_shutdown_states_normalised(self, mock_dn):
        """All libvirt/KubeVirt shutdown-like states map to 'stopped'."""
        shutdown_states = [
            "shut_off",
            "shutting_down",
            "crashed",
            "suspended",
            "paused",
            "Stopped",
        ]
        for raw_state in shutdown_states:
            project = _make_project(
                nodes=[
                    {
                        "id": "vm-xxxx",
                        "type": "vmNode",
                        "data": {"id": "vm-xxxx"},
                    }
                ]
            )
            dom = _domain_name(project.id, "vm-xxxx")
            host_batch = {dom: raw_state}
            vm_states, _, _ = _map_vm_states_for_project(
                project, host_batch, kv_batch=None
            )
            assert (
                vm_states.get("vm-xxxx") == "stopped"
            ), f"{raw_state} should map to 'stopped'"

    @patch("app.api.projects._redeploy_progress", {})
    @patch("app.api.projects._domain_name", side_effect=_domain_name)
    def test_non_vm_nodes_ignored(self, mock_dn):
        """Only vmNode entries are processed; networkNodes are skipped."""
        project = _make_project(
            nodes=[
                {"id": "net-1", "type": "networkNode", "data": {"id": "net-1"}},
            ]
        )
        host_batch = {"troshka-proj-111-net-1111": "running"}
        vm_states, _, _ = _map_vm_states_for_project(project, host_batch, kv_batch=None)
        assert vm_states == {}

    @patch("app.api.projects._redeploy_progress", {})
    @patch("app.api.projects._domain_name", side_effect=_domain_name)
    def test_empty_topology_returns_empty(self, mock_dn):
        project = _make_project()
        project.topology = {}
        host_batch = {"anything": "running"}

        vm_states, _, _ = _map_vm_states_for_project(project, host_batch, kv_batch=None)
        assert vm_states == {}


# ===========================================================================
# _map_container_states_for_project
# ===========================================================================


def _make_container_project(project_id="proj-1111-2222-3333", ctr_id="ctr-aaaa-1111"):
    nodes = [
        {
            "id": ctr_id,
            "type": "containerNode",
            "data": {"id": ctr_id, "label": "ctr-00"},
        },
    ]
    return SimpleNamespace(id=project_id, topology={"nodes": nodes})


class TestMapContainerStatesForProject:
    def test_running_container_mapped(self):
        project = _make_container_project()
        name = _container_domain_name(project.id, "ctr-aaaa-1111")
        host_batch = {name: {"state": "running", "ips": ["10.0.0.10"]}}

        result = _map_container_states_for_project(project, host_batch)

        assert result["ctr-aaaa-1111"] == {
            "state": "running",
            "ips": ["10.0.0.10"],
        }

    def test_exited_and_dead_normalised_to_stopped(self):
        project = _make_container_project()
        name = _container_domain_name(project.id, "ctr-aaaa-1111")
        for raw_state in ("stopped", "dead"):
            host_batch = {name: {"state": raw_state}}
            result = _map_container_states_for_project(project, host_batch)
            assert result["ctr-aaaa-1111"]["state"] == "stopped"

    def test_missing_container_skipped(self):
        project = _make_container_project()
        result = _map_container_states_for_project(project, {})
        assert result == {}

    def test_none_batch_returns_empty(self):
        project = _make_container_project()
        result = _map_container_states_for_project(project, None)
        assert result == {}

    def test_non_container_nodes_ignored(self):
        project = SimpleNamespace(
            id="proj-1",
            topology={"nodes": [{"id": "vm-1", "type": "vmNode", "data": {}}]},
        )
        result = _map_container_states_for_project(project, {"anything": {"state": "running"}})
        assert result == {}

    def test_missing_ips_defaults_to_empty_list(self):
        project = _make_container_project()
        name = _container_domain_name(project.id, "ctr-aaaa-1111")
        host_batch = {name: {"state": "running"}}
        result = _map_container_states_for_project(project, host_batch)
        assert result["ctr-aaaa-1111"]["ips"] == []


# ===========================================================================
# _fetch_kubevirt_vm_states
# ===========================================================================


class TestFetchKubevirtVmStates:
    @patch("app.services.providers.kubevirt._get_k8s_clients")
    @patch(
        "app.services.providers.kubevirt._project_ns", return_value="troshka-proj-1111"
    )
    def test_returns_vm_states_from_vmis(self, mock_ns, mock_clients):
        mock_custom_api = MagicMock()
        mock_clients.return_value = (mock_custom_api, MagicMock(), MagicMock())

        mock_custom_api.list_namespaced_custom_object.return_value = {
            "items": [
                {
                    "metadata": {"name": "troshka-vm-vm-aaaa-"},
                    "status": {"phase": "Running"},
                },
                {
                    "metadata": {"name": "troshka-vm-vm-bbbb-"},
                    "status": {"phase": "Succeeded"},
                },
            ]
        }

        project = _make_project()
        host = SimpleNamespace(provider_id="provider-1")
        db = MagicMock()
        provider = SimpleNamespace(id="provider-1")
        db.query.return_value.filter_by.return_value.first.return_value = provider

        result = _fetch_kubevirt_vm_states(project, host, db)

        assert result is not None
        assert result["vm-aaaa-1111"] == "Running"
        assert result["vm-bbbb-2222"] == "Succeeded"

    @patch("app.services.providers.kubevirt._get_k8s_clients")
    @patch(
        "app.services.providers.kubevirt._project_ns", return_value="troshka-proj-1111"
    )
    def test_missing_vmi_returns_stopped(self, mock_ns, mock_clients):
        """VMs not found in VMI list get 'Stopped'."""
        mock_custom_api = MagicMock()
        mock_clients.return_value = (mock_custom_api, MagicMock(), MagicMock())
        mock_custom_api.list_namespaced_custom_object.return_value = {"items": []}

        project = _make_project()
        host = SimpleNamespace(provider_id="provider-1")
        db = MagicMock()
        provider = SimpleNamespace(id="provider-1")
        db.query.return_value.filter_by.return_value.first.return_value = provider

        result = _fetch_kubevirt_vm_states(project, host, db)

        assert result is not None
        assert result["vm-aaaa-1111"] == "Stopped"
        assert result["vm-bbbb-2222"] == "Stopped"

    def test_no_provider_returns_none(self):
        project = _make_project()
        host = SimpleNamespace(provider_id="provider-1")
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = None

        result = _fetch_kubevirt_vm_states(project, host, db)
        assert result is None

    @patch("app.services.providers.kubevirt._get_k8s_clients")
    @patch(
        "app.services.providers.kubevirt._project_ns", return_value="troshka-proj-1111"
    )
    def test_api_error_returns_none(self, mock_ns, mock_clients):
        mock_custom_api = MagicMock()
        mock_clients.return_value = (mock_custom_api, MagicMock(), MagicMock())
        mock_custom_api.list_namespaced_custom_object.side_effect = Exception(
            "connection refused"
        )

        project = _make_project()
        host = SimpleNamespace(provider_id="provider-1")
        db = MagicMock()
        provider = SimpleNamespace(id="provider-1")
        db.query.return_value.filter_by.return_value.first.return_value = provider

        result = _fetch_kubevirt_vm_states(project, host, db)
        assert result is None

    @patch("app.services.providers.kubevirt._get_k8s_clients")
    @patch(
        "app.services.providers.kubevirt._project_ns", return_value="troshka-proj-1111"
    )
    def test_vmi_without_status_shows_unknown(self, mock_ns, mock_clients):
        """VMI with no status dict returns 'Unknown'."""
        mock_custom_api = MagicMock()
        mock_clients.return_value = (mock_custom_api, MagicMock(), MagicMock())
        mock_custom_api.list_namespaced_custom_object.return_value = {
            "items": [
                {
                    "metadata": {"name": "troshka-vm-vm-aaaa-"},
                    # no "status" key at all
                }
            ]
        }

        project = _make_project(
            nodes=[
                {
                    "id": "vm-aaaa-1111",
                    "type": "vmNode",
                    "data": {"id": "vm-aaaa-1111"},
                }
            ]
        )
        host = SimpleNamespace(provider_id="provider-1")
        db = MagicMock()
        provider = SimpleNamespace(id="provider-1")
        db.query.return_value.filter_by.return_value.first.return_value = provider

        result = _fetch_kubevirt_vm_states(project, host, db)
        assert result is not None
        assert result["vm-aaaa-1111"] == "Unknown"

    @patch("app.services.providers.kubevirt._get_k8s_clients")
    @patch(
        "app.services.providers.kubevirt._project_ns", return_value="troshka-proj-1111"
    )
    def test_no_vm_nodes_returns_none(self, mock_ns, mock_clients):
        """Project with no vmNodes in topology returns None (empty dict is falsy)."""
        mock_custom_api = MagicMock()
        mock_clients.return_value = (mock_custom_api, MagicMock(), MagicMock())
        mock_custom_api.list_namespaced_custom_object.return_value = {"items": []}

        project = _make_project(
            nodes=[
                {
                    "id": "net-1",
                    "type": "networkNode",
                    "data": {"id": "net-1"},
                }
            ]
        )
        host = SimpleNamespace(provider_id="provider-1")
        db = MagicMock()
        provider = SimpleNamespace(id="provider-1")
        db.query.return_value.filter_by.return_value.first.return_value = provider

        result = _fetch_kubevirt_vm_states(project, host, db)
        assert result is None


# ===========================================================================
# _check_and_notify_project_changes
# ===========================================================================


class TestCheckAndNotifyProjectChanges:
    def setup_method(self):
        _last_states.clear()

    @patch("app.services.ws_pubsub.notify_project")
    def test_project_state_change_triggers_notification(self, mock_notify):
        project = _make_project(state="deploying")
        project_id = project.id

        # Prime with old state
        _last_states[project_id] = {
            "project_state": "active",
            "deploy_error": None,
            "deploy_progress": None,
            "vm_states": {},
            "vm_progress": {},
            "vm_boot_devs": {},
        }

        _check_and_notify_project_changes(
            project_id, project, dp=None, vm_states={}, vm_progress={}, vm_boot_devs={}
        )

        # Should have sent project-state notification
        calls = [
            c
            for c in mock_notify.call_args_list
            if c[0][1].get("type") == "project-state"
        ]
        assert len(calls) == 1
        assert calls[0][0][1]["state"] == "deploying"

    @patch("app.services.ws_pubsub.notify_project")
    def test_no_change_no_notification(self, mock_notify):
        project = _make_project(state="active")
        project_id = project.id

        _last_states[project_id] = {
            "project_state": "active",
            "deploy_error": None,
            "deploy_progress": None,
            "vm_states": {"vm-aaaa-1111": "running"},
            "vm_progress": {},
            "vm_boot_devs": {},
        }

        _check_and_notify_project_changes(
            project_id,
            project,
            dp=None,
            vm_states={"vm-aaaa-1111": "running"},
            vm_progress={},
            vm_boot_devs={},
        )

        mock_notify.assert_not_called()

    @patch("app.services.ws_pubsub.notify_project")
    def test_vm_state_change_triggers_notification(self, mock_notify):
        project = _make_project(state="active")
        project_id = project.id

        _last_states[project_id] = {
            "project_state": "active",
            "deploy_error": None,
            "deploy_progress": None,
            "vm_states": {"vm-aaaa-1111": "stopped"},
            "vm_progress": {},
            "vm_boot_devs": {},
        }

        _check_and_notify_project_changes(
            project_id,
            project,
            dp=None,
            vm_states={"vm-aaaa-1111": "running"},
            vm_progress={},
            vm_boot_devs={},
        )

        calls = [
            c for c in mock_notify.call_args_list if c[0][1].get("type") == "vm-state"
        ]
        assert len(calls) == 1
        assert calls[0][0][1]["states"]["vm-aaaa-1111"] == "running"

    @patch("app.services.ws_pubsub.notify_project")
    def test_deploy_progress_change_triggers_notification(self, mock_notify):
        project = _make_project(state="active")
        project_id = project.id

        _last_states[project_id] = {
            "project_state": "active",
            "deploy_error": None,
            "deploy_progress": None,
            "vm_states": {},
            "vm_progress": {},
            "vm_boot_devs": {},
        }

        dp = {"step": "downloading", "pct": 50}
        _check_and_notify_project_changes(
            project_id, project, dp=dp, vm_states={}, vm_progress={}, vm_boot_devs={}
        )

        calls = [
            c
            for c in mock_notify.call_args_list
            if c[0][1].get("type") == "deploy-progress"
        ]
        assert len(calls) == 1
        assert calls[0][0][1]["progress"]["pct"] == 50

    @patch("app.services.ws_pubsub.notify_project")
    def test_deploy_error_change_triggers_notification(self, mock_notify):
        project = _make_project(state="error", deploy_error="disk full")
        project_id = project.id

        _last_states[project_id] = {
            "project_state": "active",
            "deploy_error": None,
            "deploy_progress": None,
            "vm_states": {},
            "vm_progress": {},
            "vm_boot_devs": {},
        }

        _check_and_notify_project_changes(
            project_id, project, dp=None, vm_states={}, vm_progress={}, vm_boot_devs={}
        )

        calls = [
            c
            for c in mock_notify.call_args_list
            if c[0][1].get("type") == "project-state"
        ]
        assert len(calls) == 1
        assert calls[0][0][1]["deploy_error"] == "disk full"

    @patch("app.services.ws_pubsub.notify_project")
    def test_cache_updated_after_check(self, mock_notify):
        project = _make_project(state="active")
        project_id = project.id

        _check_and_notify_project_changes(
            project_id,
            project,
            dp=None,
            vm_states={"vm-aaaa-1111": "running"},
            vm_progress={},
            vm_boot_devs={},
        )

        cached = _last_states[project_id]
        assert cached["project_state"] == "active"
        assert cached["vm_states"]["vm-aaaa-1111"] == "running"
        assert cached["deploy_error"] is None

    @patch("app.services.ws_pubsub.notify_project")
    def test_first_call_sends_all_notifications(self, mock_notify):
        """First call (no previous cache) always sends state notifications."""
        project = _make_project(state="active")
        project_id = project.id

        _check_and_notify_project_changes(
            project_id,
            project,
            dp=None,
            vm_states={"vm-aaaa-1111": "running"},
            vm_progress={},
            vm_boot_devs={},
        )

        # Should send project-state (no previous state) and vm-state
        types_sent = [c[0][1]["type"] for c in mock_notify.call_args_list]
        assert "project-state" in types_sent
        assert "vm-state" in types_sent

    @patch("app.services.ws_pubsub.notify_project")
    def test_empty_vm_states_no_vm_notification(self, mock_notify):
        """Empty vm_states dict does not trigger vm-state notification."""
        project = _make_project(state="active")
        project_id = project.id

        _last_states[project_id] = {
            "project_state": "active",
            "deploy_error": None,
            "deploy_progress": None,
            "vm_states": {},
            "vm_progress": {},
            "vm_boot_devs": {},
        }

        _check_and_notify_project_changes(
            project_id, project, dp=None, vm_states={}, vm_progress={}, vm_boot_devs={}
        )

        vm_calls = [
            c for c in mock_notify.call_args_list if c[0][1].get("type") == "vm-state"
        ]
        assert len(vm_calls) == 0

    @patch("app.services.ws_pubsub.notify_project")
    def test_vm_progress_change_triggers_vm_state_notification(self, mock_notify):
        """Change in vm_progress alone triggers vm-state notification."""
        project = _make_project(state="active")
        project_id = project.id

        _last_states[project_id] = {
            "project_state": "active",
            "deploy_error": None,
            "deploy_progress": None,
            "vm_states": {"vm-aaaa-1111": "redeploying"},
            "vm_progress": {},
            "vm_boot_devs": {},
        }

        new_progress = {"vm-aaaa-1111": {"step": "copying", "pct": 75}}
        _check_and_notify_project_changes(
            project_id,
            project,
            dp=None,
            vm_states={"vm-aaaa-1111": "redeploying"},
            vm_progress=new_progress,
            vm_boot_devs={},
        )

        vm_calls = [
            c for c in mock_notify.call_args_list if c[0][1].get("type") == "vm-state"
        ]
        assert len(vm_calls) == 1
        assert vm_calls[0][0][1]["progress"] == new_progress

    @patch("app.services.ws_pubsub.notify_project")
    def test_cache_preserves_vm_states_when_empty(self, mock_notify):
        """When vm_states is empty, cache retains the previous vm_states."""
        project = _make_project(state="active")
        project_id = project.id

        _last_states[project_id] = {
            "project_state": "active",
            "deploy_error": None,
            "deploy_progress": None,
            "vm_states": {"vm-aaaa-1111": "running"},
            "vm_progress": {},
            "vm_boot_devs": {},
        }

        _check_and_notify_project_changes(
            project_id, project, dp=None, vm_states={}, vm_progress={}, vm_boot_devs={}
        )

        # Empty vm_states should preserve previous in cache
        cached = _last_states[project_id]
        assert cached["vm_states"] == {"vm-aaaa-1111": "running"}
