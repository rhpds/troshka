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
    # _make_node emits the template-format keys _build_vm_data reads (vcpus /
    # ram_gb in GB), not raw cpu/memory-MB.
    assert sample["vcpus"] == 8 and sample["ram_gb"] == 16 and sample["disk"] == 120


def test_materialized_node_data_carries_sizing():
    """Count-materialized VMs must reach the FINAL node.data with real sizing.

    Regression: _make_node previously wrote cpu/memory(MB) which _build_vm_data
    ignored, so materialized nodes fell back to vcpus=2 / ram=4 defaults.
    """
    from app.services.template_loader import (
        generate_topology_from_template,
        resolve_inline_template,
    )

    tmpl = {
        "name": "t",
        "install_method": "agent",
        "category": "openshift",
        "networks": {"cluster": {"cidr": "10.0.0.0/24"}},
        "ocp": [{"name": "prod", "type": "standard", "workers": 1}],
    }
    topo = generate_topology_from_template(resolve_inline_template(tmpl))

    members = [n for n in topo["nodes"] if n["data"].get("clusterId") == "prod"]
    cp = next(
        n
        for n in members
        if "controllers" in n["data"].get("tags", {}).get("AnsibleGroup", "")
    )
    wk = next(
        n
        for n in members
        if "workers" in n["data"].get("tags", {}).get("AnsibleGroup", "")
    )
    assert cp["data"]["vcpus"] == 8 and cp["data"]["ram"] == 16
    assert wk["data"]["vcpus"] == 4 and wk["data"]["ram"] == 8
    # Ruling B: generated marker propagates into the final node.data.
    assert cp["data"]["generated"] is True and wk["data"]["generated"] is True


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


def test_ocp_port_forwards_single_cluster_canonical():
    """One cluster keeps the canonical 6443/443/80 external ports (back-compat)."""
    from app.services.template_loader import _generate_ocp_port_forwards

    vms = {"bastion": {"role": "bastion", "nics": [{"ip": "10.0.0.5"}]}}
    clusters = [{"api_vip": "10.0.0.10", "ingress_vip": "10.0.0.11"}]
    pfs = _generate_ocp_port_forwards("eip-1", vms, clusters)
    by_port = {pf["extPort"]: pf for pf in pfs}

    # Exactly the canonical set: bastion SSH + api + ingress https/http.
    assert set(by_port) == {"2222", "6443", "443", "80"}
    assert by_port["2222"]["intIp"] == "10.0.0.5"
    assert by_port["2222"]["intPort"] == "22"
    assert by_port["6443"]["intIp"] == "10.0.0.10"
    assert by_port["6443"]["intPort"] == "6443"
    assert by_port["443"]["intIp"] == "10.0.0.11"
    assert by_port["443"]["intPort"] == "443"
    assert by_port["80"]["intIp"] == "10.0.0.11"
    assert by_port["80"]["intPort"] == "80"
    # Every forward references the single EIP.
    assert all(pf["extIpId"] == "eip-1" for pf in pfs)


def test_ocp_port_forwards_two_clusters_no_collision():
    """Two clusters coexist on one EIP with distinct external ports."""
    from app.services.template_loader import _generate_ocp_port_forwards

    vms = {"bastion": {"role": "bastion", "nics": [{"ip": "10.0.0.5"}]}}
    clusters = [
        {"api_vip": "10.0.0.10", "ingress_vip": "10.0.0.11"},
        {"api_vip": "10.1.0.10", "ingress_vip": "10.1.0.11"},
    ]
    pfs = _generate_ocp_port_forwards("eip-1", vms, clusters)

    # No external-port collision across the whole set.
    ext_ports = [pf["extPort"] for pf in pfs]
    assert len(ext_ports) == len(set(ext_ports))

    by_port = {pf["extPort"]: pf for pf in pfs}
    # Cluster 0 stays canonical, mapped to cluster 0's VIPs.
    assert by_port["6443"]["intIp"] == "10.0.0.10"
    assert by_port["443"]["intIp"] == "10.0.0.11"
    assert by_port["80"]["intIp"] == "10.0.0.11"
    # Cluster 1 on distinct external ports, mapped to cluster 1's VIPs.
    assert by_port["6444"]["intIp"] == "10.1.0.10"
    assert by_port["6444"]["intPort"] == "6443"
    assert by_port["8444"]["intIp"] == "10.1.0.11"
    assert by_port["8444"]["intPort"] == "443"
    assert by_port["8081"]["intIp"] == "10.1.0.11"
    assert by_port["8081"]["intPort"] == "80"


def test_ocp_port_forwards_skips_novip_cluster():
    """A cluster without VIPs (SNO) is skipped gracefully, matching old behavior."""
    from app.services.template_loader import _generate_ocp_port_forwards

    vms = {"bastion": {"role": "bastion", "nics": [{"ip": "10.0.0.5"}]}}
    clusters = [
        {"api_vip": "10.0.0.10", "ingress_vip": "10.0.0.11"},
        {"api_vip": "", "ingress_vip": ""},
    ]
    pfs = _generate_ocp_port_forwards("eip-1", vms, clusters)
    by_port = {pf["extPort"]: pf for pf in pfs}
    assert set(by_port) == {"2222", "6443", "443", "80"}


# ---------------------------------------------------------------------------
# normalize_cluster_member_fields (Task 7)
# ---------------------------------------------------------------------------


def test_normalize_canvas_member_gains_install_fields():
    """A canvas-created member (only os/clusterId/clusterRole) becomes
    deploy-ready: firmware/bmc/boot/diskControllers stamped, AnsibleGroup synced."""
    from app.services.template_loader import normalize_cluster_member_fields

    topo = {
        "nodes": [
            {
                "id": "m1",
                "type": "vmNode",
                "data": {
                    "os": "rhcos",
                    "clusterId": "prod",
                    "clusterRole": "control-plane",
                    "name": "cp-0",
                },
            }
        ],
        "edges": [],
    }
    d = normalize_cluster_member_fields(topo)["nodes"][0]["data"]
    assert d["firmware"] == "uefi"
    assert d["bmcEnabled"] is True
    assert d["secureBoot"] is False
    assert d["powerOnAtDeploy"] is True
    assert d["bootMethod"] == "disk"
    assert "bootDevices" in d
    assert isinstance(d["diskControllers"], list) and d["diskControllers"]
    # A cdrom controller is present for agent ISO boot.
    assert any(dc.get("name", "").startswith("cdrom") for dc in d["diskControllers"])
    # clusterRole present -> AnsibleGroup synced to controllers.
    assert d["tags"]["AnsibleGroup"] == "controllers"


def test_normalize_unroled_member_defaults_to_worker():
    """A member with clusterId but NEITHER clusterRole NOR AnsibleGroup -> worker."""
    from app.services.template_loader import normalize_cluster_member_fields

    topo = {
        "nodes": [
            {
                "id": "m1",
                "type": "vmNode",
                "data": {"os": "rhcos", "clusterId": "prod", "name": "x"},
            }
        ],
        "edges": [],
    }
    d = normalize_cluster_member_fields(topo)["nodes"][0]["data"]
    assert d["clusterRole"] == "worker"
    assert "workers" in d["tags"]["AnsibleGroup"]


def test_normalize_syncs_ansiblegroup_from_clusterrole():
    """clusterRole present but no AnsibleGroup -> AnsibleGroup synced."""
    from app.services.template_loader import normalize_cluster_member_fields

    topo = {
        "nodes": [
            {
                "id": "m1",
                "type": "vmNode",
                "data": {"os": "rhcos", "clusterId": "prod", "clusterRole": "worker"},
            }
        ],
        "edges": [],
    }
    d = normalize_cluster_member_fields(topo)["nodes"][0]["data"]
    assert d["tags"]["AnsibleGroup"] == "workers"


def test_normalize_syncs_clusterrole_from_ansiblegroup():
    """AnsibleGroup present but no clusterRole -> clusterRole synced."""
    from app.services.template_loader import normalize_cluster_member_fields

    topo = {
        "nodes": [
            {
                "id": "m1",
                "type": "vmNode",
                "data": {
                    "os": "rhcos",
                    "clusterId": "prod",
                    "tags": {"AnsibleGroup": "controllers"},
                },
            }
        ],
        "edges": [],
    }
    d = normalize_cluster_member_fields(topo)["nodes"][0]["data"]
    assert d["clusterRole"] == "control-plane"
    assert d["tags"]["AnsibleGroup"] == "controllers"


def test_normalize_is_idempotent():
    """A second normalize pass is a no-op."""
    import copy

    from app.services.template_loader import normalize_cluster_member_fields

    topo = {
        "nodes": [
            {
                "id": "m1",
                "type": "vmNode",
                "data": {"os": "rhcos", "clusterId": "prod", "clusterRole": "worker"},
            }
        ],
        "edges": [],
    }
    once = normalize_cluster_member_fields(topo)
    snapshot = copy.deepcopy(once["nodes"][0]["data"])
    twice = normalize_cluster_member_fields(once)
    assert twice["nodes"][0]["data"] == snapshot


def test_normalize_leaves_configured_member_untouched():
    """An already-configured member keeps every explicit value (no overwrite)."""
    import copy

    from app.services.template_loader import normalize_cluster_member_fields

    data = {
        "os": "rhcos",
        "clusterId": "prod",
        "clusterRole": "control-plane",
        "tags": {"AnsibleGroup": "controllers"},
        "firmware": "bios",
        "bmcEnabled": False,
        "secureBoot": True,
        "powerOnAtDeploy": False,
        "bootMethod": "network",
        "bootDevices": ["disk-xyz"],
        "diskControllers": [{"id": "dp-1", "name": "disk0", "bus": "virtio"}],
    }
    topo = {
        "nodes": [{"id": "m1", "type": "vmNode", "data": copy.deepcopy(data)}],
        "edges": [],
    }
    out = normalize_cluster_member_fields(topo)
    assert out["nodes"][0]["data"] == data


def test_normalize_skips_non_member_vm():
    """A VM without clusterId is left entirely untouched."""
    import copy

    from app.services.template_loader import normalize_cluster_member_fields

    data = {"os": "rhel", "name": "bastion"}
    topo = {
        "nodes": [{"id": "b1", "type": "vmNode", "data": copy.deepcopy(data)}],
        "edges": [],
    }
    out = normalize_cluster_member_fields(topo)
    assert out["nodes"][0]["data"] == data


def test_normalize_bootdevices_from_connected_disk():
    """When a member has a data-disk controller, bootDevices points at its disk."""
    from app.services.template_loader import normalize_cluster_member_fields

    topo = {
        "nodes": [
            {
                "id": "vm1",
                "type": "vmNode",
                "data": {
                    "os": "rhcos",
                    "clusterId": "prod",
                    "clusterRole": "worker",
                    "diskControllers": [
                        {"id": "dp-abc", "name": "disk0", "bus": "virtio"}
                    ],
                },
            },
            {"id": "disk1", "type": "storageNode", "data": {"name": "d"}},
        ],
        "edges": [
            {
                "source": "disk1",
                "target": "vm1",
                "sourceHandle": "right",
                "targetHandle": "dp-abc-left",
            }
        ],
    }
    d = normalize_cluster_member_fields(topo)["nodes"][0]["data"]
    assert d["bootDevices"] == ["disk1"]
    # Non-empty controllers are left intact (no forced cdrom).
    assert d["diskControllers"] == [{"id": "dp-abc", "name": "disk0", "bus": "virtio"}]


def test_customize_topology_normalizes_member_into_agent_hosts():
    """customize_topology normalizes a canvas member (clusterRole, no AnsibleGroup)
    so it lands in the agent-config hosts list (rendezvous gap closed)."""
    import yaml

    from app.services.ocp.agent_template import customize_topology

    topo = {
        "nodes": [
            {
                "id": "net",
                "type": "networkNode",
                "data": {
                    "subtype": "network",
                    "cidr": "10.0.0.0/24",
                    "networkType": "cluster",
                },
            },
            {
                "id": "cp0",
                "type": "vmNode",
                "data": {
                    "os": "rhcos",
                    "name": "cp-0",
                    "clusterId": "dev",
                    "clusterRole": "control-plane",  # canvas-created: no AnsibleGroup
                    "bmcEnabled": True,
                    "bmcIp": "192.168.50.10",
                    "nics": [
                        {"id": "n1", "ip": "10.0.0.20", "mac": "52:54:00:aa:bb:01"}
                    ],
                },
            },
        ],
        "edges": [],
        "clusters": [
            {
                "id": "dev",
                "name": "dev",
                "type": "sno",
                "controlPlane": 1,
                "workers": 0,
                "baseDomain": "dev.local",
                "apiVip": "",
                "ingressVip": "",
            }
        ],
    }
    config = {
        "common_password": "pw",
        "pull_secret_json": "{}",
        "ssh_pub_key": "x",
        "auto_install_ocp": True,
        "resolved": {},
    }
    customize_topology(topo, "ocp-multi", config)
    ac = yaml.safe_load(topo["clusters"][0]["_generatedAgentConfig"])
    assert [h["hostname"] for h in ac["hosts"]] == ["cp-0"]
    assert ac["rendezvousIP"] == "10.0.0.20"


# ---------------------------------------------------------------------------
# Export round-trip (Task 8): ocp: list + per-VM cluster: + worker role
# ---------------------------------------------------------------------------


def test_export_roundtrip_multi_cluster():
    """A 2-cluster template survives generate -> export -> resolve -> generate.

    Export emits ``ocp:`` as a list (names/types/VIPs) and stamps each member VM
    with ``cluster:``. A worker VM exports ``role: worker``. Re-resolving and
    regenerating yields the same two clusters with identical membership counts.
    """
    from app.services.template_loader import (
        export_topology_to_template,
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
            },
            {
                "name": "dev",
                "type": "sno",
                "api_vip": "10.1.0.10",
                "ingress_vip": "10.1.0.11",
            },
        ],
    }
    topo = generate_topology_from_template(resolve_inline_template(tmpl))
    exported = export_topology_to_template(topo)

    # ocp: emitted as a LIST of two clusters with correct names/types/VIPs.
    assert isinstance(exported["ocp"], list) and len(exported["ocp"]) == 2
    by_name = {c["name"]: c for c in exported["ocp"]}
    assert set(by_name) == {"prod", "dev"}
    assert by_name["prod"]["type"] == "standard"
    assert by_name["dev"]["type"] == "sno"
    assert by_name["prod"]["api_vip"] == "10.0.0.10"
    assert by_name["prod"]["ingress_vip"] == "10.0.0.11"
    assert by_name["dev"]["api_vip"] == "10.1.0.10"
    assert by_name["prod"]["base_domain"] == "ocp.local"

    # Member VMs carry the cluster: field (resolved to the cluster NAME).
    prod_members = [v for v in exported["vms"].values() if v.get("cluster") == "prod"]
    dev_members = [v for v in exported["vms"].values() if v.get("cluster") == "dev"]
    assert len(prod_members) == 5  # 3 cp + 2 workers
    assert len(dev_members) == 1

    # A worker VM exports role: worker.
    workers = [v for v in exported["vms"].values() if v.get("role") == "worker"]
    assert len(workers) == 2

    # Re-resolve + regenerate -> two clusters with the same membership counts.
    topo2 = generate_topology_from_template(resolve_inline_template(exported))
    assert {c["id"] for c in topo2["clusters"]} == {"prod", "dev"}
    prod2 = [n for n in topo2["nodes"] if n.get("data", {}).get("clusterId") == "prod"]
    dev2 = [n for n in topo2["nodes"] if n.get("data", {}).get("clusterId") == "dev"]
    assert len(prod2) == 5
    assert len(dev2) == 1


# ---------------------------------------------------------------------------
# Plan 4b Task 1: install_via selector (bastion|pod, default pod)
# ---------------------------------------------------------------------------


def test_resolve_install_via_default_pod():
    from app.services.template_loader import resolve_install_via

    assert resolve_install_via({}) == "pod"


def test_resolve_install_via_explicit_bastion():
    from app.services.template_loader import resolve_install_via

    assert resolve_install_via({"install_via": "bastion"}) == "bastion"


def test_resolve_install_via_explicit_pod():
    from app.services.template_loader import resolve_install_via

    assert resolve_install_via({"install_via": "pod"}) == "pod"


def test_resolve_install_via_invalid_falls_back_to_pod():
    from app.services.template_loader import resolve_install_via

    assert resolve_install_via({"install_via": "garbage"}) == "pod"


def test_resolve_install_via_ignores_install_method():
    # install_method is the agent installer TYPE, not the install path selector.
    from app.services.template_loader import resolve_install_via

    assert resolve_install_via({"install_method": "agent"}) == "pod"


def test_resolve_install_via_body_default_override():
    from app.services.template_loader import resolve_install_via

    assert resolve_install_via({}, default="bastion") == "bastion"


def test_ocp_install_via_reads_topology():
    from app.services.template_loader import ocp_install_via

    assert ocp_install_via({"ocpInstallVia": "bastion"}) == "bastion"
    assert ocp_install_via({"ocpInstallVia": "pod"}) == "pod"


def test_ocp_install_via_default_when_absent():
    from app.services.template_loader import ocp_install_via

    assert ocp_install_via({}) == "pod"


def test_ocp_install_via_invalid_topology_value_falls_back():
    from app.services.template_loader import ocp_install_via

    assert ocp_install_via({"ocpInstallVia": "nope"}) == "pod"


def test_install_via_default_explicit_config(monkeypatch):
    import types

    from app.core import config as config_module

    fake = types.SimpleNamespace(
        ocp=types.SimpleNamespace(install_via_default="bastion")
    )
    monkeypatch.setattr(config_module, "config", fake)
    from app.services.template_loader import _ocp_install_via_default

    assert _ocp_install_via_default() == "bastion"


def test_install_via_default_legacy_install_via_pod_flag(monkeypatch):
    # Legacy boolean ocp.install_via_pod: true still yields the "pod" default
    # (no explicit ocp.install_via_default set).
    import types

    from app.core import config as config_module

    fake = types.SimpleNamespace(ocp=types.SimpleNamespace(install_via_pod=True))
    monkeypatch.setattr(config_module, "config", fake)
    from app.services.template_loader import _ocp_install_via_default

    assert _ocp_install_via_default() == "pod"


def test_install_via_default_no_ocp_config(monkeypatch):
    import types

    from app.core import config as config_module

    fake = types.SimpleNamespace(ocp=None)
    monkeypatch.setattr(config_module, "config", fake)
    from app.services.template_loader import _ocp_install_via_default

    assert _ocp_install_via_default() == "pod"
