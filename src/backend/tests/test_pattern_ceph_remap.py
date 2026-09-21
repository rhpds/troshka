"""Tests for cephClusterNode ref remapping on pattern deploy."""

from app.api.patterns import _remap_topology


def test_remap_topology_remaps_ceph_node_refs():
    """networkRef follows node id_map; linkedClusters follows cluster id remap."""
    topo = {
        "nodes": [
            {
                "id": "net-cluster",
                "type": "networkNode",
                "data": {"name": "cluster", "cidr": "10.0.0.0/24"},
            },
            {
                "id": "cluster-source",
                "type": "clusterNode",
                "data": {"name": "source", "clusterId": "source"},
            },
            {
                "id": "ceph-1",
                "type": "cephClusterNode",
                "data": {
                    "name": "Ceph Storage",
                    "networkRef": "net-cluster",
                    "labIp": "10.0.0.3",
                    "capacityGi": 300,
                    "osdCount": 3,
                    "linkedClusters": ["source"],
                },
            },
        ],
        "edges": [],
        "clusters": [
            {"id": "source", "nodeId": "cluster-source", "name": "source"},
        ],
        "projectCephCapture": {
            "monDiskId": "pd-mon-abc123",
            "osdDiskIds": ["pd-osd-111", "pd-osd-222", "pd-osd-333"],
        },
    }

    out = _remap_topology(topo)

    ceph = next(n for n in out["nodes"] if n["type"] == "cephClusterNode")
    net = next(n for n in out["nodes"] if n["type"] == "networkNode")
    cluster_node = next(n for n in out["nodes"] if n["type"] == "clusterNode")
    remapped_cluster = out["clusters"][0]

    assert ceph["id"] != "ceph-1"
    assert ceph["data"]["networkRef"] == net["id"]
    assert ceph["data"]["networkRef"] != "net-cluster"
    assert ceph["data"]["linkedClusters"] == [remapped_cluster["id"]]
    assert ceph["data"]["linkedClusters"] != ["source"]
    assert cluster_node["data"]["clusterId"] == remapped_cluster["id"]
    assert ceph["data"]["labIp"] == "10.0.0.3"

    capture = out["projectCephCapture"]
    assert capture["monDiskId"] == "pd-mon-abc123"
    assert capture["osdDiskIds"] == ["pd-osd-111", "pd-osd-222", "pd-osd-333"]


def test_remap_topology_preserves_lab_ip_without_capture():
    """Legacy topology (cephClusterNode only) keeps labIp through id remapping."""
    topo = {
        "nodes": [
            {
                "id": "net-cluster",
                "type": "networkNode",
                "data": {"name": "cluster", "cidr": "10.0.0.0/24"},
            },
            {
                "id": "ceph-1",
                "type": "cephClusterNode",
                "data": {
                    "name": "Ceph Storage",
                    "networkRef": "net-cluster",
                    "labIp": "10.0.0.5",
                    "capacityGi": 150,
                    "osdCount": 2,
                },
            },
        ],
        "edges": [],
    }

    out = _remap_topology(topo)

    ceph = next(n for n in out["nodes"] if n["type"] == "cephClusterNode")
    assert ceph["data"]["labIp"] == "10.0.0.5"
    assert "projectCephCapture" not in out
