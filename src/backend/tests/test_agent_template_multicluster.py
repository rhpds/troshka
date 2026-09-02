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
