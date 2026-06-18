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
    resolved["networks"] = tmpl.get("networks") or base_for_versions.get("networks", {})
    resolved["gateway"] = tmpl.get("gateway") or base_for_versions.get("gateway", {})

    # Fully-declarative templates define VMs inline
    if tmpl.get("vms"):
        resolved["vms"] = tmpl["vms"]

    # Pass through declarative config sections
    for section in (
        "ocp",
        "dns_records",
        "disconnected",
        "bastion_services",
    ):
        if tmpl.get(section):
            resolved[section] = tmpl[section]

    return resolved


def resolve_inline_template(template_yaml: str | dict) -> dict:
    """Resolve a template from inline YAML content (string or dict).

    Used when the template comes from an external source (e.g. agnosticv)
    rather than from a file in the templates directory.
    """
    if isinstance(template_yaml, str):
        tmpl = yaml.safe_load(template_yaml)
    else:
        tmpl = template_yaml

    if not isinstance(tmpl, dict):
        raise ValueError("Invalid template YAML")

    resolved = {}
    resolved["name"] = tmpl.get("name", "inline")
    resolved["display_name"] = tmpl.get("display_name", resolved["name"])
    resolved["description"] = tmpl.get("description", "")
    resolved["category"] = tmpl.get("category", "")
    resolved["install_method"] = tmpl.get("install_method", "agent")
    resolved["deploy_time"] = tmpl.get("deploy_time", "")
    resolved["bastion"] = tmpl.get("bastion", {})
    resolved["networks"] = tmpl.get("networks", {})
    resolved["gateway"] = tmpl.get("gateway", {})
    resolved["parameters"] = tmpl.get("parameters", {})

    if tmpl.get("vms"):
        resolved["vms"] = tmpl["vms"]

    for section in (
        "ocp",
        "dns_records",
        "disconnected",
        "bastion_services",
    ):
        if tmpl.get(section):
            resolved[section] = tmpl[section]

    return resolved


def list_yaml_templates(templates_dir: str = _DEFAULT_TEMPLATES_DIR) -> list[dict]:
    result = []
    templates_path = Path(templates_dir)
    for f in sorted(templates_path.glob("*.yaml")):
        tmpl = yaml.safe_load(f.read_text())
        if tmpl.get("extends") or tmpl.get("vms"):
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


def _generate_topology_from_vms(
    tmpl,
    bmc_password="password",  # pragma: allowlist secret
    external_access=False,
):
    """Generic YAML-driven topology generator.

    Reads the ``vms`` and ``networks`` sections from a fully-declarative
    template YAML and converts them to canvas JSONB (nodes + edges).
    """
    nodes = []
    edges = []
    external_ips = []

    vms_def = tmpl.get("vms", {})
    nets_def = tmpl.get("networks", {})
    gw_def = tmpl.get("gateway", {})

    VM_SPACING = 400
    GW_Y = 0
    NET_ROW_Y = 150
    VM_ROW_Y = 350

    # ── Networks ──
    net_ids = {}
    net_x = 150
    for net_name, net_cfg in nets_def.items():
        is_bmc = net_cfg.get("type") == "bmc"
        net_data = {
            "name": net_name,
            "label": net_name,
            "subtype": "network",
            "cidr": net_cfg.get("cidr", "10.0.0.0/24"),
            "dhcp": net_cfg.get("dhcp", False),
            "icon": "\U0001f310",
        }
        if is_bmc:
            net_data["networkType"] = "bmc"
            net_data["bmcUsername"] = "admin"
            net_data["bmcPassword"] = bmc_password
        net_node = {
            "id": _id(),
            "type": "networkNode",
            "position": {"x": net_x, "y": NET_ROW_Y},
            "data": net_data,
        }
        net_ids[net_name] = net_node["id"]
        nodes.append(net_node)
        net_x += VM_SPACING

    # ── Gateway ──
    gw_outbound = gw_def.get("outbound_ports", [])
    gw_node = {
        "id": _id(),
        "type": "networkNode",
        "position": {"x": 150, "y": GW_Y},
        "data": {
            "name": "gateway",
            "label": "gateway",
            "subtype": "gateway",
            "gatewayMode": "nat-portforward" if external_access else "nat",
            "portForwards": [],
            "outboundPolicy": "restrict" if gw_outbound else "allow-all",
            "outboundPorts": ",".join(str(p) for p in gw_outbound),
            "icon": "\U0001f310",
        },
    }
    nodes.append(gw_node)
    # Connect gateway to the first network
    first_net = list(nets_def.keys())[0] if nets_def else None
    if first_net and first_net in net_ids:
        edges.append(_gw_net_edge(gw_node["id"], net_ids[first_net]))

    # ── VMs ──
    vm_x = 150
    vm_index = 0
    for vm_name, vm_cfg in vms_def.items():
        role = vm_cfg.get("role", "")
        os_type = vm_cfg.get("os", "rhcos")
        power_on = vm_cfg.get("power_on", True)
        has_bmc = vm_cfg.get("bmc", role == "control-plane")
        bmc_ip = vm_cfg.get("bmc_ip", "")
        disks_cfg = vm_cfg.get("disks", [{"size_gb": 50}])
        nics_cfg = vm_cfg.get("nics", [])

        icon = "\U0001f5a5"
        if os_type == "blank":
            icon = "\U0001f4e6"

        # Build NICs
        nics = []
        for i, nic_cfg in enumerate(nics_cfg):
            mac_fn = (
                _bmc_mac
                if nets_def.get(nic_cfg.get("network", ""), {}).get("type") == "bmc"
                else _mac
            )
            nic = {
                "id": f"nic-{_id()}",
                "name": f"eth{i}",
                "mac": mac_fn(),
                "model": nic_cfg.get("model", "virtio"),
            }
            if nic_cfg.get("ip"):
                nic["ip"] = nic_cfg["ip"]
            nics.append(nic)

        # Build disk controllers and storage nodes
        disk_controllers = []
        disk_nodes = []
        disk_edges_list = []
        boot_device_ids = []
        for di, disk_cfg in enumerate(disks_cfg):
            dc = {"id": f"dp-{_id()}", "name": f"disk{di}", "bus": "virtio"}
            disk_controllers.append(dc)
            disk_id = _id()
            if di == 0:
                boot_device_ids.append(disk_id)
            disk_data = {
                "label": f"{vm_name}-disk{di}",
                "name": f"{vm_name}-disk{di}",
                "size": disk_cfg.get("size_gb", 50),
                "format": "qcow2",
                "icon": "\U0001f6e2",
            }
            if disk_cfg.get("library_item_id"):
                disk_data["libraryItemId"] = disk_cfg["library_item_id"]
            if disk_cfg.get("library_item_name"):
                disk_data["libraryItemName"] = disk_cfg["library_item_name"]
            disk_node = {
                "id": disk_id,
                "type": "storageNode",
                "position": {"x": vm_x - 190, "y": VM_ROW_Y + 70 + di * 100},
                "data": disk_data,
            }
            disk_edge = {
                "id": _id(),
                "source": disk_id,
                "target": "",  # filled after VM node is created
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
            disk_nodes.append(disk_node)
            disk_edges_list.append(disk_edge)

        # Add cdrom controller for non-blank VMs
        if os_type != "blank":
            dc_cdrom = {"id": f"dp-{_id()}", "name": "cdrom0", "bus": "sata"}
            disk_controllers.append(dc_cdrom)

        vm_data = {
            "label": vm_name,
            "name": vm_name,
            "vcpus": vm_cfg.get("vcpus", 2),
            "ram": vm_cfg.get("ram_gb", 4),
            "os": os_type,
            "icon": icon,
            "nics": nics,
            "diskControllers": disk_controllers,
            "bmcEnabled": has_bmc,
            "firmware": vm_cfg.get("firmware", "uefi"),
            "secureBoot": False,
            "bootDevices": boot_device_ids,
            "bootMethod": "disk",
            "powerOnAtDeploy": power_on,
        }
        if bmc_ip:
            vm_data["bmcIp"] = bmc_ip
        if vm_cfg.get("pxe_boot_iso_id"):
            vm_data["pxeBootIsoId"] = vm_cfg["pxe_boot_iso_id"]
        if vm_cfg.get("pxe_boot_iso_name"):
            vm_data["pxeBootIsoName"] = vm_cfg["pxe_boot_iso_name"]
        if role == "control-plane":
            vm_data["tags"] = {"AnsibleGroup": "controllers"}
        elif role == "bastion":
            vm_data["tags"] = {"AnsibleGroup": "bastions,showroom"}

        vm_node = {
            "id": _id(),
            "type": "vmNode",
            "position": {"x": vm_x, "y": VM_ROW_Y},
            "data": vm_data,
        }

        # Fix up disk edge targets
        for de in disk_edges_list:
            de["target"] = vm_node["id"]

        nodes.append(vm_node)
        nodes.extend(disk_nodes)
        edges.extend(disk_edges_list)

        # Connect NICs to networks
        for ni, nic_cfg in enumerate(nics_cfg):
            net_name = nic_cfg.get("network", "")
            if net_name in net_ids:
                handle = "top" if ni == 0 else "bottom"
                edges.append(_net_edge(net_ids[net_name], vm_node, ni, handle))

        vm_x += VM_SPACING
        vm_index += 1

    return {"nodes": nodes, "edges": edges, "externalIps": external_ips}


def export_topology_to_template(topology: dict) -> dict:
    """Reverse-map a canvas topology JSONB to a simple infra_template YAML dict."""
    nodes = topology.get("nodes", [])
    edges = topology.get("edges", [])

    # Index network nodes by id
    net_nodes = {n["id"]: n for n in nodes if n.get("type") == "networkNode"}
    vm_nodes = [n for n in nodes if n.get("type") == "vmNode"]

    # Build edge lookup: target node id -> list of source node ids
    edge_by_target = {}
    for e in edges:
        edge_by_target.setdefault(e["target"], []).append(e)

    # Map network node IDs to friendly names
    net_names = {}
    for nid, nn in net_nodes.items():
        d = nn.get("data", {})
        if d.get("subtype") == "gateway":
            continue
        net_names[nid] = d.get("name", d.get("label", nid[:8]))

    # Build NIC -> network mapping from edges
    nic_to_net = {}
    for e in edges:
        src = e.get("source", "")
        tgt_handle = e.get("targetHandle", "")
        if src in net_names and tgt_handle.startswith("nic-"):
            nic_id = tgt_handle.split("-")[1]
            nic_to_net[nic_id] = net_names[src]

    # ── Networks ──
    networks = {}
    for nid, nn in net_nodes.items():
        d = nn.get("data", {})
        if d.get("subtype") == "gateway":
            continue
        name = net_names[nid]
        net_out = {}
        if d.get("cidr"):
            net_out["cidr"] = d["cidr"]
        if d.get("dhcp"):
            net_out["dhcp"] = True
        if d.get("domain"):
            net_out["domain"] = d["domain"]
        if d.get("networkType") == "bmc":
            net_out["type"] = "bmc"
        networks[name] = net_out

    # ── Gateway ──
    gateway = {}
    for nn in net_nodes.values():
        d = nn.get("data", {})
        if d.get("subtype") == "gateway":
            ports_str = d.get("outboundPorts", "")
            if ports_str and d.get("outboundPolicy") == "restrict":
                ports = []
                for p in ports_str.split(","):
                    p = p.strip()
                    if p.isdigit():
                        ports.append(int(p))
                    elif p:
                        ports.append(p)
                if ports:
                    gateway["outbound_ports"] = ports
            break

    # ── VMs ──
    vms = {}
    for vm in vm_nodes:
        d = vm.get("data", {})
        name = d.get("name", d.get("label", vm["id"][:8]))

        vm_out = {}

        # Role from tags
        tags = d.get("tags", {})
        ag = tags.get("AnsibleGroup", "")
        if "bastions" in ag:
            vm_out["role"] = "bastion"
        elif "controllers" in ag:
            vm_out["role"] = "control-plane"
        elif d.get("os") == "blank":
            vm_out["role"] = "blank"

        vm_out["vcpus"] = d.get("vcpus", 2)
        vm_out["ram_gb"] = d.get("ram", 4)
        vm_out["os"] = d.get("os", "rhcos")
        vm_out["firmware"] = d.get("firmware", "uefi")

        if not d.get("powerOnAtDeploy", True):
            vm_out["power_on"] = False
        if d.get("bmcEnabled") and vm_out.get("role") != "control-plane":
            vm_out["bmc"] = True
        if d.get("bmcIp"):
            vm_out["bmc_ip"] = d["bmcIp"]

        # Disks — find storage nodes connected to this VM
        disk_controllers = d.get("diskControllers", [])
        disks = []
        vm_edges = edge_by_target.get(vm["id"], [])
        storage_ids = [
            e["source"] for e in vm_edges if e.get("targetHandle", "").startswith("dp-")
        ]
        storage_nodes = {n["id"]: n for n in nodes if n.get("type") == "storageNode"}
        # Maintain controller order
        for dc in disk_controllers:
            if dc.get("name", "").startswith("cdrom"):
                continue
            for sid in storage_ids:
                sn = storage_nodes.get(sid)
                if not sn:
                    continue
                # Match by edge targetHandle containing controller id
                for e in vm_edges:
                    if e["source"] == sid and dc["id"] in e.get("targetHandle", ""):
                        sd = sn.get("data", {})
                        disk_out = {"size_gb": sd.get("size", 50)}
                        if sd.get("libraryItemId"):
                            disk_out["library_item_id"] = sd["libraryItemId"]
                        if sd.get("libraryItemName"):
                            disk_out["library_item_name"] = sd["libraryItemName"]
                        disks.append(disk_out)
                        break
        if not disks:
            for dc in disk_controllers:
                if not dc.get("name", "").startswith("cdrom"):
                    disks.append({"size_gb": 50})
        vm_out["disks"] = disks

        if d.get("pxeBootIsoId"):
            vm_out["pxe_boot_iso_id"] = d["pxeBootIsoId"]
        if d.get("pxeBootIsoName"):
            vm_out["pxe_boot_iso_name"] = d["pxeBootIsoName"]

        # NICs
        nics_out = []
        for nic in d.get("nics", []):
            nic_out = {}
            net_name = nic_to_net.get(nic["id"], "")
            if net_name:
                nic_out["network"] = net_name
            model = nic.get("model", "virtio")
            nic_out["model"] = model
            if nic.get("ip"):
                nic_out["ip"] = nic["ip"]
            nics_out.append(nic_out)
        vm_out["nics"] = nics_out

        vms[name] = vm_out

    result = {"networks": networks}
    if gateway:
        result["gateway"] = gateway
    result["vms"] = vms
    return result


def generate_topology_from_template(
    resolved: dict,
    bmc_password: str = "password",
    external_access: bool = False,  # pragma: allowlist secret
) -> dict:
    # Fully-declarative templates with a `vms` section use the generic generator
    if resolved.get("vms"):
        topo = _generate_topology_from_vms(resolved, bmc_password, external_access)
        from app.services.auto_layout import auto_layout

        topo["nodes"], topo["edges"] = auto_layout(topo["nodes"], topo["edges"])
        return topo

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
