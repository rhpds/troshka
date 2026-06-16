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
    resolved["display_name"] = tmpl.get("display_name", tmpl["name"])
    resolved["description"] = tmpl.get("description", "")
    resolved["category"] = tmpl.get("category", "")
    resolved["install_method"] = tmpl.get("install_method", "agent")
    resolved["deploy_time"] = tmpl.get("deploy_time", "")
    resolved["bastion"] = base_for_versions.get("bastion", {})
    resolved["networks"] = base_for_versions.get("networks", {})
    resolved["gateway"] = base_for_versions.get("gateway", {})

    return resolved


def list_yaml_templates(templates_dir: str = _DEFAULT_TEMPLATES_DIR) -> list[dict]:
    result = []
    templates_path = Path(templates_dir)
    for f in sorted(templates_path.glob("*.yaml")):
        tmpl = yaml.safe_load(f.read_text())
        if tmpl.get("extends"):
            result.append(
                {
                    "id": tmpl["name"],
                    "name": tmpl.get("display_name", tmpl["name"]),
                    "description": tmpl.get("description", ""),
                    "category": tmpl.get("category", ""),
                    "install_method": tmpl.get("install_method", "agent"),
                    "deploy_time": tmpl.get("deploy_time", ""),
                }
            )
    return result


# ---------------------------------------------------------------------------
# Topology generation from resolved templates
# ---------------------------------------------------------------------------


def _id():
    return str(uuid.uuid4())


def _mac():
    return "52:54:00:%02x:%02x:%02x" % (
        random.randint(0, 255),
        random.randint(0, 255),
        random.randint(0, 255),
    )


def _bmc_mac():
    return "52:54:01:%02x:%02x:%02x" % (
        random.randint(0, 255),
        random.randint(0, 255),
        random.randint(0, 255),
    )


def _vm_node(name, vcpus, ram, x, y, disk_gb=50, bmc_ip="", cluster_ip="", tags=None):
    nic = {"id": f"nic-{_id()}", "name": "eth0", "mac": _mac(), "model": "virtio"}
    if cluster_ip:
        nic["ip"] = cluster_ip
    dc = {"id": f"dp-{_id()}", "name": "disk0", "bus": "virtio"}
    dc_cdrom = {"id": f"dp-{_id()}", "name": "cdrom0", "bus": "sata"}
    disk_id = _id()
    disk_node = {
        "id": disk_id,
        "type": "storageNode",
        "position": {"x": x - 190, "y": y + 70},
        "data": {
            "label": f"{name}-disk",
            "name": f"{name}-disk",
            "size": disk_gb,
            "format": "qcow2",
            "icon": "\U0001f6e2",
        },
    }
    vm_data = {
        "label": name,
        "name": name,
        "vcpus": vcpus,
        "ram": ram,
        "os": "rhcos",
        "icon": "\U0001f5a5",
        "nics": [nic],
        "diskControllers": [dc, dc_cdrom],
        "bmcEnabled": True,
        "firmware": "uefi",
        "secureBoot": False,
        "bootDevices": [disk_id],
        "bootMethod": "disk",
        "powerOnAtDeploy": True,
    }
    if bmc_ip:
        vm_data["bmcIp"] = bmc_ip
    if tags:
        vm_data["tags"] = tags
    vm_node = {
        "id": _id(),
        "type": "vmNode",
        "position": {"x": x, "y": y},
        "data": vm_data,
    }
    disk_edge = {
        "id": _id(),
        "source": disk_id,
        "target": vm_node["id"],
        "sourceHandle": "right",
        "targetHandle": f"dp-{dc['id']}-left",
        "type": "smoothstep",
        "style": {
            "stroke": "rgba(251,191,36,0.6)",
            "strokeWidth": 2,
            "strokeDasharray": "4 4",
        },
        "animated": False,
        "className": "edge-storage-pulse",
    }
    return vm_node, disk_node, disk_edge


def _bastion_node(x, y, bastion_cfg, cluster_ip="10.0.0.50"):
    nic_cluster = {
        "id": f"nic-{_id()}",
        "name": "eth0",
        "mac": _mac(),
        "model": "virtio",
        "ip": cluster_ip,
    }
    nic_bmc = {
        "id": f"nic-{_id()}",
        "name": "eth1",
        "mac": _bmc_mac(),
        "model": "virtio",
    }
    dc = {"id": f"dp-{_id()}", "name": "disk0", "bus": "virtio"}
    disk_id = _id()
    disk_node = {
        "id": disk_id,
        "type": "storageNode",
        "position": {"x": x - 190, "y": y + 70},
        "data": {
            "label": "bastion-disk",
            "name": "bastion-disk",
            "size": bastion_cfg.get("disk_gb", 20),
            "format": "qcow2",
            "icon": "\U0001f6e2",
        },
    }
    vm_node = {
        "id": _id(),
        "type": "vmNode",
        "position": {"x": x, "y": y},
        "data": {
            "label": "bastion",
            "name": "bastion",
            "vcpus": bastion_cfg.get("vcpus", 2),
            "ram": bastion_cfg.get("ram_gb", 4),
            "os": bastion_cfg.get("image", "rhel-10"),
            "icon": "\U0001f5a5",
            "nics": [nic_cluster, nic_bmc],
            "diskControllers": [dc],
            "firmware": "uefi",
            "secureBoot": False,
            "bootDevices": [disk_id],
            "bootMethod": "disk",
            "powerOnAtDeploy": True,
            "tags": {"AnsibleGroup": "bastions,showroom"},
        },
    }
    disk_edge = {
        "id": _id(),
        "source": disk_id,
        "target": vm_node["id"],
        "sourceHandle": "right",
        "targetHandle": f"dp-{dc['id']}-left",
        "type": "smoothstep",
        "style": {
            "stroke": "rgba(251,191,36,0.6)",
            "strokeWidth": 2,
            "strokeDasharray": "4 4",
        },
        "animated": False,
        "className": "edge-storage-pulse",
    }
    return vm_node, disk_node, disk_edge


def _net_edge(net_id, vm_node, nic_index=0, vm_handle="top"):
    nic = vm_node["data"]["nics"][nic_index]
    return {
        "id": _id(),
        "source": net_id,
        "target": vm_node["id"],
        "sourceHandle": "bottom" if vm_handle == "top" else "top",
        "targetHandle": f"nic-{nic['id']}-{vm_handle}",
        "type": "smoothstep",
        "style": {
            "stroke": "rgba(34,211,238,0.5)",
            "strokeWidth": 2,
            "strokeDasharray": "6 4",
        },
        "animated": True,
    }


def _gw_net_edge(gw_id, net_id):
    return {
        "id": _id(),
        "source": gw_id,
        "target": net_id,
        "sourceHandle": "left",
        "targetHandle": "left",
        "type": "smoothstep",
        "style": {
            "stroke": "rgba(251,146,60,0.5)",
            "strokeWidth": 2,
            "strokeDasharray": "6 4",
        },
        "animated": True,
    }


# Keep for backward compat with imports
_generate_mac = _mac


def generate_topology_from_template(
    resolved: dict,
    bmc_password: str = "password",
    external_access: bool = False,  # pragma: allowlist secret
) -> dict:
    nodes = []
    edges = []
    external_ips = []

    VM_SPACING = 400
    GW_Y = 0
    NET_ROW_Y = 150
    VM_ROW_Y = 350
    WORKER_ROW_Y = VM_ROW_Y + 370

    control_count = resolved.get("control_count", 3)
    worker_count = resolved.get("worker_count", 0)
    bastion_cfg = resolved.get("bastion", {})
    cluster_net = resolved["networks"].get("cluster", {})
    bmc_net = resolved["networks"].get("bmc", {})
    gateway_cfg = resolved.get("gateway", {})
    gateway_outbound_ports = gateway_cfg.get("outbound_ports", [])

    vm_x_start = 150
    bast_x = vm_x_start + control_count * VM_SPACING
    net_x = vm_x_start + int((control_count / 2) * VM_SPACING) - 120

    # Port forwards for external access
    ocp_port_forwards = []
    if external_access:
        eip_id = _id()
        external_ips = [{"id": eip_id, "label": "OCP"}]
        ocp_port_forwards = [
            {
                "extIpId": eip_id,
                "extPort": "22",
                "intIp": "10.0.0.50",
                "intPort": "22",
                "proto": "tcp",
            },
            {
                "extIpId": eip_id,
                "extPort": "6443",
                "intIp": "10.0.0.2",
                "intPort": "6443",
                "proto": "tcp",
            },
            {
                "extIpId": eip_id,
                "extPort": "443",
                "intIp": "10.0.0.3",
                "intPort": "443",
                "proto": "tcp",
            },
            {
                "extIpId": eip_id,
                "extPort": "80",
                "intIp": "10.0.0.3",
                "intPort": "80",
                "proto": "tcp",
            },
        ]

    # Network nodes
    net = {
        "id": _id(),
        "type": "networkNode",
        "position": {"x": net_x, "y": NET_ROW_Y},
        "data": {
            "name": "cluster-network",
            "label": "cluster-network",
            "subtype": "network",
            "cidr": cluster_net.get("cidr", "10.0.0.0/24"),
            "dhcp": cluster_net.get("dhcp", True),
            "icon": "\U0001f310",
        },
    }
    bmc = {
        "id": _id(),
        "type": "networkNode",
        "position": {"x": bast_x, "y": NET_ROW_Y},
        "data": {
            "name": "bmc",
            "label": "bmc",
            "subtype": "network",
            "cidr": bmc_net.get("cidr", "192.168.100.0/24"),
            "dhcp": False,
            "networkType": "bmc",
            "bmcUsername": "admin",
            "bmcPassword": bmc_password,
            "icon": "\U0001f310",
        },
    }
    gw = {
        "id": _id(),
        "type": "networkNode",
        "position": {"x": net_x, "y": GW_Y},
        "data": {
            "name": "gateway",
            "label": "gateway",
            "subtype": "gateway",
            "gatewayMode": "nat-portforward" if external_access else "nat",
            "portForwards": ocp_port_forwards,
            "outboundPolicy": "restrict" if gateway_outbound_ports else "allow-all",
            "outboundPorts": ",".join(str(p) for p in gateway_outbound_ports),
            "icon": "\U0001f310",
        },
    }
    nodes.extend([net, bmc, gw])
    edges.append(_gw_net_edge(gw["id"], net["id"]))

    # Bastion
    bast_vm, bast_disk, bast_disk_edge = _bastion_node(bast_x, VM_ROW_Y, bastion_cfg)
    nodes.extend([bast_vm, bast_disk])
    edges.extend(
        [
            bast_disk_edge,
            _net_edge(net["id"], bast_vm, 0),
            _net_edge(bmc["id"], bast_vm, 1, "bottom"),
        ]
    )

    # Control plane nodes
    for i in range(control_count):
        vm, disk, disk_edge = _vm_node(
            f"cp-{i}",
            resolved.get("control_vcpus", 4),
            resolved.get("control_ram_gb", 16),
            vm_x_start + i * VM_SPACING,
            VM_ROW_Y,
            disk_gb=resolved.get("control_disk_gb", 120),
            bmc_ip=f"192.168.100.{10 + i}",
            cluster_ip=f"10.0.0.{10 + i}",
            tags={"AnsibleGroup": "controllers"},
        )
        nodes.extend([vm, disk])
        edges.extend([disk_edge, _net_edge(net["id"], vm)])

    # Worker nodes
    for i in range(worker_count):
        vm, disk, disk_edge = _vm_node(
            f"worker-{i}",
            resolved.get("worker_vcpus", 4),
            resolved.get("worker_ram_gb", 16),
            vm_x_start + i * VM_SPACING,
            WORKER_ROW_Y,
            disk_gb=resolved.get("worker_disk_gb", 120),
            bmc_ip=f"192.168.100.{20 + i}",
            cluster_ip=f"10.0.0.{20 + i}",
            tags={"AnsibleGroup": "workers"},
        )
        nodes.extend([vm, disk])
        edges.extend([disk_edge, _net_edge(net["id"], vm)])

    return {"nodes": nodes, "edges": edges, "externalIps": external_ips}
