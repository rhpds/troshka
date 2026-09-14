"""Restart failed ops-pod OCP install (Phase 1 pre-boot + Phase 2 post-boot)."""

from unittest.mock import MagicMock, patch

import pytest

from app.services.deploy_service import (
    _clusters_for_ops_pod_restart,
    _ops_pod_cluster_complete,
    restart_ocp_cluster_install,
    validate_restart_ocp_cluster_install,
)
from app.services.ocp.ops_pod_install import (
    PHASE_COMPLETE,
    PHASE_FAILED,
    PHASE_WAITING,
    _phase_from_input,
    cluster_install_complete_in_log,
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


def test_phase_from_input_worker_join_stays_waiting_until_joined():
    sno_done = "[source] install complete\n[source] control-plane-usable"
    assert _phase_from_input(sno_done) == PHASE_COMPLETE

    joining = sno_done + "\n[source] joining 2 deferred worker(s)"
    assert _phase_from_input(joining) == PHASE_WAITING

    building = joining + "\n[source] node-image create for worker-0"
    assert _phase_from_input(building) == PHASE_WAITING

    ready_poll = building + "\n[source] worker nodes Ready: 1/2"
    assert _phase_from_input(ready_poll) == PHASE_WAITING

    joined = ready_poll + "\n[source] deferred workers joined"
    assert _phase_from_input(joined) == PHASE_COMPLETE


def test_cluster_install_complete_in_log():
    log = "[source] install complete\n[source] joining 2 deferred worker(s)"
    assert cluster_install_complete_in_log(log, "source") is True
    assert cluster_install_complete_in_log(log, "destination") is False


def test_phase_from_input_worker_join_error_is_failed():
    log = (
        "[source] install complete\n"
        "[source] joining 2 deferred worker(s)\n"
        "[source] node-image create for source-worker-0\n"
        "error: cannot create pod: Internal error occurred: admission plugin "
        '"image.openshift.io/ImagePolicy" failed to complete mutation in 13s'
    )
    assert _phase_from_input(log) == PHASE_FAILED


def test_ops_pod_cluster_complete_waits_for_deferred_workers():
    cluster = {"id": "source", "name": "source", "type": "sno", "workers": 2}
    sno_log = "[source] install complete"
    joined_log = "[source] install complete\n[source] deferred workers joined"

    with patch("app.core.redis.get_progress", return_value={"source": sno_log}):
        assert _ops_pod_cluster_complete("proj-1", cluster) is False

    with patch("app.core.redis.get_progress", return_value={"source": joined_log}):
        assert _ops_pod_cluster_complete("proj-1", cluster) is True


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
        "app.services.deploy_service._truncate_ops_pod_cluster_install_logs"
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


def test_restart_pre_boot_skips_post_boot_cleanup():
    call_order = []

    project = MagicMock()
    project.id = "proj-1"
    project.host_id = "host-1"
    project.vni_map = {}
    project.deployed_topology = {"clusters": [{"id": "ocp-1", "installOnDeploy": True}]}
    project.topology = project.deployed_topology

    host = MagicMock()
    host.host_type = "shared"
    host.ip_address = "10.0.0.1"

    session = MagicMock()
    session.query.return_value.filter_by.return_value.first.side_effect = [
        project,
        host,
    ]

    def _truncate(*_a, **_k):
        call_order.append("truncate")

    def _deploy(*_a, **_k):
        call_order.append("deploy")

    with patch("app.core.database.SessionLocal", return_value=session), patch(
        "app.services.deploy_service.validate_restart_ocp_cluster_install",
        return_value=None,
    ), patch("app.services.deploy_service.get_progress", return_value={}), patch(
        "app.services.deploy_service.delete_progress"
    ), patch(
        "app.services.deploy_service._wait_ops_monitor_idle"
    ), patch(
        "app.services.deploy_service._cancel_ops_pod_install_troshkad"
    ), patch(
        "app.services.deploy_service._truncate_ops_pod_cluster_install_logs",
        side_effect=_truncate,
    ), patch(
        "app.services.deploy_service._restart_cluster_post_boot_cleanup"
    ) as cleanup, patch(
        "app.services.deploy_service._cluster_for_key",
        return_value={"id": "ocp-1"},
    ), patch(
        "app.services.deploy_service._clusters_for_ops_pod_restart",
        return_value=[{"id": "ocp-1"}],
    ), patch(
        "app.services.deploy_service._deploy_ops_pod", side_effect=_deploy
    ), patch(
        "app.services.deploy_service._clear_ocp_install_restart_marker"
    ):
        restart_ocp_cluster_install("proj-1", "ocp-1")

    cleanup.assert_not_called()
    assert call_order == ["truncate", "deploy"]


def test_restart_post_boot_cleanup_leaves_vms_off():
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
        "app.services.deploy_service._force_off_troshkad_vms"
    ) as force_off, patch(
        "app.services.deploy_service._get_host_pool", return_value="default"
    ), patch(
        "app.services.deploy_service._wipe_vm_boot_disk_troshkad"
    ) as wipe, patch(
        "app.services.deploy_service._start_troshkad_vms"
    ) as start:
        _restart_cluster_post_boot_cleanup(session, host, "proj-1", topology, cluster)

    stop.assert_not_called()
    force_off.assert_called_once_with(host, "proj-1", vms)
    wipe.assert_called_once()
    start.assert_not_called()


def test_wipe_vm_boot_disk_troshkad_recreates_all_qcow2_disks():
    from app.services.deploy_service import _wipe_vm_boot_disk_troshkad

    host = MagicMock()
    host.ip_address = "10.0.0.1"
    topology = {
        "nodes": [
            {
                "id": "vm-1",
                "type": "vmNode",
                "data": {
                    "disks": [
                        {
                            "node_id": "disk-0",
                            "format": "qcow2",
                            "size_gb": 120,
                        },
                        {
                            "node_id": "disk-1",
                            "format": "qcow2",
                            "size_gb": 100,
                        },
                    ]
                },
            }
        ]
    }

    with patch(
        "app.services.deploy_service._find_vm_disks",
        return_value=topology["nodes"][0]["data"]["disks"],
    ), patch(
        "app.services.deploy_service._disk_path",
        side_effect=lambda _p, _v, disk_id, fmt, pool: f"/var/lib/troshka/shared/vms/p/{disk_id}.{fmt}",
    ), patch(
        "app.services.deploy_service._get_host_pool", return_value="shared"
    ), patch(
        "app.services.deploy_service.start_job", return_value="job-1"
    ) as start_job, patch(
        "app.services.deploy_service.wait_for_job"
    ):
        _wipe_vm_boot_disk_troshkad(host, "proj-1", "vm-1", topology, "shared")

    assert start_job.call_count == 2
    assert start_job.call_args_list[0].args[2]["size_gb"] == 120
    assert start_job.call_args_list[1].args[2]["size_gb"] == 100


def test_wipe_vm_boot_disk_troshkad_raises_on_failed_job():
    from app.services.deploy_service import _wipe_vm_boot_disk_troshkad

    host = MagicMock()
    topology = {"nodes": []}
    with patch(
        "app.services.deploy_service._find_vm_disks",
        return_value=[
            {"node_id": "disk-0", "format": "qcow2", "size_gb": 120},
        ],
    ), patch(
        "app.services.deploy_service._disk_path",
        return_value="/var/lib/troshka/shared/vms/p/d0.qcow2",
    ), patch(
        "app.services.deploy_service.start_job", return_value="job-1"
    ), patch(
        "app.services.deploy_service.wait_for_job",
        return_value={"status": "failed", "result": {"error": "Disk not found"}},
    ):
        with pytest.raises(RuntimeError, match="failed to wipe"):
            _wipe_vm_boot_disk_troshkad(host, "proj-1", "vm-1", topology, "shared")


def test_wait_ops_monitor_idle_clears_exit_request():
    from app.services.deploy_service import (
        _wait_ops_monitor_idle,
    )

    with patch("app.services.deploy_service._request_ops_monitor_exit") as req, patch(
        "app.core.redis.is_redis_available", return_value=False
    ), patch("app.services.deploy_service._time.sleep"), patch(
        "app.services.deploy_service._clear_ops_monitor_exit_request"
    ) as clear:
        _wait_ops_monitor_idle("proj-1")
    req.assert_called_once_with("proj-1")
    clear.assert_called_once_with("proj-1")


def test_read_ops_pod_install_log_empty_while_restart_marker_active():
    from app.services.deploy_service import read_ops_pod_install_log

    host = MagicMock()
    with patch(
        "app.services.deploy_service._ocp_clusters",
        return_value=[{"id": "ocp-1"}],
    ), patch(
        "app.services.ocp.ops_pod_install._cluster_key", return_value="ocp-1"
    ), patch(
        "app.services.deploy_service._ops_pod_container_name",
        return_value="troshka-p-ops",
    ), patch(
        "app.services.deploy_service._read_ops_pod_cluster_logs",
        return_value={"ocp-1": "old rendezvous host failure log\n" * 50},
    ), patch(
        "app.services.deploy_service.cache_ops_pod_logs",
        return_value={"ocp-1": "old rendezvous host failure log\n" * 50},
    ), patch(
        "app.services.deploy_service._ocp_install_restart_in_progress",
        return_value=True,
    ):
        logs = read_ops_pod_install_log(host, "proj-1", {"clusters": [{"id": "ocp-1"}]})
    assert logs["ocp-1"] == ""
