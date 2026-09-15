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
    cluster_needs_post_boot_restart,
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


def test_cluster_needs_post_boot_restart_from_harvested_creds():
    project = MagicMock()
    project.deployed_topology = {
        "nodes": [
            {
                "type": "vmNode",
                "data": {
                    "clusterId": "source",
                    "ocpKubeconfig": "apiVersion: v1",
                },
            }
        ]
    }
    topology = {"nodes": []}
    assert cluster_needs_post_boot_restart(project, topology, "source", "") is True
    assert (
        cluster_needs_post_boot_restart(project, topology, "destination", "") is False
    )


def test_cluster_needs_post_boot_restart_from_install_complete_marker():
    project = MagicMock()
    project.deployed_topology = None
    topology = {"nodes": []}
    log = "[source] install complete\n[source] joining 2 deferred worker(s)"
    assert cluster_needs_post_boot_restart(project, topology, "source", log) is True


def test_cluster_needs_post_boot_restart_from_wipe_breadcrumbs():
    project = MagicMock()
    project.deployed_topology = None
    topology = {"nodes": []}
    log = "[source] post-boot restart: wiping boot disks before re-install\n"
    assert cluster_needs_post_boot_restart(project, topology, "source", log) is True


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

    ready_poll = building + "\n[source] deferred workers Ready: 1/2"
    assert _phase_from_input(ready_poll) == PHASE_WAITING

    joined = ready_poll + "\n[source] deferred workers joined"
    assert _phase_from_input(joined) == PHASE_WAITING

    converged = joined + "\n[source] deferred workers converged"
    assert _phase_from_input(converged) == PHASE_COMPLETE


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


def test_phase_from_input_worker_join_retry_stays_waiting():
    log = (
        "[source] install complete\n"
        "[source] joining 2 deferred worker(s)\n"
        "[source] node-image create for source-worker-0\n"
        "[source] node-image create failed for source-worker-0 "
        "(attempt 1), retrying in 60s..."
    )
    assert _phase_from_input(log) == PHASE_WAITING


def test_ops_pod_cluster_complete_waits_for_deferred_workers():
    cluster = {"id": "source", "name": "source", "type": "sno", "workers": 2}
    sno_log = "[source] install complete"
    joined_log = "[source] install complete\n[source] deferred workers joined"
    converged_log = joined_log + "\n[source] deferred workers converged"

    with patch("app.core.redis.get_progress", return_value={"source": sno_log}):
        assert _ops_pod_cluster_complete("proj-1", cluster) is False

    with patch("app.core.redis.get_progress", return_value={"source": joined_log}):
        assert _ops_pod_cluster_complete("proj-1", cluster) is False

    with patch("app.core.redis.get_progress", return_value={"source": converged_log}):
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


def test_restart_post_boot_cleanup_force_stops_kubevirt_vms():
    from app.services.deploy_service import _restart_cluster_post_boot_cleanup

    host = MagicMock()
    host.host_type = "kubevirt-cluster"
    session = MagicMock()
    topology = {"nodes": []}
    cluster = {"id": "source"}
    vms = [{"node_id": "464f4b41-aaaa", "name": "source-cp-0"}]

    with patch(
        "app.services.deploy_service._cluster_member_vm_entries", return_value=vms
    ), patch("app.services.deploy_service._stop_kubevirt_vms") as stop, patch(
        "app.services.deploy_service._find_vm_disks",
        return_value=[{"node_id": "disk1"}],
    ), patch(
        "app.services.deploy_service._ocp_boot_disks", side_effect=lambda d: d
    ), patch(
        "app.services.deploy_service._wipe_vm_boot_disk_kubevirt"
    ), patch(
        "app.services.deploy_service._append_restart_install_log_breadcrumb"
    ):
        _restart_cluster_post_boot_cleanup(session, host, "proj-1", topology, cluster)

    stop.assert_called_once_with(session, host, "proj-1", vms, force=True)


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
    ) as start, patch(
        "app.services.deploy_service._append_restart_install_log_breadcrumb"
    ) as breadcrumb:
        _restart_cluster_post_boot_cleanup(session, host, "proj-1", topology, cluster)

    stop.assert_not_called()
    force_off.assert_called_once_with(host, "proj-1", vms)
    wipe.assert_called_once()
    start.assert_not_called()
    assert any("post-boot restart" in str(c.args[2]) for c in breadcrumb.call_args_list)
    assert any(
        "boot disk wipe complete" in str(c.args[2]) for c in breadcrumb.call_args_list
    )


def test_merge_ops_pod_log_with_preamble_keeps_wipe_lines():
    from app.services.deploy_service import _merge_ops_pod_log_with_preamble

    preamble = (
        "[source] post-boot restart: wiping boot disks before re-install\n"
        "[source] wiped boot disk on troshka-vm-464f4b41 (source-cp-0, disk-54981773)\n"
        "[source] boot disk wipe complete (1 disk(s))\n"
    )
    fresh = (
        "=== install restart 2026-09-14T22:00:00Z ===\nstarting agent-based install\n"
    )
    merged = _merge_ops_pod_log_with_preamble(preamble, fresh)
    assert merged.startswith(preamble)
    assert "starting agent-based install" in merged


def test_append_restart_install_log_breadcrumb():
    from app.services.deploy_service import _append_restart_install_log_breadcrumb

    store = {}
    with patch(
        "app.services.deploy_service._ops_pod_log_cache_key", return_value="k"
    ), patch(
        "app.core.redis.get_progress", side_effect=lambda _k: store.get("k", {})
    ), patch(
        "app.core.redis.set_progress",
        side_effect=lambda k, v, ttl=None: store.update({k: v}),
    ):
        _append_restart_install_log_breadcrumb(
            "proj-1", "source", "wiped boot disk on troshka-vm-abc"
        )
    assert "wiped boot disk" in store["k"]["source"]


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


def test_read_ops_pod_install_log_shows_restart_breadcrumbs_while_marker_active():
    from app.services.deploy_service import read_ops_pod_install_log

    host = MagicMock()
    preamble = "[ocp-1] wiped boot disk on troshka-vm-abc (cp-0, disk-123)\n"
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
    ), patch(
        "app.core.redis.get_progress",
        return_value={"ocp-1": preamble},
    ), patch(
        "app.services.deploy_service._ops_pod_log_cache_key",
        return_value="ocp-install-log:proj-1",
    ):
        logs = read_ops_pod_install_log(host, "proj-1", {"clusters": [{"id": "ocp-1"}]})
    assert logs["ocp-1"] == preamble


def test_ocp_boot_disks_prefers_disk0():
    from app.services.deploy_service import _ocp_boot_disks

    disks = [
        {
            "node_id": "cb7d2845-0000",
            "name": "source-cp-0-disk1",
            "format": "qcow2",
            "size_gb": 250,
        },
        {
            "node_id": "54981773-0000",
            "name": "source-cp-0-disk0",
            "format": "qcow2",
            "size_gb": 120,
        },
    ]
    boot = _ocp_boot_disks(disks)
    assert len(boot) == 1
    assert boot[0]["node_id"].startswith("54981773")


def test_wipe_vm_boot_disk_kubevirt_uses_compute_container():
    from app.services.deploy_service import _wipe_vm_boot_disk_kubevirt

    host = MagicMock()
    host.provider_id = "prov-1"
    session = MagicMock()
    provider = MagicMock()
    session.query.return_value.filter_by.return_value.first.return_value = provider

    with patch(
        "app.services.providers.kubevirt._get_k8s_clients",
        return_value=(MagicMock(), MagicMock(), None),
    ), patch("app.services.providers.kubevirt._project_ns", return_value="ns-1"), patch(
        "app.services.providers.kubevirt.patch_kubevirt_run_strategy"
    ), patch(
        "app.services.providers.kubevirt.wait_virt_launcher_compute_ready",
        return_value="virt-launcher-abc",
    ) as wait_pod, patch(
        "app.services.providers.kubevirt.wipe_boot_disk_in_virt_launcher"
    ) as wipe:
        _wipe_vm_boot_disk_kubevirt(
            session, host, "proj-1", "vm-12345678", ["disk-abcd1234"]
        )

    wait_pod.assert_called_once()
    wipe.assert_called_once()
    assert "disk-abcd1234"[:8] in wipe.call_args.args[3]


def test_wipe_vm_boot_disk_kubevirt_raises_on_timeout():
    from app.services.deploy_service import _wipe_vm_boot_disk_kubevirt

    host = MagicMock()
    host.provider_id = "prov-1"
    session = MagicMock()
    provider = MagicMock()
    session.query.return_value.filter_by.return_value.first.return_value = provider

    with patch(
        "app.services.providers.kubevirt._get_k8s_clients",
        return_value=(MagicMock(), MagicMock(), None),
    ), patch("app.services.providers.kubevirt._project_ns", return_value="ns-1"), patch(
        "app.services.providers.kubevirt.patch_kubevirt_run_strategy"
    ), patch(
        "app.services.providers.kubevirt.wait_virt_launcher_compute_ready",
        return_value=None,
    ):
        with pytest.raises(RuntimeError, match="Timed out waiting"):
            _wipe_vm_boot_disk_kubevirt(
                session, host, "proj-1", "vm-12345678", ["disk-abcd1234"]
            )


def test_validate_cancel_rejects_completed_cluster():
    from app.services.deploy_service import validate_cancel_ocp_cluster_install

    project = MagicMock()
    project.id = "proj-1"
    project.state = "active"
    project.deployed_topology = {
        "clusters": [
            {"id": "source", "installOnDeploy": True, "ocpInstallStatus": "ready"}
        ]
    }
    project.topology = project.deployed_topology
    host = MagicMock()

    with patch(
        "app.services.template_loader.ocp_install_via", return_value="pod"
    ), patch(
        "app.services.deploy_service._ops_pod_cluster_complete", return_value=False
    ), patch(
        "app.services.deploy_service._cluster_install_log_text", return_value=""
    ):
        err = validate_cancel_ocp_cluster_install(
            project, host, project.topology, "source"
        )
    assert err == "Cluster install already completed"


def test_cancel_ocp_cluster_install_stops_pod_and_marks_error():
    from app.services.deploy_service import cancel_ocp_cluster_install

    project = MagicMock()
    project.id = "proj-1"
    project.host_id = "host-1"
    project.deployed_topology = {
        "clusters": [
            {"id": "source", "installOnDeploy": True, "ocpInstallStatus": "monitoring"},
            {"id": "dest", "installOnDeploy": True, "ocpInstallStatus": "monitoring"},
        ]
    }
    project.topology = project.deployed_topology

    host = MagicMock()
    host.host_type = "kubevirt-cluster"

    session = MagicMock()
    session.query.return_value.filter_by.return_value.first.side_effect = [
        project,
        host,
    ]

    with patch("app.core.database.SessionLocal", return_value=session), patch(
        "app.services.deploy_service.validate_cancel_ocp_cluster_install",
        return_value=None,
    ), patch("app.services.deploy_service._wait_ops_monitor_idle") as wait_idle, patch(
        "app.services.deploy_service._release_ops_monitor_lock"
    ) as release_lock, patch(
        "app.services.deploy_service._cancel_ops_pod_install_kubevirt"
    ) as cancel_pod, patch(
        "app.services.deploy_service._project_deploy_start_epoch", return_value=1000
    ), patch(
        "app.services.deploy_service._finalize_cluster_ocp_status"
    ) as finalize, patch(
        "app.services.deploy_service._sync_project_ocp_status_from_clusters"
    ), patch(
        "app.services.deploy_service._publish_ops_pod_progress"
    ):
        cancel_ocp_cluster_install("proj-1", "source")

    cancel_pod.assert_called_once()
    wait_idle.assert_called_once_with("proj-1", timeout=5)
    release_lock.assert_called_once_with("proj-1")
    assert finalize.call_count == 2


def test_mark_cancel_pending_freezes_log_cache():
    from app.services.deploy_service import (
        _mark_ocp_install_cancel_pending,
        cache_ops_pod_logs,
    )

    project_id = "proj-cancel"
    cluster_key = "source"
    cache_key = f"ocp-install-log:{project_id}"

    def fake_get_progress(key):
        if key == cache_key:
            return {"source": "line1\n"}
        return None

    with patch("app.core.redis.set_progress"), patch(
        "app.services.deploy_service._request_ops_monitor_exit"
    ), patch(
        "app.services.deploy_service._append_restart_install_log_breadcrumb"
    ) as breadcrumb:
        _mark_ocp_install_cancel_pending(project_id, cluster_key)

    breadcrumb.assert_called_once_with(
        project_id,
        cluster_key,
        "install cancel requested — log frozen pending teardown",
    )

    with patch(
        "app.services.deploy_service._ocp_install_cancel_in_progress",
        return_value=True,
    ), patch(
        "app.core.redis.get_progress",
        side_effect=fake_get_progress,
    ), patch(
        "app.core.redis.set_progress"
    ):
        merged = cache_ops_pod_logs(project_id, {"source": "line1\nline2\n"})
    assert merged["source"] == "line1"
