import os

import pytest

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "templates")


def test_load_sno_template():
    from app.services.template_loader import load_template

    tmpl = load_template("ocp-sno", templates_dir=TEMPLATES_DIR)
    assert tmpl["name"] == "ocp-sno"
    assert "vms" in tmpl
    assert "cp-0" in tmpl["vms"]
    assert "bastion" in tmpl["vms"]


def test_load_compact_template():
    from app.services.template_loader import load_template

    tmpl = load_template("ocp-compact", templates_dir=TEMPLATES_DIR)
    assert tmpl["name"] == "ocp-compact"
    assert "vms" in tmpl
    assert len([k for k in tmpl["vms"] if k.startswith("cp-")]) == 3


def test_load_standard_template():
    from app.services.template_loader import load_template

    tmpl = load_template("ocp-standard", templates_dir=TEMPLATES_DIR)
    assert tmpl["name"] == "ocp-standard"
    assert "vms" in tmpl
    assert len([k for k in tmpl["vms"] if k.startswith("cp-")]) == 3
    assert len([k for k in tmpl["vms"] if k.startswith("worker-")]) == 2


def test_resolve_sno_has_vms():
    from app.services.template_loader import resolve_template

    resolved = resolve_template("ocp-sno", overrides={}, templates_dir=TEMPLATES_DIR)
    assert resolved["install_method"] == "agent"
    assert "vms" in resolved
    assert "cp-0" in resolved["vms"]


def test_resolve_rejects_unknown_override():
    from app.services.template_loader import resolve_template

    with pytest.raises(ValueError, match="Unknown parameter"):
        resolve_template(
            "ocp-sno", overrides={"fake_param": 99}, templates_dir=TEMPLATES_DIR
        )


def test_sno_topology_has_dns_records():
    from app.services.template_loader import (
        generate_topology_from_template,
        resolve_template,
    )

    resolved = resolve_template("ocp-sno", templates_dir=TEMPLATES_DIR)
    topo = generate_topology_from_template(resolved)

    cluster_net = next(
        n
        for n in topo["nodes"]
        if n["type"] == "networkNode" and n["data"]["name"] == "cluster"
    )
    dns = cluster_net["data"].get("dnsRecords", [])
    dns_names = [r["name"] for r in dns]
    assert any("api." in n for n in dns_names)
    assert any(".apps." in n for n in dns_names)


def test_sno_topology_has_second_disk():
    from app.services.template_loader import (
        generate_topology_from_template,
        resolve_template,
    )

    resolved = resolve_template("ocp-sno", templates_dir=TEMPLATES_DIR)
    topo = generate_topology_from_template(resolved)

    storage_nodes = [
        n
        for n in topo["nodes"]
        if n["type"] == "storageNode" and "cp-0" in n["data"]["name"]
    ]
    assert len(storage_nodes) == 2
    sizes = sorted(n["data"]["size"] for n in storage_nodes)
    assert sizes == [120, 250]


def test_standard_topology_has_workers():
    from app.services.template_loader import (
        generate_topology_from_template,
        resolve_template,
    )

    resolved = resolve_template("ocp-standard", templates_dir=TEMPLATES_DIR)
    topo = generate_topology_from_template(resolved)

    vm_nodes = [n for n in topo["nodes"] if n["type"] == "vmNode"]
    vm_names = [n["data"]["name"] for n in vm_nodes]
    assert "worker-0" in vm_names
    assert "worker-1" in vm_names

    worker = next(n for n in vm_nodes if n["data"]["name"] == "worker-0")
    assert worker["data"]["tags"] == {"AnsibleGroup": "workers"}


def test_compact_topology_has_ansible_groups():
    from app.services.template_loader import (
        generate_topology_from_template,
        resolve_template,
    )

    resolved = resolve_template("ocp-compact", templates_dir=TEMPLATES_DIR)
    topo = generate_topology_from_template(resolved)

    vm_nodes = [n for n in topo["nodes"] if n["type"] == "vmNode"]
    bastion = next(n for n in vm_nodes if n["data"]["name"] == "bastion")
    cp0 = next(n for n in vm_nodes if n["data"]["name"] == "cp-0")

    assert bastion["data"]["tags"] == {"AnsibleGroup": "bastions,showroom"}
    assert cp0["data"]["tags"] == {"AnsibleGroup": "controllers"}


def test_sno_topology_gateway_outbound():
    from app.services.template_loader import (
        generate_topology_from_template,
        resolve_template,
    )

    resolved = resolve_template("ocp-sno", templates_dir=TEMPLATES_DIR)
    topo = generate_topology_from_template(resolved)

    gw = next(
        n
        for n in topo["nodes"]
        if n["type"] == "networkNode" and n["data"]["subtype"] == "gateway"
    )
    assert gw["data"]["outboundPolicy"] == "restrict"
    assert "53" in gw["data"]["outboundPorts"]
    assert "443" in gw["data"]["outboundPorts"]


# ── YAML-driven topology tests (using example.yaml) ──


def test_load_example_template():
    from app.services.template_loader import load_template

    tmpl = load_template("example", templates_dir=TEMPLATES_DIR)
    assert tmpl["name"] == "example"
    assert "vms" in tmpl
    assert "bastion" in tmpl["vms"]
    assert "cp-0" in tmpl["vms"]


def test_resolve_example_has_vms():
    from app.services.template_loader import resolve_template

    resolved = resolve_template("example", overrides={}, templates_dir=TEMPLATES_DIR)
    assert resolved["install_method"] == "agent"
    assert "vms" in resolved
    assert len(resolved["vms"]) == 2


def test_resolve_declarative_sections():
    """Templates can declare ocp, dns_records, and other sections that pass through."""
    from app.services.template_loader import resolve_template

    resolved = resolve_template("example", overrides={}, templates_dir=TEMPLATES_DIR)
    assert resolved["ocp"]["cluster_name"] == "test"
    assert resolved["ocp"]["base_domain"] == "example.local"
    assert len(resolved["dns_records"]) >= 1
    assert resolved["dns_records"][0]["target"] == "bastion"


def test_generate_topology_node_counts():
    from app.services.template_loader import (
        generate_topology_from_template,
        resolve_template,
    )

    resolved = resolve_template("example", templates_dir=TEMPLATES_DIR)
    topo = generate_topology_from_template(resolved)

    vm_nodes = [n for n in topo["nodes"] if n["type"] == "vmNode"]
    net_nodes = [n for n in topo["nodes"] if n["type"] == "networkNode"]

    assert len(vm_nodes) == 2
    assert len(net_nodes) == 3  # cluster + bmc + gateway

    vm_names = [n["data"]["name"] for n in vm_nodes]
    assert "bastion" in vm_names
    assert "cp-0" in vm_names


def test_generate_topology_vm_properties():
    from app.services.template_loader import (
        generate_topology_from_template,
        resolve_template,
    )

    resolved = resolve_template("example", templates_dir=TEMPLATES_DIR)
    topo = generate_topology_from_template(resolved)

    bastion = next(
        n
        for n in topo["nodes"]
        if n["type"] == "vmNode" and n["data"]["name"] == "bastion"
    )
    assert bastion["data"]["os"] == "rhel-10"
    assert bastion["data"]["vcpus"] == 2
    assert bastion["data"]["ram"] == 4
    assert bastion["data"]["powerOnAtDeploy"] is True

    cp = next(
        n
        for n in topo["nodes"]
        if n["type"] == "vmNode" and n["data"]["name"] == "cp-0"
    )
    assert cp["data"]["os"] == "rhcos"
    assert cp["data"]["bmcEnabled"] is True
    assert cp["data"]["bmcIp"] == "192.168.100.10"


def test_generate_topology_nic_models():
    from app.services.template_loader import (
        generate_topology_from_template,
        resolve_template,
    )

    resolved = resolve_template("example", templates_dir=TEMPLATES_DIR)
    topo = generate_topology_from_template(resolved)

    cp = next(
        n
        for n in topo["nodes"]
        if n["type"] == "vmNode" and n["data"]["name"] == "cp-0"
    )
    nics = cp["data"]["nics"]
    assert len(nics) == 2
    assert nics[0]["model"] == "virtio"
    assert nics[1]["model"] == "virtio"


def test_generate_topology_network_cidrs():
    from app.services.template_loader import (
        generate_topology_from_template,
        resolve_template,
    )

    resolved = resolve_template("example", templates_dir=TEMPLATES_DIR)
    topo = generate_topology_from_template(resolved)

    nets = {
        n["data"]["name"]: n["data"].get("cidr", "")
        for n in topo["nodes"]
        if n["type"] == "networkNode"
    }
    assert nets["cluster"] == "10.0.0.0/24"
    assert nets["bmc"] == "192.168.100.0/24"


def test_dns_records_from_template():
    from app.services.template_loader import (
        generate_topology_from_template,
        resolve_template,
    )
    from app.services.ocp.agent_template import _setup_dns_records

    resolved = resolve_template("example", templates_dir=TEMPLATES_DIR)
    topo = generate_topology_from_template(resolved)

    _setup_dns_records(
        topo, "test", "example.local", "10.0.0.10", "10.0.0.10", resolved
    )

    cluster_net = next(
        n
        for n in topo["nodes"]
        if n["type"] == "networkNode" and n["data"]["name"] == "cluster"
    )
    dns = cluster_net["data"].get("dnsRecords", [])
    dns_names = [r["name"] for r in dns]
    assert "api.test.example.local" in dns_names
    assert ".apps.test.example.local" in dns_names
    assert "infra.example.local" in dns_names


def test_export_import_round_trip():
    from app.services.template_loader import (
        export_topology_to_template,
        generate_topology_from_template,
        resolve_inline_template,
    )

    tmpl = {
        "name": "round-trip-test",
        "networks": {
            "mgmt": {"cidr": "10.0.0.0/24", "dhcp": True},
        },
        "gateway": {"outbound_ports": [53, 80, 443]},
        "vms": {
            "server": {
                "role": "bastion",
                "vcpus": 4,
                "ram_gb": 8,
                "os": "rhel-10",
                "firmware": "uefi",
                "disks": [{"size_gb": 100}],
                "nics": [{"network": "mgmt", "model": "virtio", "ip": "10.0.0.10"}],
            },
        },
    }
    resolved = resolve_inline_template(tmpl)
    topo = generate_topology_from_template(resolved)

    # Verify edges exist in generated topology
    net_edges = [e for e in topo["edges"] if "nic-" in e.get("targetHandle", "")]
    assert len(net_edges) > 0, "Generated topology should have network edges"

    # Export back to template YAML
    exported = export_topology_to_template(topo)
    assert "mgmt" in exported["networks"]
    assert "server" in exported["vms"]
    server = exported["vms"]["server"]
    assert server["nics"][0]["network"] == "mgmt"
    assert server["nics"][0]["ip"] == "10.0.0.10"

    # Re-import the exported template
    resolved2 = resolve_inline_template(exported)
    topo2 = generate_topology_from_template(resolved2)
    net_edges2 = [e for e in topo2["edges"] if "nic-" in e.get("targetHandle", "")]
    assert len(net_edges2) > 0, "Re-imported topology must have network edges"


def test_resolve_inline_template_pull_through_registry():
    from app.services.template_loader import resolve_inline_template

    tmpl = {
        "template_name": "test-ptr",
        "networks": {"cluster": {"cidr": "10.0.0.0/24"}},
        "vms": {
            "bastion": {
                "role": "bastion",
                "vcpus": 2,
                "ram_gb": 4,
                "os": "rhel9",
                "disks": [{"size_gb": 50}],
                "nics": [{"network": "cluster"}],
            }
        },
        "pull_through_registry": {
            "enabled": True,
            "url": "registry-quay-quay-enterprise.apps.example.com",
            "orgs": {
                "registry.redhat.io": "registry_redhat_io",
                "quay.io": "quay_io",
            },
        },
    }
    resolved = resolve_inline_template(tmpl)
    assert resolved["pull_through_registry"]["enabled"] is True
    assert (
        resolved["pull_through_registry"]["url"]
        == "registry-quay-quay-enterprise.apps.example.com"
    )
    assert (
        resolved["pull_through_registry"]["orgs"]["registry.redhat.io"]
        == "registry_redhat_io"
    )


def test_resolve_inline_template_no_pull_through_registry():
    from app.services.template_loader import resolve_inline_template

    tmpl = {
        "template_name": "test-no-ptr",
        "networks": {"cluster": {"cidr": "10.0.0.0/24"}},
        "vms": {
            "bastion": {
                "role": "bastion",
                "vcpus": 2,
                "ram_gb": 4,
                "os": "rhel9",
                "disks": [{"size_gb": 50}],
                "nics": [{"network": "cluster"}],
            }
        },
    }
    resolved = resolve_inline_template(tmpl)
    assert "pull_through_registry" not in resolved
