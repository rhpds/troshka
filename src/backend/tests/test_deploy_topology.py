"""Tests for deploy_topology.py — covers the 4 uncovered lines."""

from app.services.deploy_topology import (
    _filter_topology_for_host,
    _find_vm_disks,
)


def test_filter_topology_for_host():
    topo = {
        "nodes": [
            {"id": "vm1", "type": "vmNode", "data": {"name": "kept"}},
            {"id": "vm2", "type": "vmNode", "data": {"name": "removed"}},
            {"id": "net1", "type": "networkNode", "data": {"name": "net"}},
        ],
        "edges": [],
        "startOrder": [
            {"vmId": "vm1", "delay": 0},
            {"vmId": "vm2", "delay": 5},
        ],
    }
    result = _filter_topology_for_host(topo, {"vm1"})
    vm_ids = [n["id"] for n in result["nodes"] if n["type"] == "vmNode"]
    assert vm_ids == ["vm1"]
    assert result["nodes"][-1]["type"] == "networkNode"
    assert len(result["startOrder"]) == 1
    assert result["startOrder"][0]["vmId"] == "vm1"


def test_filter_topology_empty_keeps_networks():
    topo = {
        "nodes": [
            {"id": "net1", "type": "networkNode", "data": {}},
        ],
        "edges": [],
    }
    result = _filter_topology_for_host(topo, set())
    assert len(result["nodes"]) == 1


def test_find_vm_disks_rotation_rate():
    topo = {
        "nodes": [
            {
                "id": "vm1",
                "type": "vmNode",
                "data": {
                    "diskControllers": [
                        {"id": "dp-ctrl1", "bus": "sata", "rotationRate": 1},
                    ],
                },
            },
            {
                "id": "disk1",
                "type": "storageNode",
                "data": {"name": "ssd-disk", "size": 100, "format": "qcow2"},
            },
        ],
        "edges": [
            {
                "source": "vm1",
                "target": "disk1",
                "sourceHandle": "dp-ctrl1",
                "targetHandle": "storage-top",
            },
        ],
    }
    disks = _find_vm_disks("vm1", topo)
    assert len(disks) == 1
    assert disks[0]["rotation_rate"] == 1
    assert disks[0]["bus"] == "sata"


def test_metadata_bridges_first_network_only():
    from app.services.deploy_topology import metadata_bridges_for_topology

    topo = {
        "nodes": [
            {"id": "mgmt", "type": "networkNode", "data": {"name": "mgmt"}},
            {"id": "lab", "type": "networkNode", "data": {"name": "lab"}},
        ],
        "edges": [],
    }
    vni_map = {"mgmt": 1782, "lab": 1783}
    assert metadata_bridges_for_topology(topo, vni_map) == ["br-1782"]
