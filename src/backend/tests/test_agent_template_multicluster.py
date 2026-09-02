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
