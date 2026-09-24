from app.services.ocp.join_deferred_workers import (
    build_deferred_worker_nmstate,
    build_join_deferred_workers_cmd,
    deferred_workers_for_cluster,
    mark_deferred_workers_joined,
)


def _worker_node(name, cid, mac, bmc_ip, cluster_ip="10.0.0.20"):
    return {
        "id": name,
        "type": "vmNode",
        "data": {
            "name": name,
            "clusterId": cid,
            "tags": {"AnsibleGroup": "workers"},
            "bmcEnabled": True,
            "bmcIp": bmc_ip,
            "nics": [
                {"ip": cluster_ip, "mac": mac},
                {"ip": "172.16.100.20", "mac": "52:54:00:aa:bb:99"},
            ],
        },
    }


def _cclm_topology():
    return {
        "clusters": [
            {
                "id": "source",
                "name": "source",
                "type": "sno",
                "controlPlane": 1,
                "workers": 2,
                "networkIds": ["net-cluster", "net-migration"],
            }
        ],
        "nodes": [
            {
                "id": "gw-1",
                "type": "networkNode",
                "data": {"name": "gateway", "subtype": "gateway"},
            },
            {
                "id": "net-cluster",
                "type": "networkNode",
                "data": {
                    "id": "net-cluster",
                    "subtype": "network",
                    "cidr": "10.0.0.0/24",
                    "dns": True,
                },
            },
            {
                "id": "net-migration",
                "type": "networkNode",
                "data": {
                    "id": "net-migration",
                    "subtype": "network",
                    "cidr": "172.16.100.0/24",
                    "networkType": "migration",
                },
            },
            _worker_node(
                "source-worker-0", "source", "52:54:00:aa:bb:02", "192.168.100.20"
            ),
            {
                "id": "source-worker-1",
                "type": "vmNode",
                "data": {
                    "name": "source-worker-1",
                    "clusterId": "source",
                    "tags": {"AnsibleGroup": "workers"},
                    "bmcEnabled": True,
                    "bmcIp": "192.168.100.21",
                    "nics": [
                        {"ip": "10.0.0.21", "mac": "52:54:00:aa:bb:03"},
                        {"ip": "172.16.100.21", "mac": "52:54:00:aa:bb:99"},
                    ],
                },
            },
            {
                "id": "source-cp-0",
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
        "edges": [
            {"source": "gw-1", "target": "net-cluster"},
            {"source": "gw-1", "target": "net-migration"},
        ],
    }


def test_deferred_workers_for_cluster_filters_deferred_only():
    topo = _cclm_topology()
    workers = deferred_workers_for_cluster(topo, topo["clusters"][0])
    assert len(workers) == 2
    assert workers[0]["name"] == "source-worker-0"
    assert workers[0]["mac"] == "52:54:00:aa:bb:02"
    assert workers[0]["ip"] == "10.0.0.20"
    assert workers[0]["gateway"] == "10.0.0.1"
    assert workers[0]["aux_nics"][0]["ip"] == "172.16.100.20"
    assert workers[0]["aux_nics"][0]["mac"] == "52:54:00:aa:bb:99"


def test_mark_deferred_workers_joined_flips_power_on():
    topo = _cclm_topology()
    for n in topo["nodes"]:
        if n.get("data", {}).get("name", "").startswith("source-worker"):
            n["data"]["deferOcpInstall"] = True
            n["data"]["powerOnAtDeploy"] = False
    assert mark_deferred_workers_joined(topo, topo["clusters"][0]) is True
    for n in topo["nodes"]:
        if n.get("data", {}).get("name", "").startswith("source-worker"):
            assert n["data"]["powerOnAtDeploy"] is True
            assert n["data"]["deferOcpInstall"] is False
    # Idempotent
    assert mark_deferred_workers_joined(topo, topo["clusters"][0]) is False
    # Cleared workers are no longer join targets
    assert deferred_workers_for_cluster(topo, topo["clusters"][0]) == []


def test_sync_joined_worker_flags_from_deployed_heals_stale_canvas():
    from app.services.ocp.join_deferred_workers import (
        sync_joined_worker_flags_from_deployed,
    )

    deployed = {
        "nodes": [
            {
                "id": "w0",
                "type": "vmNode",
                "data": {
                    "name": "source-worker-0",
                    "deferOcpInstall": False,
                    "powerOnAtDeploy": True,
                },
            }
        ]
    }
    editable = {
        "nodes": [
            {
                "id": "w0",
                "type": "vmNode",
                "data": {
                    "name": "source-worker-0",
                    "deferOcpInstall": True,
                    "powerOnAtDeploy": False,
                },
            }
        ]
    }
    assert sync_joined_worker_flags_from_deployed(deployed, editable) is True
    data = editable["nodes"][0]["data"]
    assert data["deferOcpInstall"] is False
    assert data["powerOnAtDeploy"] is True
    # Real post-join user edit (defer already false) is left alone
    editable["nodes"][0]["data"]["powerOnAtDeploy"] = False
    assert sync_joined_worker_flags_from_deployed(deployed, editable) is False
    assert editable["nodes"][0]["data"]["powerOnAtDeploy"] is False


def test_build_deferred_worker_nmstate_configures_both_nics():
    worker = {
        "mac": "52:54:00:aa:bb:02",
        "ip": "10.0.0.20",
        "prefix_len": 24,
        "gateway": "10.0.0.1",
        "dns_ip": "10.0.0.2",
        "iface_name": "cluster-nic",
        "aux_nics": [
            {
                "mac": "52:54:00:aa:bb:99",
                "ip": "172.16.100.20",
                "prefix_len": 24,
                "iface_name": "net1-nic",
            }
        ],
    }
    nmstate = build_deferred_worker_nmstate(worker)
    # MACs + identifier live at the host level (nodes-config interfaces), NOT the
    # networkConfig — oc adm node-image create keys interfaces by device name.
    assert "mac-address:" not in nmstate
    assert "identifier:" not in nmstate
    assert "name: cluster-nic" in nmstate
    assert "name: net1-nic" in nmstate
    assert "state: down" in nmstate
    assert "ip: 172.16.100.20" not in nmstate
    assert "next-hop-interface: cluster-nic" in nmstate
    assert "next-hop-interface: net1-nic" not in nmstate


def test_build_deferred_worker_nodes_config_includes_routes():
    """nodes-config.yaml must carry the full per-host networkConfig including the
    default route — the standalone --network-config-path flag dropped routes, so
    the joined worker had no gateway."""
    import yaml

    from app.services.ocp.join_deferred_workers import (
        build_deferred_worker_nodes_config,
    )

    worker = {
        "name": "source-worker-0",
        "mac": "52:54:00:aa:bb:02",
        "ip": "10.0.0.20",
        "prefix_len": 24,
        "gateway": "10.0.0.1",
        "dns_ip": "10.0.0.2",
        "iface_name": "enp1s0",
        "aux_nics": [
            {
                "mac": "52:54:00:aa:bb:99",
                "ip": "172.16.100.20",
                "prefix_len": 24,
                "iface_name": "enp2s0",
            }
        ],
    }
    cfg = yaml.safe_load(build_deferred_worker_nodes_config(worker))
    host = cfg["hosts"][0]
    assert host["hostname"] == "source-worker-0"
    # Every NIC listed at host level with device name + MAC.
    by_mac = {i["macAddress"]: i["name"] for i in host["interfaces"]}
    assert by_mac == {"52:54:00:aa:bb:02": "enp1s0", "52:54:00:aa:bb:99": "enp2s0"}
    nc = host["networkConfig"]
    assert nc["routes"]["config"][0]["next-hop-address"] == "10.0.0.1"
    assert nc["routes"]["config"][0]["next-hop-interface"] == "enp1s0"
    assert nc["dns-resolver"]["config"]["server"] == ["10.0.0.2"]


def test_build_join_cmd_emits_node_image_and_redfish():
    workers = [
        {
            "name": "source-worker-0",
            "mac": "52:54:00:aa:bb:02",
            "bmc_ip": "192.168.100.20",
            "ip": "10.0.0.20",
            "prefix_len": 24,
            "gateway": "10.0.0.1",
            "dns_ip": "10.0.0.2",
            "iface_name": "cluster-nic",
        },
    ]
    script = build_join_deferred_workers_cmd(
        "  ", "source", workers, "secret", 8080, serving_ip="10.0.0.5"
    )
    assert "oc adm node-image create" in script
    assert "nodes-config.yaml" in script
    assert "--network-config-path=" not in script
    assert "next-hop-address: 10.0.0.1" in script
    assert "cluster-nic" in script
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
            "ip": "10.0.0.20",
            "prefix_len": 24,
            "gateway": "10.0.0.1",
            "dns_ip": "10.0.0.2",
            "iface_name": "cluster-nic",
        },
        {
            "name": "source-worker-1",
            "mac": "52:54:00:aa:bb:03",
            "bmc_ip": "192.168.100.21",
            "ip": "10.0.0.21",
            "prefix_len": 24,
            "gateway": "10.0.0.1",
            "dns_ip": "10.0.0.2",
            "iface_name": "cluster-nic",
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
