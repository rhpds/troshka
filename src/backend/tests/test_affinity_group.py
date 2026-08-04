from app.services.template_loader import (
    generate_topology_from_template,
    resolve_inline_template,
)


def test_affinity_group_import():
    template = {
        "networks": {
            "net1": {"cidr": "192.168.1.0/24", "dhcp": True},
        },
        "vms": {
            "hub": {
                "vcpus": 4,
                "ram_gb": 16,
                "nics": [{"network": "net1"}],
                "disks": [{"size_gb": 50}],
                "affinity_group": "control-plane",
            },
            "worker1": {
                "vcpus": 2,
                "ram_gb": 8,
                "nics": [{"network": "net1"}],
                "disks": [{"size_gb": 30}],
                "affinity_group": "workers",
            },
            "worker2": {
                "vcpus": 2,
                "ram_gb": 8,
                "nics": [{"network": "net1"}],
                "disks": [{"size_gb": 30}],
                "affinity_group": "workers",
            },
        },
    }

    resolved = resolve_inline_template(template)
    topo = generate_topology_from_template(resolved)

    vm_nodes = [n for n in topo["nodes"] if n["type"] == "vmNode"]
    hub = next(n for n in vm_nodes if n["data"]["name"] == "hub")
    w1 = next(n for n in vm_nodes if n["data"]["name"] == "worker1")
    w2 = next(n for n in vm_nodes if n["data"]["name"] == "worker2")

    assert hub["data"]["affinityGroup"] == "control-plane"
    assert w1["data"]["affinityGroup"] == "workers"
    assert w2["data"]["affinityGroup"] == "workers"
