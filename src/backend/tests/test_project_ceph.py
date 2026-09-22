"""Tests for project Ceph helpers."""

from app.services.project_ceph import (
    build_project_ceph_stamp,
    extract_ceph_cluster_spec,
    merge_project_ceph_extra_vars,
    topology_has_ceph,
)


def test_topology_has_ceph():
    assert not topology_has_ceph({"nodes": [{"type": "vmNode"}]})
    assert topology_has_ceph({"nodes": [{"type": "cephClusterNode"}]})


def test_build_project_ceph_stamp_defaults():
    stamp = build_project_ceph_stamp(
        namespace="troshka-abc",
        status={"phase": "Ready", "monEndpoint": "10.0.0.3:6789"},
        spec={"labIp": "10.0.0.3", "osdCount": 3},
    )
    assert stamp["phase"] == "Ready"
    assert stamp["secretNamespace"] == "troshka-abc"
    assert stamp["monHost"] == "10.0.0.3"
    assert stamp["monEndpoint"] == "10.0.0.3:6789"
    assert stamp["cclm_ceph_secret_name"] == "rook-ceph-external-cluster-details"


def test_extract_ceph_cluster_spec_resolves_network_nad():
    topology = {
        "nodes": [
            {
                "id": "net-cluster",
                "type": "networkNode",
                "data": {"id": "net-cluster", "cidr": "10.0.0.0/24"},
            },
            {
                "id": "ceph-1",
                "type": "cephClusterNode",
                "data": {
                    "networkRef": "net-cluster",
                    "labIp": "10.0.0.3",
                    "capacityGi": 300,
                    "osdCount": 3,
                    "linkedClusters": ["source"],
                },
            },
        ],
        "edges": [],
    }
    spec = extract_ceph_cluster_spec(topology)
    assert spec is not None
    assert spec["networkNad"] == "net-net-clus-nad"
    assert spec["labIp"] == "10.0.0.3"


def test_extract_ceph_cluster_spec_allocates_osd_ips_top_down():
    topology = {
        "nodes": [
            {
                "id": "net-cluster",
                "type": "networkNode",
                "data": {"id": "net-cluster", "cidr": "10.0.0.0/24"},
            },
            {"id": "w0", "type": "vmNode", "data": {"nics": [{"ip": "10.0.0.254"}]}},
            {
                "id": "ceph-1",
                "type": "cephClusterNode",
                "data": {
                    "networkRef": "net-cluster",
                    "labIp": "10.0.0.4",
                    "osdCount": 3,
                },
            },
        ],
        "edges": [],
    }
    spec = extract_ceph_cluster_spec(topology)
    assert spec is not None
    # top-down, skipping the mon (.4) and the .254 NIC
    assert spec["osdIps"] == ["10.0.0.253", "10.0.0.252", "10.0.0.251"]
    # stamped back onto the ceph node for stability + display
    ceph = next(n for n in topology["nodes"] if n["type"] == "cephClusterNode")
    assert ceph["data"]["osdIps"] == spec["osdIps"]


def test_extract_ceph_cluster_spec_preserves_stamped_osd_ips():
    topology = {
        "nodes": [
            {
                "id": "net-cluster",
                "type": "networkNode",
                "data": {"id": "net-cluster", "cidr": "10.0.0.0/24"},
            },
            {
                "id": "ceph-1",
                "type": "cephClusterNode",
                "data": {
                    "networkRef": "net-cluster",
                    "labIp": "10.0.0.4",
                    "osdCount": 2,
                    "osdIps": ["10.0.0.200", "10.0.0.201"],
                },
            },
        ],
        "edges": [],
    }
    spec = extract_ceph_cluster_spec(topology)
    assert spec is not None
    assert spec["osdIps"] == ["10.0.0.200", "10.0.0.201"]


def test_merge_project_ceph_extra_vars():
    topo = {
        "projectCeph": {
            "troshka_ceph_secret_name": "rook-ceph-external-cluster-details",
            "troshka_ceph_mon_host": "10.0.0.3",
            "secretName": "troshka-ceph-external",
            "secretNamespace": "troshka-abc",
            "poolName": "troshka-ceph-pool",
        }
    }
    out = merge_project_ceph_extra_vars(topo, {"foo": "bar"})
    assert out["foo"] == "bar"
    assert out["troshka_ceph_mon_host"] == "10.0.0.3"
    assert out["troshka_project_ceph_secret"] == "troshka-ceph-external"
    assert out["poolName"] == "troshka-ceph-pool"
