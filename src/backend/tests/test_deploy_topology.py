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


def test_find_vm_disks_ide_bus_from_dp_left_handle():
    topo = {
        "nodes": [
            {
                "id": "vm1",
                "type": "vmNode",
                "data": {
                    "diskControllers": [
                        {
                            "id": "dp-a47aec13-d604-4fbf-84a8-8eb99ae9b82b",
                            "bus": "ide",
                            "name": "disk0",
                        }
                    ]
                },
            },
            {
                "id": "stor1",
                "type": "storageNode",
                "data": {"format": "qcow2", "size": 10},
            },
        ],
        "edges": [
            {
                "source": "stor1",
                "target": "vm1",
                "sourceHandle": "right",
                "targetHandle": "dp-dp-a47aec13-d604-4fbf-84a8-8eb99ae9b82b-left",
            }
        ],
    }
    disks = _find_vm_disks("vm1", topo)
    assert len(disks) == 1
    assert disks[0]["bus"] == "ide"


def test_normalize_disk_port_id_formats():
    from app.services.deploy_topology import _normalize_disk_port_id

    assert (
        _normalize_disk_port_id("dp-dp-a47aec13-d604-4fbf-84a8-8eb99ae9b82b-left")
        == "dp-a47aec13-d604-4fbf-84a8-8eb99ae9b82b"
    )
    assert _normalize_disk_port_id("dp-ctrl1") == "dp-ctrl1"
    assert _normalize_disk_port_id("dp-dp-ctrl1-left") == "dp-ctrl1"


def test_build_troshkavm_disk_spec_uses_controller_bus():
    from app.services.deploy_topology import build_troshkavm_vm_spec

    topo = {
        "nodes": [
            {
                "id": "vm1",
                "type": "vmNode",
                "data": {
                    "id": "vm1",
                    "name": "rtr",
                    "diskControllers": [
                        {"id": "dp-ctrl1", "bus": "ide", "name": "disk0"},
                    ],
                    "nics": [
                        {"id": "nic1", "mac": "52:54:00:00:00:01", "model": "e1000"}
                    ],
                },
            },
            {
                "id": "stor1",
                "type": "storageNode",
                "data": {
                    "id": "stor1",
                    "source": "library",
                    "libraryItemId": "lib-1",
                    "format": "qcow2",
                    "size": 20,
                    "resolvedS3Path": "library/lib-1.qcow2",
                },
            },
        ],
        "edges": [
            {
                "source": "vm1",
                "target": "stor1",
                "sourceHandle": "dp-dp-ctrl1-left",
            }
        ],
    }
    spec = build_troshkavm_vm_spec(
        "vm1",
        {"name": "rtr", "vcpus": 2, "ram_gb": 4, "firmware": "bios"},
        topo,
    )
    assert "machineType" not in spec
    assert spec["disks"][0]["bus"] == "ide"
    assert spec["nics"][0]["model"] == "e1000"


def _disk_spec_topology(source_size_gb=None):
    data = {
        "id": "stor1",
        "source": "library",
        "libraryItemId": "lib-1",
        "format": "qcow2",
        "size": 20,
        "resolvedS3Path": "library/lib-1.qcow2",
    }
    if source_size_gb is not None:
        data["sourceSizeGb"] = source_size_gb
    return {
        "nodes": [
            {"id": "stor1", "type": "storageNode", "data": data},
        ],
        "edges": [],
    }


def test_build_troshkavm_disk_spec_emits_source_size_gb_for_library():
    from app.services.deploy_topology import build_troshkavm_disk_spec

    topo = _disk_spec_topology(source_size_gb=11)
    disk = {
        "node_id": "stor1",
        "size": 20,
        "format": "qcow2",
        "source": "library",
        "libraryItemId": "lib-1",
        "resolvedS3Path": "library/lib-1.qcow2",
    }
    spec = build_troshkavm_disk_spec(disk, topo)
    assert "libraryImage" in spec
    assert spec["sourceSizeGb"] == 11


def test_build_troshkavm_disk_spec_omits_source_size_gb_when_absent():
    from app.services.deploy_topology import build_troshkavm_disk_spec

    topo = _disk_spec_topology(source_size_gb=None)
    disk = {
        "node_id": "stor1",
        "size": 20,
        "format": "qcow2",
        "source": "library",
        "libraryItemId": "lib-1",
        "resolvedS3Path": "library/lib-1.qcow2",
    }
    spec = build_troshkavm_disk_spec(disk, topo)
    assert "sourceSizeGb" not in spec


def test_build_troshkavm_disk_spec_omits_source_size_gb_when_zero():
    from app.services.deploy_topology import build_troshkavm_disk_spec

    topo = _disk_spec_topology(source_size_gb=0)
    disk = {
        "node_id": "stor1",
        "size": 20,
        "format": "qcow2",
        "source": "library",
        "libraryItemId": "lib-1",
        "resolvedS3Path": "library/lib-1.qcow2",
    }
    spec = build_troshkavm_disk_spec(disk, topo)
    assert "sourceSizeGb" not in spec


def test_build_troshkavm_disk_spec_emits_source_size_gb_for_pattern():
    from app.services.deploy_topology import build_troshkavm_disk_spec

    topo = {
        "nodes": [
            {
                "id": "stor1",
                "type": "storageNode",
                "data": {
                    "id": "stor1",
                    "source": "pattern",
                    "patternId": "pat-1",
                    "patternDiskId": "pd-1",
                    "format": "qcow2",
                    "size": 40,
                    "resolvedS3Path": "patterns/pat-1/pd-1.qcow2",
                    "sourceSizeGb": 40,
                },
            }
        ],
        "edges": [],
    }
    disk = {
        "node_id": "stor1",
        "size": 40,
        "format": "qcow2",
        "source": "pattern",
        "patternId": "pat-1",
        "resolvedS3Path": "patterns/pat-1/pd-1.qcow2",
    }
    spec = build_troshkavm_disk_spec(disk, topo)
    assert "patternImage" in spec
    assert spec["sourceSizeGb"] == 40


def test_build_troshkavm_disk_spec_no_source_size_gb_for_blank():
    from app.services.deploy_topology import build_troshkavm_disk_spec

    topo = {
        "nodes": [
            {
                "id": "stor1",
                "type": "storageNode",
                "data": {
                    "id": "stor1",
                    "source": "blank",
                    "size": 100,
                    "format": "qcow2",
                    "sourceSizeGb": 100,
                },
            }
        ],
        "edges": [],
    }
    disk = {"node_id": "stor1", "size": 100, "format": "qcow2", "source": "blank"}
    spec = build_troshkavm_disk_spec(disk, topo)
    assert spec["blank"] is True
    assert "sourceSizeGb" not in spec


def test_build_troshkavm_vm_spec_secure_boot_firmware():
    from app.services.deploy_topology import build_troshkavm_vm_spec

    topo = {
        "nodes": [
            {
                "id": "vm1",
                "type": "vmNode",
                "data": {
                    "id": "vm1",
                    "firmware": "uefi",
                    "secureBoot": True,
                    "nics": [],
                },
            }
        ]
    }
    spec = build_troshkavm_vm_spec(
        "vm1",
        {"name": "vm", "vcpus": 2, "ram_gb": 4, "firmware": "uefi"},
        topo,
    )
    assert spec["firmware"] == "uefi-secure"


def test_build_troshkavm_vm_spec_video_and_input():
    from app.services.deploy_topology import build_troshkavm_vm_spec

    topo = {
        "nodes": [
            {
                "id": "vm1",
                "type": "vmNode",
                "data": {
                    "id": "vm1",
                    "videoModel": "qxl",
                    "inputModel": "usb",
                    "nics": [],
                },
            }
        ]
    }
    spec = build_troshkavm_vm_spec(
        "vm1",
        {"name": "vm", "vcpus": 2, "ram_gb": 4},
        topo,
    )
    assert spec["videoModel"] == "qxl"
    assert spec["inputModel"] == "usb"


def test_build_troshkavm_vm_spec_serial_defaults():
    from app.services.deploy_topology import build_troshkavm_vm_spec

    topo = {
        "nodes": [
            {
                "id": "vm1",
                "type": "vmNode",
                "data": {"id": "vm1", "nics": []},
            }
        ]
    }
    spec = build_troshkavm_vm_spec(
        "vm1",
        {"name": "vm", "vcpus": 2, "ram_gb": 4},
        topo,
    )
    assert spec["serialModel"] == "isa"
    assert spec["serialConsole"] is True


def test_build_troshkavm_vm_spec_serial_custom():
    from app.services.deploy_topology import build_troshkavm_vm_spec

    topo = {
        "nodes": [
            {
                "id": "vm1",
                "type": "vmNode",
                "data": {
                    "id": "vm1",
                    "serialModel": "virtio",
                    "serialConsole": False,
                    "nics": [],
                },
            }
        ]
    }
    spec = build_troshkavm_vm_spec(
        "vm1",
        {"name": "vm", "vcpus": 2, "ram_gb": 4},
        topo,
    )
    assert spec["serialModel"] == "virtio"
    assert spec["serialConsole"] is False


def test_build_troshkavm_vm_spec_legacy_root_bus():
    from app.services.deploy_topology import build_troshkavm_vm_spec

    topo = {
        "nodes": [
            {
                "id": "vm1",
                "type": "vmNode",
                "data": {
                    "id": "vm1",
                    "legacyRootBus": True,
                    "nics": [],
                },
            }
        ]
    }
    spec = build_troshkavm_vm_spec(
        "vm1",
        {"name": "rtr2", "vcpus": 2, "ram_gb": 4},
        topo,
    )
    assert spec["legacyRootBus"] is True


def test_build_troshkavm_vm_spec_nested_virt():
    from app.services.deploy_topology import build_troshkavm_vm_spec

    topo = {
        "nodes": [
            {
                "id": "vm1",
                "type": "vmNode",
                "data": {
                    "id": "vm1",
                    "nestedVirt": True,
                    "nics": [],
                },
            }
        ]
    }
    spec = build_troshkavm_vm_spec(
        "vm1",
        {"name": "hypervisor", "vcpus": 2, "ram_gb": 4},
        topo,
    )
    assert spec["nestedVirt"] is True


def test_build_troshkavm_vm_spec_nested_virt_absent_by_default():
    from app.services.deploy_topology import build_troshkavm_vm_spec

    topo = {
        "nodes": [
            {
                "id": "vm1",
                "type": "vmNode",
                "data": {"id": "vm1", "nics": []},
            }
        ]
    }
    spec = build_troshkavm_vm_spec(
        "vm1",
        {"name": "plain", "vcpus": 2, "ram_gb": 4},
        topo,
    )
    assert "nestedVirt" not in spec


def test_build_troshkavm_nic_specs_includes_network_ref():
    from app.services.deploy_topology import build_troshkavm_vm_spec

    nic_id = "nic-f76ca5cd-0051-4081-bf7f-f2a206e2245e"
    net_id = "fa889200-1cac-43e9-b49c-5d84a80abd6f"
    vm_id = "281550eb-d04b-40ef-be8f-5bf8debe9fa4"
    topo = {
        "nodes": [
            {"id": net_id, "type": "networkNode", "data": {"name": "lab"}},
            {
                "id": vm_id,
                "type": "vmNode",
                "data": {
                    "id": vm_id,
                    "nics": [{"id": nic_id, "mac": "52:54:00:11:87:a6"}],
                },
            },
        ],
        "edges": [
            {
                "source": net_id,
                "target": vm_id,
                "targetHandle": f"nic-{nic_id}-bottom",
            }
        ],
    }
    spec = build_troshkavm_vm_spec(
        vm_id,
        {"name": "rtr3", "vcpus": 2, "ram_gb": 4},
        topo,
    )
    assert spec["nics"][0]["networkRef"] == "net-fa889200"


def test_validate_kubevirt_vm_disk_buses_rejects_ide():
    from app.services.deploy_topology import validate_kubevirt_vm_disk_buses

    vm_id = "281550eb-d04b-40ef-be8f-5bf8debe9fa4"
    storage_id = "disk-storage-1"
    topo = {
        "nodes": [
            {
                "id": storage_id,
                "type": "storageNode",
                "data": {"name": "rtr3-disk0", "format": "qcow2"},
            },
            {
                "id": vm_id,
                "type": "vmNode",
                "data": {
                    "id": vm_id,
                    "name": "rtr3",
                    "diskControllers": [
                        {"id": "dp-ctrl1", "bus": "ide", "name": "disk0"},
                    ],
                },
            },
        ],
        "edges": [
            {
                "source": storage_id,
                "target": vm_id,
                "targetHandle": "dp-ctrl1-left",
            }
        ],
    }
    err = validate_kubevirt_vm_disk_buses(
        topo,
        [vm_id],
        capabilities={"diskBuses": ["virtio", "scsi", "sata", "usb"]},
    )
    assert err is not None
    assert "IDE" in err
    assert "rtr3" in err


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
            "dns_network": "mgmt",
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
                "id": "net-mgmt",
                "type": "networkNode",
                "data": {
                    "name": "mgmt",
                    "subtype": "network",
                    "cidr": "10.0.0.0/24",
                    "dns": True,
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
                    "dnsNetwork": "mgmt",
                    "nics": [],
                },
            },
        ],
        "edges": [
            {
                "id": "gw-net",
                "source": "gw-1",
                "target": "net-mgmt",
                "sourceHandle": "bottom",
                "targetHandle": "top",
            },
        ],
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
    showroom["data"]["dnsNetwork"] = "mgmt"
    assert validate_showroom_topology(topo) == []


def test_validate_showroom_topology_requires_dns_network():
    from app.services.deploy_topology import validate_showroom_topology

    topo = _showroom_topology()
    topo["showroom"]["dns_network"] = ""
    showroom = next(n for n in topo["nodes"] if n["data"].get("isShowroom"))
    showroom["data"]["dnsNetwork"] = ""
    # mgmt has DNS — implicit default applies
    assert validate_showroom_topology(topo) == []

    topo["nodes"] = [n for n in topo["nodes"] if n["id"] != "net-mgmt"]
    errors = validate_showroom_topology(topo)
    assert any("gateway" in e.lower() for e in errors)


def test_showroom_dns_network_implicit_default():
    from app.services.deploy_topology import _showroom_dns_network_name

    topo = _showroom_topology()
    topo["showroom"]["dns_network"] = ""
    showroom = next(n for n in topo["nodes"] if n["data"].get("isShowroom"))
    showroom["data"]["dnsNetwork"] = ""
    assert _showroom_dns_network_name(topo) == "mgmt"


def test_showroom_dns_network_implicit_default_ignores_non_gateway_dns():
    from app.services.deploy_topology import _showroom_dns_network_name

    topo = _showroom_topology()
    topo["showroom"]["dns_network"] = ""
    showroom = next(n for n in topo["nodes"] if n["data"].get("isShowroom"))
    showroom["data"]["dnsNetwork"] = ""
    topo["nodes"].append(
        {
            "id": "net-cluster",
            "type": "networkNode",
            "data": {
                "name": "cluster",
                "subtype": "network",
                "cidr": "192.168.50.0/24",
                "dns": True,
            },
        }
    )
    assert _showroom_dns_network_name(topo) == "mgmt"


def test_validate_showroom_topology_requires_gateway_connected_dns():
    from app.services.deploy_topology import validate_showroom_topology

    topo = _showroom_topology()
    topo["showroom"]["dns_network"] = "cluster"
    showroom = next(n for n in topo["nodes"] if n["data"].get("isShowroom"))
    showroom["data"]["dnsNetwork"] = "cluster"
    topo["nodes"].append(
        {
            "id": "net-cluster",
            "type": "networkNode",
            "data": {
                "name": "cluster",
                "subtype": "network",
                "cidr": "192.168.50.0/24",
                "dns": True,
            },
        }
    )
    errors = validate_showroom_topology(topo)
    assert any("connected to the gateway" in e for e in errors)


def test_inject_showroom_gateway_outbound_ports_restrict():
    from app.services.deploy_topology import inject_showroom_gateway_port_forwards

    topo = _showroom_topology()
    gw = next(n for n in topo["nodes"] if n["id"] == "gw-1")
    gw["data"]["outboundPolicy"] = "restrict"
    gw["data"]["outboundPorts"] = "80"
    assert inject_showroom_gateway_port_forwards(topo, {"net-mgmt": 1000})
    ports = [p.strip() for p in gw["data"]["outboundPorts"].split(",")]
    assert "53" in ports
    assert "443" in ports
    assert "53" in gw["data"]["showroomManagedOutbound"]


def test_validate_showroom_topology_after_inject_allows_restrict_dns():
    from app.services.deploy_topology import (
        inject_showroom_gateway_port_forwards,
        validate_showroom_topology,
    )

    topo = _showroom_topology()
    gw = next(n for n in topo["nodes"] if n["id"] == "gw-1")
    gw["data"]["outboundPolicy"] = "restrict"
    gw["data"]["outboundPorts"] = "80"
    inject_showroom_gateway_port_forwards(topo, {"net-mgmt": 1000})
    assert validate_showroom_topology(topo) == []


def test_showroom_dns_nameserver_from_configured_network():
    from app.services.deploy_topology import _showroom_dns_nameserver

    topo = _showroom_topology()
    topo["nodes"].append(
        {
            "id": "net-cluster",
            "type": "networkNode",
            "data": {
                "name": "cluster",
                "subtype": "network",
                "cidr": "192.168.50.0/24",
                "dns": True,
                "dhcpGateway": "192.168.50.1",
            },
        }
    )
    topo["showroom"]["dns_network"] = "cluster"
    assert _showroom_dns_nameserver(topo) == "192.168.50.1"


def test_showroom_dns_nameserver_prefers_dns_server_ip():
    from app.services.deploy_topology import _showroom_dns_nameserver

    topo = _showroom_topology()
    mgmt = next(n for n in topo["nodes"] if n["id"] == "net-mgmt")
    mgmt["data"]["dnsServerIp"] = "10.0.0.254"
    assert _showroom_dns_nameserver(topo) == "10.0.0.254"


def test_showroom_infra_network():
    from app.services.deploy_topology import showroom_infra_ip, showroom_infra_network

    vni_map = {"net-1": 1000}
    assert showroom_infra_ip(vni_map) == "172.30.232.3"
    nets = showroom_infra_network(vni_map, "52:54:00:00:00:01")
    assert len(nets) == 1
    assert nets[0]["infra_transit"] is True
    assert nets[0]["ip"] == "172.30.232.3"
    assert nets[0]["gateway"] == "172.30.232.2"


def test_showroom_transit_octet3_uses_min_vni():
    from app.services.deploy_topology import showroom_infra_ip, showroom_transit_octet3

    vni_map = {"net-a": 1899, "net-b": 1898}
    assert showroom_transit_octet3(vni_map) == 106
    assert showroom_infra_ip(vni_map) == "172.30.106.3"


def test_showroom_infra_network_dns_nameserver():
    from app.services.deploy_topology import showroom_infra_network

    nets = showroom_infra_network(
        {"net-1": 1000}, "52:54:00:00:00:01", dns_nameserver="10.0.0.1"
    )
    assert nets[0]["dns_nameserver"] == "10.0.0.1"

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
    assert inject_showroom_gateway_port_forwards(topo, vni_map, "kubevirt")
    gw = topo["nodes"][0]["data"]
    ext_ports = {pf["extPort"] for pf in gw["portForwards"]}
    assert ext_ports == {"443"}
    assert all(pf["intIp"] == "172.30.232.3" for pf in gw["portForwards"])
    # On kubevirt/ocpvirt, 443 is served by an OpenShift Route, never bound to the
    # EIP LoadBalancer — so the showroom forward must NOT carry an extIpId.
    showroom_pf = next(pf for pf in gw["portForwards"] if pf["extPort"] == "443")
    assert not showroom_pf.get("extIpId")


def test_inject_showroom_443_binds_eip_on_cloud_providers():
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
    # Cloud providers (ec2/gcp/azure) have no ingress — 443 stays on the EIP.
    inject_showroom_gateway_port_forwards(topo, {"net-1": 1000}, "ec2")
    gw = topo["nodes"][0]["data"]
    showroom_pf = next(pf for pf in gw["portForwards"] if pf["extPort"] == "443")
    assert showroom_pf["extIpId"] == "eip-1"


def test_inject_showroom_keeps_443_off_eip_but_binds_other_ports():
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
                    "portForwards": [
                        # a user-added non-web forward (should bind to the EIP)
                        {
                            "extPort": "9090",
                            "intPort": "9090",
                            "intIp": "10.0.0.50",
                            "proto": "tcp",
                        },
                    ],
                },
            },
            {
                "id": "showroom-1",
                "type": "containerNode",
                "data": {"name": "showroom", "isShowroom": True, "nics": []},
            },
        ],
    }
    inject_showroom_gateway_port_forwards(topo, {"net-1": 1000}, "kubevirt")
    pfs = {pf["extPort"]: pf for pf in topo["nodes"][0]["data"]["portForwards"]}
    assert not pfs["443"].get("extIpId")  # showroom 443 -> Route, not EIP
    assert pfs["9090"]["extIpId"] == "eip-1"  # other ports -> EIP LB


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
