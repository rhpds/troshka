"""Tests for recert kubeconfig-delivery helpers in deploy_service."""

from app.services.deploy_service import _recert_cp_member_vm_id


def _topo():
    return {
        "nodes": [
            {
                "id": "cp-node-1",
                "type": "vmNode",
                "data": {
                    "id": "cp-node-1",
                    "os": "rhcos",
                    "clusterId": "c1",
                    "clusterRole": "master",
                },
            },
            {
                "id": "worker-1",
                "type": "vmNode",
                "data": {
                    "id": "worker-1",
                    "os": "rhcos",
                    "clusterId": "c1",
                    "clusterRole": "worker",
                },
            },
        ]
    }


def test_picks_control_plane_member_not_worker():
    assert _recert_cp_member_vm_id(_topo(), {"id": "c1", "name": "c1"}) == "cp-node-1"


def test_returns_empty_when_no_rhcos_cp_member():
    topo = {
        "nodes": [
            {
                "id": "w",
                "type": "vmNode",
                "data": {
                    "id": "w",
                    "os": "rhcos",
                    "clusterId": "c1",
                    "clusterRole": "worker",
                },
            }
        ]
    }
    assert _recert_cp_member_vm_id(topo, {"id": "c1", "name": "c1"}) == ""
