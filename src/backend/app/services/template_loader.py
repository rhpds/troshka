import os
import random
import uuid
from pathlib import Path

import yaml  # type: ignore[import-untyped]

_DEFAULT_TEMPLATES_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "templates"
)
_STORAGE_EDGE_STROKE = "rgba(251,191,36,0.6)"

# Topology "content" sections carried verbatim from a template into the resolved
# form. Both resolve_template (quickstart by id) and resolve_inline_template
# (normal import) MUST copy the same set, or a quickstart silently drops entries
# (e.g. showroom). Keep this the single source of truth for that list.
_TEMPLATE_CONTENT_SECTIONS = (
    "ocp",
    "dns_records",
    "disconnected",
    "bastion_services",
    "start_order",
    "hidden_nodes",
    "pull_through_registry",
    "clock_target",
    "showroom",
)


def normalize_ocp_section(ocp) -> list[dict]:
    """Normalize the template ``ocp:`` section to a list of cluster dicts.

    Accepts the legacy singular mapping (wrapped into a one-element list) or
    the new list form. Each cluster is guaranteed a ``name`` (from ``name`` or
    legacy ``cluster_name``, default ``"ocp"``).
    """
    if not ocp:
        return []
    entries = [ocp] if isinstance(ocp, dict) else list(ocp)
    clusters = []
    for entry in entries:
        c = dict(entry)
        c["name"] = c.get("name") or c.get("cluster_name") or "ocp"
        c.pop("cluster_name", None)
        clusters.append(c)
    return clusters


def _copy_template_content_sections(tmpl: dict, resolved: dict) -> None:
    """Copy vms/containers and all topology content sections from tmpl."""
    if tmpl.get("vms"):
        resolved["vms"] = tmpl["vms"]
    if tmpl.get("containers"):
        resolved["containers"] = tmpl["containers"]
    for section in _TEMPLATE_CONTENT_SECTIONS:
        if tmpl.get(section):
            if section == "ocp":
                resolved["ocp"] = normalize_ocp_section(tmpl["ocp"])
            else:
                resolved[section] = tmpl[section]


def load_template(name: str, templates_dir: str = _DEFAULT_TEMPLATES_DIR) -> dict:
    base = Path(templates_dir).resolve()
    path = (base / f"{name}.yaml").resolve()
    if not path.is_relative_to(base):
        raise ValueError(f"Invalid template name: '{name}'")
    if not path.exists():
        raise FileNotFoundError(f"Template '{name}' not found at {path}")
    with open(path) as f:
        return yaml.safe_load(f)


def _resolve_params(base_params, overrides, preset_defaults):
    """Resolve parameter values from overrides, presets, and defaults."""
    resolved = {}
    for param_name, param_def in base_params.items():
        if param_name in overrides:
            value = overrides[param_name]
        elif param_name in preset_defaults:
            value = preset_defaults[param_name]
        else:
            value = param_def["default"]
        resolved[param_name] = value
    return resolved


def _validate_params(resolved, base_params, overrides):
    """Validate resolved parameters and reject unknown overrides."""
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
    resolved = _resolve_params(base_params, overrides, preset_defaults)
    _validate_params(resolved, base_params, overrides)

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

    _copy_template_content_sections(tmpl, resolved)

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

    _copy_template_content_sections(tmpl, resolved)

    return resolved


def _find_bastion_image_name(tmpl: dict) -> str:
    """Find the bastion VM's library image name from a template."""
    for vm_cfg in (tmpl.get("vms") or {}).values():
        if vm_cfg.get("role") != "bastion":
            continue
        for disk in vm_cfg.get("disks", []):
            if disk.get("library_item_name"):
                return disk["library_item_name"]
        break
    return ""


def _parse_template_entry(f, tmpl: dict) -> dict:
    """Build a template listing entry from a parsed template dict."""
    bastion_image_name = _find_bastion_image_name(tmpl)
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
    return entry


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
            result.append(_parse_template_entry(f, tmpl))
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
    return _workload_net_edge(net_id, vm_node["id"], nic["id"], vm_handle)


def _workload_net_edge(net_id, workload_id, nic_id, workload_handle="top"):
    """Network-to-workload edge (VM or container): network above, workload below."""
    return {
        "id": _id(),
        "source": net_id,
        "target": workload_id,
        "sourceHandle": "bottom" if workload_handle == "top" else "top",
        "targetHandle": f"nic-{nic_id}-{workload_handle}",
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
        "sourceHandle": "bottom",
        "targetHandle": "top",
        "type": "smoothstep",
        "style": {
            "stroke": "rgba(34,211,238,0.5)",
            "strokeWidth": 2,
            "strokeDasharray": "6 4",
        },
        "animated": True,
    }


def _create_network_nodes(nets_def, bmc_password, net_row_y, vm_spacing):
    """Create network nodes from template networks definition."""
    net_ids = {}
    nodes = []
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
            "position": {"x": net_x, "y": net_row_y},
            "data": net_data,
        }
        net_ids[net_name] = net_node["id"]
        nodes.append(net_node)
        net_x += vm_spacing
    return nodes, net_ids


def _generate_ocp_port_forwards(eip_id, vms_def, ocp_cfg):
    """Generate OCP port forwards when no custom forwards exist."""
    port_forwards = []
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
                "extPort": "2222",
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
    return port_forwards


def _create_gateway_node(gw_def, vms_def, tmpl, external_access, gw_y, nets_def):
    """Create gateway node from template gateway definition."""
    gw_outbound = gw_def.get("outbound_ports", [])
    gw_external = external_access or gw_def.get("external_access", False)
    port_forwards = []
    external_ips = []

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
            ocp_clusters = normalize_ocp_section(tmpl.get("ocp"))
            ocp_cfg = ocp_clusters[0] if ocp_clusters else {}
            port_forwards = _generate_ocp_port_forwards(eip_id, vms_def, ocp_cfg)

    gw_node = {
        "id": _id(),
        "type": "networkNode",
        "position": {"x": 150, "y": gw_y},
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

    # Create edge to gateway network
    gw_edges = []
    gw_net_name = gw_def.get("network")
    if not gw_net_name:
        for nn, nc in nets_def.items():
            if nc.get("type") != "bmc":
                gw_net_name = nn
                break

    return gw_node, external_ips, gw_edges, gw_net_name


def _parse_pod_sub_container(sc_cfg, default_name, disk_name_to_id):
    """Parse sub-container config for pod init/main containers."""
    sc_env = [{"key": k, "value": str(v)} for k, v in (sc_cfg.get("env") or {}).items()]
    sc_ports = []
    for p in sc_cfg.get("ports", []):
        if isinstance(p, int):
            sc_ports.append({"containerPort": p, "hostPort": None, "protocol": "tcp"})
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


def _build_vm_nics(nics_cfg, nets_def):
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
    return nics


def _apply_vm_cloud_init_fields(vm_cfg, vm_data):
    if vm_cfg.get("cloud_init"):
        vm_data["cloudInit"] = True
    if vm_cfg.get("minimal_cloud_init"):
        vm_data["ciMinimalCloudInit"] = True
    if vm_cfg.get("login_user"):
        vm_data["ciLoginUser"] = vm_cfg["login_user"]
    if vm_cfg.get("cloud_user_password"):
        vm_data["ciCloudUserPassword"] = vm_cfg["cloud_user_password"]
    if vm_cfg.get("userdata_only"):
        vm_data["ciUserDataOnly"] = True
    if vm_cfg.get("user_data"):
        vm_data["ciUserData"] = vm_cfg["user_data"]
    if vm_cfg.get("packages"):
        vm_data["ciPackages"] = vm_cfg["packages"]
    if vm_cfg.get("network_config"):
        vm_data["ciNetworkConfig"] = vm_cfg["network_config"]


def _apply_vm_optional_fields(vm_name, vm_cfg, vm_data, role, bmc_ip):
    if vm_cfg.get("uuid"):
        try:
            uuid.UUID(vm_cfg["uuid"])
        except ValueError:
            raise ValueError(
                f"VM '{vm_name}': invalid uuid '{vm_cfg['uuid']}' — must be UUID format"
            )
        vm_data["smbiosUuid"] = vm_cfg["uuid"]
    if bmc_ip:
        vm_data["bmcIp"] = bmc_ip
    if vm_cfg.get("pxe_boot_iso_id"):
        vm_data["pxeBootIsoId"] = vm_cfg["pxe_boot_iso_id"]
    if vm_cfg.get("pxe_boot_iso_name"):
        vm_data["pxeBootIsoName"] = vm_cfg["pxe_boot_iso_name"]

    if vm_cfg.get("serial_exec"):
        vm_data["serialExecType"] = str(vm_cfg["serial_exec"]).lower()

    machine_type = vm_cfg.get("machine_type") or vm_cfg.get("machineType")
    if machine_type:
        vm_data["machineType"] = str(machine_type)

    legacy_root_bus = vm_cfg.get("legacy_root_bus")
    if legacy_root_bus is None:
        legacy_root_bus = vm_cfg.get("legacyRootBus")
    if legacy_root_bus:
        vm_data["legacyRootBus"] = True

    if vm_cfg.get("tags"):
        vm_data["tags"] = vm_cfg["tags"]
    elif role == "control-plane":
        vm_data["tags"] = {"AnsibleGroup": "controllers"}
    elif role == "worker":
        vm_data["tags"] = {"AnsibleGroup": "workers"}
    elif role == "bastion":
        vm_data["tags"] = {"AnsibleGroup": "bastions,showroom"}

    _apply_vm_cloud_init_fields(vm_cfg, vm_data)

    if vm_cfg.get("affinity_group"):
        vm_data["affinityGroup"] = vm_cfg["affinity_group"]
    if vm_cfg.get("separate_host"):
        vm_data["separateHost"] = vm_cfg["separate_host"]


def _build_vm_iso_nodes(
    vm_cfg, vm_name, vm_node_id, disk_controllers, disks_cfg, vm_x, vm_row_y
):
    iso_nodes_edges = []
    cdrom_dc = next(
        (dc for dc in disk_controllers if dc.get("name", "").startswith("cdrom")),
        None,
    )
    for iso_cfg in vm_cfg.get("isos", []):
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
        # Config-only bootstrap ISOs (IOS-XE CVAC, Junos KVM) must not become boot device.
        iso_data["bootableIso"] = bool(iso_cfg.get("boot", False))
        iso_node = {
            "id": iso_id,
            "type": "storageNode",
            "position": {
                "x": vm_x - 190,
                "y": vm_row_y + 70 + len(disks_cfg) * 100,
            },
            "data": iso_data,
        }
        iso_edge = {
            "id": _id(),
            "source": iso_id,
            "target": vm_node_id,
            "sourceHandle": "right",
            "targetHandle": f"dp-{cdrom_dc['id']}-left",
            "type": "smoothstep",
            "style": {
                "stroke": _STORAGE_EDGE_STROKE,
                "strokeWidth": 2,
                "strokeDasharray": "4 4",
            },
            "animated": False,
            "className": "edge-storage-pulse",
        }
        iso_nodes_edges.append((iso_node, iso_edge))
    return iso_nodes_edges


def _build_disk_node_and_edge(vm_name, disk_cfg, di, vm_x, vm_row_y):
    disk_bus = disk_cfg.get("bus", "virtio")
    dc = {"id": f"dp-{_id()}", "name": f"disk{di}", "bus": disk_bus}
    if disk_bus in ("scsi", "sata", "ide"):
        dc["rotationRate"] = disk_cfg.get("rotation_rate", 1)
    disk_id = _id()
    disk_data = {
        "label": disk_cfg.get("name", f"{vm_name}-disk{di}"),
        "name": disk_cfg.get("name", f"{vm_name}-disk{di}"),
        "size": disk_cfg.get("size_gb", 50),
        "format": "qcow2",
        "icon": "\U0001f6e2",
    }
    if disk_cfg.get("library_item_id"):
        disk_data["libraryItemId"] = disk_cfg["library_item_id"]
    if disk_cfg.get("library_item_name"):
        disk_data["libraryItemName"] = disk_cfg["library_item_name"]
    if disk_cfg.get("library_item_id") or disk_cfg.get("library_item_name"):
        disk_data["source"] = "library"
    if disk_cfg.get("ocp_mount"):
        disk_data["ocpMount"] = disk_cfg["ocp_mount"]
    disk_node = {
        "id": disk_id,
        "type": "storageNode",
        "position": {"x": vm_x - 190, "y": vm_row_y + 70 + di * 100},
        "data": disk_data,
    }
    disk_edge = {
        "id": _id(),
        "source": disk_id,
        "target": "",
        "sourceHandle": "right",
        "targetHandle": f"dp-{dc['id']}-left",
        "type": "smoothstep",
        "style": {
            "stroke": _STORAGE_EDGE_STROKE,
            "strokeWidth": 2,
            "strokeDasharray": "4 4",
        },
        "animated": False,
        "className": "edge-storage-pulse",
    }
    return dc, disk_id, disk_node, disk_edge


def _build_vm_data(vm_name, vm_cfg, _vms_def, nets_def, net_ids, vm_x, vm_row_y):
    """Build VM node and associated disk/iso/edge nodes from VM config."""
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

    nics = _build_vm_nics(nics_cfg, nets_def)

    disk_controllers = []
    disk_nodes = []
    disk_edges_list = []
    boot_device_ids = []
    for di, disk_cfg in enumerate(disks_cfg):
        dc, disk_id, disk_node, disk_edge = _build_disk_node_and_edge(
            vm_name, disk_cfg, di, vm_x, vm_row_y
        )
        disk_controllers.append(dc)
        if di == 0:
            boot_device_ids.append(disk_id)
        disk_nodes.append(disk_node)
        disk_edges_list.append(disk_edge)

    isos_cfg = vm_cfg.get("isos", [])
    if os_type != "blank" or isos_cfg:
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
        "recertEnabled": vm_cfg.get("recert", False),
        "ocpMonitor": vm_cfg.get("ocp_monitor", False),
        "configureBastionBrowser": vm_cfg.get("configure_bastion_browser", False),
        "firmware": vm_cfg.get("firmware", "uefi"),
        "secureBoot": vm_cfg.get("secure_boot", False),
        "bootDevices": boot_device_ids,
        "bootMethod": "disk",
        "powerOnAtDeploy": power_on,
    }

    _apply_vm_optional_fields(vm_name, vm_cfg, vm_data, role, bmc_ip)

    vm_node = {
        "id": _id(),
        "type": "vmNode",
        "position": {"x": vm_x, "y": vm_row_y},
        "data": vm_data,
    }

    for de in disk_edges_list:
        de["target"] = vm_node["id"]

    iso_nodes_edges = _build_vm_iso_nodes(
        vm_cfg, vm_name, vm_node["id"], disk_controllers, disks_cfg, vm_x, vm_row_y
    )

    nic_edges = []
    for ni, nic_cfg in enumerate(nics_cfg):
        net_name = nic_cfg.get("network", "")
        if net_name in net_ids:
            handle = "top" if ni == 0 else "bottom"
            nic_edges.append(_net_edge(net_ids[net_name], vm_node, ni, handle))

    return vm_node, disk_nodes, disk_edges_list, iso_nodes_edges, nic_edges


def _build_container_nics_and_edges(ctr_id, ctr_cfg, net_ids):
    ctr_nics = []
    nic_edges = []
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

        net_name = nic_cfg.get("network", "")
        net_node_id = net_ids.get(net_name)
        if net_node_id:
            nic_edges.append(_workload_net_edge(net_node_id, ctr_id, nic_id, "top"))
    return ctr_nics, nic_edges


def _build_container_node(ctr_key, ctr_cfg, net_ids, _nets_def, vm_x, vm_row_y):
    """Build container node and associated disk/edge nodes from container config."""
    ctr_id = _id()
    is_pod = ctr_cfg.get("type") == "pod"

    ctr_nics, nic_edges = _build_container_nics_and_edges(ctr_id, ctr_cfg, net_ids)

    disk_name_to_id = {}
    disk_nodes = []
    disk_edges = []
    ctr_mounts = []
    for disk_idx, disk_cfg in enumerate(ctr_cfg.get("disks", [])):
        disk_id = _id()
        disk_name = f"{ctr_key}-vol{disk_idx}"
        disk_node = {
            "id": disk_id,
            "type": "storageNode",
            "position": {"x": vm_x - 190, "y": vm_row_y + 70 + disk_idx * 100},
            "data": {
                "label": disk_name,
                "name": disk_name,
                "size": disk_cfg.get("size_gb", 10),
                "format": "raw",
                "icon": "\U0001f6e2",
            },
        }
        disk_nodes.append(disk_node)
        disk_name_to_id[disk_name] = disk_id
        ctr_mounts.append(
            {
                "diskNodeId": disk_id,
                "mountPath": disk_cfg.get("mount_path", ""),
            }
        )
        disk_edges.append(
            {
                "id": _id(),
                "source": disk_id,
                "target": ctr_id,
                "sourceHandle": "right",
                "targetHandle": f"mnt-{disk_id}-left",
                "type": "smoothstep",
                "style": {
                    "stroke": _STORAGE_EDGE_STROKE,
                    "strokeWidth": 2,
                    "strokeDasharray": "4 4",
                },
            }
        )

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
        "position": {"x": vm_x, "y": vm_row_y},
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
        if "build_content" in ctr_cfg:
            ctr_node["data"]["buildContent"] = bool(ctr_cfg["build_content"])

        init_ctrs = []
        for ic_cfg in ctr_cfg.get("init_containers", []):
            init_ctrs.append(
                _parse_pod_sub_container(
                    ic_cfg, f"init-{len(init_ctrs)}", disk_name_to_id
                )
            )
        ctr_node["data"]["initContainers"] = init_ctrs

        pod_ctrs = []
        for pc_cfg in ctr_cfg.get("containers", []):
            pod_ctrs.append(
                _parse_pod_sub_container(
                    pc_cfg, f"ctr-{len(pod_ctrs)}", disk_name_to_id
                )
            )
        ctr_node["data"]["podContainers"] = pod_ctrs

    return ctr_node, disk_nodes, disk_edges, nic_edges


def _build_container_start_entry(entry, container_name_to_id):
    ctr_id = container_name_to_id.get(entry["container"], "")
    if not ctr_id:
        return None
    return {
        "vmId": ctr_id,
        "containerId": ctr_id,
        "entryType": "container",
        "autoStart": True,
        "waitForVm": None,
        "waitForService": "none",
        "waitForPort": "",
        "delaySeconds": entry.get("delay", 0),
    }


def _build_vm_start_entry(entry, vm_name_to_id):
    vm_id = vm_name_to_id.get(entry.get("vm", ""), "")
    if not vm_id:
        return None
    so = {"vmId": vm_id, "autoStart": entry.get("auto_start", True)}
    wait_name = entry.get("wait_for", "")
    if wait_name and wait_name in vm_name_to_id:
        so["waitForVm"] = vm_name_to_id[wait_name]
    if entry.get("delay"):
        so["delay"] = entry["delay"]
    return so


def _build_start_order(tmpl, vm_name_to_id, container_name_to_id):
    """Build startOrder array from template start_order section."""
    start_order = []
    for entry in tmpl.get("start_order", []):
        if "container" in entry:
            so = _build_container_start_entry(entry, container_name_to_id)
        elif "vm" in entry:
            so = _build_vm_start_entry(entry, vm_name_to_id)
        else:
            so = None
        if so:
            start_order.append(so)
    return start_order


def _generate_ocp_cluster_dns_records(ocp_cfg, top_dns):
    cn = ocp_cfg.get("name")
    bd = ocp_cfg.get("base_domain")
    if not (cn and bd):
        return
    api_vip = ocp_cfg.get("api_vip", "")
    ingress_vip = ocp_cfg.get("ingress_vip", api_vip)
    if not api_vip:
        return
    for rec_name in [f"api.{cn}.{bd}", f"api-int.{cn}.{bd}"]:
        if not any(r.get("name") == rec_name for r in top_dns):
            top_dns.append({"name": rec_name, "ip": api_vip})
    apps_name = f".apps.{cn}.{bd}"
    if not any(r.get("name") == apps_name for r in top_dns):
        top_dns.append({"name": apps_name, "ip": ingress_vip})


def _generate_ocp_dns_records(ocp_section, top_dns):
    """Generate api/api-int/apps DNS records for each normalized OCP cluster."""
    for ocp_cfg in normalize_ocp_section(ocp_section):
        _generate_ocp_cluster_dns_records(ocp_cfg, top_dns)


def _collect_workload_ips(nodes: list) -> dict[str, str]:
    """Map VM/container name (and label) to first static NIC IP."""
    ips: dict[str, str] = {}
    for n in nodes:
        if n.get("type") not in ("vmNode", "containerNode"):
            continue
        data = n.get("data", {})
        ip = ""
        for nic in data.get("nics", []):
            if nic.get("ip"):
                ip = nic["ip"]
                break
        if not ip:
            continue
        name = data.get("name", "")
        label = data.get("label", "")
        if name:
            ips[name] = ip
        if label:
            ips[label] = ip
    return ips


def _resolve_dns_record_entry(rec: dict, workload_ips: dict[str, str]) -> dict:
    """Resolve template target=vm_name to ip when ip is omitted."""
    name = rec.get("name", "")
    if not name:
        return dict(rec)
    ip = str(rec.get("ip") or "").strip()
    target = str(rec.get("target") or "").strip()
    if not ip and target:
        ip = workload_ips.get(target, "")
    out = dict(rec)
    out["ip"] = ip
    return out


def _resolve_dns_records_on_networks(nodes: list) -> None:
    """Resolve dnsRecords targets on each lab network after VMs are built."""
    workload_ips = _collect_workload_ips(nodes)
    for net_node in nodes:
        if net_node.get("type") != "networkNode":
            continue
        data = net_node.get("data", {})
        if data.get("subtype", "network") != "network":
            continue
        if data.get("networkType") == "bmc":
            continue
        records = data.get("dnsRecords")
        if not records:
            continue
        data["dnsRecords"] = [
            _resolve_dns_record_entry(r, workload_ips) for r in records
        ]


def _merge_dns_to_network(net_node, top_dns, workload_ips):
    existing = net_node["data"].get("dnsRecords", [])
    existing_names = {r["name"] for r in existing}
    for rec in top_dns:
        resolved = _resolve_dns_record_entry(rec, workload_ips)
        ip = resolved.get("ip", "")
        if ip and resolved.get("name") and resolved["name"] not in existing_names:
            existing.append({"name": resolved["name"], "ip": ip})
    if existing:
        net_node["data"]["dnsRecords"] = existing


def _apply_dns_records(tmpl, nodes):
    """Apply top-level DNS records to network nodes, with OCP auto-generation."""
    top_dns = list(tmpl.get("dns_records", []))
    _generate_ocp_dns_records(tmpl.get("ocp", {}), top_dns)

    if not top_dns:
        return

    workload_ips = _collect_workload_ips(nodes)
    for net_node in nodes:
        if (
            net_node.get("type") == "networkNode"
            and net_node.get("data", {}).get("subtype") == "network"
            and net_node.get("data", {}).get("networkType") != "bmc"
        ):
            _merge_dns_to_network(net_node, top_dns, workload_ips)
            break


def _validate_uuid_uniqueness(nodes):
    seen_uuids = {}
    for n in nodes:
        d = n.get("data", {})
        u = d.get("smbiosUuid") or d.get("uuid")
        if n.get("type") == "vmNode" and u:
            if u in seen_uuids:
                raise ValueError(
                    f"Duplicate uuid '{u}' on VMs '{seen_uuids[u]}' and '{d.get('name')}'"
                )
            seen_uuids[u] = d.get("name", "")


def _default_dns_network_name(
    nets_def: dict, gateway_network: str | None = None
) -> str:
    """Gateway-outbound default: gateway.network when it has DNS enabled."""
    gw_net = (gateway_network or "").strip()
    if gw_net and gw_net in nets_def:
        net_cfg = nets_def[gw_net]
        if net_cfg.get("type") != "bmc" and (
            net_cfg.get("domain") or net_cfg.get("dns") or net_cfg.get("dns_upstream")
        ):
            return gw_net
    return ""


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

    vms_def = tmpl.get("vms", {})
    nets_def = tmpl.get("networks", {})
    gw_def = tmpl.get("gateway", {})

    VM_SPACING = 400
    GW_Y = 0
    NET_ROW_Y = 150
    VM_ROW_Y = 350

    net_nodes, net_ids = _create_network_nodes(
        nets_def, bmc_password, NET_ROW_Y, VM_SPACING
    )
    nodes.extend(net_nodes)

    gw_node, external_ips, gw_edges, gw_net_name = _create_gateway_node(
        gw_def, vms_def, tmpl, external_access, GW_Y, nets_def
    )
    nodes.append(gw_node)
    edges.extend(gw_edges)
    if gw_net_name and gw_net_name in net_ids:
        edges.append(_gw_net_edge(gw_node["id"], net_ids[gw_net_name]))

    vm_name_to_id = {}
    vm_x = 150
    for vm_name, vm_cfg in vms_def.items():
        vm_node, disk_nodes, disk_edges, iso_nodes_edges, nic_edges = _build_vm_data(
            vm_name, vm_cfg, vms_def, nets_def, net_ids, vm_x, VM_ROW_Y
        )
        nodes.append(vm_node)
        nodes.extend(disk_nodes)
        edges.extend(disk_edges)
        for iso_node, iso_edge in iso_nodes_edges:
            nodes.append(iso_node)
            edges.append(iso_edge)
        edges.extend(nic_edges)
        vm_name_to_id[vm_name] = vm_node["id"]
        vm_x += VM_SPACING

    container_name_to_id = {}
    containers_def = tmpl.get("containers", {})
    showroom_cfg = tmpl.get("showroom") or {}
    scaffold_showroom = (
        bool(showroom_cfg.get("enabled")) and "showroom" not in containers_def
    )

    if scaffold_showroom:
        from app.services.showroom_scaffold import build_showroom_from_config

        (
            ctr_node,
            disk_nodes,
            disk_edges,
            nic_edges,
            showroom_meta,
        ) = build_showroom_from_config(
            showroom_cfg,
            vm_name_to_id,
            vms_def,
            net_ids,
            gw_node["position"]["x"] - VM_SPACING,
            gw_node["position"]["y"],
        )
        nodes.append(ctr_node)
        nodes.extend(disk_nodes)
        edges.extend(disk_edges)
        edges.extend(nic_edges)
        container_name_to_id["showroom"] = ctr_node["id"]
        if not showroom_meta.get("dns_network"):
            default_dns = _default_dns_network_name(nets_def, gw_net_name)
            if default_dns:
                showroom_meta["dns_network"] = default_dns
                ctr_node["data"]["dnsNetwork"] = default_dns
        vm_x += VM_SPACING
    else:
        showroom_meta = None

    for ctr_key, ctr_cfg in containers_def.items():
        ctr_node, disk_nodes, disk_edges, nic_edges = _build_container_node(
            ctr_key, ctr_cfg, net_ids, nets_def, vm_x, VM_ROW_Y
        )
        nodes.append(ctr_node)
        nodes.extend(disk_nodes)
        edges.extend(disk_edges)
        edges.extend(nic_edges)
        container_name_to_id[ctr_key] = ctr_node["id"]
        vm_x += VM_SPACING

    start_order = _build_start_order(tmpl, vm_name_to_id, container_name_to_id)
    _apply_dns_records(tmpl, nodes)
    _resolve_dns_records_on_networks(nodes)
    _validate_uuid_uniqueness(nodes)

    hidden_ids = []
    all_name_to_id = {**vm_name_to_id, **container_name_to_id, **net_ids}
    for name in tmpl.get("hidden_nodes", []):
        nid = all_name_to_id.get(name)
        if nid:
            hidden_ids.append(nid)

    result = {
        "nodes": nodes,
        "edges": edges,
        "externalIps": external_ips,
        "startOrder": start_order,
        "hiddenNodeIds": hidden_ids,
    }
    if showroom_meta is not None:
        result["showroom"] = showroom_meta
    elif tmpl.get("showroom"):
        result["showroom"] = tmpl["showroom"]
    return result


def _collect_iso_item_ids(nodes: list[dict], db) -> set[str]:
    """Build set of library item IDs that are actually ISOs (check DB)."""
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
    return _iso_item_ids


def _build_nic_to_net_map(
    edges: list[dict], net_names: dict[str, str]
) -> dict[str, str]:
    """Build NIC -> network mapping from edges."""
    nic_to_net = {}

    def _extract_nic_id(handle: str) -> str:
        h = handle.removeprefix("nic-")
        for suffix in ("-top", "-bottom"):
            if h.endswith(suffix):
                h = h.removesuffix(suffix)
                break
        return h

    for e in edges:
        src = e.get("source", "")
        tgt = e.get("target", "")
        src_h = e.get("sourceHandle", "")
        tgt_h = e.get("targetHandle", "")
        if src in net_names and tgt_h.startswith("nic-"):
            nic_to_net[_extract_nic_id(tgt_h)] = net_names[src]
        elif tgt in net_names and src_h.startswith("nic-"):
            nic_to_net[_extract_nic_id(src_h)] = net_names[tgt]
    return nic_to_net


def _export_single_network(d):
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
    return net_out


def _export_networks(net_nodes: dict, net_names: dict[str, str]) -> dict:
    """Export networks section from network nodes."""
    networks = {}
    for nid, nn in net_nodes.items():
        d = nn.get("data", {})
        if d.get("subtype") == "gateway":
            continue
        networks[net_names[nid]] = _export_single_network(d)
    return networks


def _parse_outbound_ports(d):
    ports_str = d.get("outboundPorts", "")
    if not ports_str or d.get("outboundPolicy") != "restrict":
        return []
    ports: list[int | str] = []
    for p in ports_str.split(","):
        p = p.strip()
        if p.isdigit():
            ports.append(int(p))
        elif p:
            ports.append(p)
    return ports


def _export_port_forwards(d):
    pfs = d.get("portForwards", [])
    if not pfs:
        return []
    return [
        {
            "ext_port": int(pf.get("extPort", 0)),
            "int_ip": pf.get("intIp", ""),
            "int_port": int(pf.get("intPort", 0)),
            "proto": pf.get("proto", "tcp"),
        }
        for pf in pfs
        if pf.get("extPort") and pf.get("intIp")
    ]


def _find_gateway_network(gw_id, edges, net_names):
    for e in edges:
        if e.get("source") == gw_id and e.get("target") in net_names:
            return net_names[e["target"]]
        if e.get("target") == gw_id and e.get("source") in net_names:
            return net_names[e["source"]]
    return None


def _export_gateway(
    net_nodes: dict, edges: list[dict], net_names: dict[str, str]
) -> dict:
    """Export gateway section from gateway node."""
    gateway: dict[str, object] = {}
    for nn in net_nodes.values():
        d = nn.get("data", {})
        if d.get("subtype") != "gateway":
            continue
        ports = _parse_outbound_ports(d)
        if ports:
            gateway["outbound_ports"] = ports
        if d.get("gatewayMode") == "nat-portforward":
            gateway["external_access"] = True
            port_forwards = _export_port_forwards(d)
            if port_forwards:
                gateway["port_forwards"] = port_forwards
        net_name = _find_gateway_network(nn["id"], edges, net_names)
        if net_name:
            gateway["network"] = net_name
        break
    return gateway


def _is_iso_storage_node(snode, _iso_item_ids):
    sd = snode.get("data", {})
    if sd.get("format") == "iso":
        return True
    lid = sd.get("libraryItemId", "")
    return lid in _iso_item_ids


def _build_disk_output(sn, dc=None):
    sd = sn.get("data", {})
    disk_out = {}
    disk_name = sd.get("name", "")
    if disk_name:
        disk_out["name"] = disk_name
    disk_out["size_gb"] = sd.get("size", 50)
    if dc and dc.get("bus") and dc["bus"] != "virtio":
        disk_out["bus"] = dc["bus"]
    if dc and dc.get("rotationRate") is not None:
        disk_out["rotation_rate"] = dc["rotationRate"]
    if sd.get("libraryItemId"):
        disk_out["library_item_id"] = sd["libraryItemId"]
    if sd.get("libraryItemName"):
        disk_out["library_item_name"] = sd["libraryItemName"]
    return disk_out


def _find_disk_for_controller(dc, storage_ids, storage_nodes, vm_edges, _iso_item_ids):
    for sid in storage_ids:
        sn = storage_nodes.get(sid)
        if not sn:
            continue
        for e in vm_edges:
            th = e.get("targetHandle", "")
            sh = e.get("sourceHandle", "")
            if (e["source"] == sid and dc["id"] in th) or (
                e["target"] == sid and dc["id"] in sh
            ):
                if _is_iso_storage_node(sn, _iso_item_ids):
                    break
                return _build_disk_output(sn, dc)
    return None


def _export_vm_disks(
    vm: dict,
    vm_edges: list[dict],
    storage_ids: list[str],
    storage_nodes: dict,
    _iso_item_ids: set[str],
) -> list[dict]:
    """Export disks for a VM."""
    d = vm.get("data", {})
    disk_controllers = d.get("diskControllers", [])
    disks = []

    for dc in disk_controllers:
        if dc.get("name", "").startswith("cdrom"):
            continue
        disk_out = _find_disk_for_controller(
            dc, storage_ids, storage_nodes, vm_edges, _iso_item_ids
        )
        if disk_out:
            disks.append(disk_out)
    if not disks:
        for dc in disk_controllers:
            if not dc.get("name", "").startswith("cdrom"):
                disks.append({"size_gb": 50})
    return disks


def _extract_iso_entry(sn, is_cdrom, _iso_item_ids):
    if not (is_cdrom or _is_iso_storage_node(sn, _iso_item_ids)):
        return None
    sd = sn.get("data", {})
    if not sd.get("libraryItemId"):
        return None
    return {
        "name": sd.get("name", "iso"),
        "library_item_id": sd["libraryItemId"],
        "library_item_name": sd.get("libraryItemName", ""),
    }


def _check_iso_from_controller(dc, storage_ids, storage_nodes, vm_edges, _iso_item_ids):
    is_cdrom = dc.get("name", "").startswith("cdrom")
    isos = []
    for sid in storage_ids:
        sn = storage_nodes.get(sid)
        if not sn:
            continue
        for e in vm_edges:
            th = e.get("targetHandle", "")
            sh = e.get("sourceHandle", "")
            if (e["source"] == sid and dc["id"] in th) or (
                e["target"] == sid and dc["id"] in sh
            ):
                entry = _extract_iso_entry(sn, is_cdrom, _iso_item_ids)
                if entry:
                    isos.append(entry)
                break
    return isos


def _export_vm_isos(
    vm: dict,
    vm_edges: list[dict],
    storage_ids: list[str],
    storage_nodes: dict,
    _iso_item_ids: set[str],
) -> list[dict]:
    """Export ISOs for a VM."""
    d = vm.get("data", {})
    disk_controllers = d.get("diskControllers", [])
    isos = []
    for dc in disk_controllers:
        isos.extend(
            _check_iso_from_controller(
                dc, storage_ids, storage_nodes, vm_edges, _iso_item_ids
            )
        )
    return isos


def _export_vm_role(d):
    tags = d.get("tags", {})
    ag = tags.get("AnsibleGroup", "")
    if "bastions" in ag:
        return "bastion"
    if "controllers" in ag:
        return "control-plane"
    if d.get("os") == "blank":
        return "blank"
    return ""


def _export_vm_flags(d, vm_out):
    if d.get("secureBoot"):
        vm_out["secure_boot"] = True
    smbios_uuid = d.get("smbiosUuid") or d.get("uuid")
    if smbios_uuid:
        vm_out["uuid"] = smbios_uuid
    if not d.get("powerOnAtDeploy", True):
        vm_out["power_on"] = False
    if d.get("recertEnabled"):
        vm_out["recert"] = True
    if d.get("ocpMonitor"):
        vm_out["ocp_monitor"] = True
    if d.get("configureBastionBrowser"):
        vm_out["configure_bastion_browser"] = True
    if d.get("serialExecType") and d.get("serialExecType") != "linux":
        vm_out["serial_exec"] = d["serialExecType"]
    if d.get("machineType"):
        vm_out["machine_type"] = d["machineType"]
    if d.get("legacyRootBus"):
        vm_out["legacy_root_bus"] = True
    if d.get("bmcEnabled") and vm_out.get("role") != "control-plane":
        vm_out["bmc"] = True
    if d.get("bmcIp"):
        vm_out["bmc_ip"] = d["bmcIp"]

    tags = d.get("tags", {})
    if (
        tags
        and tags != {"AnsibleGroup": "controllers"}
        and tags != {"AnsibleGroup": "bastions,showroom"}
    ):
        vm_out["tags"] = tags


def _export_vm_cloud_init(d, vm_out):
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
    if d.get("affinityGroup"):
        vm_out["affinity_group"] = d["affinityGroup"]
    if d.get("separateHost"):
        vm_out["separate_host"] = True


def _export_vm_nics(d, nic_to_net):
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
    return nics_out


def _export_vm(
    vm: dict,
    edge_by_target: dict,
    nodes: list[dict],
    _net_names: dict[str, str],
    nic_to_net: dict[str, str],
    _iso_item_ids: set[str],
) -> dict:
    """Export a single VM to template dict format."""
    d = vm.get("data", {})
    vm_out: dict[str, object] = {}

    role = _export_vm_role(d)
    if role:
        vm_out["role"] = role

    vm_out["vcpus"] = d.get("vcpus", 2)
    vm_out["ram_gb"] = d.get("ram", 4)
    vm_out["os"] = d.get("os", "rhcos")
    vm_out["firmware"] = d.get("firmware", "uefi")

    _export_vm_flags(d, vm_out)
    _export_vm_cloud_init(d, vm_out)

    vm_edges = edge_by_target.get(vm["id"], [])
    storage_ids = []
    for e in vm_edges:
        if e.get("targetHandle", "").startswith("dp-"):
            storage_ids.append(e["source"])
        elif e.get("sourceHandle", "").startswith("dp-"):
            storage_ids.append(e["target"])
    storage_nodes = {n["id"]: n for n in nodes if n.get("type") == "storageNode"}

    vm_out["disks"] = _export_vm_disks(
        vm, vm_edges, storage_ids, storage_nodes, _iso_item_ids
    )

    isos = _export_vm_isos(vm, vm_edges, storage_ids, storage_nodes, _iso_item_ids)
    if isos:
        vm_out["isos"] = isos

    if d.get("pxeBootIsoId"):
        vm_out["pxe_boot_iso_id"] = d["pxeBootIsoId"]
    if d.get("pxeBootIsoName"):
        vm_out["pxe_boot_iso_name"] = d["pxeBootIsoName"]

    vm_out["nics"] = _export_vm_nics(d, nic_to_net)

    return vm_out


def _resolve_disk_name(disk_node_id, all_storage_nodes):
    node = all_storage_nodes.get(disk_node_id)
    if node:
        return node.get("data", {}).get("name", disk_node_id[:8])
    return disk_node_id[:8]


def _find_container_nic_net_name(nic_id, ctr_node_id, edges, net_nodes):
    handle_top = f"nic-{nic_id}-top"
    handle_bottom = f"nic-{nic_id}-bottom"
    for edge in edges:
        if edge.get("source") == ctr_node_id and edge.get("sourceHandle") in (
            handle_top,
            handle_bottom,
        ):
            net_node = net_nodes.get(edge["target"])
            if net_node:
                return net_node.get("data", {}).get("name")
        elif edge.get("target") == ctr_node_id and edge.get("targetHandle") in (
            handle_top,
            handle_bottom,
        ):
            net_node = net_nodes.get(edge["source"])
            if net_node:
                return net_node.get("data", {}).get("name")
    return None


def _export_container_nics(cd, ctr_node_id, edges, net_nodes):
    nics_export = []
    for nic in cd.get("nics", []):
        nic_id = nic.get("id", "")
        net_name = _find_container_nic_net_name(nic_id, ctr_node_id, edges, net_nodes)
        nic_entry = {}
        if net_name:
            nic_entry["network"] = net_name
        if nic.get("ip"):
            nic_entry["ip"] = nic["ip"]
        if nic.get("model") and nic["model"] != "virtio":
            nic_entry["model"] = nic["model"]
        if nic_entry:
            nics_export.append(nic_entry)
    return nics_export


def _export_container_disks(cd, all_storage_nodes):
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
    return disks_export


def _export_sub_container(sc, all_storage_nodes):
    entry: dict = {"name": sc["name"], "image": sc.get("image", "")}
    if sc.get("command"):
        entry["command"] = sc["command"]
    if sc.get("envVars"):
        entry["env"] = {ev["key"]: ev["value"] for ev in sc["envVars"] if ev.get("key")}
    if sc.get("mounts"):
        entry["mounts"] = [
            {
                "disk": _resolve_disk_name(m.get("diskNodeId", ""), all_storage_nodes),
                "mount_path": m.get("mountPath", ""),
            }
            for m in sc["mounts"]
            if m.get("diskNodeId")
        ]
    if sc.get("ports"):
        entry["ports"] = [
            p["containerPort"] for p in sc["ports"] if p.get("containerPort")
        ]
    return entry


def _export_pod_container(cd, nics_export, disks_export, all_storage_nodes):
    ctr_export: dict = {"type": "pod"}
    if "buildContent" in cd:
        ctr_export["build_content"] = bool(cd["buildContent"])
    if nics_export:
        ctr_export["nics"] = nics_export
    if cd.get("restartPolicy", "always") != "always":
        ctr_export["restart_policy"] = cd["restartPolicy"]
    if cd.get("privileged"):
        ctr_export["privileged"] = True

    init_ctrs_export = []
    for ic in cd.get("initContainers", []):
        init_ctrs_export.append(_export_sub_container(ic, all_storage_nodes))
    if init_ctrs_export:
        ctr_export["init_containers"] = init_ctrs_export

    pod_ctrs_export = []
    for pc in cd.get("podContainers", []):
        pc_entry = _export_sub_container(pc, all_storage_nodes)
        if pc.get("cpus", 1) != 1:
            pc_entry["cpus"] = pc["cpus"]
        if pc.get("memory", 512) != 512:
            pc_entry["memory_mb"] = pc["memory"]
        pod_ctrs_export.append(pc_entry)
    if pod_ctrs_export:
        ctr_export["containers"] = pod_ctrs_export

    if disks_export:
        ctr_export["disks"] = disks_export

    return ctr_export


def _export_single_container(cd, nics_export, disks_export):
    ctr_export: dict = {"image": cd.get("image", "")}
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
    return ctr_export


def _export_containers(
    container_nodes: list[dict],
    edges: list[dict],
    net_nodes: dict,
    all_storage_nodes: dict,
) -> dict:
    """Export containers section from container nodes."""
    containers = {}
    for ctr_node in container_nodes:
        cd = ctr_node.get("data", {})
        ctr_name = cd.get("name", "container")

        nics_export = _export_container_nics(cd, ctr_node["id"], edges, net_nodes)
        disks_export = _export_container_disks(cd, all_storage_nodes)

        if cd.get("isPod"):
            containers[ctr_name] = _export_pod_container(
                cd, nics_export, disks_export, all_storage_nodes
            )
        else:
            containers[ctr_name] = _export_single_container(
                cd, nics_export, disks_export
            )

    return containers


def _export_container_start_entry(entry, container_nodes):
    ctr_node = next(
        (
            n
            for n in container_nodes
            if n["id"] == entry.get("containerId", entry.get("vmId", ""))
        ),
        None,
    )
    if not ctr_node:
        return None
    so_entry = {"container": ctr_node["data"]["name"]}
    if entry.get("delaySeconds"):
        so_entry["delay"] = entry["delaySeconds"]
    return so_entry


def _export_vm_start_entry(entry, id_to_name):
    so_entry = {"vm": id_to_name.get(entry.get("vmId", ""), "")}
    if entry.get("waitForVm"):
        so_entry["wait_for"] = id_to_name.get(entry["waitForVm"], "")
    if "autoStart" in entry:
        so_entry["auto_start"] = entry["autoStart"]
    if entry.get("delay"):
        so_entry["delay"] = entry["delay"]
    return so_entry


def _export_start_order(
    topology: dict, container_nodes: list[dict], id_to_name: dict[str, str]
) -> list[dict]:
    """Export start_order section from topology."""
    start_order = topology.get("startOrder", [])
    if not start_order:
        return []

    so_out = []
    for entry in start_order:
        if entry.get("entryType") == "container":
            so_entry = _export_container_start_entry(entry, container_nodes)
            if so_entry:
                so_out.append(so_entry)
        else:
            so_out.append(_export_vm_start_entry(entry, id_to_name))
    return so_out


def export_topology_to_template(topology: dict, db=None) -> dict:
    """Reverse-map a canvas topology JSONB to a simple infra_template YAML dict."""
    nodes = topology.get("nodes", [])
    edges = topology.get("edges", [])

    # Build set of library item IDs that are actually ISOs (check DB)
    _iso_item_ids = _collect_iso_item_ids(nodes, db)

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
    nic_to_net = _build_nic_to_net_map(edges, net_names)

    # ── Networks ──
    networks = _export_networks(net_nodes, net_names)

    # ── Gateway ──
    gateway = _export_gateway(net_nodes, edges, net_names)

    # ── VMs ──
    vms = {}
    for vm in vm_nodes:
        d = vm.get("data", {})
        name = d.get("name", d.get("label", vm["id"][:8]))
        vms[name] = _export_vm(
            vm, edge_by_target, nodes, net_names, nic_to_net, _iso_item_ids
        )

    result: dict = {"networks": networks}
    if gateway:
        result["gateway"] = gateway
    result["vms"] = vms

    # ── Containers (non-showroom pods; showroom exports via showroom section) ──
    container_nodes = [
        n
        for n in nodes
        if n.get("type") == "containerNode" and not n.get("data", {}).get("isShowroom")
    ]
    showroom_nodes = [
        n
        for n in nodes
        if n.get("type") == "containerNode" and n.get("data", {}).get("isShowroom")
    ]
    if container_nodes:
        all_storage_nodes = {
            n["id"]: n for n in nodes if n.get("type") == "storageNode"
        }
        result["containers"] = _export_containers(
            container_nodes, edges, net_nodes, all_storage_nodes
        )

    # Map node IDs to names for start_order and hidden_nodes
    id_to_name = {}
    for n in nodes:
        d = n.get("data", {})
        id_to_name[n["id"]] = d.get("name", d.get("label", n["id"][:8]))

    so_out = _export_start_order(topology, container_nodes + showroom_nodes, id_to_name)
    if so_out:
        result["start_order"] = so_out

    hidden = topology.get("hiddenNodeIds", [])
    if hidden:
        result["hidden_nodes"] = [id_to_name.get(h, h) for h in hidden]

    from app.services.showroom_scaffold import export_showroom_section

    showroom_export = export_showroom_section(
        topology, showroom_nodes, id_to_name, edges, net_nodes
    )
    if showroom_export:
        result["showroom"] = showroom_export

    return result


def generate_topology_from_template(
    resolved: dict,
    bmc_password: str = "password",
    external_access: bool = False,  # pragma: allowlist secret
) -> dict:
    if not resolved.get("vms") and not resolved.get("containers"):
        showroom = resolved.get("showroom") or {}
        if not showroom.get("enabled"):
            raise ValueError("Template must have a 'vms' or 'containers' section")

    topo = _generate_topology_from_vms(resolved, bmc_password, external_access)
    from app.services.auto_layout import auto_layout

    topo["nodes"], topo["edges"] = auto_layout(topo["nodes"], topo["edges"])
    return topo
