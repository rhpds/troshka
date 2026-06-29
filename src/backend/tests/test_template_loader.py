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

    # Find cp-0 VM node, then find storage nodes connected to it via edges
    cp0 = next(
        n
        for n in topo["nodes"]
        if n["type"] == "vmNode" and n["data"]["name"] == "cp-0"
    )
    cp0_storage_ids = {
        e["source"]
        for e in topo["edges"]
        if e["target"] == cp0["id"] and "dp-" in e.get("targetHandle", "")
    }
    storage_nodes = [
        n
        for n in topo["nodes"]
        if n["type"] == "storageNode" and n["id"] in cp0_storage_ids
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
    from app.services.ocp.agent_template import _setup_dns_records
    from app.services.template_loader import (
        generate_topology_from_template,
        resolve_template,
    )

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


def test_resolve_inline_clock_target():
    from app.services.template_loader import resolve_inline_template

    tmpl = {
        "name": "test-clock",
        "clock_target": "2025-01-15T00:00:00Z",
        "networks": {"net1": {"cidr": "192.168.1.0/24"}},
        "vms": {"vm1": {"vcpus": 2, "ram_gb": 4, "os": "rhel-9"}},
    }
    resolved = resolve_inline_template(tmpl)
    assert resolved["clock_target"] == "2025-01-15T00:00:00Z"


def test_resolve_inline_no_clock_target():
    from app.services.template_loader import resolve_inline_template

    tmpl = {
        "name": "test-no-clock",
        "networks": {"net1": {"cidr": "192.168.1.0/24"}},
        "vms": {"vm1": {"vcpus": 2, "ram_gb": 4, "os": "rhel-9"}},
    }
    resolved = resolve_inline_template(tmpl)
    assert resolved.get("clock_target") is None


def test_pod_import_creates_pod_node():
    from app.services.template_loader import (
        generate_topology_from_template,
        resolve_inline_template,
    )

    tmpl = {
        "template_name": "pod-test",
        "networks": {"cluster": {"cidr": "10.0.0.0/24"}},
        "vms": {},
        "containers": {
            "showroom": {
                "type": "pod",
                "nics": [{"network": "cluster", "ip": "10.0.0.100"}],
                "init_containers": [
                    {
                        "name": "git-cloner",
                        "image": "quay.io/rhpds/showroom-git-cloner:latest",
                        "env": {"GIT_REPO_URL": "https://example.com/repo"},
                    },
                ],
                "containers": [
                    {
                        "name": "nginx",
                        "image": "quay.io/rhpds/nginx:1.25",
                        "cpus": 1,
                        "memory_mb": 256,
                        "ports": [80],
                    },
                    {
                        "name": "wetty",
                        "image": "quay.io/rhpds/wetty:v2.7.6",
                        "cpus": 1,
                        "memory_mb": 512,
                        "ports": [3000],
                        "env": {"SSH_HOST": "10.0.0.50"},
                    },
                ],
            },
        },
    }
    resolved = resolve_inline_template(tmpl)
    topo = generate_topology_from_template(resolved)

    ctr_nodes = [n for n in topo["nodes"] if n.get("type") == "containerNode"]
    assert len(ctr_nodes) == 1
    pod = ctr_nodes[0]
    assert pod["data"]["isPod"] is True
    assert pod["data"]["icon"] == "🫛"
    assert len(pod["data"]["initContainers"]) == 1
    assert pod["data"]["initContainers"][0]["name"] == "git-cloner"
    assert pod["data"]["initContainers"][0]["envVars"] == [
        {"key": "GIT_REPO_URL", "value": "https://example.com/repo"}
    ]
    assert len(pod["data"]["podContainers"]) == 2
    assert pod["data"]["podContainers"][0]["name"] == "nginx"
    assert pod["data"]["podContainers"][0]["cpus"] == 1
    assert pod["data"]["podContainers"][0]["memory"] == 256
    assert pod["data"]["podContainers"][0]["ports"] == [
        {"containerPort": 80, "hostPort": None, "protocol": "tcp"}
    ]
    assert pod["data"]["podContainers"][1]["name"] == "wetty"
    assert pod["data"]["podContainers"][1]["envVars"] == [
        {"key": "SSH_HOST", "value": "10.0.0.50"}
    ]
    assert len(pod["data"]["nics"]) == 1
    assert pod["data"]["nics"][0]["ip"] == "10.0.0.100"


def test_pod_export_round_trip():
    from app.services.template_loader import (
        export_topology_to_template,
        generate_topology_from_template,
        resolve_inline_template,
    )

    tmpl = {
        "template_name": "pod-round-trip",
        "networks": {"cluster": {"cidr": "10.0.0.0/24"}},
        "vms": {},
        "containers": {
            "showroom": {
                "type": "pod",
                "nics": [{"network": "cluster", "ip": "10.0.0.100"}],
                "init_containers": [
                    {"name": "builder", "image": "quay.io/rhpds/antora:v1"},
                ],
                "containers": [
                    {
                        "name": "nginx",
                        "image": "nginx:1.25",
                        "ports": [80],
                        "cpus": 2,
                        "memory_mb": 1024,
                    },
                    {"name": "wetty", "image": "wetty:v2", "ports": [3000]},
                ],
            },
        },
    }
    resolved = resolve_inline_template(tmpl)
    topo = generate_topology_from_template(resolved)
    exported = export_topology_to_template(topo)

    assert "showroom" in exported["containers"]
    sr = exported["containers"]["showroom"]
    assert sr["type"] == "pod"
    assert len(sr["init_containers"]) == 1
    assert sr["init_containers"][0]["name"] == "builder"
    assert len(sr["containers"]) == 2
    assert sr["containers"][0]["name"] == "nginx"
    assert sr["containers"][0]["cpus"] == 2
    assert sr["containers"][0]["memory_mb"] == 1024
    assert sr["containers"][1]["name"] == "wetty"


def test_pod_with_shared_volumes_round_trip():
    from app.services.template_loader import (
        export_topology_to_template,
        generate_topology_from_template,
        resolve_inline_template,
    )

    tmpl = {
        "template_name": "pod-vol-test",
        "networks": {"cluster": {"cidr": "10.0.0.0/24"}},
        "vms": {},
        "containers": {
            "showroom": {
                "type": "pod",
                "nics": [{"network": "cluster", "ip": "10.0.0.100"}],
                "disks": [{"size_gb": 20}],
                "init_containers": [
                    {
                        "name": "builder",
                        "image": "antora:v1",
                        "env": {"OUTPUT_DIR": "/shared/html"},
                    },
                ],
                "containers": [
                    {
                        "name": "nginx",
                        "image": "nginx:1.25",
                        "ports": [80],
                    },
                ],
            },
        },
    }
    resolved = resolve_inline_template(tmpl)
    topo = generate_topology_from_template(resolved)

    ctr_nodes = [n for n in topo["nodes"] if n.get("type") == "containerNode"]
    assert len(ctr_nodes) == 1
    pod = ctr_nodes[0]
    assert pod["data"]["isPod"] is True
    assert len(pod["data"]["mounts"]) == 1

    storage_nodes = [n for n in topo["nodes"] if n.get("type") == "storageNode"]
    assert any(s["data"].get("size") == 20 for s in storage_nodes)

    pod_edges = [
        e
        for e in topo["edges"]
        if e.get("target") == pod["id"] and "mnt-" in e.get("targetHandle", "")
    ]
    assert len(pod_edges) == 1

    exported = export_topology_to_template(topo)
    assert exported["containers"]["showroom"]["type"] == "pod"
    assert exported["containers"]["showroom"]["init_containers"][0]["name"] == "builder"
    assert exported["containers"]["showroom"]["containers"][0]["name"] == "nginx"


def test_single_container_unchanged_after_pod_support():
    from app.services.template_loader import (
        export_topology_to_template,
        generate_topology_from_template,
        resolve_inline_template,
    )

    tmpl = {
        "template_name": "single-ctr-test",
        "networks": {"mgmt": {"cidr": "10.0.0.0/24"}},
        "vms": {},
        "containers": {
            "registry": {
                "image": "registry:2",
                "cpus": 2,
                "memory_mb": 1024,
                "nics": [{"network": "mgmt", "ip": "10.0.0.5"}],
                "ports": [{"container_port": 5000}],
            },
        },
    }
    resolved = resolve_inline_template(tmpl)
    topo = generate_topology_from_template(resolved)

    ctr_nodes = [n for n in topo["nodes"] if n.get("type") == "containerNode"]
    assert len(ctr_nodes) == 1
    ctr = ctr_nodes[0]
    assert ctr["data"].get("isPod") is not True
    assert ctr["data"]["image"] == "registry:2"
    assert ctr["data"]["cpus"] == 2

    exported = export_topology_to_template(topo)
    assert "registry" in exported["containers"]
    assert exported["containers"]["registry"]["image"] == "registry:2"
    assert "type" not in exported["containers"]["registry"]
