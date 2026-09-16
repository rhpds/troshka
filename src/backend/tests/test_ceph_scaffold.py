"""Tests for cephCluster template scaffold."""

from app.services.ceph_scaffold import (
    build_ceph_from_config,
    ceph_position_for_cluster_count,
    export_ceph_section,
)


def test_build_ceph_from_config_edges():
    net_id = "net-cluster"
    cluster_id = "cluster-source"
    node, edges = build_ceph_from_config(
        {
            "name": "Ceph Storage",
            "network": "cluster",
            "labIp": "10.0.0.3",
            "capacityGi": 300,
            "osdCount": 3,
            "clusters": ["source"],
        },
        net_ids={"cluster": net_id},
        nets_def={"cluster": {"cidr": "10.0.0.0/24"}},
        cluster_name_to_id={"source": cluster_id},
    )
    assert node["type"] == "cephClusterNode"
    assert node["data"]["networkRef"] == net_id
    assert node["data"]["osdCount"] == 3
    cluster_edge = next(e for e in edges if e["target"] == cluster_id)
    assert cluster_edge["sourceHandle"] == "right"
    assert cluster_edge["targetHandle"] == "ceph-left"


def test_ceph_position_between_two_clusters():
    x, y = ceph_position_for_cluster_count(2)
    # destination box ends at 620; source starts at 1000 — ceph sits in the gap.
    assert 620 < x < 1000
    assert y >= 250


def test_export_ceph_section_roundtrip():
    topology = {
        "nodes": [
            {
                "type": "cephClusterNode",
                "data": {
                    "name": "Ceph Storage",
                    "networkRef": "net-1",
                    "labIp": "10.0.0.3",
                    "capacityGi": 300,
                    "osdCount": 3,
                    "linkedClusters": ["cl-1"],
                    "storageClassName": "troshka-ceph-rbd",
                },
            }
        ]
    }
    exported = export_ceph_section(topology, {"net-1": "cluster"})
    assert exported["name"] == "Ceph Storage"
    assert exported["network"] == "cluster"
    assert exported["osdCount"] == 3
