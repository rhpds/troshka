import os

import pytest

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "templates")


def test_load_base_template():
    from app.services.template_loader import load_template

    tmpl = load_template("ocp-cluster", templates_dir=TEMPLATES_DIR)
    assert tmpl["name"] == "ocp-cluster"
    assert "parameters" in tmpl
    assert "control_count" in tmpl["parameters"]


def test_load_preset_template():
    from app.services.template_loader import load_template

    tmpl = load_template("ocp-compact", templates_dir=TEMPLATES_DIR)
    assert tmpl["name"] == "ocp-compact"
    assert tmpl["extends"] == "ocp-cluster"


def test_resolve_preset_parameters():
    from app.services.template_loader import resolve_template

    resolved = resolve_template(
        "ocp-compact", overrides={}, templates_dir=TEMPLATES_DIR
    )
    assert resolved["control_count"] == 3
    assert resolved["control_schedulable"] is True
    assert resolved["worker_count"] == 0
    assert "parameters" in resolved


def test_resolve_with_overrides():
    from app.services.template_loader import resolve_template

    resolved = resolve_template(
        "ocp-compact",
        overrides={"worker_count": 2, "control_ram_gb": 32},
        templates_dir=TEMPLATES_DIR,
    )
    assert resolved["worker_count"] == 2
    assert resolved["control_ram_gb"] == 32


def test_resolve_rejects_unknown_override():
    from app.services.template_loader import resolve_template

    with pytest.raises(ValueError, match="Unknown parameter"):
        resolve_template(
            "ocp-compact", overrides={"fake_param": 99}, templates_dir=TEMPLATES_DIR
        )


def test_resolve_rejects_below_minimum():
    from app.services.template_loader import resolve_template

    with pytest.raises(ValueError, match="below minimum"):
        resolve_template(
            "ocp-compact", overrides={"control_vcpus": 1}, templates_dir=TEMPLATES_DIR
        )


def test_validate_version():
    from app.services.template_loader import resolve_template

    resolved = resolve_template(
        "ocp-compact", overrides={}, version="4.16", templates_dir=TEMPLATES_DIR
    )
    assert resolved["version"] == "4.16"


def test_validate_version_rejects_invalid():
    from app.services.template_loader import resolve_template

    with pytest.raises(ValueError, match="not available"):
        resolve_template(
            "ocp-compact", overrides={}, version="3.11", templates_dir=TEMPLATES_DIR
        )


def test_load_nonexistent_template():
    from app.services.template_loader import load_template

    with pytest.raises(FileNotFoundError):
        load_template("nonexistent", templates_dir=TEMPLATES_DIR)


def test_vm_node_nic_model_preserved():
    """NIC model from topology should be preserved (not hardcoded to virtio)."""
    from app.services.template_loader import _vm_node

    vm, _disk, _edge = _vm_node("test-vm", 4, 16, 100, 100)
    assert vm["data"]["nics"][0]["model"] == "virtio"
    vm["data"]["nics"][0]["model"] = "igb"
    assert vm["data"]["nics"][0]["model"] == "igb"


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
