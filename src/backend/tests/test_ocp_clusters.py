def test_remap_topology_remaps_cluster_refs():
    from app.api.patterns import _remap_topology

    topo = {
        "clusters": [{"id": "prod", "nodeId": "cluster-prod", "name": "prod"}],
        "nodes": [
            {"id": "cluster-prod", "type": "clusterNode", "data": {"name": "prod"}},
            {
                "id": "n1",
                "type": "vmNode",
                "parentId": "cluster-prod",
                "data": {
                    "os": "rhcos",
                    "clusterId": "prod",
                    "nics": [{"id": "nic1", "mac": "52:54:00:aa:bb:cc"}],
                },
            },
        ],
        "edges": [],
    }
    out = _remap_topology(topo)
    new_cluster = out["clusters"][0]
    assert new_cluster["id"] != "prod"
    member = next(n for n in out["nodes"] if n["type"] == "vmNode")
    assert member["data"]["clusterId"] == new_cluster["id"]
    assert member["parentId"] == new_cluster["nodeId"]
    # the cluster node itself got a new id matching nodeId
    cluster_node = next(n for n in out["nodes"] if n["type"] == "clusterNode")
    assert cluster_node["id"] == new_cluster["nodeId"]


def test_normalize_legacy_mapping_wraps_to_list():
    from app.services.template_loader import normalize_ocp_section

    legacy = {
        "cluster_name": "ocp",
        "base_domain": "ocp.local",
        "api_vip": "10.0.0.10",
        "ingress_vip": "10.0.0.11",
    }
    out = normalize_ocp_section(legacy)
    assert isinstance(out, list) and len(out) == 1
    assert out[0]["name"] == "ocp"
    assert out[0]["base_domain"] == "ocp.local"
    assert out[0]["api_vip"] == "10.0.0.10"
    assert out[0]["ingress_vip"] == "10.0.0.11"


def test_normalize_new_list_passthrough_and_name_default():
    from app.services.template_loader import normalize_ocp_section

    out = normalize_ocp_section(
        [
            {"name": "prod", "type": "standard", "workers": 2},
            {"type": "sno", "base_domain": "dev.local"},
        ]
    )
    assert [c["name"] for c in out] == ["prod", "ocp"]  # 2nd defaults name
    assert out[0]["type"] == "standard" and out[0]["workers"] == 2


def test_normalize_none_returns_empty():
    from app.services.template_loader import normalize_ocp_section

    assert normalize_ocp_section(None) == []
    assert normalize_ocp_section({}) == []


def test_build_clusters_type_from_explicit_field():
    from app.services.template_loader import (
        build_topology_clusters,
        normalize_ocp_section,
    )

    ocp = normalize_ocp_section(
        [
            {
                "name": "prod",
                "type": "standard",
                "workers": 2,
                "base_domain": "ocp.local",
                "api_vip": "10.0.0.10",
                "ingress_vip": "10.0.0.11",
            }
        ]
    )
    clusters = build_topology_clusters(ocp, vms_def=None)
    assert len(clusters) == 1
    c = clusters[0]
    assert c["id"] == "prod" and c["name"] == "prod"
    assert c["type"] == "standard"
    assert c["controlPlane"] == 3 and c["workers"] == 2
    assert c["apiVip"] == "10.0.0.10" and c["ingressVip"] == "10.0.0.11"
    assert c["controlPlaneCpu"] == 8 and c["workerMemory"] == 8192


def test_build_clusters_infer_type_from_vm_roles():
    from app.services.template_loader import (
        build_topology_clusters,
        normalize_ocp_section,
    )

    # legacy mapping, no type — 3 CP + 2 workers => standard
    ocp = normalize_ocp_section({"cluster_name": "ocp", "base_domain": "ocp.local"})
    vms = {
        "cp-0": {"role": "control-plane"},
        "cp-1": {"role": "control-plane"},
        "cp-2": {"role": "control-plane"},
        "worker-0": {"role": "worker"},
        "worker-1": {"role": "worker"},
        "bastion": {"role": "bastion"},
    }
    clusters = build_topology_clusters(ocp, vms_def=vms)
    assert clusters[0]["type"] == "standard"
    assert clusters[0]["controlPlane"] == 3 and clusters[0]["workers"] == 2


def test_build_clusters_non_numeric_workers_falls_back():
    from app.services.template_loader import (
        build_topology_clusters,
        normalize_ocp_section,
    )

    # A non-numeric workers value must not raise; it falls back to counted VMs.
    ocp = normalize_ocp_section(
        [{"name": "prod", "type": "standard", "workers": "abc"}]
    )
    clusters = build_topology_clusters(ocp, vms_def={})
    assert len(clusters) == 1
    assert clusters[0]["workers"] == 0


def test_role_count_recognizes_ansiblegroup_tag():
    from app.services.template_loader import (
        generate_topology_from_template,
        resolve_inline_template,
    )

    # VMs express role via tags.AnsibleGroup (no `role:`); an explicit type is
    # given. Generation must NOT double-create a full control-plane set.
    tmpl = {
        "name": "t",
        "install_method": "agent",
        "category": "openshift",
        "networks": {"cluster": {"cidr": "10.0.0.0/24"}},
        "ocp": [{"name": "prod", "type": "standard", "workers": 0}],
        "vms": {
            "cp-0": {"os": "rhcos", "tags": {"AnsibleGroup": "controllers"}},
            "cp-1": {"os": "rhcos", "tags": {"AnsibleGroup": "controllers"}},
            "cp-2": {"os": "rhcos", "tags": {"AnsibleGroup": "controllers"}},
        },
    }
    resolved = resolve_inline_template(tmpl)
    topo = generate_topology_from_template(resolved)

    controllers = [
        n
        for n in topo["nodes"]
        if "controllers" in n.get("data", {}).get("tags", {}).get("AnsibleGroup", "")
    ]
    # exactly 3 control-plane VMs, not a doubled set of 6
    assert len(controllers) == 3


def test_build_clusters_infer_sno():
    from app.services.template_loader import (
        build_topology_clusters,
        normalize_ocp_section,
    )

    ocp = normalize_ocp_section({"cluster_name": "ocp"})
    vms = {"cp-0": {"role": "control-plane"}}
    clusters = build_topology_clusters(ocp, vms_def=vms)
    assert clusters[0]["type"] == "sno" and clusters[0]["controlPlane"] == 1
    assert clusters[0]["workers"] == 0


def test_materialize_generates_missing_cp_and_workers():
    from app.services.template_loader import (
        build_topology_clusters,
        materialize_cluster_vms,
        normalize_ocp_section,
    )

    ocp = normalize_ocp_section([{"name": "prod", "type": "standard", "workers": 2}])
    clusters = build_topology_clusters(ocp, vms_def={})
    vms = materialize_cluster_vms(clusters, vms_def={})
    cps = [n for n, c in vms.items() if c.get("role") == "control-plane"]
    wks = [n for n, c in vms.items() if c.get("role") == "worker"]
    assert len(cps) == 3 and len(wks) == 2
    sample = vms[cps[0]]
    assert sample["os"] == "rhcos" and sample["cluster"] == "prod"
    assert sample["cpu"] == 8 and sample["memory"] == 16384 and sample["disk"] == 120


def test_materialize_marks_generated_vms():
    from app.services.template_loader import (
        build_topology_clusters,
        materialize_cluster_vms,
        normalize_ocp_section,
    )

    ocp = normalize_ocp_section([{"name": "prod", "type": "standard", "workers": 2}])
    # One hand-enumerated CP that must NOT be marked generated.
    vms_in = {"cp-0": {"role": "control-plane", "cluster": "prod", "cpu": 16}}
    clusters = build_topology_clusters(ocp, vms_def=vms_in)
    vms = materialize_cluster_vms(clusters, vms_def=vms_in)

    # Every auto-generated CP/worker carries generated=True so count-driven
    # add/remove (backend + canvas) only ever reaps VMs it created.
    generated = [n for n, c in vms.items() if c.get("generated") is True]
    assert len(generated) == 4  # 2 topped-up CP + 2 workers
    assert all(
        vms[n]["os"] == "rhcos" and vms[n]["cluster"] == "prod" for n in generated
    )
    # The enumerated VM stays untouched (no generated flag).
    assert "generated" not in vms["cp-0"]


def test_materialize_preserves_enumerated_vms():
    from app.services.template_loader import (
        build_topology_clusters,
        materialize_cluster_vms,
        normalize_ocp_section,
    )

    ocp = normalize_ocp_section([{"name": "prod", "type": "standard", "workers": 2}])
    vms_in = {"cp-0": {"role": "control-plane", "cluster": "prod", "cpu": 16}}
    clusters = build_topology_clusters(ocp, vms_def=vms_in)
    vms = materialize_cluster_vms(clusters, vms_def=vms_in)
    # keeps the custom cp-0 (cpu 16), tops up to 3 CP total
    assert vms["cp-0"]["cpu"] == 16
    assert len([c for c in vms.values() if c.get("role") == "control-plane"]) == 3


def test_materialize_does_not_overwrite_gapped_enumerated_vm():
    from app.services.template_loader import (
        build_topology_clusters,
        materialize_cluster_vms,
        normalize_ocp_section,
    )

    ocp = normalize_ocp_section([{"name": "prod", "type": "standard", "workers": 0}])
    # enumerated CP at a gapped/high index carrying a distinctive field
    vms_in = {"prod-cp-2": {"role": "control-plane", "cluster": "prod", "cpu": 32}}
    clusters = build_topology_clusters(ocp, vms_def=vms_in)
    vms = materialize_cluster_vms(clusters, vms_def=vms_in)
    # the enumerated node must survive untouched
    assert vms["prod-cp-2"]["cpu"] == 32
    # exactly 3 control-plane VMs total (enumerated + generated)
    assert len([c for c in vms.values() if c.get("role") == "control-plane"]) == 3


def test_materialize_sno_single_node():
    from app.services.template_loader import (
        build_topology_clusters,
        materialize_cluster_vms,
        normalize_ocp_section,
    )

    ocp = normalize_ocp_section([{"name": "dev", "type": "sno"}])
    clusters = build_topology_clusters(ocp, vms_def={})
    vms = materialize_cluster_vms(clusters, vms_def={})
    assert len([c for c in vms.values() if c.get("role") == "control-plane"]) == 1
    assert len([c for c in vms.values() if c.get("role") == "worker"]) == 0


def test_generated_topology_has_clusters_and_member_refs():
    from app.services.template_loader import (
        generate_topology_from_template,
        resolve_inline_template,
    )

    tmpl = {
        "name": "t",
        "install_method": "agent",
        "category": "openshift",
        "networks": {"cluster": {"cidr": "10.0.0.0/24"}},
        "ocp": [
            {
                "name": "prod",
                "type": "standard",
                "workers": 2,
                "api_vip": "10.0.0.10",
                "ingress_vip": "10.0.0.11",
            }
        ],
    }
    resolved = resolve_inline_template(tmpl)
    topo = generate_topology_from_template(resolved)

    assert "clusters" in topo and len(topo["clusters"]) == 1
    prod = topo["clusters"][0]
    assert prod["id"] == "prod" and prod["nodeId"] == "cluster-prod"

    cluster_nodes = [
        n
        for n in topo["nodes"]
        if n.get("type") == "clusterNode" and n.get("id") == "cluster-prod"
    ]
    assert len(cluster_nodes) == 1

    members = [n for n in topo["nodes"] if n.get("data", {}).get("clusterId") == "prod"]
    assert len(members) == 5  # 3 cp + 2 workers
    assert all(n.get("parentId") == "cluster-prod" for n in members)


def test_generate_topology_multi_cluster_membership():
    from app.services.template_loader import (
        generate_topology_from_template,
        resolve_inline_template,
    )

    tmpl = {
        "name": "t",
        "install_method": "agent",
        "category": "openshift",
        "networks": {"cluster": {"cidr": "10.0.0.0/24"}},
        "ocp": [
            {"name": "prod", "type": "standard", "workers": 2},
            {"name": "dev", "type": "sno"},
        ],
    }
    resolved = resolve_inline_template(tmpl)
    topo = generate_topology_from_template(resolved)

    # Two clusters and two boundary nodes.
    assert {c["id"] for c in topo["clusters"]} == {"prod", "dev"}
    cluster_node_ids = {
        n["id"] for n in topo["nodes"] if n.get("type") == "clusterNode"
    }
    assert cluster_node_ids == {"cluster-prod", "cluster-dev"}

    prod_members = [
        n for n in topo["nodes"] if n.get("data", {}).get("clusterId") == "prod"
    ]
    dev_members = [
        n for n in topo["nodes"] if n.get("data", {}).get("clusterId") == "dev"
    ]
    # prod = 3 cp + 2 workers, dev (sno) = 1 cp.
    assert len(prod_members) == 5
    assert len(dev_members) == 1
    assert all(n.get("parentId") == "cluster-prod" for n in prod_members)
    assert all(n.get("parentId") == "cluster-dev" for n in dev_members)


def test_non_rhcos_vm_excluded_from_cluster():
    from app.services.template_loader import (
        generate_topology_from_template,
        resolve_inline_template,
    )

    tmpl = {
        "name": "t",
        "install_method": "agent",
        "category": "openshift",
        "networks": {"cluster": {"cidr": "10.0.0.0/24"}},
        "ocp": [
            {"name": "prod", "type": "sno"},
            {"name": "dev", "type": "sno"},
        ],
        # A non-RHCOS VM tagged with a cluster must NOT become a member.
        "vms": {
            "bastion": {"role": "bastion", "os": "rhel", "cluster": "prod"},
        },
    }
    resolved = resolve_inline_template(tmpl)
    topo = generate_topology_from_template(resolved)

    bastion = next(
        n
        for n in topo["nodes"]
        if n.get("type") == "vmNode" and n["data"]["name"] == "bastion"
    )
    assert "clusterId" not in bastion["data"]
    assert "parentId" not in bastion


def test_migrate_legacy_topology_synthesizes_cluster():
    from app.services.ocp.cluster_migration import migrate_topology_clusters

    legacy = {
        "nodes": [
            {
                "id": "n1",
                "type": "vmNode",
                "data": {
                    "os": "rhcos",
                    "name": "cp-0",
                    "tags": {"AnsibleGroup": "controllers"},
                },
            },
            {
                "id": "n2",
                "type": "vmNode",
                "data": {
                    "os": "rhcos",
                    "name": "worker-0",
                    "tags": {"AnsibleGroup": "workers"},
                },
            },
        ],
        "edges": [],
    }
    out = migrate_topology_clusters(legacy)
    assert len(out["clusters"]) == 1
    assert out["clusters"][0]["id"] == "ocp"
    assert out["clusters"][0]["type"] == "standard"  # 1 cp + 1 worker => standard
    assert all(
        n["data"]["clusterId"] == "ocp" and n["parentId"] == "cluster-ocp"
        for n in out["nodes"]
        if n["data"].get("os") == "rhcos"
    )


def test_migrate_is_idempotent_when_clusters_present():
    from app.services.ocp.cluster_migration import migrate_topology_clusters

    topo = {"nodes": [], "edges": [], "clusters": [{"id": "prod"}]}
    assert migrate_topology_clusters(topo) is topo


def test_migrate_noop_without_ocp_nodes():
    from app.services.ocp.cluster_migration import migrate_topology_clusters

    topo = {"nodes": [{"id": "n1", "data": {"os": "rhel"}}], "edges": []}
    out = migrate_topology_clusters(topo)
    assert "clusters" not in out or out["clusters"] == []


def test_migrate_adds_cluster_node():
    from app.services.ocp.cluster_migration import migrate_topology_clusters

    legacy = {
        "nodes": [
            {
                "id": "n1",
                "type": "vmNode",
                "data": {
                    "os": "rhcos",
                    "name": "cp-0",
                    "tags": {"AnsibleGroup": "controllers"},
                },
            },
        ],
        "edges": [],
    }
    out = migrate_topology_clusters(legacy)
    cluster_nodes = [
        n
        for n in out["nodes"]
        if n.get("id") == "cluster-ocp" and n.get("type") == "clusterNode"
    ]
    assert len(cluster_nodes) == 1
    # 1 cp + 0 workers => sno; assert the node's data mirrors the inferred cluster.
    cdata = cluster_nodes[0]["data"]
    assert cdata["name"] == "ocp"
    assert cdata["type"] == "sno"
    assert cdata["controlPlane"] == 1
    assert cdata["workers"] == 0
    assert cdata["baseDomain"] == "ocp.local"
    assert "apiVip" in cdata and "ingressVip" in cdata
    assert cdata["apiVip"] is None and cdata["ingressVip"] is None
    # The clusters[] entry must carry the generator's sizing/registry defaults.
    cluster = out["clusters"][0]
    assert cluster["controlPlaneCpu"] == 8
    assert cluster["controlPlaneMemory"] == 16384
    assert cluster["controlPlaneDisk"] == 120
    assert cluster["workerCpu"] == 4
    assert cluster["workerMemory"] == 8192
    assert cluster["workerDisk"] == 100
    assert cluster["pullThroughRegistry"] is None


def test_shipped_templates_use_ocp_list_and_generate_clusters():
    import os

    from app.services.template_loader import (
        generate_topology_from_template,
        load_template,
        resolve_inline_template,
    )

    tdir = os.path.join(os.path.dirname(__file__), "..", "templates")
    for name, expect_type in [
        ("ocp-sno", "sno"),
        ("ocp-compact", "compact"),
        ("ocp-standard", "standard"),
    ]:
        raw = load_template(name, templates_dir=tdir)
        assert isinstance(raw["ocp"], list), f"{name} ocp not a list"
        topo = generate_topology_from_template(resolve_inline_template(raw))
        assert topo["clusters"][0]["type"] == expect_type
