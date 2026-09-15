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
    assert "preflight: checking SCC UID allocation" in script
    assert "preflight: probing SCC UID allocator" in script
    assert "removing stale $_ns (missing sa.scc.uid-range)" in script
    assert "grep openshift-node-joiner || true" in script
    assert 'grep -qi "sa\\.scc\\.uid-range" create.log' in script
    assert "joining workers in parallel" in script
    assert "worker_pids+=($!)" in script
    assert 'wait "$p" || join_fail=1' in script
    assert "52:54:00:aa:bb:02" in script
    assert "192.168.100.20" in script
    assert "control-plane-usable" not in script
    assert ".deferred-workers-joined" in script
    assert "waiting for worker source-worker-0 to become Ready" in script
    assert "worker source-worker-0 joined (ISO ejected)" in script
    assert "deferred workers Ready" in script
    assert "waiting for deferred workers to converge" in script
    assert "deferred workers converged" in script
    assert script.index("deferred workers joined") < script.index(
        "waiting for deferred workers to converge"
    )
    assert script.index("deferred workers converged") < script.index(
        "touch .deferred-workers-joined"
    )
    assert "node-role.kubernetes.io/worker" not in script


def test_build_join_cmd_parallelizes_multiple_workers():
    workers = [
        {
            "name": "source-worker-0",
            "mac": "52:54:00:aa:bb:02",
            "bmc_ip": "192.168.100.20",
        },
        {
            "name": "source-worker-1",
            "mac": "52:54:00:aa:bb:03",
            "bmc_ip": "192.168.100.21",
        },
    ]
    script = build_join_deferred_workers_cmd(
        "  ", "source", workers, "secret", 8181, serving_ip=None
    )
    assert script.count("node-image create for source-worker-0") == 1
    assert script.count("node-image create for source-worker-1") == 1
    assert script.count("worker_pids+=($!)") == 2
    assert "http.server 8281" in script
    assert "http.server 8282" in script
    # node-image create runs before the wait loop, not one-after-another boots
    w0 = script.index("node-image create for source-worker-0")
    w1 = script.index("node-image create for source-worker-1")
    wait = script.index('for p in "${worker_pids[@]}"')
    assert w0 < wait and w1 < wait


def test_build_join_cmd_empty_when_no_workers():
    assert build_join_deferred_workers_cmd("  ", "source", [], "x", 8080, None) == ""
