"""Tests for the ocp-cclm multi-cluster KubeVirt template."""

import os

from app.services.template_loader import (
    generate_topology_from_template,
    load_template,
    resolve_inline_template,
)


def _load_cclm_topology():
    tdir = os.path.join(os.path.dirname(__file__), "..", "templates")
    raw = load_template("ocp-cclm", templates_dir=tdir)
    resolved = resolve_inline_template(raw)
    return generate_topology_from_template(resolved)


def test_ocp_cclm_two_clusters_and_placement():
    topo = _load_cclm_topology()

    assert topo["placement"]["requires_kubevirt"] is True
    cluster_ids = {c["id"] for c in topo["clusters"]}
    assert cluster_ids == {"source", "destination"}

    source = next(c for c in topo["clusters"] if c["id"] == "source")
    dest = next(c for c in topo["clusters"] if c["id"] == "destination")
    assert source["type"] == "sno"
    assert source["workers"] == 2
    assert source["ocpVersion"] == "4.22"
    assert dest["type"] == "sno"
    assert dest["workers"] == 0
    assert dest["ocpVersion"] == "4.22"


def test_ocp_cclm_networks():
    topo = _load_cclm_topology()

    net_nodes = [
        n
        for n in topo["nodes"]
        if n.get("type") == "networkNode" and n["data"].get("subtype") == "network"
    ]
    net_names = {n["data"]["name"] for n in net_nodes}
    assert net_names == {"source-cluster", "dest-cluster", "migration", "bmc"}

    migration = next(n for n in net_nodes if n["data"]["name"] == "migration")
    assert migration["data"]["dhcp"] is True
    assert migration["data"]["cidr"] == "172.16.100.0/24"


def test_ocp_cclm_explicit_vms_dual_nics_and_nested_virt():
    topo = _load_cclm_topology()

    rhcos_vms = [
        n
        for n in topo["nodes"]
        if n.get("type") == "vmNode" and n["data"].get("os") == "rhcos"
    ]
    assert len(rhcos_vms) == 4
    assert all(n["data"].get("nestedVirt") for n in rhcos_vms)

    source_cp = next(n for n in rhcos_vms if n["data"]["name"] == "source-cp-0")
    assert source_cp["data"]["clusterId"] == "source"
    nics = source_cp["data"]["nics"]
    assert len(nics) == 2
    assert nics[0]["ip"] == "10.1.0.10"
    assert nics[1]["ip"] == "172.16.100.10"

    dest_cp = next(n for n in rhcos_vms if n["data"]["name"] == "dest-cp-0")
    assert dest_cp["data"]["clusterId"] == "destination"
    dest_nics = dest_cp["data"]["nics"]
    assert dest_nics[0]["ip"] == "10.2.0.10"
    assert dest_nics[1]["ip"] == "172.16.100.110"


def test_nested_virt_auto_when_placement_requires_kubevirt():
    from app.services.template_loader import (
        generate_topology_from_template,
        resolve_inline_template,
    )

    tmpl = {
        "name": "t",
        "install_method": "agent",
        "category": "openshift",
        "placement": {"requires_kubevirt": True},
        "networks": {"cluster": {"cidr": "10.0.0.0/24"}},
        "ocp": [{"name": "ocp", "type": "sno"}],
    }
    topo = generate_topology_from_template(resolve_inline_template(tmpl))
    rhcos = [
        n
        for n in topo["nodes"]
        if n.get("type") == "vmNode" and n["data"].get("os") == "rhcos"
    ]
    assert rhcos
    assert all(n["data"].get("nestedVirt") for n in rhcos)
