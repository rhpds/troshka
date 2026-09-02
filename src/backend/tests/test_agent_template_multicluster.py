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


def test_resolve_vips_standard_uses_unused_high_ips():
    """A multi-CP cluster with no explicit VIPs picks unused IPs from the top-down.

    When no member IPs collide and the network is empty, picks .254/.253 (top-down).
    """
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
    # Now picks unused IPs top-down, so .254 and .253 (highest free IPs)
    assert resolve_cluster_vips(cluster, members, topo) == ("10.5.0.254", "10.5.0.253")


def test_customize_topology_two_clusters_dns_and_configs():
    """A two-cluster topology yields per-cluster DNS on each cluster's own
    network node, plus generated install-config/agent-config stored on each
    cluster object for Plan 4's ops pod. No single-bastion bake in multi."""
    import yaml

    from app.services.ocp.agent_template import customize_topology

    topo = _two_cluster_topo()
    topo["clusters"] = [
        {
            "id": "prod",
            "name": "prod",
            "type": "standard",
            "controlPlane": 3,
            "workers": 2,
            "baseDomain": "ocp.local",
            "apiVip": "10.0.0.10",
            "ingressVip": "10.0.0.11",
        },
        {
            "id": "dev",
            "name": "dev",
            "type": "sno",
            "controlPlane": 1,
            "workers": 0,
            "baseDomain": "dev.local",
            "apiVip": "",
            "ingressVip": "",
        },
    ]
    config = {
        "cluster_name": "prod",
        "base_domain": "ocp.local",
        "ocp_version": "4.20",
        "common_password": "pw",
        "pull_secret_json": '{"auths":{}}',
        "ssh_pub_key": "ssh-rsa x",
        "auto_install_ocp": True,
        "resolved": {},
    }
    customize_topology(topo, "ocp-multi", config)

    net_prod = next(n for n in topo["nodes"] if n["id"] == "net-prod")
    net_dev = next(n for n in topo["nodes"] if n["id"] == "net-dev")
    prod_names = [r["name"] for r in net_prod["data"]["dnsRecords"]]
    dev_names = [r["name"] for r in net_dev["data"]["dnsRecords"]]

    # Each cluster's api/api-int/apps land on its OWN network, no bleed.
    assert "api.prod.ocp.local" in prod_names
    assert "api-int.prod.ocp.local" in prod_names
    assert ".apps.prod.ocp.local" in prod_names
    assert not any(n.endswith("dev.local") for n in prod_names)

    assert "api.dev.dev.local" in dev_names
    assert "api-int.dev.dev.local" in dev_names
    assert ".apps.dev.dev.local" in dev_names
    assert not any("prod" in n for n in dev_names)

    # Per-cluster generated configs stored on each cluster object.
    for cluster in topo["clusters"]:
        assert cluster["_generatedInstallConfig"]
        assert cluster["_generatedAgentConfig"]

    prod_ic = yaml.safe_load(topo["clusters"][0]["_generatedInstallConfig"])
    assert prod_ic["metadata"]["name"] == "prod"
    assert prod_ic["platform"]["baremetal"]["apiVIPs"] == ["10.0.0.10"]
    assert len(prod_ic["platform"]["baremetal"]["hosts"]) == 5

    dev_ic = yaml.safe_load(topo["clusters"][1]["_generatedInstallConfig"])
    assert dev_ic["metadata"]["name"] == "dev"
    assert dev_ic["platform"] == {"none": {}}

    ac_dev = yaml.safe_load(topo["clusters"][1]["_generatedAgentConfig"])
    assert ac_dev["rendezvousIP"] == "10.1.0.20"


def test_customize_topology_single_cluster_bakes_bastion():
    """One-cluster (legacy, no ``clusters`` key) still bakes bastion cloud-init
    exactly as before — ``ciUserData`` present, ``cloudInit`` set."""
    from app.services.ocp.agent_template import customize_topology

    topo = {
        "nodes": [
            {
                "id": "net",
                "type": "networkNode",
                "position": {"x": 0, "y": 0},
                "data": {
                    "subtype": "network",
                    "cidr": "10.0.0.0/24",
                    "networkType": "cluster",
                },
            },
            {
                "id": "bastion",
                "type": "vmNode",
                "position": {"x": 0, "y": 0},
                "data": {
                    "name": "bastion",
                    "os": "rhel",
                    "diskControllers": [],
                    "nics": [
                        {"id": "n1", "mac": "52:54:00:00:00:01", "ip": "10.0.0.50"},
                        {"id": "n2", "mac": "52:54:00:00:00:02"},
                    ],
                },
            },
            {
                "id": "cp0",
                "type": "vmNode",
                "position": {"x": 0, "y": 0},
                "data": {
                    "name": "cp0",
                    "os": "rhcos",
                    "tags": {"AnsibleGroup": "controllers"},
                    "bmcEnabled": True,
                    "bmcIp": "192.168.100.10",
                    "nics": [
                        {"id": "n3", "mac": "52:54:00:00:00:03", "ip": "10.0.0.10"}
                    ],
                    "diskControllers": [],
                },
            },
        ],
        "edges": [],
    }
    config = {
        "cluster_name": "ocp",
        "base_domain": "ocp.local",
        "ocp_version": "4.20",
        "common_password": "pw",
        "pull_secret_json": '{"auths":{}}',
        "ssh_pub_key": "ssh-rsa x",
        "auto_install_ocp": True,
        "install_via": "bastion",
        "resolved": {},
    }
    customize_topology(topo, "ocp-sno", config)

    bastion = next(n for n in topo["nodes"] if n["data"].get("name") == "bastion")
    assert bastion["data"].get("ciUserData")
    assert bastion["data"].get("cloudInit") is True

    # Single-cluster DNS still written to the cluster network.
    net = next(n for n in topo["nodes"] if n["id"] == "net")
    dns_names = [r["name"] for r in net["data"].get("dnsRecords", [])]
    assert "api.ocp.ocp.local" in dns_names
    assert ".apps.ocp.ocp.local" in dns_names


def test_customize_topology_shared_network_merges_dns():
    """Two clusters whose members share ONE network node must both keep their
    api/api-int/apps records — the second write MERGES, never clobbers."""
    prod_members = [
        _member("p-cp-0", "prod", "controllers", "10.0.0.20", "52:54:00:aa:bb:01"),
        _member("p-cp-1", "prod", "controllers", "10.0.0.21", "52:54:00:aa:bb:02"),
        _member("p-cp-2", "prod", "controllers", "10.0.0.22", "52:54:00:aa:bb:03"),
    ]
    dev_members = [
        _member("d-cp-0", "dev", "controllers", "10.0.0.40", "52:54:00:aa:cc:11"),
    ]
    net = {
        "id": "net-shared",
        "type": "networkNode",
        "data": {
            "subtype": "network",
            "cidr": "10.0.0.0/24",
            "networkType": "cluster",
        },
    }
    topo = {"nodes": [net] + prod_members + dev_members, "edges": []}
    topo["clusters"] = [
        {
            "id": "prod",
            "name": "prod",
            "type": "standard",
            "controlPlane": 3,
            "workers": 0,
            "baseDomain": "ocp.local",
            "apiVip": "10.0.0.10",
            "ingressVip": "10.0.0.11",
        },
        {
            "id": "dev",
            "name": "dev",
            "type": "sno",
            "controlPlane": 1,
            "workers": 0,
            "baseDomain": "ocp.local",
            "apiVip": "10.0.0.40",
            "ingressVip": "10.0.0.40",
        },
    ]
    from app.services.ocp.agent_template import customize_topology

    config = {
        "cluster_name": "prod",
        "base_domain": "ocp.local",
        "ocp_version": "4.20",
        "common_password": "pw",
        "pull_secret_json": '{"auths":{}}',
        "ssh_pub_key": "ssh-rsa x",
        "auto_install_ocp": True,
        "resolved": {},
    }
    customize_topology(topo, "ocp-multi", config)

    shared = next(n for n in topo["nodes"] if n["id"] == "net-shared")
    names = [r["name"] for r in shared["data"]["dnsRecords"]]
    # Both clusters' records survive on the shared network node.
    assert "api.prod.ocp.local" in names
    assert "api-int.prod.ocp.local" in names
    assert ".apps.prod.ocp.local" in names
    assert "api.dev.ocp.local" in names
    assert "api-int.dev.ocp.local" in names
    assert ".apps.dev.ocp.local" in names


# ---------------------------------------------------------------------------
# Plan 4b Task 3: bastion bake decided by install_via (not cluster count)
# ---------------------------------------------------------------------------


def _single_cluster_topo_with_bastion():
    """One-cluster (clusters key present) topology with a bastion + one CP.

    Lets tests inspect both the bastion bake (ciUserData) AND the per-cluster
    ``_generated*`` configs stored on ``topology["clusters"][0]``.
    """
    return {
        "nodes": [
            {
                "id": "net",
                "type": "networkNode",
                "position": {"x": 0, "y": 0},
                "data": {
                    "subtype": "network",
                    "cidr": "10.0.0.0/24",
                    "networkType": "cluster",
                },
            },
            {
                "id": "bastion",
                "type": "vmNode",
                "position": {"x": 0, "y": 0},
                "data": {
                    "name": "bastion",
                    "os": "rhel",
                    "diskControllers": [],
                    "nics": [
                        {"id": "n1", "mac": "52:54:00:00:00:01", "ip": "10.0.0.50"},
                        {"id": "n2", "mac": "52:54:00:00:00:02"},
                    ],
                },
            },
            {
                "id": "cp0",
                "type": "vmNode",
                "position": {"x": 0, "y": 0},
                "data": {
                    "name": "cp0",
                    "os": "rhcos",
                    "clusterId": "ocp",
                    "tags": {"AnsibleGroup": "controllers"},
                    "bmcEnabled": True,
                    "bmcIp": "192.168.100.10",
                    "nics": [
                        {"id": "n3", "mac": "52:54:00:00:00:03", "ip": "10.0.0.10"}
                    ],
                    "diskControllers": [],
                },
            },
        ],
        "edges": [],
        "clusters": [
            {
                "id": "ocp",
                "name": "ocp",
                "type": "sno",
                "controlPlane": 1,
                "workers": 0,
                "baseDomain": "ocp.local",
                "apiVip": "",
                "ingressVip": "",
            }
        ],
    }


def _two_clusters_def():
    return [
        {
            "id": "prod",
            "name": "prod",
            "type": "standard",
            "controlPlane": 3,
            "workers": 2,
            "baseDomain": "ocp.local",
            "apiVip": "10.0.0.10",
            "ingressVip": "10.0.0.11",
        },
        {
            "id": "dev",
            "name": "dev",
            "type": "sno",
            "controlPlane": 1,
            "workers": 0,
            "baseDomain": "dev.local",
            "apiVip": "",
            "ingressVip": "",
        },
    ]


def _install_via_config(install_via):
    return {
        "cluster_name": "ocp",
        "base_domain": "ocp.local",
        "ocp_version": "4.20",
        "common_password": "pw",
        "pull_secret_json": '{"auths":{}}',
        "ssh_pub_key": "ssh-rsa x",
        "auto_install_ocp": True,
        "install_via": install_via,
        "resolved": {},
    }


def test_customize_bastion_single_cluster_bakes():
    """install_via=bastion + single cluster -> bastion cloud-init baked."""
    from app.services.ocp.agent_template import customize_topology

    topo = _single_cluster_topo_with_bastion()
    customize_topology(topo, "ocp-sno", _install_via_config("bastion"))

    bastion = next(n for n in topo["nodes"] if n["data"].get("name") == "bastion")
    assert bastion["data"].get("ciUserData")
    assert bastion["data"].get("cloudInit") is True


def test_customize_pod_single_cluster_no_bake_but_generated():
    """install_via=pod + single cluster -> NO bastion bake, but _generated*
    still stored on the cluster for the ops pod."""
    from app.services.ocp.agent_template import customize_topology

    topo = _single_cluster_topo_with_bastion()
    customize_topology(topo, "ocp-sno", _install_via_config("pod"))

    bastion = next(n for n in topo["nodes"] if n["data"].get("name") == "bastion")
    assert not bastion["data"].get("ciUserData")
    assert bastion["data"].get("cloudInit") is not True

    cluster = topo["clusters"][0]
    assert cluster["_generatedInstallConfig"]
    assert cluster["_generatedAgentConfig"]


def test_customize_pod_multi_cluster_no_bake_generated_on_all():
    """install_via=pod + multi cluster -> no bake, _generated* on EVERY cluster."""
    from app.services.ocp.agent_template import customize_topology

    topo = _two_cluster_topo()
    topo["clusters"] = _two_clusters_def()
    customize_topology(topo, "ocp-multi", _install_via_config("pod"))

    for cluster in topo["clusters"]:
        assert cluster["_generatedInstallConfig"]
        assert cluster["_generatedAgentConfig"]


def test_customize_bastion_multi_cluster_raises():
    """install_via=bastion + multi cluster -> validation error (a single bastion
    can't install multiple clusters)."""
    import pytest

    from app.services.ocp.agent_template import customize_topology

    topo = _two_cluster_topo()
    topo["clusters"] = _two_clusters_def()
    with pytest.raises(ValueError):
        customize_topology(topo, "ocp-multi", _install_via_config("bastion"))


def test_pick_unused_ips_top_down_excludes_used():
    """pick_unused_ips picks top-down and excludes used IPs."""
    from app.services.ocp.agent_template import pick_unused_ips

    used = {"10.0.0.0", "10.0.0.255", "10.0.0.1", "10.0.0.254"}
    result = pick_unused_ips("10.0.0.0/24", used, 2)
    assert result == ["10.0.0.253", "10.0.0.252"]


def test_pick_unused_ips_exhausted_raises():
    """pick_unused_ips raises ValueError when fewer free IPs than requested."""
    import pytest

    from app.services.ocp.agent_template import pick_unused_ips

    # Exhausted CIDR with only 3 hosts
    used = {"10.0.0.0", "10.0.0.1", "10.0.0.2"}
    with pytest.raises(ValueError, match="no 2 free IPs"):
        pick_unused_ips("10.0.0.0/30", used, 2)


def test_derive_vips_avoid_member_ip_collision():
    """Multi-node cluster whose members occupy .2/.3/.4 gets VIPs from high range."""
    from app.services.ocp.agent_template import (
        resolve_cluster_vips,
    )

    members = [
        _member("cp-0", "test", "controllers", "10.0.0.2", "52:54:00:aa:bb:01"),
        _member("cp-1", "test", "controllers", "10.0.0.3", "52:54:00:aa:bb:02"),
        _member("w-0", "test", "workers", "10.0.0.4", "52:54:00:aa:bb:03"),
    ]
    topo = {
        "nodes": [
            {
                "id": "net-1",
                "type": "networkNode",
                "data": {
                    "subtype": "network",
                    "cidr": "10.0.0.0/24",
                    "networkType": "cluster",
                },
            }
        ]
        + members,
        "edges": [],
    }
    cluster = {
        "id": "test",
        "name": "test",
        "type": "standard",
        "controlPlane": 3,
        "workers": 1,
        "baseDomain": "ocp.local",
    }
    api_vip, ingress_vip = resolve_cluster_vips(cluster, members, topo)
    # Should pick from high end, not collide with .2, .3, .4
    assert api_vip not in ("10.0.0.2", "10.0.0.3", "10.0.0.4")
    assert ingress_vip not in ("10.0.0.2", "10.0.0.3", "10.0.0.4")
    assert api_vip != ingress_vip
    # Both should be in the /24
    import ipaddress

    net = ipaddress.ip_network("10.0.0.0/24")
    assert ipaddress.ip_address(api_vip) in net
    assert ipaddress.ip_address(ingress_vip) in net


def test_derive_vips_two_clusters_distinct():
    """Two multi-node clusters on same network get distinct VIPs.

    Simulates the real deployment flow where each cluster's VIPs are stored
    back on the cluster object so subsequent calls can exclude them.
    """
    from app.services.ocp.agent_template import resolve_cluster_vips

    prod_members = [
        _member("p-cp-0", "prod", "controllers", "10.0.0.20", "52:54:00:aa:bb:01"),
        _member("p-cp-1", "prod", "controllers", "10.0.0.21", "52:54:00:aa:bb:02"),
    ]
    dev_members = [
        _member("d-cp-0", "dev", "controllers", "10.0.0.30", "52:54:00:aa:cc:01"),
        _member("d-cp-1", "dev", "controllers", "10.0.0.31", "52:54:00:aa:cc:02"),
    ]
    topo = {
        "nodes": [
            {
                "id": "net-1",
                "type": "networkNode",
                "data": {
                    "subtype": "network",
                    "cidr": "10.0.0.0/24",
                    "networkType": "cluster",
                },
            }
        ]
        + prod_members
        + dev_members,
        "edges": [],
        "clusters": [
            {
                "id": "prod",
                "name": "prod",
                "type": "standard",
                "controlPlane": 2,
                "workers": 0,
                "baseDomain": "ocp.local",
            },
            {
                "id": "dev",
                "name": "dev",
                "type": "standard",
                "controlPlane": 2,
                "workers": 0,
                "baseDomain": "dev.local",
            },
        ],
    }

    prod_cluster = topo["clusters"][0]
    dev_cluster = topo["clusters"][1]

    # Simulate _customize_one_cluster which stores VIPs back on the cluster
    prod_api, prod_ing = resolve_cluster_vips(prod_cluster, prod_members, topo)
    prod_cluster["apiVip"] = prod_api
    prod_cluster["ingressVip"] = prod_ing

    # Now when dev cluster resolves, it should exclude prod's VIPs
    dev_api, dev_ing = resolve_cluster_vips(dev_cluster, dev_members, topo)

    # All VIPs distinct from each other
    vips = {prod_api, prod_ing, dev_api, dev_ing}
    assert len(vips) == 4, f"Expected 4 distinct VIPs, got {vips}"

    # None collide with member IPs
    member_ips = {"10.0.0.20", "10.0.0.21", "10.0.0.30", "10.0.0.31"}
    assert prod_api not in member_ips
    assert prod_ing not in member_ips
    assert dev_api not in member_ips
    assert dev_ing not in member_ips


def test_explicit_vips_still_win():
    """Explicit VIPs in cluster dict are used, not derived."""
    from app.services.ocp.agent_template import resolve_cluster_vips

    members = [
        _member("cp-0", "test", "controllers", "10.0.0.10", "52:54:00:aa:bb:01"),
        _member("cp-1", "test", "controllers", "10.0.0.11", "52:54:00:aa:bb:02"),
    ]
    topo = {
        "nodes": [
            {
                "id": "net-1",
                "type": "networkNode",
                "data": {
                    "subtype": "network",
                    "cidr": "10.0.0.0/24",
                    "networkType": "cluster",
                },
            }
        ]
        + members,
        "edges": [],
    }
    cluster = {
        "id": "test",
        "name": "test",
        "type": "standard",
        "controlPlane": 2,
        "workers": 0,
        "baseDomain": "ocp.local",
        "apiVip": "10.0.0.100",
        "ingressVip": "10.0.0.101",
    }
    api_vip, ingress_vip = resolve_cluster_vips(cluster, members, topo)
    assert api_vip == "10.0.0.100"
    assert ingress_vip == "10.0.0.101"


def test_sno_vips_are_node_ip():
    """SNO cluster gets both VIPs from control-plane node IP."""
    from app.services.ocp.agent_template import resolve_cluster_vips

    members = [
        _member("cp-0", "test", "controllers", "10.0.0.50", "52:54:00:aa:bb:01"),
    ]
    topo = {
        "nodes": [
            {
                "id": "net-1",
                "type": "networkNode",
                "data": {
                    "subtype": "network",
                    "cidr": "10.0.0.0/24",
                    "networkType": "cluster",
                },
            }
        ]
        + members,
        "edges": [],
    }
    cluster = {
        "id": "test",
        "name": "test",
        "type": "sno",
        "controlPlane": 1,
        "workers": 0,
        "baseDomain": "ocp.local",
    }
    api_vip, ingress_vip = resolve_cluster_vips(cluster, members, topo)
    assert api_vip == "10.0.0.50"
    assert ingress_vip == "10.0.0.50"
