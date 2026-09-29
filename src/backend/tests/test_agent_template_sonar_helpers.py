"""Unit tests for agent_template helpers extracted for Sonar S3776."""

from app.services.ocp.agent_template import (
    _cidr_contains_any_ip,
    _cluster_network_node,
    _edge_peer_id,
    _find_ocp_mount_device,
    _is_eligible_lab_network_node,
    _member_nic_ips,
    _rhcos_disk_device_for_edge,
)


class TestMemberNicIps:
    def test_collects_ips(self):
        members = [
            {
                "type": "vmNode",
                "data": {"nics": [{"ip": "10.0.0.10"}, {"ip": ""}]},
            },
            {"type": "networkNode", "data": {"nics": [{"ip": "ignored"}]}},
        ]
        assert _member_nic_ips(members) == ["10.0.0.10"]


class TestEligibleLabNetwork:
    def test_rejects_bmc(self):
        node = {
            "type": "networkNode",
            "data": {"subtype": "network", "networkType": "bmc"},
        }
        assert _is_eligible_lab_network_node(node) is False

    def test_accepts_lab(self):
        node = {
            "type": "networkNode",
            "data": {"subtype": "network", "networkType": "lab", "cidr": "10.0.0.0/24"},
        }
        assert _is_eligible_lab_network_node(node) is True


class TestCidrContainsAnyIp:
    def test_match(self):
        assert _cidr_contains_any_ip("10.0.0.0/24", ["10.0.0.5"]) is True

    def test_no_match(self):
        assert _cidr_contains_any_ip("10.0.0.0/24", ["192.168.1.1"]) is False

    def test_bad_cidr(self):
        assert _cidr_contains_any_ip("not-a-cidr", ["10.0.0.1"]) is False

    def test_bad_ip_skipped(self):
        assert _cidr_contains_any_ip("10.0.0.0/24", ["bad", "10.0.0.2"]) is True


class TestClusterNetworkNode:
    def test_prefers_matching_cidr(self):
        topology = {
            "nodes": [
                {
                    "id": "net-a",
                    "type": "networkNode",
                    "data": {
                        "subtype": "network",
                        "networkType": "lab",
                        "cidr": "10.0.0.0/24",
                    },
                },
                {
                    "id": "net-b",
                    "type": "networkNode",
                    "data": {
                        "subtype": "network",
                        "networkType": "lab",
                        "cidr": "192.168.0.0/24",
                    },
                },
            ]
        }
        members = [{"type": "vmNode", "data": {"nics": [{"ip": "192.168.0.10"}]}}]
        assert _cluster_network_node(topology, members)["id"] == "net-b"

    def test_falls_back_to_first(self):
        topology = {
            "nodes": [
                {
                    "id": "net-a",
                    "type": "networkNode",
                    "data": {
                        "subtype": "network",
                        "networkType": "lab",
                        "cidr": "10.0.0.0/24",
                    },
                }
            ]
        }
        assert _cluster_network_node(topology, [])["id"] == "net-a"


class TestEdgePeerAndRhcosDevice:
    def test_edge_peer(self):
        assert _edge_peer_id({"source": "a", "target": "b"}, "a") == "b"
        assert _edge_peer_id({"source": "a", "target": "b"}, "b") == "a"
        assert _edge_peer_id({"source": "a", "target": "b"}, "c") is None

    def test_rhcos_device(self):
        vm = {
            "type": "vmNode",
            "data": {
                "os": "rhcos",
                "diskControllers": [{"id": "dc0"}, {"id": "dc1"}],
            },
        }
        edge = {"targetHandle": "dc1-left"}
        assert _rhcos_disk_device_for_edge(edge, vm) == "/dev/vdb"

    def test_find_ocp_mount_device(self):
        edges = [{"source": "disk1", "target": "vm1", "targetHandle": "dc0"}]
        nodes = {
            "vm1": {
                "type": "vmNode",
                "data": {"os": "rhcos", "diskControllers": [{"id": "dc0"}]},
            }
        }
        assert _find_ocp_mount_device("disk1", edges, nodes) == "/dev/vda"
