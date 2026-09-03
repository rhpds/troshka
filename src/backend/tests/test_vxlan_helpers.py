"""Tests for vxlan.py — VNI allocation, transit subnets, host network config."""

import os
import tempfile
import uuid
from unittest.mock import patch

from app.models.project import Project
from app.models.user import User
from app.services.vxlan import (
    VNI_MIN,
    _get_all_used_vnis,
    _transit_subnet,
    allocate_vnis_for_project,
    build_host_network_config,
)
from tests.conftest import TestSession


def _make_user(db):
    u = User(
        id=str(uuid.uuid4()),
        email=f"vxlan-test-{uuid.uuid4().hex[:6]}@example.com",
        display_name="VXLAN Test",
        role="user",
        auth_source="local",
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


# ---------------------------------------------------------------------------
# _transit_subnet
# ---------------------------------------------------------------------------


def test_transit_subnet_basic():
    """Transit subnet returns valid /30 addresses."""
    result = _transit_subnet(1000)
    assert "host_ip" in result
    assert "ns_ip" in result
    assert "cidr" in result
    assert result["cidr"].endswith("/30")


def test_transit_subnet_deterministic():
    """Same VNI always produces the same subnet."""
    r1 = _transit_subnet(5000)
    r2 = _transit_subnet(5000)
    assert r1 == r2


def test_transit_subnet_different_vnis():
    """Different VNIs produce different subnets."""
    r1 = _transit_subnet(1000)
    r2 = _transit_subnet(1001)
    assert r1["host_ip"] != r2["host_ip"]


def test_transit_subnet_host_ns_ip_adjacent():
    """Host and NS IPs are adjacent in the /30."""
    result = _transit_subnet(1000)
    host_last = int(result["host_ip"].split(".")[-1])
    ns_last = int(result["ns_ip"].split(".")[-1])
    assert ns_last == host_last + 1


def test_transit_subnet_172_30_prefix():
    """Transit subnets are in the 172.30.0.0/16 range."""
    result = _transit_subnet(1000)
    assert result["host_ip"].startswith("172.30.")
    assert result["ns_ip"].startswith("172.30.")


# ---------------------------------------------------------------------------
# _get_all_used_vnis
# ---------------------------------------------------------------------------


def test_get_all_used_vnis_empty():
    """No projects -> empty set."""
    db = TestSession()
    try:
        result = _get_all_used_vnis(db)
        assert isinstance(result, set)
        # May have VNIs from other tests, but should be a set
    finally:
        db.close()


def test_get_all_used_vnis_with_projects():
    """Projects with vni_map contribute to used VNIs."""
    db = TestSession()
    try:
        user = _make_user(db)
        p = Project(
            id=str(uuid.uuid4()),
            name="vni-test",
            owner_id=user.id,
            state="active",
            vni_map={"net-1": 2000, "net-2": 2001},
        )
        db.add(p)
        db.commit()

        result = _get_all_used_vnis(db)
        assert 2000 in result
        assert 2001 in result
    finally:
        db.rollback()
        db.close()


# ---------------------------------------------------------------------------
# allocate_vnis_for_project
# ---------------------------------------------------------------------------


def test_allocate_vnis_basic():
    """Allocates unique VNIs for network nodes."""
    db = TestSession()
    try:
        topology = {
            "nodes": [
                {
                    "id": "net-a",
                    "type": "networkNode",
                    "data": {"subtype": "network", "name": "lan1"},
                },
                {
                    "id": "net-b",
                    "type": "networkNode",
                    "data": {"subtype": "network", "name": "lan2"},
                },
            ]
        }

        # Use a temp file for HWM to avoid interfering with real state
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".vni_hwm", delete=False
        ) as f:
            hwm_path = f.name
            f.write(str(VNI_MIN - 1))

        with patch("app.services.vxlan.os.path.join", return_value=hwm_path):
            result = allocate_vnis_for_project(db, topology)

        assert len(result) == 2
        assert "net-a" in result
        assert "net-b" in result
        assert result["net-a"] != result["net-b"]
        assert result["net-a"] >= VNI_MIN
        assert result["net-b"] >= VNI_MIN
        os.unlink(hwm_path)
    finally:
        db.rollback()
        db.close()


def test_allocate_vnis_skips_non_network_nodes():
    """Non-network nodes and gateways/routers are skipped."""
    db = TestSession()
    try:
        topology = {
            "nodes": [
                {
                    "id": "vm-1",
                    "type": "vmNode",
                    "data": {"name": "vm"},
                },
                {
                    "id": "gw-1",
                    "type": "networkNode",
                    "data": {"subtype": "gateway", "name": "gw"},
                },
                {
                    "id": "rtr-1",
                    "type": "networkNode",
                    "data": {"subtype": "router", "name": "rtr"},
                },
                {
                    "id": "net-1",
                    "type": "networkNode",
                    "data": {"subtype": "network", "name": "lan"},
                },
            ]
        }

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".vni_hwm", delete=False
        ) as f:
            hwm_path = f.name
            f.write(str(VNI_MIN - 1))

        with patch("app.services.vxlan.os.path.join", return_value=hwm_path):
            result = allocate_vnis_for_project(db, topology)

        assert len(result) == 1
        assert "net-1" in result
        assert "vm-1" not in result
        assert "gw-1" not in result
        assert "rtr-1" not in result
        os.unlink(hwm_path)
    finally:
        db.rollback()
        db.close()


def test_allocate_vnis_skips_bmc_networks():
    """BMC network type nodes are skipped."""
    db = TestSession()
    try:
        topology = {
            "nodes": [
                {
                    "id": "bmc-1",
                    "type": "networkNode",
                    "data": {"subtype": "network", "networkType": "bmc", "name": "bmc"},
                },
            ]
        }

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".vni_hwm", delete=False
        ) as f:
            hwm_path = f.name
            f.write(str(VNI_MIN - 1))

        with patch("app.services.vxlan.os.path.join", return_value=hwm_path):
            result = allocate_vnis_for_project(db, topology)

        assert len(result) == 0
        os.unlink(hwm_path)
    finally:
        db.rollback()
        db.close()


def test_allocate_vnis_empty_topology():
    """Empty topology returns empty map."""
    db = TestSession()
    try:
        result = allocate_vnis_for_project(db, {"nodes": []})
        assert result == {}
    finally:
        db.close()


def test_allocate_vnis_hwm_file_persists():
    """HWM file is updated after allocation."""
    db = TestSession()
    try:
        topology = {
            "nodes": [
                {
                    "id": "net-1",
                    "type": "networkNode",
                    "data": {"subtype": "network", "name": "lan"},
                },
            ]
        }

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".vni_hwm", delete=False
        ) as f:
            hwm_path = f.name
            f.write(str(VNI_MIN - 1))

        with patch("app.services.vxlan.os.path.join", return_value=hwm_path):
            result = allocate_vnis_for_project(db, topology)

        with open(hwm_path) as f:
            saved_hwm = int(f.read().strip())

        assert saved_hwm == result["net-1"]
        os.unlink(hwm_path)
    finally:
        db.rollback()
        db.close()


def test_allocate_vnis_respects_existing_hwm():
    """Allocation starts above existing HWM value."""
    db = TestSession()
    try:
        topology = {
            "nodes": [
                {
                    "id": "net-1",
                    "type": "networkNode",
                    "data": {"subtype": "network", "name": "lan"},
                },
            ]
        }

        high_hwm = 50000
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".vni_hwm", delete=False
        ) as f:
            hwm_path = f.name
            f.write(str(high_hwm))

        with patch("app.services.vxlan.os.path.join", return_value=hwm_path):
            result = allocate_vnis_for_project(db, topology)

        assert result["net-1"] > high_hwm
        os.unlink(hwm_path)
    finally:
        db.rollback()
        db.close()


def test_allocate_vnis_missing_hwm_file():
    """Missing HWM file defaults to VNI_MIN - 1."""
    db = TestSession()
    try:
        topology = {
            "nodes": [
                {
                    "id": "net-1",
                    "type": "networkNode",
                    "data": {"subtype": "network", "name": "lan"},
                },
            ]
        }

        # Point to a nonexistent file
        hwm_path = "/tmp/nonexistent_vni_hwm_test_file"
        if os.path.exists(hwm_path):
            os.unlink(hwm_path)

        with patch("app.services.vxlan.os.path.join", return_value=hwm_path):
            result = allocate_vnis_for_project(db, topology)

        assert result["net-1"] >= VNI_MIN
        # Clean up
        if os.path.exists(hwm_path):
            os.unlink(hwm_path)
    finally:
        db.rollback()
        db.close()


# ---------------------------------------------------------------------------
# build_host_network_config
# ---------------------------------------------------------------------------


def test_build_host_network_config_basic():
    """Basic network config with one network."""
    topology = {
        "nodes": [
            {
                "id": "net-1",
                "type": "networkNode",
                "data": {
                    "subtype": "network",
                    "name": "lab-net",
                    "cidr": "10.0.0.0/24",
                    "dhcp": True,
                    "dns": True,
                    "dnsDomain": "lab.local",
                },
            },
        ],
        "edges": [],
    }
    vni_map = {"net-1": 1500}

    result = build_host_network_config(topology, vni_map, ["10.0.0.2"])

    assert len(result["networks"]) == 1
    net = result["networks"][0]
    assert net["name"] == "lab-net"
    assert net["vni"] == 1500
    assert net["bridge_name"] == "br-1500"
    assert net["vxlan_name"] == "vxlan-1500"
    assert net["dhcp_enabled"] is True
    assert net["dns_enabled"] is True
    assert net["dns_domain"] == "lab.local"
    assert net["peers"] == ["10.0.0.2"]


def test_build_host_network_config_dhcp_auto_generate():
    """DHCP config is auto-generated from CIDR when not explicitly set."""
    topology = {
        "nodes": [
            {
                "id": "net-1",
                "type": "networkNode",
                "data": {
                    "subtype": "network",
                    "name": "auto-dhcp",
                    "cidr": "10.0.0.0/24",
                    "dhcp": True,
                },
            },
        ],
        "edges": [],
    }
    vni_map = {"net-1": 2000}

    result = build_host_network_config(topology, vni_map, [])
    net = result["networks"][0]

    assert "dhcp_config" in net
    dhcp = net["dhcp_config"]
    assert dhcp["gateway"] == "10.0.0.1"
    assert dhcp["range_start"]
    assert dhcp["range_end"]
    assert dhcp["lease_time"] == "24h"


def test_build_host_network_config_no_dhcp():
    """Network without DHCP has no dhcp_config."""
    topology = {
        "nodes": [
            {
                "id": "net-1",
                "type": "networkNode",
                "data": {"subtype": "network", "name": "no-dhcp", "dhcp": False},
            },
        ],
        "edges": [],
    }
    vni_map = {"net-1": 3000}

    result = build_host_network_config(topology, vni_map, [])
    assert "dhcp_config" not in result["networks"][0]


def test_build_host_network_config_gateway():
    """Gateway node produces gateway config."""
    topology = {
        "nodes": [
            {
                "id": "net-1",
                "type": "networkNode",
                "data": {"subtype": "network", "name": "lan"},
            },
            {
                "id": "gw-1",
                "type": "networkNode",
                "data": {
                    "subtype": "gateway",
                    "name": "gw",
                    "gatewayMode": "nat",
                    "portForwards": [],
                },
            },
        ],
        "edges": [],
        "externalIps": [],
    }
    vni_map = {"net-1": 4000}

    result = build_host_network_config(topology, vni_map, [])

    assert result["gateway"] is not None
    assert result["gateway"]["name"] == "gw"
    assert result["gateway"]["mode"] == "nat"


def test_build_host_network_config_router():
    """Router node produces router config."""
    topology = {
        "nodes": [
            {
                "id": "net-1",
                "type": "networkNode",
                "data": {"subtype": "network", "name": "lan1"},
            },
            {
                "id": "net-2",
                "type": "networkNode",
                "data": {"subtype": "network", "name": "lan2"},
            },
            {
                "id": "rtr-1",
                "type": "networkNode",
                "data": {
                    "subtype": "router",
                    "name": "router1",
                    "staticRoutes": [{"dst": "192.168.0.0/24", "gw": "10.0.0.1"}],
                },
            },
        ],
        "edges": [
            {"source": "rtr-1", "target": "net-1"},
            {"source": "rtr-1", "target": "net-2"},
        ],
    }
    vni_map = {"net-1": 5000, "net-2": 5001}

    result = build_host_network_config(topology, vni_map, [])

    assert len(result["routers"]) == 1
    rtr = result["routers"][0]
    assert rtr["name"] == "router1"
    assert set(rtr["connected_vnis"]) == {5000, 5001}
    assert len(rtr["static_routes"]) == 1


def test_build_host_network_config_empty_topology():
    """Empty topology returns empty config."""
    result = build_host_network_config({"nodes": [], "edges": []}, {}, [])

    assert result["networks"] == []
    assert result["gateway"] is None
    assert result["routers"] == []
    assert result["loadbalancer"] is None


def test_build_host_network_config_connected_vms():
    """VMs connected to network appear in connected_vms."""
    topology = {
        "nodes": [
            {
                "id": "net-1",
                "type": "networkNode",
                "data": {"subtype": "network", "name": "lan"},
            },
            {
                "id": "vm-1",
                "type": "vmNode",
                "data": {"name": "server1", "nics": []},
            },
        ],
        "edges": [{"source": "vm-1", "target": "net-1"}],
    }
    vni_map = {"net-1": 6000}

    result = build_host_network_config(topology, vni_map, [])
    net = result["networks"][0]
    assert len(net["connected_vms"]) == 1
    assert net["connected_vms"][0]["name"] == "server1"


def test_build_host_network_config_pxe():
    """PXE-enabled network includes pxe_config."""
    topology = {
        "nodes": [
            {
                "id": "net-1",
                "type": "networkNode",
                "data": {
                    "subtype": "network",
                    "name": "pxe-net",
                    "pxeEnabled": True,
                    "pxeServerMode": "builtin",
                },
            },
            {
                "id": "vm-1",
                "type": "vmNode",
                "data": {
                    "name": "pxe-vm",
                    "nics": [],
                    "pxeBootIsoId": "iso-123",
                },
            },
        ],
        "edges": [{"source": "vm-1", "target": "net-1"}],
    }
    vni_map = {"net-1": 7000}

    result = build_host_network_config(topology, vni_map, [])
    net = result["networks"][0]

    assert "pxe_config" in net
    assert net["pxe_config"]["server_mode"] == "builtin"
    assert "iso_path" in net["pxe_config"]
    assert net["pxe_config"]["http_port"] == 8080 + (7000 % 1000)


def test_build_host_network_config_loadbalancer():
    """Load balancer node produces lb config."""
    topology = {
        "nodes": [
            {
                "id": "lb-1",
                "type": "networkNode",
                "data": {
                    "networkType": "loadbalancer",
                    "name": "api-lb",
                    "frontends": [{"port": 6443, "protocol": "tcp"}],
                    "lbIp": "10.0.0.100",
                    "external": True,
                },
            },
            {
                "id": "vm-1",
                "type": "vmNode",
                "data": {
                    "name": "master1",
                    "nics": [{"ip": "10.0.0.11", "mac": "00:11:22:33:44:55"}],
                },
            },
        ],
        "edges": [{"source": "lb-1", "target": "vm-1"}],
    }

    result = build_host_network_config(topology, {}, [])

    assert result["loadbalancer"] is not None
    lb = result["loadbalancer"]
    assert lb["name"] == "api-lb"
    assert lb["lb_ip"] == "10.0.0.100"
    assert lb["external"] is True
    assert len(lb["backends"]) == 1
    assert lb["backends"][0]["ip"] == "10.0.0.11"


def test_build_host_network_config_loadbalancer_pod_backend():
    topology = {
        "nodes": [
            {
                "id": "lb-1",
                "type": "networkNode",
                "data": {
                    "networkType": "loadbalancer",
                    "name": "api-lb",
                    "frontends": [
                        {
                            "name": "https",
                            "bindPort": 443,
                            "mode": "tcp",
                            "backendPort": 443,
                        }
                    ],
                    "lbIp": "10.0.0.100",
                    "external": True,
                },
            },
            {
                "id": "pod-1",
                "type": "containerNode",
                "data": {
                    "name": "router",
                    "isPod": True,
                    "nics": [
                        {"id": "nic-1", "ip": "10.0.0.50", "mac": "52:54:00:00:00:01"}
                    ],
                },
            },
        ],
        "edges": [{"source": "lb-1", "target": "pod-1"}],
    }

    result = build_host_network_config(topology, {}, [])
    lb = result["loadbalancer"]
    assert lb is not None
    assert len(lb["backends"]) == 1
    assert lb["backends"][0]["name"] == "router"
    assert lb["backends"][0]["ip"] == "10.0.0.50"


def test_cluster_vip_reservations_dedups_identical_api_ingress():
    """api_vip == ingress_vip (SNO-style) must yield ONE dhcp-host, not two —
    a duplicate reservation for the same address makes dnsmasq exit 1."""
    from app.services.vxlan import _cluster_vip_reservations

    nodes = [
        {"id": "net1", "type": "networkNode", "data": {}},
        {
            "id": "cluster-ocp",
            "type": "clusterNode",
            "data": {"name": "ocp", "apiVip": "10.0.0.10", "ingressVip": "10.0.0.10"},
        },
        {"id": "cp-0", "type": "vmNode", "parentId": "cluster-ocp", "data": {}},
    ]
    edges = [{"source": "net1", "target": "cp-0"}]

    res = _cluster_vip_reservations("net1", nodes, edges)
    assert len(res) == 1
    assert res[0]["ip"] == "10.0.0.10"
    assert res[0]["mac"] == "02:00:0a:00:00:0a"


def test_cluster_vip_reservations_distinct_api_ingress():
    """Distinct api/ingress VIPs (compact/standard) reserve BOTH addresses."""
    from app.services.vxlan import _cluster_vip_reservations

    nodes = [
        {"id": "net1", "type": "networkNode", "data": {}},
        {
            "id": "cluster-ocp",
            "type": "clusterNode",
            "data": {"name": "ocp", "apiVip": "10.0.0.10", "ingressVip": "10.0.0.11"},
        },
        {"id": "cp-0", "type": "vmNode", "parentId": "cluster-ocp", "data": {}},
    ]
    edges = [{"source": "net1", "target": "cp-0"}]

    res = _cluster_vip_reservations("net1", nodes, edges)
    assert sorted(r["ip"] for r in res) == ["10.0.0.10", "10.0.0.11"]
