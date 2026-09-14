"""Tests for cluster topology heal (legacy ghost removal + member reparenting)."""

from app.services.ocp.cluster_topology_heal import (
    LEGACY_GHOST_NODE_ID,
    _clusters_are_legacy_ghost_only,
    freeze_deployed_cluster_ocp_versions,
    heal_cluster_topology,
    seed_topology_clusters_from_deployed,
)


def test_clusters_are_legacy_ghost_only():
    ghost = {
        "id": "ocp",
        "nodeId": "cluster-ocp",
        "baseDomain": "ocp.local",
        "type": "compact",
    }
    real = {
        "id": "ocp-68a740",
        "nodeId": "68a740b1",
        "baseDomain": "local",
        "type": "sno",
    }
    assert _clusters_are_legacy_ghost_only([ghost]) is True
    assert _clusters_are_legacy_ghost_only([ghost, real]) is False


def test_heal_kv_pattern_corruption():
    deployed = [
        {
            "id": "ocp-68a740",
            "name": "ocp",
            "nodeId": "68a740b1",
            "type": "sno",
            "controlPlane": 1,
            "workers": 0,
            "baseDomain": "local",
            "ocpVersion": "4.22",
        }
    ]
    topo = {
        "clusters": [
            {
                "id": "ocp",
                "name": "ocp",
                "nodeId": "cluster-ocp",
                "type": "compact",
                "controlPlane": 3,
                "workers": 0,
                "baseDomain": "ocp.local",
            },
            {
                "id": "ocp-2-hbf8w5",
                "name": "ocp-2",
                "nodeId": "cluster-ocp-2-hbf8w5",
                "type": "sno",
                "controlPlane": 1,
                "workers": 0,
                "baseDomain": "local",
            },
        ],
        "nodes": [
            {
                "id": "68a740b1",
                "type": "clusterNode",
                "data": {
                    "name": "ocp",
                    "clusterId": "ocp-68a740",
                    "type": "sno",
                    "controlPlane": 1,
                    "workers": 0,
                    "baseDomain": "local",
                },
            },
            {
                "id": "cp-0",
                "type": "vmNode",
                "parentId": LEGACY_GHOST_NODE_ID,
                "data": {"name": "cp-0", "os": "rhcos", "clusterId": "ocp"},
            },
            {
                "id": "cluster-ocp-2-hbf8w5",
                "type": "clusterNode",
                "data": {
                    "name": "ocp-2",
                    "clusterId": "ocp-2-hbf8w5",
                    "type": "sno",
                    "controlPlane": 1,
                    "workers": 0,
                },
            },
            {
                "id": "ocp-2-hbf8w5-cp-0",
                "type": "vmNode",
                "parentId": LEGACY_GHOST_NODE_ID,
                "data": {
                    "name": "ocp-2-hbf8w5-cp-0",
                    "os": "rhcos",
                    "clusterId": "ocp",
                },
            },
            {
                "id": LEGACY_GHOST_NODE_ID,
                "type": "clusterNode",
                "data": {"name": "ocp", "type": "compact", "baseDomain": "ocp.local"},
            },
        ],
        "edges": [],
    }

    out = heal_cluster_topology(topo, deployed_clusters=deployed)
    cluster_ids = {c["id"] for c in out["clusters"]}
    assert cluster_ids == {"ocp-68a740", "ocp-2-hbf8w5"}
    assert LEGACY_GHOST_NODE_ID not in {n["id"] for n in out["nodes"]}

    cp0 = next(n for n in out["nodes"] if n["id"] == "cp-0")
    cp2 = next(n for n in out["nodes"] if n["id"] == "ocp-2-hbf8w5-cp-0")
    assert cp0["parentId"] == "68a740b1"
    assert cp0["data"]["clusterId"] == "ocp-68a740"
    assert cp2["parentId"] == "cluster-ocp-2-hbf8w5"
    assert cp2["data"]["clusterId"] == "ocp-2-hbf8w5"

    parent_idx = next(i for i, n in enumerate(out["nodes"]) if n["id"] == "68a740b1")
    child_idx = next(i for i, n in enumerate(out["nodes"]) if n["id"] == "cp-0")
    assert parent_idx < child_idx


def test_seed_topology_clusters_from_deployed():
    topo = {
        "nodes": [
            {
                "id": "cp-0",
                "type": "vmNode",
                "data": {"name": "cp-0", "os": "rhcos", "clusterId": "ocp"},
            }
        ],
        "edges": [],
    }
    deployed = [
        {
            "id": "ocp-68a740",
            "name": "ocp",
            "nodeId": "68a740b1",
            "type": "sno",
            "baseDomain": "local",
            "_generatedInstallConfig": "x",
        }
    ]
    out = seed_topology_clusters_from_deployed(topo, deployed)
    assert out["clusters"][0]["id"] == "ocp-68a740"
    assert "_generatedInstallConfig" not in out["clusters"][0]


def test_freeze_deployed_cluster_ocp_versions_reverts_canvas_drift():
    current = {
        "clusters": [
            {"id": "ocp", "ocpVersion": "4.23"},
            {"id": "ocp-2", "ocpVersion": "4.22"},
        ]
    }
    deployed = {
        "clusters": [
            {"id": "ocp", "ocpVersion": "4.21"},
            {"id": "ocp-2", "ocpVersion": ""},
        ]
    }
    freeze_deployed_cluster_ocp_versions(current, deployed)
    assert current["clusters"][0]["ocpVersion"] == "4.21"
    assert current["clusters"][1]["ocpVersion"] == "4.22"
