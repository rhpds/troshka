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
                "id": "gw-1",
                "type": "networkNode",
                "data": {
                    "name": "gateway",
                    "subtype": "gateway",
                    "portForwards": [],
                },
            },
            {
                "id": "sr-1",
                "type": "containerNode",
                "data": {
                    "name": "showroom",
                    "isShowroom": True,
                    "isPod": True,
                    "infraNetworking": True,
                    "nics": [],
                },
            },
        ],
        "edges": [],
    }
    topo.update(overrides)
    return topo


def test_validate_showroom_topology_ok():
    from app.services.deploy_topology import validate_showroom_topology

    assert validate_showroom_topology(_showroom_topology()) == []


def test_validate_showroom_topology_requires_gateway():
    from app.services.deploy_topology import validate_showroom_topology

    topo = _showroom_topology()
    topo["nodes"] = [n for n in topo["nodes"] if n["id"] != "gw-1"]
    errors = validate_showroom_topology(topo)
    assert any("gateway" in e.lower() for e in errors)


def test_validate_showroom_topology_requires_content_repo():
    from app.services.deploy_topology import validate_showroom_topology

    topo = _showroom_topology()
    topo["showroom"]["content_repo"] = ""
    showroom = next(n for n in topo["nodes"] if n["data"].get("isShowroom"))
    showroom["data"]["contentRepo"] = ""
    errors = validate_showroom_topology(topo)
    assert any("content repo" in e for e in errors)


def test_validate_showroom_topology_content_repo_from_node():
    from app.services.deploy_topology import validate_showroom_topology

    topo = _showroom_topology()
    topo["showroom"] = None
    showroom = next(n for n in topo["nodes"] if n["data"].get("isShowroom"))
    showroom["data"]["contentRepo"] = "https://github.com/example/repo.git"
    assert validate_showroom_topology(topo) == []


def test_showroom_infra_network():
    from app.services.deploy_topology import showroom_infra_ip, showroom_infra_network

    vni_map = {"net-1": 1000}
    assert showroom_infra_ip(vni_map) == "172.30.232.3"
    nets = showroom_infra_network(vni_map, "52:54:00:00:00:01")
    assert len(nets) == 1
    assert nets[0]["infra_transit"] is True
    assert nets[0]["ip"] == "172.30.232.3"
    assert nets[0]["gateway"] == "172.30.232.2"


def test_inject_showroom_port_forward():
    from app.services.vxlan import _inject_showroom_port_forward

    topo = {
        "nodes": [
            {
                "type": "containerNode",
                "data": {"name": "showroom", "isShowroom": True, "nics": []},
            }
        ]
    }
    stale = [
        {
            "extPort": "443",
            "intIp": "10.0.0.5",
            "intPort": "80",
            "proto": "tcp",
            "extIpId": "eip-1",
        }
    ]
    out = _inject_showroom_port_forward(stale, topo, 1000)
    ext_ports = {pf["extPort"] for pf in out}
    assert ext_ports == {"443"}
    assert all(pf["intIp"] == "172.30.232.3" for pf in out)
    assert not any(pf["intIp"] == "10.0.0.5" for pf in out)


def test_strip_showroom_gateway_access():
    from app.services.deploy_topology import (
        inject_showroom_gateway_port_forwards,
        strip_showroom_gateway_access,
    )

    gw_id = "gw-1"
    topo = {
        "externalIps": [{"id": "eip-1", "name": "IP-1", "ip": ""}],
        "nodes": [
            {
                "id": gw_id,
                "type": "networkNode",
                "data": {
                    "subtype": "gateway",
                    "gatewayMode": "nat-portforward",
                    "portForwards": [
                        {
                            "extPort": "443",
                            "intIp": "172.30.232.3",
                            "intPort": "80",
                            "proto": "tcp",
                            "extIpId": "eip-1",
                        },
                        {
                            "extPort": "2222",
                            "intIp": "10.0.0.5",
                            "intPort": "22",
                            "proto": "tcp",
                            "extIpId": "eip-1",
                        },
                    ],
                },
            },
        ],
    }
    assert strip_showroom_gateway_access(topo)
    gw = next(n for n in topo["nodes"] if n["id"] == gw_id)
    pfs = gw["data"]["portForwards"]
    assert len(pfs) == 1
    assert pfs[0]["extPort"] == "2222"
    assert topo["externalIps"] == [{"id": "eip-1", "name": "IP-1", "ip": ""}]

    topo_with_showroom = {
        **topo,
        "nodes": topo["nodes"]
        + [
            {
                "type": "containerNode",
                "data": {"name": "showroom", "isShowroom": True},
            }
        ],
    }
    topo_with_showroom["externalIps"] = [{"id": "eip-1", "name": "IP-1", "ip": ""}]
    topo_with_showroom["nodes"][0]["data"]["portForwards"] = pfs
    assert inject_showroom_gateway_port_forwards(topo_with_showroom, {"net-1": 1000})
    gw = topo_with_showroom["nodes"][0]
    assert any(pf["extPort"] == "443" for pf in gw["data"]["portForwards"])


def test_inject_showroom_gateway_port_forwards_on_topology():
    from app.services.deploy_topology import inject_showroom_gateway_port_forwards

    topo = {
        "externalIps": [{"id": "eip-1", "name": "IP-1"}],
        "nodes": [
            {
                "id": "gw-1",
                "type": "networkNode",
                "data": {
                    "subtype": "gateway",
                    "gatewayMode": "nat-portforward",
                    "portForwards": [],
                },
            },
            {
                "id": "showroom-1",
                "type": "containerNode",
                "data": {"name": "showroom", "isShowroom": True, "nics": []},
            },
        ],
    }
    vni_map = {"net-1": 1000}
    assert inject_showroom_gateway_port_forwards(topo, vni_map)
    gw = topo["nodes"][0]["data"]
    ext_ports = {pf["extPort"] for pf in gw["portForwards"]}
    assert ext_ports == {"443"}
    assert all(pf["intIp"] == "172.30.232.3" for pf in gw["portForwards"])
    assert all(pf["extIpId"] == "eip-1" for pf in gw["portForwards"])


def test_troshkad_network_entries_includes_infra_transit():
    from app.services.deploy_service import _troshkad_network_entries

    entries = _troshkad_network_entries(
        [
            {
                "bridge": "",
                "ip": "172.30.232.3",
                "cidr": "172.30.232.0/24",
                "gateway": "172.30.232.2",
                "infra_transit": True,
            },
        ]
    )
    assert entries[0]["infra_transit"] is True
    assert entries[0]["bridge"] == ""


def test_inject_showroom_gateway_port_forwards_net_automation_template():
    from pathlib import Path

    import yaml

    from app.services.deploy_topology import inject_showroom_gateway_port_forwards
    from app.services.template_loader import generate_topology_from_template

    tmpl_path = (
        Path(__file__).resolve().parents[3]
        / "example_templates"
        / "net-automation-workshop.yaml"
    )
    topo = generate_topology_from_template(yaml.safe_load(tmpl_path.read_text()))
    vni_map = {
        n["id"]: 1000 + i
        for i, n in enumerate(
            [
                node
                for node in topo["nodes"]
                if node.get("type") == "networkNode"
                and node.get("data", {}).get("subtype") == "network"
            ]
        )
    }
    assert inject_showroom_gateway_port_forwards(topo, vni_map)
    gw = next(n for n in topo["nodes"] if n.get("data", {}).get("subtype") == "gateway")
    ext_ports = {pf["extPort"] for pf in gw["data"]["portForwards"]}
    assert ext_ports == {"443"}
