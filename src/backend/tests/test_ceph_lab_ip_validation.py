"""Tests for Ceph Storage lab IP collision checks."""

from app.services.deploy_topology import validate_ceph_lab_ips, validate_topology_ips


def _net_node(net_id="net-cluster", cidr="10.0.0.0/24", dhcp=True):
    return {
        "id": net_id,
        "type": "networkNode",
        "data": {
            "name": "cluster",
            "subtype": "network",
            "cidr": cidr,
            "dhcp": dhcp,
            "dhcpRangeStart": "10.0.0.20",
            "dhcpRangeEnd": "10.0.0.99",
        },
    }


def _ceph_node(network_ref="net-cluster", lab_ip=""):
    return {
        "id": "ceph-1",
        "type": "cephClusterNode",
        "data": {
            "name": "Ceph Storage",
            "networkRef": network_ref,
            "labIp": lab_ip,
            "capacityGi": 300,
            "osdCount": 3,
        },
    }


def test_ceph_default_dot3_ok_when_network_clear():
    topo = {"nodes": [_net_node(), _ceph_node()], "edges": [], "clusters": []}
    assert validate_ceph_lab_ips(topo) == []


def test_ceph_dot3_conflicts_with_vm_nic():
    topo = {
        "nodes": [
            _net_node(),
            _ceph_node(),
            {
                "id": "vm-1",
                "type": "vmNode",
                "data": {
                    "name": "busy",
                    "nics": [{"id": "nic-1", "ip": "10.0.0.3"}],
                },
            },
        ],
        "edges": [
            {
                "id": "e1",
                "source": "vm-1",
                "target": "net-cluster",
                "sourceHandle": "nic-nic-1-top",
                "targetHandle": "top",
            }
        ],
        "clusters": [],
    }
    errors = validate_ceph_lab_ips(topo)
    assert len(errors) == 1
    assert "10.0.0.3" in errors[0]
    assert "busy" in errors[0]


def test_ceph_lab_ip_in_dhcp_pool_rejected():
    topo = {
        "nodes": [_net_node(), _ceph_node(lab_ip="10.0.0.25")],
        "edges": [],
        "clusters": [],
    }
    errors = validate_ceph_lab_ips(topo)
    assert any("DHCP pool" in e for e in errors)


def test_validate_topology_ips_includes_ceph_checks():
    topo = {
        "nodes": [
            _net_node(),
            _ceph_node(),
            {
                "id": "vm-1",
                "type": "vmNode",
                "data": {"name": "x", "nics": [{"id": "nic-1", "ip": "10.0.0.3"}]},
            },
        ],
        "edges": [
            {
                "id": "e1",
                "source": "vm-1",
                "target": "net-cluster",
                "sourceHandle": "nic-nic-1-top",
                "targetHandle": "top",
            }
        ],
        "clusters": [],
    }
    errors = validate_topology_ips(topo)
    assert any("Ceph Storage" in e and "10.0.0.3" in e for e in errors)
