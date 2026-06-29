"""Tests for pre-boot kubelet cert cleanup during pattern deploys."""

from unittest.mock import MagicMock, patch

from app.services.deploy_service import (
    _clean_kubelet_certs,
    _extract_vms,
    _find_vm_disks,
    _is_ocp_topology,
    _is_pattern_deploy,
)


def _make_ocp_topology(vm_configs, with_pattern=True):
    """Build a minimal OCP topology with the given VM configs.

    vm_configs: list of dicts with keys: name, os, optional vcpus/ram
    Each VM gets one qcow2 storage node connected via a dp- edge.
    """
    nodes = []
    edges = []
    for i, cfg in enumerate(vm_configs):
        vm_id = f"vm-{i:04d}-0000-0000"
        disk_id = f"disk-{i:04d}-0000-0000"
        disk_ctrl_id = f"dp-{i}"
        nodes.append(
            {
                "id": vm_id,
                "type": "vmNode",
                "data": {
                    "name": cfg["name"],
                    "label": cfg.get("label", cfg["name"]),
                    "os": cfg["os"],
                    "vcpus": cfg.get("vcpus", 4),
                    "ram": cfg.get("ram", 16),
                    "diskControllers": [{"id": disk_ctrl_id, "bus": "virtio"}],
                },
            }
        )
        storage_data = {"size": 120, "format": "qcow2", "source": "blank"}
        if with_pattern:
            storage_data["patternId"] = "pat-0001"
            storage_data["patternDiskId"] = disk_id
        nodes.append(
            {
                "id": disk_id,
                "type": "storageNode",
                "data": storage_data,
            }
        )
        edges.append(
            {
                "source": vm_id,
                "target": disk_id,
                "sourceHandle": disk_ctrl_id,
                "targetHandle": "storage-in",
            }
        )
    return {"nodes": nodes, "edges": edges}


# -- Detection tests ----------------------------------------------------------


def test_is_ocp_topology_true():
    topo = _make_ocp_topology(
        [
            {"name": "bastion", "label": "bastion", "os": "rhel"},
            {"name": "cp-0", "os": "rhcos"},
        ]
    )
    assert _is_ocp_topology(topo) is True


def test_is_ocp_topology_false_no_rhcos():
    topo = _make_ocp_topology([{"name": "bastion", "label": "bastion", "os": "rhel"}])
    assert _is_ocp_topology(topo) is False


def test_is_ocp_topology_false_no_bastion():
    topo = _make_ocp_topology([{"name": "cp-0", "os": "rhcos"}])
    assert _is_ocp_topology(topo) is False


def test_is_pattern_deploy_true():
    topo = _make_ocp_topology([{"name": "cp-0", "os": "rhcos"}], with_pattern=True)
    assert _is_pattern_deploy(topo) is True


def test_is_pattern_deploy_false():
    topo = _make_ocp_topology([{"name": "cp-0", "os": "rhcos"}], with_pattern=False)
    assert _is_pattern_deploy(topo) is False


# -- RHCOS VM filtering -------------------------------------------------------


def test_extract_only_rhcos_vms():
    topo = _make_ocp_topology(
        [
            {"name": "bastion", "label": "bastion", "os": "rhel"},
            {"name": "cp-0", "os": "rhcos"},
            {"name": "cp-1", "os": "rhcos"},
        ]
    )
    vms = _extract_vms(topo)
    rhcos = [v for v in vms if v.get("os") == "rhcos"]
    assert len(rhcos) == 2
    assert all(v["os"] == "rhcos" for v in rhcos)


def test_find_boot_disk_for_vm():
    topo = _make_ocp_topology([{"name": "cp-0", "os": "rhcos"}])
    vms = _extract_vms(topo)
    disks = _find_vm_disks(vms[0]["node_id"], topo)
    boot = next((d for d in disks if d.get("format") == "qcow2"), None)
    assert boot is not None
    assert boot["format"] == "qcow2"


# -- Cert cleanup integration (mocked troshkad) -------------------------------


@patch("app.services.deploy_service.wait_for_job")
@patch("app.services.deploy_service.start_job")
def test_clean_kubelet_certs_calls_modify_fs(mock_start, mock_wait):
    """Verify cert cleanup calls /vms/modify-fs for each RHCOS VM."""
    mock_start.return_value = "job-001"
    mock_wait.return_value = {"status": "complete", "result": {"results": []}}
    host = MagicMock()

    topo = _make_ocp_topology(
        [
            {"name": "bastion", "label": "bastion", "os": "rhel"},
            {"name": "cp-0", "os": "rhcos"},
            {"name": "cp-1", "os": "rhcos"},
            {"name": "worker-0", "os": "rhcos"},
        ]
    )

    _clean_kubelet_certs(host, "proj-0001-0000", topo, pool=None)

    # Should be called 3 times (cp-0, cp-1, worker-0) — NOT for bastion
    assert mock_start.call_count == 3
    for call in mock_start.call_args_list:
        args = call[0]
        assert args[1] == "/vms/modify-fs"
        params = args[2]
        assert params["operations"] == [
            {"action": "rm-rf", "path": "/var/lib/kubelet/pki"},
            {"action": "rm-f", "path": "/var/lib/kubelet/kubeconfig"},
        ]
        assert params["disk"].endswith(".qcow2")


@patch("app.services.deploy_service.wait_for_job")
@patch("app.services.deploy_service.start_job")
def test_clean_kubelet_certs_nonfatal_on_failure(mock_start, mock_wait):
    """Verify cert cleanup does not raise on failure."""
    mock_start.return_value = "job-001"
    mock_wait.return_value = {
        "status": "failed",
        "result": {"error": "guestfish crashed"},
    }
    host = MagicMock()

    topo = _make_ocp_topology(
        [
            {"name": "bastion", "label": "bastion", "os": "rhel"},
            {"name": "cp-0", "os": "rhcos"},
        ]
    )

    # Should not raise
    _clean_kubelet_certs(host, "proj-0001-0000", topo, pool=None)
    assert mock_start.call_count == 1


@patch("app.services.deploy_service.wait_for_job")
@patch("app.services.deploy_service.start_job")
def test_clean_kubelet_certs_nonfatal_on_exception(mock_start, mock_wait):
    """Verify cert cleanup does not raise on troshkad exception."""
    mock_start.side_effect = Exception("connection refused")
    host = MagicMock()

    topo = _make_ocp_topology(
        [
            {"name": "bastion", "label": "bastion", "os": "rhel"},
            {"name": "cp-0", "os": "rhcos"},
        ]
    )

    # Should not raise
    _clean_kubelet_certs(host, "proj-0001-0000", topo, pool=None)


@patch("app.services.deploy_service.wait_for_job")
@patch("app.services.deploy_service.start_job")
def test_clean_kubelet_certs_sno(mock_start, mock_wait):
    """Verify cert cleanup works for SNO (single RHCOS node)."""
    mock_start.return_value = "job-001"
    mock_wait.return_value = {"status": "complete", "result": {"results": []}}
    host = MagicMock()

    topo = _make_ocp_topology(
        [
            {"name": "bastion", "label": "bastion", "os": "rhel"},
            {"name": "cp-0", "os": "rhcos"},
        ]
    )

    _clean_kubelet_certs(host, "proj-0001-0000", topo, pool=None)

    # SNO: exactly 1 RHCOS VM
    assert mock_start.call_count == 1


@patch("app.services.deploy_service.wait_for_job")
@patch("app.services.deploy_service.start_job")
def test_clean_kubelet_certs_skips_non_qcow2(mock_start, mock_wait):
    """Verify cert cleanup skips VMs whose only disk is not qcow2."""
    host = MagicMock()

    topo = _make_ocp_topology(
        [
            {"name": "bastion", "label": "bastion", "os": "rhel"},
            {"name": "cp-0", "os": "rhcos"},
        ]
    )
    # Change the storage node format to iso
    for n in topo["nodes"]:
        if n["type"] == "storageNode" and n["id"] == "disk-0001-0000-0000":
            n["data"]["format"] = "iso"

    _clean_kubelet_certs(host, "proj-0001-0000", topo, pool=None)

    # Should skip cp-0 because its disk is iso, not qcow2
    assert mock_start.call_count == 0
