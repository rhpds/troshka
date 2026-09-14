from app.services.ocp.join_deferred_workers import (
    build_join_deferred_workers_cmd,
    deferred_workers_for_cluster,
)


def _worker_node(name, cid, mac, bmc_ip):
    return {
        "type": "vmNode",
        "data": {
            "name": name,
            "clusterId": cid,
            "tags": {"AnsibleGroup": "workers"},
            "bmcEnabled": True,
            "bmcIp": bmc_ip,
            "nics": [{"ip": "10.0.0.20", "mac": mac}],
        },
    }


def test_deferred_workers_for_cluster_filters_deferred_only():
    topo = {
        "clusters": [
            {
                "id": "source",
                "name": "source",
                "type": "sno",
                "controlPlane": 1,
                "workers": 2,
            }
        ],
        "nodes": [
            _worker_node(
                "source-worker-0", "source", "52:54:00:aa:bb:02", "192.168.100.20"
            ),
            _worker_node(
                "source-worker-1", "source", "52:54:00:aa:bb:03", "192.168.100.21"
            ),
            {
                "type": "vmNode",
                "data": {
                    "name": "source-cp-0",
                    "clusterId": "source",
                    "tags": {"AnsibleGroup": "controllers"},
                    "bmcEnabled": True,
                    "bmcIp": "192.168.100.10",
                    "nics": [{"ip": "10.0.0.10", "mac": "52:54:00:aa:bb:01"}],
                },
            },
        ],
    }
    workers = deferred_workers_for_cluster(topo, topo["clusters"][0])
    assert len(workers) == 2
    assert workers[0]["name"] == "source-worker-0"
    assert workers[0]["mac"] == "52:54:00:aa:bb:02"


def test_build_join_cmd_emits_node_image_and_redfish():
    workers = [
        {
            "name": "source-worker-0",
            "mac": "52:54:00:aa:bb:02",
            "bmc_ip": "192.168.100.20",
        },
    ]
    script = build_join_deferred_workers_cmd(
        "  ", "source", workers, "secret", 8080, serving_ip="10.0.0.5"
    )
    assert "oc adm node-image create" in script
    assert "node-image create failed for source-worker-0" in script
    assert "waiting for API before worker join" in script
    assert "52:54:00:aa:bb:02" in script
    assert "192.168.100.20" in script
    assert "control-plane-usable" not in script
    assert ".deferred-workers-joined" in script
    assert "worker nodes Ready" in script


def test_build_join_cmd_empty_when_no_workers():
    assert build_join_deferred_workers_cmd("  ", "source", [], "x", 8080, None) == ""
