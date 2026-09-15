from helpers.topology import (
    cluster_egress_network_id,
    install_nics_for_vm,
    resolve_nic_networks,
)


def _cclm_topology():
    return {
        "clusters": [
            {
                "id": "source",
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
                    "cidr": "10.0.0.0/24",
                    "dns": True,
                },
            },
            {
                "id": "net-migration",
                "type": "networkNode",
                "data": {
                    "id": "net-migration",
                    "cidr": "172.16.100.0/24",
                    "networkType": "migration",
                },
            },
            {
                "id": "vm-worker",
                "type": "vmNode",
                "data": {"id": "vm-worker", "clusterId": "source"},
            },
        ],
        "edges": [
            {"source": "gw-1", "target": "net-cluster"},
            {"source": "gw-1", "target": "net-migration"},
            {
                "source": "net-cluster",
                "target": "vm-worker",
                "targetHandle": "nic-nic-cluster01-bottom",
            },
            {
                "source": "net-migration",
                "target": "vm-worker",
                "targetHandle": "nic-nic-migrat01-bottom",
            },
        ],
    }


def test_cluster_egress_prefers_dns_enabled_gateway_network():
    topo = _cclm_topology()
    cluster = topo["clusters"][0]
    assert cluster_egress_network_id(cluster, topo) == "net-cluster"


def test_install_nics_for_vm_keeps_both_nics_for_deferred_workers():
    topo = _cclm_topology()
    nic_map = resolve_nic_networks(topo)
    vm = {
        "deferOcpInstall": True,
        "clusterId": "source",
        "nics": [
            {"id": "nic-cluster01", "mac": "52:54:00:01:01:01"},
            {"id": "nic-migrat01", "mac": "52:54:00:02:02:02"},
        ],
    }
    kept = install_nics_for_vm(vm, nic_map, topo)
    assert [n["id"] for n in kept] == ["nic-cluster01", "nic-migrat01"]


def test_install_nics_for_vm_keeps_all_nics_for_normal_vms():
    topo = _cclm_topology()
    nic_map = resolve_nic_networks(topo)
    vm = {
        "deferOcpInstall": False,
        "nics": [
            {"id": "nic-cluster01", "mac": "52:54:00:01:01:01"},
            {"id": "nic-migrat01", "mac": "52:54:00:02:02:02"},
        ],
    }
    kept = install_nics_for_vm(vm, nic_map, topo)
    assert len(kept) == 2
