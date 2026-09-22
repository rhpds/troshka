"""Tests for _cluster_uses_recert — recert (pre-installed) vs fresh-install detection."""

from app.services.deploy_service import _cluster_uses_recert


def _ocp_cluster_topology(*, pattern=False, ocp_kubeconfig=False):
    vm_id = "vm-0000-0000-0000"
    disk_id = "disk-0000-0000-0000"
    ctrl = "dp-0"
    vm_data = {
        "name": "cp-0",
        "os": "rhcos",
        "diskControllers": [{"id": ctrl, "bus": "virtio"}],
    }
    if ocp_kubeconfig:
        vm_data["ocpKubeconfig"] = "apiVersion: v1\nkind: Config\n"
    storage = {
        "size": 120,
        "format": "qcow2",
        "source": "pattern" if pattern else "blank",
    }
    if pattern:
        storage["patternId"] = "pat-1"
        storage["patternDiskId"] = "pd-1"
    return {
        "nodes": [
            {"id": vm_id, "type": "vmNode", "data": vm_data},
            {"id": disk_id, "type": "storageNode", "data": storage},
        ],
        "edges": [
            {
                "source": vm_id,
                "target": disk_id,
                "sourceHandle": ctrl,
                "targetHandle": "storage-in",
            }
        ],
    }


def test_pattern_sourced_cluster_is_recert():
    # Pattern-captured disks already have OCP installed → recert, never a fresh
    # install, even when the capture didn't persist ocpKubeconfig on the member.
    topo = _ocp_cluster_topology(pattern=True)
    assert _cluster_uses_recert(topo, {}) is True


def test_ocp_kubeconfig_member_is_recert():
    topo = _ocp_cluster_topology(ocp_kubeconfig=True)
    assert _cluster_uses_recert(topo, {}) is True


def test_canvas_added_fresh_cluster_is_not_recert():
    # Blank disks, no kubeconfig → genuinely needs a fresh agent install.
    topo = _ocp_cluster_topology()
    assert _cluster_uses_recert(topo, {}) is False
