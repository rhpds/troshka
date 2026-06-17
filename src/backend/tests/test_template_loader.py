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
    # Default model should be virtio
    assert vm["data"]["nics"][0]["model"] == "virtio"

    # Manually set model to igb and verify it's preserved
    vm["data"]["nics"][0]["model"] = "igb"
    assert vm["data"]["nics"][0]["model"] == "igb"


def test_load_ran_5g_template():
    from app.services.template_loader import load_template

    tmpl = load_template("ocp-ran-5g", templates_dir=TEMPLATES_DIR)
    assert tmpl["name"] == "ocp-ran-5g"
    assert tmpl["category"] == "openshift"
    assert "vms" in tmpl
    assert "hub-cp-0" in tmpl["vms"]
    assert "sno-seed" in tmpl["vms"]


def test_resolve_ran_5g_has_vms():
    from app.services.template_loader import resolve_template

    resolved = resolve_template("ocp-ran-5g", overrides={}, templates_dir=TEMPLATES_DIR)
    assert resolved["install_method"] == "agent"
    assert "vms" in resolved
    assert len(resolved["vms"]) == 5
    assert resolved["networks"]["cluster"]["cidr"] == "192.168.125.0/24"


def test_generate_ran_topology_node_counts():
    from app.services.template_loader import (
        generate_topology_from_template,
        resolve_template,
    )

    resolved = resolve_template("ocp-ran-5g", templates_dir=TEMPLATES_DIR)
    topo = generate_topology_from_template(resolved)
    nodes = topo["nodes"]

    vm_nodes = [n for n in nodes if n["type"] == "vmNode"]
    net_nodes = [n for n in nodes if n["type"] == "networkNode"]
    storage_nodes = [n for n in nodes if n["type"] == "storageNode"]

    # SNO hub (1 CP) + bastion + 3 SNOs = 5 VMs
    assert len(vm_nodes) == 5
    # cluster + bmc + sriov + ptp + gateway = 5 networks
    assert len(net_nodes) == 5

    vm_names = [n["data"]["name"] for n in vm_nodes]
    assert "hub-cp-0" in vm_names
    assert "bastion" in vm_names
    assert "sno-seed" in vm_names
    assert "sno-abi" in vm_names
    assert "sno-ibi" in vm_names


def test_generate_ran_topology_sno_vms_are_blank():
    from app.services.template_loader import (
        generate_topology_from_template,
        resolve_template,
    )

    resolved = resolve_template("ocp-ran-5g", templates_dir=TEMPLATES_DIR)
    topo = generate_topology_from_template(resolved)

    sno_vms = [
        n
        for n in topo["nodes"]
        if n["type"] == "vmNode" and n["data"]["name"].startswith("sno-")
    ]
    for vm in sno_vms:
        assert vm["data"]["os"] == "blank"
        assert vm["data"]["powerOnAtDeploy"] is False
        assert vm["data"]["bmcEnabled"] is True
        assert vm["data"]["firmware"] == "uefi"


def test_generate_ran_topology_sno_igb_nics():
    from app.services.template_loader import (
        generate_topology_from_template,
        resolve_template,
    )

    resolved = resolve_template("ocp-ran-5g", templates_dir=TEMPLATES_DIR)
    topo = generate_topology_from_template(resolved)

    sno_vms = [
        n
        for n in topo["nodes"]
        if n["type"] == "vmNode" and n["data"]["name"].startswith("sno-")
    ]
    for vm in sno_vms:
        nics = vm["data"]["nics"]
        # 4 NICs: cluster (virtio), ptp (igb), sriov x2 (igb)
        assert len(nics) == 4
        assert nics[0]["model"] == "virtio"  # cluster
        assert nics[1]["model"] == "igb"  # ptp
        assert nics[2]["model"] == "igb"  # sriov 0
        assert nics[3]["model"] == "igb"  # sriov 1


def test_generate_ran_topology_hub_has_bmc_and_ptp_nics():
    from app.services.template_loader import (
        generate_topology_from_template,
        resolve_template,
    )

    resolved = resolve_template("ocp-ran-5g", templates_dir=TEMPLATES_DIR)
    topo = generate_topology_from_template(resolved)

    hub = next(
        n
        for n in topo["nodes"]
        if n["type"] == "vmNode" and n["data"]["name"] == "hub-cp-0"
    )
    nics = hub["data"]["nics"]
    # 3 NICs: cluster (virtio), bmc (virtio), ptp (igb)
    assert len(nics) == 3
    assert nics[0]["model"] == "virtio"
    assert nics[1]["model"] == "virtio"
    assert nics[2]["model"] == "igb"


def test_generate_ran_topology_network_cidrs():
    from app.services.template_loader import (
        generate_topology_from_template,
        resolve_template,
    )

    resolved = resolve_template("ocp-ran-5g", templates_dir=TEMPLATES_DIR)
    topo = generate_topology_from_template(resolved)

    nets = {
        n["data"]["name"]: n["data"].get("cidr", "")
        for n in topo["nodes"]
        if n["type"] == "networkNode"
    }
    assert nets["cluster"] == "192.168.125.0/24"
    assert nets["bmc"] == "192.168.50.0/24"
    assert nets["sriov"] == "192.168.100.0/24"
    assert nets["ptp"] == "192.168.200.0/24"


def test_ran_topology_dns_records():
    from app.services.template_loader import (
        generate_topology_from_template,
        resolve_template,
    )
    from app.services.ocp.agent_template import _setup_dns_records

    resolved = resolve_template("ocp-ran-5g", templates_dir=TEMPLATES_DIR)
    topo = generate_topology_from_template(resolved)

    # For RAN template, VIPs should be 192.168.125.10/11
    # For SNO hub (1 CP), DNS should point to the hub node IP, not VIPs
    hub_ip = next(
        n["data"]["nics"][0]["ip"]
        for n in topo["nodes"]
        if n["type"] == "vmNode" and n["data"]["name"] == "hub-cp-0"
    )

    _setup_dns_records(topo, "hub", "5g-deployment.lab", hub_ip, hub_ip)

    cluster_net = next(
        n
        for n in topo["nodes"]
        if n["type"] == "networkNode" and n["data"]["name"] == "cluster"
    )
    dns = cluster_net["data"].get("dnsRecords", [])
    dns_names = [r["name"] for r in dns]
    assert "api.hub.5g-deployment.lab" in dns_names
    assert "api-int.hub.5g-deployment.lab" in dns_names
    assert ".apps.hub.5g-deployment.lab" in dns_names
