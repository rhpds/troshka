"""Tests for off_action (stop/pause/hibernate) handling in the project
stop/start cascades — deploy_service.stop_project_async / start_project_async.
"""

from unittest.mock import MagicMock, patch

from tests.test_deploy_orchestration import (
    DB_MOD,
    KV_MOD,
    PROJECT_ID,
    VM_NODE_ID,
    _make_host,
    _make_project,
    _minimal_topology,
    _vm_node,
)

SVC = "app.services.deploy_service"
VMP_MOD = "app.services.vm_power"


# ---------------------------------------------------------------------------
# stop_project_async — off_action=pause
# ---------------------------------------------------------------------------
class TestStopProjectAsyncPause:
    """off_action=pause must pause VMs and keep the project active."""

    @patch(f"{SVC}.notify_project")
    @patch(f"{VMP_MOD}.pause_vm_on_host")
    @patch(f"{SVC}._extract_vms")
    @patch("app.services.ws_pubsub.get_cached_vm_states")
    def test_pause_troshkad_keeps_project_active(
        self, mock_cached, mock_extract, mock_pause, mock_notify
    ):
        from app.services.deploy_service import stop_project_async

        mock_extract.return_value = [{"node_id": VM_NODE_ID, "name": "vm1"}]
        mock_cached.return_value = {"states": {VM_NODE_ID: "running"}}

        project = _make_project(
            state="stopping",
            topology=_minimal_topology(
                vm_nodes=[_vm_node(node_id=VM_NODE_ID, name="vm1")]
            ),
        )
        project.off_action = "pause"
        host = _make_host()

        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.side_effect = [
            project,
            host,
        ]

        with patch(f"{DB_MOD}.SessionLocal", return_value=mock_session):
            stop_project_async(PROJECT_ID)

        mock_pause.assert_called_once_with(host, PROJECT_ID, VM_NODE_ID)
        assert project.state == "active"

        vm_state_calls = [
            c for c in mock_notify.call_args_list if c.args[1].get("type") == "vm-state"
        ]
        assert vm_state_calls
        assert vm_state_calls[0].args[1]["states"] == {VM_NODE_ID: "paused"}

    @patch(f"{SVC}.notify_project")
    @patch(f"{VMP_MOD}.pause_vm_on_host")
    @patch(f"{SVC}._extract_vms")
    @patch("app.services.ws_pubsub.get_cached_vm_states")
    def test_pause_skips_vm_that_is_already_off(
        self, mock_cached, mock_extract, mock_pause, mock_notify
    ):
        """A VM already off must not be virsh-suspended nor broadcast paused."""
        from app.services.deploy_service import stop_project_async

        mock_extract.return_value = [{"node_id": VM_NODE_ID, "name": "vm1"}]
        mock_cached.return_value = {"states": {VM_NODE_ID: "shut_off"}}

        project = _make_project(
            state="stopping",
            topology=_minimal_topology(
                vm_nodes=[_vm_node(node_id=VM_NODE_ID, name="vm1")]
            ),
        )
        project.off_action = "pause"
        host = _make_host()

        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.side_effect = [
            project,
            host,
        ]

        with patch(f"{DB_MOD}.SessionLocal", return_value=mock_session):
            stop_project_async(PROJECT_ID)

        mock_pause.assert_not_called()
        assert project.state == "active"

        vm_state_calls = [
            c for c in mock_notify.call_args_list if c.args[1].get("type") == "vm-state"
        ]
        assert vm_state_calls
        assert vm_state_calls[0].args[1]["states"] == {}

    @patch(f"{SVC}.notify_project")
    @patch(f"{VMP_MOD}.pause_vm_on_host")
    @patch(f"{SVC}._extract_vms")
    @patch("app.services.ws_pubsub.get_cached_vm_states")
    def test_pause_kubevirt_keeps_project_active(
        self, mock_cached, mock_extract, mock_pause, mock_notify
    ):
        from app.services.deploy_service import stop_project_async

        mock_extract.return_value = [{"node_id": VM_NODE_ID, "name": "vm1"}]
        mock_cached.return_value = {"states": {VM_NODE_ID: "running"}}

        project = _make_project(state="stopping")
        project.off_action = "pause"
        host = _make_host(host_type="kubevirt-cluster")

        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.side_effect = [
            project,
            host,
        ]

        with patch(f"{DB_MOD}.SessionLocal", return_value=mock_session):
            stop_project_async(PROJECT_ID)

        mock_pause.assert_called_once_with(host, PROJECT_ID, VM_NODE_ID)
        assert project.state == "active"


# ---------------------------------------------------------------------------
# stop_project_async — off_action=hibernate
# ---------------------------------------------------------------------------
class TestStopProjectAsyncHibernate:
    """off_action=hibernate hibernates VMs; ends up 'stopped' like a stop."""

    @patch(f"{SVC}.notify_project")
    @patch(f"{VMP_MOD}.hibernate_vm_on_host")
    @patch(f"{SVC}._extract_vms")
    @patch("app.services.ws_pubsub.get_cached_vm_states")
    def test_hibernate_troshkad_sets_stopped(
        self, mock_cached, mock_extract, mock_hibernate, mock_notify
    ):
        from app.services.deploy_service import stop_project_async

        mock_extract.return_value = [{"node_id": VM_NODE_ID, "name": "vm1"}]
        mock_cached.return_value = {"states": {VM_NODE_ID: "running"}}

        project = _make_project(state="stopping")
        project.off_action = "hibernate"
        host = _make_host()

        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.side_effect = [
            project,
            host,
        ]

        with patch(f"{DB_MOD}.SessionLocal", return_value=mock_session):
            stop_project_async(PROJECT_ID)

        mock_hibernate.assert_called_once_with(host, PROJECT_ID, VM_NODE_ID)
        assert project.state == "stopped"

    @patch(f"{SVC}.notify_project")
    @patch(f"{SVC}._extract_vms")
    def test_hibernate_kubevirt_fails_fast_to_error(self, mock_extract, mock_notify):
        """Hibernate unsupported on KubeVirt must not leave the project stuck
        in 'stopping' — it should go straight to 'error'."""
        from app.services.deploy_service import stop_project_async

        mock_extract.return_value = [{"node_id": VM_NODE_ID, "name": "vm1"}]

        project = _make_project(state="stopping")
        project.off_action = "hibernate"
        host = _make_host(host_type="kubevirt-cluster")

        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.side_effect = [
            project,
            host,
            project,  # re-query inside _set_project_error
        ]

        with patch(f"{DB_MOD}.SessionLocal", return_value=mock_session):
            stop_project_async(PROJECT_ID)

        assert project.state == "error"
        assert project.state != "stopping"

    @patch(f"{SVC}.notify_project")
    @patch(f"{SVC}._extract_vms")
    def test_hibernate_kubevirt_error_message(self, mock_extract, mock_notify):
        from app.services.deploy_service import stop_project_async

        mock_extract.return_value = []
        project = _make_project(state="stopping")
        project.off_action = "hibernate"
        host = _make_host(host_type="kubevirt-cluster")

        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.side_effect = [
            project,
            host,
            project,
        ]

        with patch(f"{DB_MOD}.SessionLocal", return_value=mock_session):
            stop_project_async(PROJECT_ID)

        assert "hibernate" in (project.deploy_error or "").lower()


# ---------------------------------------------------------------------------
# start_project_async — resuming a paused VM instead of a cold start
# ---------------------------------------------------------------------------
class TestStartProjectAsyncResumesPaused:
    @patch(f"{SVC}.notify_project")
    @patch(f"{SVC}._has_ocp_monitor", return_value=False)
    @patch(f"{SVC}._extract_bmc_config", return_value=None)
    @patch(f"{SVC}._start_vms_via_troshkad", return_value=[])
    @patch(f"{VMP_MOD}.unpause_vm_on_host")
    @patch(f"{SVC}._setup_pxe_via_troshkad")
    @patch(f"{SVC}.cache_library_images", return_value=[])
    @patch(f"{SVC}._setup_networks_via_troshkad", return_value=True)
    @patch(f"{SVC}._get_network_lock")
    @patch("app.services.ws_pubsub.get_cached_vm_states")
    def test_paused_vm_unpaused_not_cold_started(
        self,
        mock_cached,
        mock_lock,
        mock_net,
        mock_cache,
        mock_pxe,
        mock_unpause,
        mock_start_vms,
        mock_bmc,
        mock_ocp,
        mock_notify,
    ):
        from app.services.deploy_service import start_project_async

        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)
        mock_cached.return_value = {"states": {VM_NODE_ID: "paused"}}

        project = _make_project(
            state="starting",
            topology=_minimal_topology(
                vm_nodes=[_vm_node(node_id=VM_NODE_ID, name="vm1")]
            ),
            vni_map={"net1": 100},
        )
        host = _make_host()

        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.side_effect = [
            project,
            host,
        ]
        mock_session.query.return_value.filter_by.return_value.all.return_value = []

        with patch(f"{DB_MOD}.SessionLocal", return_value=mock_session):
            start_project_async(PROJECT_ID)

        mock_unpause.assert_called_once_with(host, PROJECT_ID, VM_NODE_ID)
        mock_start_vms.assert_not_called()
        assert project.state == "active"

    @patch(f"{SVC}.notify_project")
    @patch(f"{SVC}._extract_vms")
    @patch(f"{VMP_MOD}.unpause_vm_on_host")
    @patch("app.services.ws_pubsub.get_cached_vm_states")
    def test_kubevirt_paused_vm_unpaused(
        self, mock_cached, mock_unpause, mock_extract, mock_notify
    ):
        from app.services.deploy_service import start_project_async

        mock_extract.return_value = [{"node_id": VM_NODE_ID, "name": "vm1"}]
        mock_cached.return_value = {"states": {VM_NODE_ID: "paused"}}

        project = _make_project(state="starting")
        host = _make_host(host_type="kubevirt-cluster")
        provider = MagicMock()
        provider.id = "prov-1"

        mock_custom_api = MagicMock()
        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.side_effect = [
            project,
            host,
            provider,
        ]

        with patch(f"{DB_MOD}.SessionLocal", return_value=mock_session), patch(
            f"{KV_MOD}._get_k8s_clients",
            return_value=(mock_custom_api, None, None),
        ), patch(f"{KV_MOD}._project_ns", return_value="troshka-ns"):
            start_project_async(PROJECT_ID)

        mock_unpause.assert_called_once_with(host, PROJECT_ID, VM_NODE_ID)
        mock_custom_api.patch_namespaced_custom_object.assert_not_called()
        assert project.state == "active"
