"""Tests for deploy_topology.py — covers the 4 uncovered lines."""

import pytest

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


@pytest.mark.parametrize(
    "cur_data,dep_data,expected",
    [
        (
            {"firmware": "bios"},
            {"firmware": "uefi"},
            {"vm1"},
        ),
        (
            {"firmware": "uefi", "secureBoot": False},
            {"firmware": "uefi", "secureBoot": True},
            {"vm1"},
        ),
        (
            {"videoModel": "virtio"},
            {"videoModel": "vga"},
            {"vm1"},
        ),
        (
            {"inputModel": "virtio"},
            {"inputModel": "usb"},
            {"vm1"},
        ),
        (
            {"smbiosUuid": "11111111-1111-1111-1111-111111111111"},
            {"smbiosUuid": "22222222-2222-2222-2222-222222222222"},
            {"vm1"},
        ),
        (
            {
                "diskControllers": [
                    {"id": "dp-1", "bus": "virtio", "name": "disk0"},
                ]
            },
            {
                "diskControllers": [
                    {"id": "dp-1", "bus": "scsi", "name": "disk0"},
                ]
            },
            {"vm1"},
        ),
        (
            {"pxeBootIsoId": "iso-1"},
            {"pxeBootIsoId": "iso-2"},
            {"vm1"},
        ),
        (
            {"firmware": "bios", "vcpus": 2},
            {"firmware": "bios", "vcpus": 4},
            set(),
        ),
        (
            {"firmware": "bios"},
            {"firmware": "bios"},
            set(),
        ),
    ],
)
def test_vm_ids_needing_redeploy(cur_data, dep_data, expected):
    from app.services.deploy_topology import vm_ids_needing_redeploy

    current = {"nodes": [{"id": "vm1", "type": "vmNode", "data": cur_data}]}
    deployed = {"nodes": [{"id": "vm1", "type": "vmNode", "data": dep_data}]}
    assert vm_ids_needing_redeploy(current, deployed) == expected


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


def _showroom_topology(**overrides):
    topo = {
        "showroom": {
            "enabled": True,
            "content_repo": "https://github.com/example/lab.git",
            "content_ref": "main",
            "build_content": True,
        },
        "nodes": [
            {
                "id": "net-1",
                "type": "networkNode",
                "data": {"name": "mgmt", "subtype": "network", "cidr": "10.0.0.0/24"},
            },
            {
                "id": "gw-1",
                "type": "networkNode",
                "data": {
                    "name": "gateway",
                    "subtype": "gateway",
                    "portForwards": [
                        {
                            "extPort": "80",
                            "intIp": "10.0.0.5",
                            "intPort": "80",
                            "proto": "tcp",
                        },
                    ],
                },
            },
            {
                "id": "sr-1",
                "type": "containerNode",
                "data": {
                    "name": "showroom",
                    "isShowroom": True,
                    "isPod": True,
                    "nics": [
                        {
                            "id": "nic-1",
                            "name": "eth0",
                            "mac": "52:54:00:00:00:01",
                            "ip": "10.0.0.5",
                        }
                    ],
                },
            },
        ],
        "edges": [
            {
                "source": "sr-1",
                "target": "net-1",
                "sourceHandle": "nic-nic-1-top",
                "targetHandle": "bottom",
            },
            {
                "source": "net-1",
                "target": "gw-1",
                "sourceHandle": "left",
                "targetHandle": "right",
            },
        ],
    }
    topo.update(overrides)
    return topo


def test_validate_showroom_topology_ok():
    from app.services.deploy_topology import validate_showroom_topology

    assert validate_showroom_topology(_showroom_topology()) == []


def test_validate_showroom_topology_requires_network():
    from app.services.deploy_topology import validate_showroom_topology

    topo = _showroom_topology()
    topo["edges"] = [topo["edges"][1]]
    errors = validate_showroom_topology(topo)
    assert any("connected to a network" in e for e in errors)


def test_validate_showroom_topology_requires_gateway():
    from app.services.deploy_topology import validate_showroom_topology

    topo = _showroom_topology()
    topo["edges"] = [topo["edges"][0]]
    errors = validate_showroom_topology(topo)
    assert any("gateway" in e for e in errors)


def test_validate_showroom_topology_requires_port_forward():
    from app.services.deploy_topology import validate_showroom_topology

    topo = _showroom_topology()
    topo["nodes"][1]["data"]["portForwards"] = []
    errors = validate_showroom_topology(topo)
    assert any("port forward" in e for e in errors)


def test_validate_showroom_topology_requires_content_repo():
    from app.services.deploy_topology import validate_showroom_topology

    topo = _showroom_topology()
    topo["showroom"]["content_repo"] = ""
    errors = validate_showroom_topology(topo)
    assert any("content repo" in e for e in errors)
