"""Restart failed ops-pod OCP install (Phase 1 pre-boot + Phase 2 post-boot)."""

from unittest.mock import MagicMock, patch

import pytest

from app.services.deploy_service import (
    _clusters_for_ops_pod_restart,
    restart_ocp_cluster_install,
    validate_restart_ocp_cluster_install,
)
from app.services.ocp.ops_pod_install import (
    PHASE_FAILED,
    _phase_from_input,
    cluster_install_post_boot,
    filter_install_log_noise,
)


@pytest.mark.parametrize(
    "log,expected",
    [
        ("", False),
        ("level=fatal create image failed", False),
        ("Serving via HTTP on port 8080", True),
        ("InsertMedia and ForceRestart", True),
        ("reached installation stage waiting", True),
    ],
)
def test_cluster_install_post_boot(log, expected):
    assert cluster_install_post_boot(log) is expected


def test_phase_from_input_treats_bootstrap_timeout_as_failed():
    log = (
        "level=error msg=Bootstrap failed to complete: "
        "bootstrap process timed out: context deadline exceeded"
    )
    assert _phase_from_input(log) == PHASE_FAILED


def test_filter_install_log_noise_drops_assisted_service_poll_spam():
    raw = (
        "progress line\n"
        "Unable to retrieve cluster metadata from Agent Rest API v2GetClusterNotFound\n"
        "Agent Rest API never initialized. Bootstrap Kube API never initialized\n"
    )
    filtered = filter_install_log_noise(raw)
    assert "progress line" in filtered
    assert "v2GetClusterNotFound" not in filtered


def test_validate_restart_rejects_complete():
    project = MagicMock()
    project.state = "active"
    project.id = "proj-1"
    project.deployed_topology = {
        "clusters": [
            {"id": "ocp-1", "ocpInstallStatus": "ready", "installOnDeploy": True}
        ]
    }
    host = MagicMock()
    topology = project.deployed_topology
    with patch(
        "app.services.template_loader.ocp_install_via", return_value="pod"
    ), patch(
        "app.services.deploy_service._ops_pod_cluster_complete", return_value=False
    ):
        err = validate_restart_ocp_cluster_install(project, host, topology, "ocp-1")
    assert err == "Cluster install already completed"


def test_validate_restart_rejects_recert():
    project = MagicMock()
    project.state = "active"
    project.id = "proj-1"
    project.deployed_topology = {
        "clusters": [
            {"id": "ocp-1", "ocpInstallStatus": "error", "installOnDeploy": True}
        ]
    }
    host = MagicMock()
    topology = project.deployed_topology
    with patch(
        "app.services.template_loader.ocp_install_via", return_value="pod"
    ), patch(
        "app.services.deploy_service._cluster_install_log_text",
        return_value="[ocp-1] Waiting (recert)",
    ), patch(
        "app.services.deploy_service._ops_pod_cluster_complete", return_value=False
    ):
        err = validate_restart_ocp_cluster_install(project, host, topology, "ocp-1")
    assert err == "Cannot restart a recert install"


def test_validate_restart_allows_failed():
    project = MagicMock()
    project.state = "active"
    project.id = "proj-1"
    project.deployed_topology = {
        "clusters": [
            {"id": "ocp-1", "ocpInstallStatus": "error", "installOnDeploy": True}
        ]
    }
    host = MagicMock()
    topology = project.deployed_topology
    with patch(
        "app.services.template_loader.ocp_install_via", return_value="pod"
    ), patch(
        "app.services.deploy_service._cluster_install_log_text",
        return_value="level=fatal unauthorized",
    ), patch(
        "app.services.deploy_service._ops_pod_cluster_complete", return_value=False
    ):
        err = validate_restart_ocp_cluster_install(project, host, topology, "ocp-1")
    assert err is None


def test_clusters_for_ops_pod_restart_keeps_inflight_siblings():
    topology = {
        "clusters": [
            {"id": "ocp-a", "installOnDeploy": True},
            {"id": "ocp-b", "installOnDeploy": True},
        ]
    }
    deployed = topology
    with patch(
        "app.services.deploy_service._ops_pod_cluster_complete",
        return_value=False,
    ):
        expanded = _clusters_for_ops_pod_restart(topology, deployed, "ocp-a", "proj-1")
    keys = {c["id"] for c in expanded}
    assert keys == {"ocp-a", "ocp-b"}


def test_clusters_for_ops_pod_restart_excludes_complete_siblings():
    topology = {
        "clusters": [
            {"id": "ocp-a", "installOnDeploy": True},
            {"id": "ocp-b", "installOnDeploy": True},
        ]
    }
    deployed = topology
    with patch(
        "app.services.deploy_service._ops_pod_cluster_complete",
        side_effect=lambda _pid, c: c.get("id") == "ocp-b",
    ):
        expanded = _clusters_for_ops_pod_restart(topology, deployed, "ocp-a", "proj-1")
    keys = {c["id"] for c in expanded}
    assert keys == {"ocp-a"}


def test_restart_post_boot_ejects_before_ops_pod_destroy():
    call_order = []

    project = MagicMock()
    project.id = "proj-1"
    project.host_id = "host-1"
    project.vni_map = {}
    project.deployed_topology = {"clusters": [{"id": "ocp-1", "installOnDeploy": True}]}
    project.topology = project.deployed_topology

    host = MagicMock()
    host.host_type = "shared"  # ocpvirt hosts use troshkad, not kubevirt-cluster
    host.ip_address = "10.0.0.1"

    session = MagicMock()
    session.query.return_value.filter_by.return_value.first.side_effect = [
        project,
        host,
    ]

    def _eject(*_a, **_k):
        call_order.append("eject")

    def _cancel(*_a, **_k):
        call_order.append("cancel")

    def _cleanup(*_a, **_k):
        call_order.append("cleanup")

    with patch("app.core.database.SessionLocal", return_value=session), patch(
        "app.services.deploy_service.validate_restart_ocp_cluster_install",
        return_value=None,
    ), patch(
        "app.services.deploy_service.get_progress", return_value={"post_boot": True}
    ), patch(
        "app.services.deploy_service.delete_progress"
    ), patch(
        "app.services.deploy_service._wait_ops_monitor_idle"
    ), patch(
        "app.services.deploy_service._eject_cluster_bmc_media", side_effect=_eject
    ), patch(
        "app.services.deploy_service._cancel_ops_pod_install_troshkad",
        side_effect=_cancel,
    ), patch(
        "app.services.deploy_service._restart_cluster_post_boot_cleanup",
        side_effect=_cleanup,
    ), patch(
        "app.services.deploy_service._cluster_for_key",
        return_value={"id": "ocp-1"},
    ), patch(
        "app.services.deploy_service._clusters_for_ops_pod_restart",
        return_value=[{"id": "ocp-1"}],
    ), patch(
        "app.services.deploy_service._deploy_ops_pod"
    ), patch(
        "app.services.deploy_service._clear_ocp_install_restart_marker"
    ):
        restart_ocp_cluster_install("proj-1", "ocp-1")

    assert call_order.index("eject") < call_order.index("cancel")
    assert call_order.index("cleanup") > call_order.index("cancel")


def test_restart_post_boot_cleanup_starts_troshkad_vms():
    from app.services.deploy_service import _restart_cluster_post_boot_cleanup

    host = MagicMock()
    host.host_type = "shared"
    host.ip_address = "10.0.0.1"
    session = MagicMock()
    topology = {"nodes": []}
    cluster = {"id": "ocp-1"}
    vms = [{"node_id": "vm-1", "name": "cp-0"}]

    with patch(
        "app.services.deploy_service._cluster_member_vm_entries", return_value=vms
    ), patch("app.services.deploy_service._stop_troshkad_vms") as stop, patch(
        "app.services.deploy_service._get_host_pool", return_value="default"
    ), patch(
        "app.services.deploy_service._wipe_vm_boot_disk_troshkad"
    ) as wipe, patch(
        "app.services.deploy_service._start_troshkad_vms"
    ) as start:
        _restart_cluster_post_boot_cleanup(session, host, "proj-1", topology, cluster)

    stop.assert_called_once_with(host, "proj-1", vms)
    wipe.assert_called_once()
    start.assert_called_once_with(host, "proj-1", vms)
