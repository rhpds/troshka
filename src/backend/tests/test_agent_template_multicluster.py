def _vm(name, cid, group):
    return {
        "type": "vmNode",
        "data": {
            "name": name,
            "clusterId": cid,
            "tags": {"AnsibleGroup": group},
            "os": "rhcos",
        },
    }


def _member(name, cid, group, ip, mac):
    return {
        "type": "vmNode",
        "data": {
            "name": name,
            "clusterId": cid,
            "tags": {"AnsibleGroup": group},
            "os": "rhcos",
            "bmcEnabled": True,
            "bmcIp": "192.168.50.10",
            "nics": [{"ip": ip, "mac": mac}],
        },
    }


def _two_cluster_topo():
    prod_members = [
        _member("p-cp-0", "prod", "controllers", "10.0.0.20", "52:54:00:aa:bb:01"),
        _member("p-cp-1", "prod", "controllers", "10.0.0.21", "52:54:00:aa:bb:02"),
        _member("p-cp-2", "prod", "controllers", "10.0.0.22", "52:54:00:aa:bb:03"),
        _member("p-w-0", "prod", "workers", "10.0.0.30", "52:54:00:aa:bb:04"),
        _member("p-w-1", "prod", "workers", "10.0.0.31", "52:54:00:aa:bb:05"),
    ]
    dev_members = [
        _member("d-cp-0", "dev", "controllers", "10.1.0.20", "52:54:00:aa:cc:11"),
    ]
    nets = [
        {
            "id": "net-prod",
            "type": "networkNode",
            "data": {
                "subtype": "network",
                "cidr": "10.0.0.0/24",
                "networkType": "cluster",
            },
        },
        {
            "id": "net-dev",
            "type": "networkNode",
            "data": {
                "subtype": "network",
                "cidr": "10.1.0.0/24",
                "networkType": "cluster",
            },
        },
    ]
    return {"nodes": nets + prod_members + dev_members, "edges": []}


def test_install_config_standard_and_sno():
    import yaml

    from app.services.ocp.agent_template import (
        _build_install_config,
        cluster_member_nodes,
    )

    topo = _two_cluster_topo()

    prod = {
        "id": "prod",
        "name": "prod",
        "type": "standard",
        "controlPlane": 3,
        "workers": 2,
        "baseDomain": "ocp.local",
        "apiVip": "10.0.0.10",
        "ingressVip": "10.0.0.11",
    }
    ic = yaml.safe_load(
        _build_install_config(
            prod,
            cluster_member_nodes(topo, "prod"),
            topo,
            pull_secret="{}",
            ssh_key="ssh-rsa x",
            pull_through_registry=None,
        )
    )
    assert ic["metadata"]["name"] == "prod"
    assert ic["baseDomain"] == "ocp.local"
    assert ic["controlPlane"]["replicas"] == 3
    assert ic["compute"][0]["replicas"] == 2
    assert ic["platform"]["baremetal"]["apiVIPs"] == ["10.0.0.10"]
    assert ic["platform"]["baremetal"]["ingressVIPs"] == ["10.0.0.11"]
    assert ic["networking"]["machineNetwork"][0]["cidr"] == "10.0.0.0/24"
    # BMC hosts scoped to prod members only (5 hosts, no dev leakage)
    hosts = ic["platform"]["baremetal"]["hosts"]
    assert len(hosts) == 5
    assert all(not h["name"].startswith("d-") for h in hosts)

    dev = {
        "id": "dev",
        "name": "dev",
        "type": "sno",
        "controlPlane": 1,
        "workers": 0,
        "baseDomain": "dev.local",
        "apiVip": "",
        "ingressVip": "",
    }
    icd = yaml.safe_load(
        _build_install_config(
            dev,
            cluster_member_nodes(topo, "dev"),
            topo,
            pull_secret="{}",
            ssh_key="ssh-rsa x",
            pull_through_registry=None,
        )
    )
    assert icd["metadata"]["name"] == "dev"
    assert icd["baseDomain"] == "dev.local"
    assert icd["platform"] == {"none": {}}
    assert icd["controlPlane"]["replicas"] == 1
    assert icd["compute"][0]["replicas"] == 0
    assert "baremetal" not in icd.get("platform", {})


def test_agent_config_per_cluster():
    import yaml

    from app.services.ocp.agent_template import (
        _build_agent_config,
        cluster_member_nodes,
    )

    topo = _two_cluster_topo()

    prod = {
        "id": "prod",
        "name": "prod",
        "type": "standard",
        "baseDomain": "ocp.local",
    }
    dev = {
        "id": "dev",
        "name": "dev",
        "type": "sno",
        "baseDomain": "dev.local",
    }

    ac_prod = yaml.safe_load(
        _build_agent_config(prod, cluster_member_nodes(topo, "prod"), topo)
    )
    ac_dev = yaml.safe_load(
        _build_agent_config(dev, cluster_member_nodes(topo, "dev"), topo)
    )

    # distinct metadata.name
    assert ac_prod["metadata"]["name"] == "prod"
    assert ac_dev["metadata"]["name"] == "dev"

    # rendezvousIP = each cluster's own first control-plane member IP
    assert ac_prod["rendezvousIP"] == "10.0.0.20"
    assert ac_dev["rendezvousIP"] == "10.1.0.20"
    assert ac_prod["rendezvousIP"] != ac_dev["rendezvousIP"]

    # host lists do not bleed across clusters
    prod_hosts = [h["hostname"] for h in ac_prod["hosts"]]
    dev_hosts = [h["hostname"] for h in ac_dev["hosts"]]
    assert len(prod_hosts) == 5
    assert all(h.startswith("p-") for h in prod_hosts)
    assert dev_hosts == ["d-cp-0"]

    # per-host static IP comes from the member's own NIC
    prod_ip = ac_prod["hosts"][0]["networkConfig"]["interfaces"][0]["ipv4"]["address"][
        0
    ]["ip"]
    assert prod_ip == "10.0.0.20"


def test_count_scoped_by_cluster():
    from app.services.ocp.agent_template import _count_ocp_nodes_by_group

    topo = {
        "nodes": [
            _vm("p-cp-0", "prod", "controllers"),
            _vm("p-cp-1", "prod", "controllers"),
            _vm("p-cp-2", "prod", "controllers"),
            _vm("p-w-0", "prod", "workers"),
            _vm("d-cp-0", "dev", "controllers"),
        ]
    }
    assert _count_ocp_nodes_by_group(topo, "controllers", cluster_id="prod") == 3
    assert _count_ocp_nodes_by_group(topo, "workers", cluster_id="prod") == 1
    assert _count_ocp_nodes_by_group(topo, "controllers", cluster_id="dev") == 1
    # back-compat: no cluster_id = whole topology
    assert _count_ocp_nodes_by_group(topo, "controllers") == 4


def test_cluster_member_nodes():
    from app.services.ocp.agent_template import cluster_member_nodes

    topo = {
        "nodes": [
            _vm("p-cp-0", "prod", "controllers"),
            _vm("d-cp-0", "dev", "controllers"),
        ]
    }
    assert [n["data"]["name"] for n in cluster_member_nodes(topo, "prod")] == ["p-cp-0"]


def test_resolve_vips_explicit():
    from app.services.ocp.agent_template import resolve_cluster_vips

    cluster = {
        "id": "prod",
        "type": "standard",
        "controlPlane": 3,
        "apiVip": "10.0.0.10",
        "ingressVip": "10.0.0.11",
    }
    assert resolve_cluster_vips(cluster, [], {"nodes": []}) == (
        "10.0.0.10",
        "10.0.0.11",
    )


def test_resolve_vips_sno_uses_node_ip():
    from app.services.ocp.agent_template import resolve_cluster_vips

    cluster = {
        "id": "dev",
        "type": "sno",
        "controlPlane": 1,
        "apiVip": "",
        "ingressVip": "",
    }
    members = [
        {
            "type": "vmNode",
            "data": {
                "clusterId": "dev",
                "tags": {"AnsibleGroup": "controllers"},
                "nics": [{"ip": "10.1.0.20"}],
            },
        }
    ]
    assert resolve_cluster_vips(cluster, members, {"nodes": members}) == (
        "10.1.0.20",
        "10.1.0.20",
    )


def test_resolve_vips_standard_uses_cidr_offset():
    """A multi-CP cluster with no explicit VIPs falls back to CIDR network+2/+3."""
    from app.services.ocp.agent_template import resolve_cluster_vips

    cluster = {"id": "prod", "type": "standard", "controlPlane": 3, "workers": 0}
    members = [_vm("p-cp-0", "prod", "controllers")]
    topo = {
        "nodes": [
            {
                "id": "net1",
                "type": "networkNode",
                "data": {
                    "subtype": "network",
                    "cidr": "10.5.0.0/24",
                    "networkType": "cluster",
                },
            }
        ]
        + members
    }
    assert resolve_cluster_vips(cluster, members, topo) == ("10.5.0.2", "10.5.0.3")
