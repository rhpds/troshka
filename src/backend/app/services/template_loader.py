import os
import random
import uuid
from pathlib import Path

import yaml  # type: ignore[import-untyped]

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
        "pull_through_registry",
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
    resolved["name"] = tmpl.get("template_name", tmpl.get("name", "inline"))
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

    if tmpl.get("containers"):
        resolved["containers"] = tmpl["containers"]

    for section in (
        "ocp",
        "dns_records",
        "disconnected",
        "bastion_services",
        "start_order",
        "hidden_nodes",
        "pull_through_registry",
        "clock_target",
    ):
        if tmpl.get(section):
            resolved[section] = tmpl[section]

    return resolved


def list_yaml_templates(templates_dir: str = _DEFAULT_TEMPLATES_DIR) -> list[dict]:
    result = []
    templates_path = Path(templates_dir)
    for f in sorted(templates_path.glob("*.yaml")):
        try:
            tmpl = yaml.safe_load(f.read_text())
            if not isinstance(tmpl, dict):
                continue
            if not (tmpl.get("extends") or tmpl.get("vms")):
                continue
            bastion_image_name = ""
            for vm_cfg in (tmpl.get("vms") or {}).values():
                if vm_cfg.get("role") == "bastion":
                    for disk in vm_cfg.get("disks", []):
                        if disk.get("library_item_name"):
                            bastion_image_name = disk["library_item_name"]
                            break
                    break
            entry = {
                "id": tmpl.get("name", f.stem),
                "name": tmpl.get("display_name", tmpl.get("name", f.stem)),
                "description": tmpl.get("description", ""),
                "category": tmpl.get("category", ""),
                "install_method": tmpl.get("install_method", ""),
                "deploy_time": tmpl.get("deploy_time", ""),
            }
            if bastion_image_name:
                entry["bastion_image_name"] = bastion_image_name
            result.append(entry)
        except Exception as e:
            result.append(
                {
                    "id": f.stem,
                    "name": f"{f.stem} (error)",
                    "description": f"Failed to load: {e}",
                    "category": "error",
                    "install_method": "",
                    "deploy_time": "",
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
            "dhcp": net_cfg.get("dhcp", not is_bmc),
            "icon": "\U0001f310",
        }
        if net_cfg.get("domain"):
            net_data["dns"] = True
            net_data["dnsDomain"] = net_cfg["domain"]
        if net_cfg.get("dns_records"):
            net_data["dnsRecords"] = net_cfg["dns_records"]
        if net_cfg.get("dns_upstream"):
            net_data["dnsUpstream"] = True
        if is_bmc:
            net_data["networkType"] = "bmc"
            net_data["bmcUsername"] = net_cfg.get("bmc_username", "admin")
            net_data["bmcPassword"] = net_cfg.get("bmc_password", bmc_password)
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
    gw_external = external_access or gw_def.get("external_access", False)
    port_forwards = []
    if gw_external:
        eip_id = _id()
        external_ips = [{"id": eip_id, "name": "IP-1"}]

        # Custom port forwards from template gateway section
        for pf in gw_def.get("port_forwards", []):
            port_forwards.append(
                {
                    "extIpId": eip_id,
                    "extPort": str(pf["ext_port"]),
                    "intIp": pf["int_ip"],
                    "intPort": str(pf["int_port"]),
                    "proto": pf.get("proto", "tcp"),
                }
            )

        # Auto-generate OCP port forwards if no custom ones and OCP config exists
        if not port_forwards:
            ocp_cfg = tmpl.get("ocp", {})
            bastion_ip = ""
            for vm_name, vm_cfg in vms_def.items():
                if vm_cfg.get("role") == "bastion":
                    for nic_cfg in vm_cfg.get("nics", []):
                        if nic_cfg.get("ip"):
                            bastion_ip = nic_cfg["ip"]
                            break
                    break
            api_vip = ocp_cfg.get("api_vip", "")
            ingress_vip = ocp_cfg.get("ingress_vip", api_vip)
            if bastion_ip:
                port_forwards.append(
                    {
                        "extIpId": eip_id,
                        "extPort": "22",
                        "intIp": bastion_ip,
                        "intPort": "22",
                        "proto": "tcp",
                    }
                )
            if api_vip:
                port_forwards.append(
                    {
                        "extIpId": eip_id,
                        "extPort": "6443",
                        "intIp": api_vip,
                        "intPort": "6443",
                        "proto": "tcp",
                    }
                )
            if ingress_vip:
                port_forwards.append(
                    {
                        "extIpId": eip_id,
                        "extPort": "443",
                        "intIp": ingress_vip,
                        "intPort": "443",
                        "proto": "tcp",
                    }
                )
                port_forwards.append(
                    {
                        "extIpId": eip_id,
                        "extPort": "80",
                        "intIp": ingress_vip,
                        "intPort": "80",
                        "proto": "tcp",
                    }
                )
    gw_node = {
        "id": _id(),
        "type": "networkNode",
        "position": {"x": 150, "y": GW_Y},
        "data": {
            "name": "gateway",
            "label": "gateway",
            "subtype": "gateway",
            "gatewayMode": "nat-portforward" if gw_external else "nat",
            "portForwards": port_forwards,
            "outboundPolicy": "restrict" if gw_outbound else "allow-all",
            "outboundPorts": ",".join(str(p) for p in gw_outbound),
            "icon": "\U0001f310",
        },
    }
    nodes.append(gw_node)
    # Connect gateway to the specified network, or first non-BMC network
    gw_net_name = gw_def.get("network")
    if not gw_net_name:
        for nn, nc in nets_def.items():
            if nc.get("type") != "bmc":
                gw_net_name = nn
                break
    if gw_net_name and gw_net_name in net_ids:
        edges.append(_gw_net_edge(gw_node["id"], net_ids[gw_net_name]))

    # ── VMs ──
    vm_name_to_id = {}
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
                "label": disk_cfg.get("name", f"disk-{di:02d}"),
                "name": disk_cfg.get("name", f"disk-{di:02d}"),
                "size": disk_cfg.get("size_gb", 50),
                "format": "qcow2",
                "icon": "\U0001f6e2",
            }
            if disk_cfg.get("library_item_id"):
                disk_data["libraryItemId"] = disk_cfg["library_item_id"]
                disk_data["source"] = "library"
            if disk_cfg.get("library_item_name"):
                disk_data["libraryItemName"] = disk_cfg["library_item_name"]
            if disk_cfg.get("ocp_mount"):
                disk_data["ocpMount"] = disk_cfg["ocp_mount"]
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
            "secureBoot": vm_cfg.get("secure_boot", False),
            "bootDevices": boot_device_ids,
            "bootMethod": "disk",
            "powerOnAtDeploy": power_on,
        }
        if vm_cfg.get("uuid"):
            try:
                uuid.UUID(vm_cfg["uuid"])
            except ValueError:
                raise ValueError(
                    f"VM '{vm_name}': invalid uuid '{vm_cfg['uuid']}' — must be UUID format"
                )
            vm_data["uuid"] = vm_cfg["uuid"]
        if bmc_ip:
            vm_data["bmcIp"] = bmc_ip
        if vm_cfg.get("pxe_boot_iso_id"):
            vm_data["pxeBootIsoId"] = vm_cfg["pxe_boot_iso_id"]
        if vm_cfg.get("pxe_boot_iso_name"):
            vm_data["pxeBootIsoName"] = vm_cfg["pxe_boot_iso_name"]

        # Tags: use explicit tags if provided, otherwise derive from role
        if vm_cfg.get("tags"):
            vm_data["tags"] = vm_cfg["tags"]
        elif role == "control-plane":
            vm_data["tags"] = {"AnsibleGroup": "controllers"}
        elif role == "worker":
            vm_data["tags"] = {"AnsibleGroup": "workers"}
        elif role == "bastion":
            vm_data["tags"] = {"AnsibleGroup": "bastions,showroom"}

        # Cloud-init
        if vm_cfg.get("cloud_init"):
            vm_data["cloudInit"] = True
        if vm_cfg.get("cloud_user_password"):
            vm_data["ciCloudUserPassword"] = vm_cfg["cloud_user_password"]
        if vm_cfg.get("user_data"):
            vm_data["ciUserData"] = vm_cfg["user_data"]
        if vm_cfg.get("packages"):
            vm_data["ciPackages"] = vm_cfg["packages"]
        if vm_cfg.get("network_config"):
            vm_data["ciNetworkConfig"] = vm_cfg["network_config"]

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

        # Create ISO storage nodes from template isos field
        isos_cfg = vm_cfg.get("isos", [])
        cdrom_dc = next(
            (dc for dc in disk_controllers if dc.get("name", "").startswith("cdrom")),
            None,
        )
        for iso_cfg in isos_cfg:
            if not cdrom_dc:
                break
            iso_id = _id()
            iso_node_name = iso_cfg.get("name", f"{vm_name}-iso")
            iso_data = {
                "label": iso_node_name,
                "name": iso_node_name,
                "size": 10,
                "format": "iso",
                "source": "library",
                "icon": "\U0001f4bf",
            }
            if iso_cfg.get("library_item_id"):
                iso_data["libraryItemId"] = iso_cfg["library_item_id"]
            if iso_cfg.get("library_item_name"):
                iso_data["libraryItemName"] = iso_cfg["library_item_name"]
            iso_node = {
                "id": iso_id,
                "type": "storageNode",
                "position": {
                    "x": vm_x - 190,
                    "y": VM_ROW_Y + 70 + len(disks_cfg) * 100,
                },
                "data": iso_data,
            }
            iso_edge = {
                "id": _id(),
                "source": iso_id,
                "target": vm_node["id"],
                "sourceHandle": "right",
                "targetHandle": f"dp-{cdrom_dc['id']}-left",
                "type": "smoothstep",
                "style": {
                    "stroke": "rgba(251,191,36,0.6)",
                    "strokeWidth": 2,
                    "strokeDasharray": "4 4",
                },
                "animated": False,
                "className": "edge-storage-pulse",
            }
            nodes.append(iso_node)
            edges.append(iso_edge)

        # Connect NICs to networks
        for ni, nic_cfg in enumerate(nics_cfg):
            net_name = nic_cfg.get("network", "")
            if net_name in net_ids:
                handle = "top" if ni == 0 else "bottom"
                edges.append(_net_edge(net_ids[net_name], vm_node, ni, handle))

        vm_name_to_id[vm_name] = vm_node["id"]
        vm_x += VM_SPACING
        vm_index += 1

    # ── Containers ──
    container_name_to_id = {}
    containers_def = tmpl.get("containers", {})
    for ctr_key, ctr_cfg in containers_def.items():
        ctr_id = _id()
        is_pod = ctr_cfg.get("type") == "pod"

        ctr_nics = []
        for i, nic_cfg in enumerate(ctr_cfg.get("nics", [])):
            nic_id = f"nic-{_id()}"
            mac = _mac()
            if nic_cfg.get("mac"):
                mac = nic_cfg["mac"]
            ctr_nics.append(
                {
                    "id": nic_id,
                    "name": f"eth{i}",
                    "mac": mac,
                    "model": nic_cfg.get("model", "virtio"),
                    "ip": nic_cfg.get("ip", ""),
                }
            )

            # Create edge from container NIC to network node
            net_name = nic_cfg.get("network", "")
            net_node_id = net_ids.get(net_name)
            if net_node_id:
                edges.append(
                    {
                        "id": _id(),
                        "source": ctr_id,
                        "target": net_node_id,
                        "sourceHandle": f"{nic_id}-bottom",
                        "targetHandle": "top",
                        "type": "smoothstep",
                        "style": {"stroke": "rgba(96,165,250,0.5)", "strokeWidth": 2},
                    }
                )

        disk_name_to_id = {}
        ctr_mounts = []
        for disk_idx, disk_cfg in enumerate(ctr_cfg.get("disks", [])):
            disk_id = _id()
            disk_name = f"{ctr_key}-vol{disk_idx}"
            disk_node = {
                "id": disk_id,
                "type": "storageNode",
                "position": {"x": vm_x - 190, "y": VM_ROW_Y + 70 + disk_idx * 100},
                "data": {
                    "label": disk_name,
                    "name": disk_name,
                    "size": disk_cfg.get("size_gb", 10),
                    "format": "raw",
                    "icon": "\U0001f6e2",
                },
            }
            nodes.append(disk_node)
            disk_name_to_id[disk_name] = disk_id
            ctr_mounts.append(
                {
                    "diskNodeId": disk_id,
                    "mountPath": disk_cfg.get("mount_path", ""),
                }
            )
            edges.append(
                {
                    "id": _id(),
                    "source": disk_id,
                    "target": ctr_id,
                    "sourceHandle": "right",
                    "targetHandle": f"mnt-{disk_id}-left",
                    "type": "smoothstep",
                    "style": {
                        "stroke": "rgba(251,191,36,0.6)",
                        "strokeWidth": 2,
                        "strokeDasharray": "4 4",
                    },
                }
            )

        def _parse_sub_container(sc_cfg, default_name="ctr"):
            sc_env = [
                {"key": k, "value": str(v)}
                for k, v in (sc_cfg.get("env") or {}).items()
            ]
            sc_ports = []
            for p in sc_cfg.get("ports", []):
                if isinstance(p, int):
                    sc_ports.append(
                        {"containerPort": p, "hostPort": None, "protocol": "tcp"}
                    )
                else:
                    sc_ports.append(
                        {
                            "containerPort": p.get("container_port", 0),
                            "hostPort": p.get("host_port"),
                            "protocol": p.get("protocol", "tcp"),
                        }
                    )
            sc_mounts = []
            for m in sc_cfg.get("mounts", []):
                disk_name = m.get("disk", "")
                sc_mounts.append(
                    {
                        "diskNodeId": disk_name_to_id.get(disk_name, ""),
                        "mountPath": m.get("mount_path", ""),
                    }
                )
            return {
                "name": sc_cfg.get("name", default_name),
                "image": sc_cfg.get("image", ""),
                "registryCredentialId": None,
                "cpus": sc_cfg.get("cpus", 1),
                "memory": sc_cfg.get("memory_mb", 512),
                "envVars": sc_env,
                "ports": sc_ports,
                "command": sc_cfg.get("command"),
                "mounts": sc_mounts,
            }

        env_vars = []
        for k, v in (ctr_cfg.get("env") or {}).items():
            env_vars.append({"key": k, "value": str(v)})

        ports = []
        for p in ctr_cfg.get("ports", []):
            if isinstance(p, int):
                ports.append({"containerPort": p, "hostPort": None, "protocol": "tcp"})
            else:
                ports.append(
                    {
                        "containerPort": p.get("container_port", 0),
                        "hostPort": p.get("host_port"),
                        "protocol": p.get("protocol", "tcp"),
                    }
                )

        ctr_node = {
            "id": ctr_id,
            "type": "containerNode",
            "position": {"x": vm_x, "y": VM_ROW_Y},
            "data": {
                "label": ctr_key,
                "name": ctr_key,
                "image": ctr_cfg.get("image", ""),
                "registryCredentialId": None,
                "registryCredentialName": ctr_cfg.get("registry_credential"),
                "cpus": ctr_cfg.get("cpus", 1),
                "memory": ctr_cfg.get("memory_mb", 512),
                "nics": ctr_nics,
                "envVars": env_vars,
                "ports": ports,
                "command": ctr_cfg.get("command"),
                "restartPolicy": ctr_cfg.get("restart_policy", "always"),
                "privileged": ctr_cfg.get("privileged", False),
                "mounts": ctr_mounts,
                "status": "stopped",
                "icon": "\U0001f4e6",
            },
        }

        if is_pod:
            ctr_node["data"]["isPod"] = True
            ctr_node["data"]["icon"] = "\U0001fadb"

            init_ctrs = []
            for ic_cfg in ctr_cfg.get("init_containers", []):
                init_ctrs.append(_parse_sub_container(ic_cfg, f"init-{len(init_ctrs)}"))
            ctr_node["data"]["initContainers"] = init_ctrs

            pod_ctrs = []
            for pc_cfg in ctr_cfg.get("containers", []):
                pod_ctrs.append(_parse_sub_container(pc_cfg, f"ctr-{len(pod_ctrs)}"))
            ctr_node["data"]["podContainers"] = pod_ctrs

        nodes.append(ctr_node)
        container_name_to_id[ctr_key] = ctr_id
        vm_x += VM_SPACING

    # Build startOrder from template
    start_order = []
    for entry in tmpl.get("start_order", []):
        if "container" in entry:
            ctr_name = entry["container"]
            ctr_id = container_name_to_id.get(ctr_name, "")
            if ctr_id:
                so = {
                    "vmId": ctr_id,
                    "containerId": ctr_id,
                    "entryType": "container",
                    "autoStart": True,
                    "waitForVm": None,
                    "waitForService": "none",
                    "waitForPort": "",
                    "delaySeconds": entry.get("delay", 0),
                }
                start_order.append(so)
        elif "vm" in entry:
            vm_id = vm_name_to_id.get(entry.get("vm", ""), "")
            if not vm_id:
                continue
            so = {"vmId": vm_id, "autoStart": entry.get("auto_start", True)}
            wait_name = entry.get("wait_for", "")
            if wait_name and wait_name in vm_name_to_id:
                so["waitForVm"] = vm_name_to_id[wait_name]
            if entry.get("delay"):
                so["delay"] = entry["delay"]
            start_order.append(so)

    # Apply top-level dns_records (with target resolution) + auto-generate OCP records
    top_dns = list(tmpl.get("dns_records", []))
    ocp_cfg = tmpl.get("ocp", {})
    if ocp_cfg.get("cluster_name") and ocp_cfg.get("base_domain"):
        cn = ocp_cfg["cluster_name"]
        bd = ocp_cfg["base_domain"]
        api_vip = ocp_cfg.get("api_vip", "")
        ingress_vip = ocp_cfg.get("ingress_vip", api_vip)
        if api_vip:
            for rec_name in [f"api.{cn}.{bd}", f"api-int.{cn}.{bd}"]:
                if not any(r.get("name") == rec_name for r in top_dns):
                    top_dns.append({"name": rec_name, "ip": api_vip})
            apps_name = f".apps.{cn}.{bd}"
            if not any(r.get("name") == apps_name for r in top_dns):
                top_dns.append({"name": apps_name, "ip": ingress_vip})

    if top_dns:
        vm_ips = {}
        for n in nodes:
            if n.get("type") == "vmNode":
                nics = n.get("data", {}).get("nics", [])
                if nics:
                    vm_ips[n["data"].get("name", "")] = nics[0].get("ip", "")
        for net_node in nodes:
            if (
                net_node.get("type") == "networkNode"
                and net_node.get("data", {}).get("subtype") == "network"
                and net_node.get("data", {}).get("networkType") != "bmc"
            ):
                existing = net_node["data"].get("dnsRecords", [])
                existing_names = {r["name"] for r in existing}
                for rec in top_dns:
                    target = rec.get("target", "")
                    ip = rec.get("ip", "")
                    if target and not ip:
                        ip = vm_ips.get(target, "")
                    if ip and rec.get("name") and rec["name"] not in existing_names:
                        existing.append({"name": rec["name"], "ip": ip})
                if existing:
                    net_node["data"]["dnsRecords"] = existing
                break

    # Validate UUID uniqueness
    seen_uuids = {}
    for n in nodes:
        if n.get("type") == "vmNode" and n.get("data", {}).get("uuid"):
            u = n["data"]["uuid"]
            if u in seen_uuids:
                raise ValueError(
                    f"Duplicate uuid '{u}' on VMs '{seen_uuids[u]}' and '{n['data'].get('name')}'"
                )
            seen_uuids[u] = n["data"].get("name", "")

    # Build hiddenNodeIds from template
    hidden_ids = []
    all_name_to_id = {**vm_name_to_id, **container_name_to_id, **net_ids}
    for name in tmpl.get("hidden_nodes", []):
        nid = all_name_to_id.get(name)
        if nid:
            hidden_ids.append(nid)

    return {
        "nodes": nodes,
        "edges": edges,
        "externalIps": external_ips,
        "startOrder": start_order,
        "hiddenNodeIds": hidden_ids,
    }


def export_topology_to_template(topology: dict, db=None) -> dict:
    """Reverse-map a canvas topology JSONB to a simple infra_template YAML dict."""
    nodes = topology.get("nodes", [])
    edges = topology.get("edges", [])

    # Build set of library item IDs that are actually ISOs (check DB)
    _iso_item_ids: set[str] = set()
    if db:
        from app.models.library import LibraryItem

        lib_ids = []
        for n in nodes:
            if n.get("type") == "storageNode":
                lid = n.get("data", {}).get("libraryItemId")
                if lid:
                    lib_ids.append(lid)
        if lib_ids:
            items = (
                db.query(LibraryItem.id, LibraryItem.format)
                .filter(LibraryItem.id.in_(lib_ids))
                .all()
            )
            _iso_item_ids = {i.id for i in items if i.format == "iso"}

    # Index network nodes by id
    net_nodes = {n["id"]: n for n in nodes if n.get("type") == "networkNode"}
    vm_nodes = [n for n in nodes if n.get("type") == "vmNode"]

    # Build edge lookup: target node id -> list of source node ids
    edge_by_target: dict[str, list] = {}
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
    # targetHandle format: "nic-{nic_id}-{top|bottom}" where nic_id itself is "nic-{uuid}"
    nic_to_net = {}
    for e in edges:
        src = e.get("source", "")
        tgt_handle = e.get("targetHandle", "")
        if src in net_names and tgt_handle.startswith("nic-"):
            # Strip "nic-" prefix and "-top"/"-bottom" suffix to get the nic ID
            nic_id = tgt_handle.removeprefix("nic-")
            for suffix in ("-top", "-bottom"):
                if nic_id.endswith(suffix):
                    nic_id = nic_id.removesuffix(suffix)
                    break
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
        if d.get("dnsDomain"):
            net_out["domain"] = d["dnsDomain"]
        if d.get("dnsRecords"):
            net_out["dns_records"] = d["dnsRecords"]
        if d.get("dnsUpstream"):
            net_out["dns_upstream"] = True
        if d.get("networkType") == "bmc":
            net_out["type"] = "bmc"
            if d.get("bmcUsername"):
                net_out["bmc_username"] = d["bmcUsername"]
            if d.get("bmcPassword"):
                net_out["bmc_password"] = d["bmcPassword"]
        networks[name] = net_out

    # ── Gateway ──
    gateway: dict[str, object] = {}
    for nn in net_nodes.values():
        d = nn.get("data", {})
        if d.get("subtype") == "gateway":
            ports_str = d.get("outboundPorts", "")
            if ports_str and d.get("outboundPolicy") == "restrict":
                ports: list[int | str] = []
                for p in ports_str.split(","):
                    p = p.strip()
                    if p.isdigit():
                        ports.append(int(p))
                    elif p:
                        ports.append(p)
                if ports:
                    gateway["outbound_ports"] = ports
            if d.get("gatewayMode") == "nat-portforward":
                gateway["external_access"] = True
                pfs = d.get("portForwards", [])
                if pfs:
                    gateway["port_forwards"] = [
                        {
                            "ext_port": int(pf.get("extPort", 0)),
                            "int_ip": pf.get("intIp", ""),
                            "int_port": int(pf.get("intPort", 0)),
                            "proto": pf.get("proto", "tcp"),
                        }
                        for pf in pfs
                        if pf.get("extPort") and pf.get("intIp")
                    ]
            # Find which network the gateway connects to
            gw_id = nn["id"]
            for e in edges:
                if e.get("source") == gw_id and e.get("target") in net_names:
                    gateway["network"] = net_names[e["target"]]
                    break
                if e.get("target") == gw_id and e.get("source") in net_names:
                    gateway["network"] = net_names[e["source"]]
                    break
            break

    # ── VMs ──
    vms = {}
    for vm in vm_nodes:
        d = vm.get("data", {})
        name = d.get("name", d.get("label", vm["id"][:8]))

        vm_out: dict[str, object] = {}

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

        if d.get("secureBoot"):
            vm_out["secure_boot"] = True
        if not d.get("powerOnAtDeploy", True):
            vm_out["power_on"] = False
        if d.get("bmcEnabled") and vm_out.get("role") != "control-plane":
            vm_out["bmc"] = True
        if d.get("bmcIp"):
            vm_out["bmc_ip"] = d["bmcIp"]

        if (
            tags
            and tags != {"AnsibleGroup": "controllers"}
            and tags != {"AnsibleGroup": "bastions,showroom"}
        ):
            vm_out["tags"] = tags

        # Cloud-init
        if d.get("cloudInit"):
            vm_out["cloud_init"] = True
        if d.get("ciCloudUserPassword"):
            vm_out["cloud_user_password"] = d["ciCloudUserPassword"]
        if d.get("ciUserData"):
            vm_out["user_data"] = d["ciUserData"]
        if d.get("ciPackages"):
            vm_out["packages"] = d["ciPackages"]
        if d.get("ciNetworkConfig"):
            vm_out["network_config"] = d["ciNetworkConfig"]

        # Disks — find storage nodes connected to this VM
        disk_controllers = d.get("diskControllers", [])
        disks = []
        vm_edges = edge_by_target.get(vm["id"], [])
        storage_ids = [
            e["source"] for e in vm_edges if e.get("targetHandle", "").startswith("dp-")
        ]
        storage_nodes = {n["id"]: n for n in nodes if n.get("type") == "storageNode"}

        def _is_iso_storage(snode):
            sd = snode.get("data", {})
            if sd.get("format") == "iso":
                return True
            lid = sd.get("libraryItemId", "")
            return lid in _iso_item_ids

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
                        if _is_iso_storage(sn):
                            break
                        sd = sn.get("data", {})
                        disk_out = {}
                        disk_name = sd.get("name", "")
                        if disk_name:
                            disk_out["name"] = disk_name
                        disk_out["size_gb"] = sd.get("size", 50)
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

        # ISOs — find cdrom-attached or ISO-format storage nodes
        isos = []
        for dc in disk_controllers:
            is_cdrom = dc.get("name", "").startswith("cdrom")
            for sid in storage_ids:
                sn = storage_nodes.get(sid)
                if not sn:
                    continue
                for e in vm_edges:
                    if e["source"] == sid and dc["id"] in e.get("targetHandle", ""):
                        if is_cdrom or _is_iso_storage(sn):
                            sd = sn.get("data", {})
                            if sd.get("libraryItemId"):
                                isos.append(
                                    {
                                        "name": sd.get("name", "iso"),
                                        "library_item_id": sd["libraryItemId"],
                                        "library_item_name": sd.get(
                                            "libraryItemName", ""
                                        ),
                                    }
                                )
                        break
        if isos:
            vm_out["isos"] = isos

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

    result: dict = {"networks": networks}
    if gateway:
        result["gateway"] = gateway
    result["vms"] = vms

    # ── Containers ──
    container_nodes = [n for n in nodes if n.get("type") == "containerNode"]
    if container_nodes:
        all_storage_nodes = {
            n["id"]: n for n in nodes if n.get("type") == "storageNode"
        }

        def _resolve_disk_name(disk_node_id, all_storage_nodes):
            node = all_storage_nodes.get(disk_node_id)
            if node:
                return node.get("data", {}).get("name", disk_node_id[:8])
            return disk_node_id[:8]

        containers = {}
        for ctr_node in container_nodes:
            cd = ctr_node.get("data", {})
            ctr_name = cd.get("name", "container")

            # Resolve NIC → network connections (same edge-walking as VMs)
            nics_export = []
            for nic in cd.get("nics", []):
                nic_id = nic.get("id", "")
                handle_top = f"nic-{nic_id}-top"
                handle_bottom = f"nic-{nic_id}-bottom"
                net_name = None
                for edge in edges:
                    if edge.get("source") == ctr_node["id"] and edge.get(
                        "sourceHandle"
                    ) in (handle_top, handle_bottom):
                        net_node = net_nodes.get(edge["target"])
                        if net_node:
                            net_name = net_node.get("data", {}).get("name")
                    elif edge.get("target") == ctr_node["id"] and edge.get(
                        "targetHandle"
                    ) in (handle_top, handle_bottom):
                        net_node = net_nodes.get(edge["source"])
                        if net_node:
                            net_name = net_node.get("data", {}).get("name")
                nic_entry = {}
                if net_name:
                    nic_entry["network"] = net_name
                if nic.get("ip"):
                    nic_entry["ip"] = nic["ip"]
                if nic.get("model") and nic["model"] != "virtio":
                    nic_entry["model"] = nic["model"]
                if nic_entry:
                    nics_export.append(nic_entry)

            # Resolve mount → storage connections
            disks_export = []
            for mount in cd.get("mounts", []):
                disk_node = all_storage_nodes.get(mount.get("diskNodeId", ""))
                if disk_node:
                    dd = disk_node.get("data", {})
                    disks_export.append(
                        {
                            "size_gb": dd.get("size", 10),
                            "mount_path": mount.get("mountPath", ""),
                        }
                    )

            if cd.get("isPod"):
                ctr_export: dict = {"type": "pod"}
                if nics_export:
                    ctr_export["nics"] = nics_export
                if cd.get("restartPolicy", "always") != "always":
                    ctr_export["restart_policy"] = cd["restartPolicy"]
                if cd.get("privileged"):
                    ctr_export["privileged"] = True

                init_ctrs_export = []
                for ic in cd.get("initContainers", []):
                    ic_entry: dict = {"name": ic["name"], "image": ic.get("image", "")}
                    if ic.get("command"):
                        ic_entry["command"] = ic["command"]
                    if ic.get("envVars"):
                        ic_entry["env"] = {
                            ev["key"]: ev["value"]
                            for ev in ic["envVars"]
                            if ev.get("key")
                        }
                    if ic.get("mounts"):
                        ic_entry["mounts"] = [
                            {
                                "disk": _resolve_disk_name(
                                    m.get("diskNodeId", ""), all_storage_nodes
                                ),
                                "mount_path": m.get("mountPath", ""),
                            }
                            for m in ic["mounts"]
                            if m.get("diskNodeId")
                        ]
                    if ic.get("ports"):
                        ic_entry["ports"] = [
                            p["containerPort"]
                            for p in ic["ports"]
                            if p.get("containerPort")
                        ]
                    init_ctrs_export.append(ic_entry)
                if init_ctrs_export:
                    ctr_export["init_containers"] = init_ctrs_export

                pod_ctrs_export = []
                for pc in cd.get("podContainers", []):
                    pc_entry: dict = {"name": pc["name"], "image": pc.get("image", "")}
                    if pc.get("cpus", 1) != 1:
                        pc_entry["cpus"] = pc["cpus"]
                    if pc.get("memory", 512) != 512:
                        pc_entry["memory_mb"] = pc["memory"]
                    if pc.get("command"):
                        pc_entry["command"] = pc["command"]
                    if pc.get("envVars"):
                        pc_entry["env"] = {
                            ev["key"]: ev["value"]
                            for ev in pc["envVars"]
                            if ev.get("key")
                        }
                    if pc.get("ports"):
                        pc_entry["ports"] = [
                            p["containerPort"]
                            for p in pc["ports"]
                            if p.get("containerPort")
                        ]
                    if pc.get("mounts"):
                        pc_entry["mounts"] = [
                            {
                                "disk": _resolve_disk_name(
                                    m.get("diskNodeId", ""), all_storage_nodes
                                ),
                                "mount_path": m.get("mountPath", ""),
                            }
                            for m in pc["mounts"]
                            if m.get("diskNodeId")
                        ]
                    pod_ctrs_export.append(pc_entry)
                if pod_ctrs_export:
                    ctr_export["containers"] = pod_ctrs_export

                if disks_export:
                    ctr_export["disks"] = disks_export

                containers[ctr_name] = ctr_export
                continue

            ctr_export = {"image": cd.get("image", "")}
            if cd.get("registryCredentialName"):
                ctr_export["registry_credential"] = cd["registryCredentialName"]
            if cd.get("cpus", 1) != 1:
                ctr_export["cpus"] = cd["cpus"]
            if cd.get("memory", 512) != 512:
                ctr_export["memory_mb"] = cd["memory"]
            if cd.get("privileged"):
                ctr_export["privileged"] = True
            if cd.get("restartPolicy", "always") != "always":
                ctr_export["restart_policy"] = cd["restartPolicy"]
            if cd.get("command"):
                ctr_export["command"] = cd["command"]
            if nics_export:
                ctr_export["nics"] = nics_export
            if cd.get("envVars"):
                ctr_export["env"] = {
                    ev["key"]: ev["value"] for ev in cd["envVars"] if ev.get("key")
                }
            if cd.get("ports"):
                ctr_export["ports"] = [
                    {
                        "container_port": p["containerPort"],
                        **({"host_port": p["hostPort"]} if p.get("hostPort") else {}),
                        **(
                            {"protocol": p["protocol"]}
                            if p.get("protocol", "tcp") != "tcp"
                            else {}
                        ),
                    }
                    for p in cd["ports"]
                ]
            if disks_export:
                ctr_export["disks"] = disks_export

            containers[ctr_name] = ctr_export

        result["containers"] = containers

    # Map node IDs to names for start_order and hidden_nodes
    id_to_name = {}
    for n in nodes:
        d = n.get("data", {})
        id_to_name[n["id"]] = d.get("name", d.get("label", n["id"][:8]))

    start_order = topology.get("startOrder", [])
    if start_order:
        so_out = []
        for entry in start_order:
            if entry.get("entryType") == "container":
                ctr_node = next(
                    (
                        n
                        for n in container_nodes
                        if n["id"] == entry.get("containerId", entry.get("vmId", ""))
                    ),
                    None,
                )
                if ctr_node:
                    so_entry = {"container": ctr_node["data"]["name"]}
                    if entry.get("delaySeconds"):
                        so_entry["delay"] = entry["delaySeconds"]
                    so_out.append(so_entry)
            else:
                # VM start order entry
                so_entry = {"vm": id_to_name.get(entry.get("vmId", ""), "")}
                if entry.get("waitForVm"):
                    so_entry["wait_for"] = id_to_name.get(entry["waitForVm"], "")
                if "autoStart" in entry:
                    so_entry["auto_start"] = entry["autoStart"]
                if entry.get("delay"):
                    so_entry["delay"] = entry["delay"]
                so_out.append(so_entry)
        result["start_order"] = so_out

    hidden = topology.get("hiddenNodeIds", [])
    if hidden:
        result["hidden_nodes"] = [id_to_name.get(h, h) for h in hidden]

    return result


def generate_topology_from_template(
    resolved: dict,
    bmc_password: str = "password",
    external_access: bool = False,  # pragma: allowlist secret
) -> dict:
    if not resolved.get("vms") and not resolved.get("containers"):
        raise ValueError("Template must have a 'vms' or 'containers' section")

    topo = _generate_topology_from_vms(resolved, bmc_password, external_access)
    from app.services.auto_layout import auto_layout

    topo["nodes"], topo["edges"] = auto_layout(topo["nodes"], topo["edges"])
    return topo
