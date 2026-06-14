import os
import random
import uuid
from pathlib import Path

import yaml

_DEFAULT_TEMPLATES_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "templates"
)


def load_template(name: str, templates_dir: str = _DEFAULT_TEMPLATES_DIR) -> dict:
    path = Path(templates_dir) / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Template '{name}' not found at {path}")
    with open(path) as f:
        return yaml.safe_load(f)


def resolve_template(
    name: str,
    overrides: dict | None = None,
    version: str | None = None,
    templates_dir: str = _DEFAULT_TEMPLATES_DIR,
) -> dict:
    tmpl = load_template(name, templates_dir)
    overrides = overrides or {}

    base_params = {}
    if tmpl.get("extends"):
        base = load_template(tmpl["extends"], templates_dir)
        base_params = base.get("parameters", {})
    else:
        base_params = tmpl.get("parameters", {})

    preset_defaults = tmpl.get("defaults", {})

    resolved = {}
    for param_name, param_def in base_params.items():
        if param_name in overrides:
            value = overrides[param_name]
        elif param_name in preset_defaults:
            value = preset_defaults[param_name]
        else:
            value = param_def["default"]
        resolved[param_name] = value

    unknown = set(overrides.keys()) - set(base_params.keys())
    if unknown:
        raise ValueError(f"Unknown parameter(s): {', '.join(sorted(unknown))}")

    for param_name, value in resolved.items():
        param_def = base_params[param_name]
        if "min" in param_def and isinstance(value, (int, float)):
            if value < param_def["min"]:
                raise ValueError(
                    f"Parameter '{param_name}' value {value} is below minimum {param_def['min']}"
                )

    base_for_versions = load_template(tmpl.get("extends", name), templates_dir)
    versions = base_for_versions.get("versions", [])
    if version is not None:
        if version not in versions:
            raise ValueError(f"Version '{version}' not available. Options: {versions}")
        resolved["version"] = version

    resolved["parameters"] = base_params
    resolved["name"] = tmpl["name"]
    resolved["description"] = tmpl.get("description", "")
    resolved["bastion"] = base_for_versions.get("bastion", {})
    resolved["networks"] = base_for_versions.get("networks", {})

    return resolved


def _generate_mac() -> str:
    return "52:54:00:%02x:%02x:%02x" % (
        random.randint(0, 255),
        random.randint(0, 255),
        random.randint(0, 255),
    )


def generate_topology_from_template(resolved: dict) -> dict:
    nodes = []
    edges = []

    x_spacing = 400
    cp_y = 350
    worker_y = 720
    bastion_x_offset = 150

    cluster_net_id = f"net-{uuid.uuid4()}"
    bmc_net_id = f"net-{uuid.uuid4()}"
    cluster_net = resolved["networks"].get("cluster", {})
    bmc_net = resolved["networks"].get("bmc", {})

    nodes.append(
        {
            "id": cluster_net_id,
            "type": "networkNode",
            "position": {"x": 600, "y": 100},
            "data": {
                "name": "cluster",
                "label": "cluster",
                "cidr": cluster_net.get("cidr", "10.0.0.0/24"),
                "dhcp": cluster_net.get("dhcp", True),
                "icon": "\U0001F310",
            },
        }
    )
    nodes.append(
        {
            "id": bmc_net_id,
            "type": "networkNode",
            "position": {"x": 600, "y": 900},
            "data": {
                "name": "bmc",
                "label": "bmc",
                "cidr": bmc_net.get("cidr", "192.168.100.0/24"),
                "dhcp": False,
                "networkType": "bmc",
                "icon": "\U0001F310",
            },
        }
    )

    control_count = resolved.get("control_count", 3)
    worker_count = resolved.get("worker_count", 0)
    bastion_cfg = resolved.get("bastion", {})

    # Bastion
    bastion_id = f"vm-{uuid.uuid4()}"
    nic_id = f"nic-{uuid.uuid4()}"
    dp_id = f"dp-{uuid.uuid4()}"
    total_cols = control_count + (1 if worker_count == 0 else 0)
    bastion_x = bastion_x_offset + total_cols * x_spacing

    nodes.append(
        {
            "id": bastion_id,
            "type": "vmNode",
            "position": {"x": bastion_x, "y": cp_y},
            "data": {
                "name": "bastion",
                "label": "bastion",
                "vcpus": bastion_cfg.get("vcpus", 2),
                "ram": bastion_cfg.get("ram_gb", 4),
                "os": bastion_cfg.get("image", "rhel-10"),
                "icon": "\U0001F5A5",
                "firmware": "uefi",
                "powerOnAtDeploy": True,
                "bootMethod": "disk",
                "nics": [
                    {
                        "id": nic_id,
                        "name": "eth0",
                        "mac": _generate_mac(),
                        "model": "virtio",
                    }
                ],
                "diskControllers": [{"id": dp_id, "name": "disk0", "bus": "virtio"}],
                "tags": {"AnsibleGroup": "bastions,showroom"},
            },
        }
    )
    edges.append(
        {
            "id": f"e-{uuid.uuid4()}",
            "source": bastion_id,
            "target": cluster_net_id,
            "sourceHandle": f"nic-{nic_id}-bottom",
            "targetHandle": f"port-{cluster_net_id}-top",
        }
    )

    # Control plane nodes
    for i in range(control_count):
        vm_id = f"vm-{uuid.uuid4()}"
        nic_cluster_id = f"nic-{uuid.uuid4()}"
        nic_bmc_id = f"nic-{uuid.uuid4()}"
        dp_id = f"dp-{uuid.uuid4()}"
        nodes.append(
            {
                "id": vm_id,
                "type": "vmNode",
                "position": {"x": bastion_x_offset + i * x_spacing, "y": cp_y},
                "data": {
                    "name": f"cp-{i}",
                    "label": f"cp-{i}",
                    "vcpus": resolved.get("control_vcpus", 4),
                    "ram": resolved.get("control_ram_gb", 16),
                    "os": "rhcos",
                    "icon": "\U0001F5A5",
                    "firmware": "uefi",
                    "powerOnAtDeploy": True,
                    "bootMethod": "disk",
                    "bmcEnabled": True,
                    "nics": [
                        {
                            "id": nic_cluster_id,
                            "name": "eth0",
                            "mac": _generate_mac(),
                            "model": "virtio",
                        },
                        {
                            "id": nic_bmc_id,
                            "name": "eth1",
                            "mac": _generate_mac(),
                            "model": "virtio",
                        },
                    ],
                    "diskControllers": [
                        {"id": dp_id, "name": "disk0", "bus": "virtio"}
                    ],
                    "tags": {"AnsibleGroup": "controllers"},
                },
            }
        )
        edges.append(
            {
                "id": f"e-{uuid.uuid4()}",
                "source": vm_id,
                "target": cluster_net_id,
                "sourceHandle": f"nic-{nic_cluster_id}-bottom",
                "targetHandle": f"port-{cluster_net_id}-top",
            }
        )
        edges.append(
            {
                "id": f"e-{uuid.uuid4()}",
                "source": vm_id,
                "target": bmc_net_id,
                "sourceHandle": f"nic-{nic_bmc_id}-bottom",
                "targetHandle": f"port-{bmc_net_id}-top",
            }
        )

    # Worker nodes
    for i in range(worker_count):
        vm_id = f"vm-{uuid.uuid4()}"
        nic_cluster_id = f"nic-{uuid.uuid4()}"
        nic_bmc_id = f"nic-{uuid.uuid4()}"
        dp_id = f"dp-{uuid.uuid4()}"
        nodes.append(
            {
                "id": vm_id,
                "type": "vmNode",
                "position": {"x": bastion_x_offset + i * x_spacing, "y": worker_y},
                "data": {
                    "name": f"worker-{i}",
                    "label": f"worker-{i}",
                    "vcpus": resolved.get("worker_vcpus", 4),
                    "ram": resolved.get("worker_ram_gb", 16),
                    "os": "rhcos",
                    "icon": "\U0001F5A5",
                    "firmware": "uefi",
                    "powerOnAtDeploy": True,
                    "bootMethod": "disk",
                    "bmcEnabled": True,
                    "nics": [
                        {
                            "id": nic_cluster_id,
                            "name": "eth0",
                            "mac": _generate_mac(),
                            "model": "virtio",
                        },
                        {
                            "id": nic_bmc_id,
                            "name": "eth1",
                            "mac": _generate_mac(),
                            "model": "virtio",
                        },
                    ],
                    "diskControllers": [
                        {"id": dp_id, "name": "disk0", "bus": "virtio"}
                    ],
                    "tags": {"AnsibleGroup": "workers"},
                },
            }
        )
        edges.append(
            {
                "id": f"e-{uuid.uuid4()}",
                "source": vm_id,
                "target": cluster_net_id,
                "sourceHandle": f"nic-{nic_cluster_id}-bottom",
                "targetHandle": f"port-{cluster_net_id}-top",
            }
        )
        edges.append(
            {
                "id": f"e-{uuid.uuid4()}",
                "source": vm_id,
                "target": bmc_net_id,
                "sourceHandle": f"nic-{nic_bmc_id}-bottom",
                "targetHandle": f"port-{bmc_net_id}-top",
            }
        )

    return {"nodes": nodes, "edges": edges}
