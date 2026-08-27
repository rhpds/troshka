"""Topology helpers for the Troshka KubeVirt operator."""


def _gateway_ip_for_cidr(cidr):
    if not cidr or "/" not in cidr:
        return ""
    octets = cidr.split("/")[0].split(".")
    octets[3] = "1"
    return ".".join(octets)


def _find_gateway_nodes(nodes):
    """Extract gateway node IDs from topology nodes."""
    gateway_nodes = {}
    for node in nodes:
        data = node.get("data", {})
        if data.get("subtype") == "gateway":
            gateway_nodes[node.get("id", data.get("id", ""))] = data
    return gateway_nodes


def _find_networks_with_gateway(edges, gateway_nodes):
    """Find network nodes connected to gateway nodes via edges."""
    networks_with_gateway = set()
    for edge in edges:
        src, tgt = edge.get("source", ""), edge.get("target", "")
        if src in gateway_nodes:
            networks_with_gateway.add(tgt)
        elif tgt in gateway_nodes:
            networks_with_gateway.add(src)
    return networks_with_gateway


def _build_network_entry(data, node, node_id, networks_with_gateway):
    """Build a single network entry from node data."""
    cidr = data.get("cidr", "")
    gateway_ip = data.get("gatewayIp", "")
    if not gateway_ip and cidr:
        gateway_ip = _gateway_ip_for_cidr(cidr)

    dhcp_range = data.get("dhcpRange", "")
    has_gateway = node_id in networks_with_gateway
    external_access = data.get("externalAccess", has_gateway)

    dns_forwarders = data.get("dnsForwarders", [])
    if not dns_forwarders and data.get("dns") and gateway_ip:
        dns_forwarders = [gateway_ip]

    return {
        "id": node_id,
        "label": data.get("label", ""),
        "cidr": cidr,
        "gateway": gateway_ip,
        "dhcpRange": dhcp_range,
        "networkType": data.get("networkType", "standard"),
        "dnsForwarders": dns_forwarders,
        "externalAccess": external_access,
        "pxeConfig": data.get("pxeConfig", {}),
        "dnsRecords": data.get("dnsRecords", []),
        "staticLeases": [],
    }


def extract_networks(topology):
    nodes = topology.get("nodes", [])
    edges = topology.get("edges", [])

    gateway_nodes = _find_gateway_nodes(nodes)
    networks_with_gateway = _find_networks_with_gateway(edges, gateway_nodes)

    networks = []
    for node in nodes:
        data = node.get("data", {})
        if node.get("type") != "networkNode":
            continue
        if data.get("subtype") == "gateway":
            continue
        node_id = data.get("id", node.get("id", ""))
        networks.append(
            _build_network_entry(data, node, node_id, networks_with_gateway)
        )
    return networks


def extract_vms(topology):
    nodes = topology.get("nodes", [])
    vms = []
    for node in nodes:
        data = node.get("data", {})
        if node.get("type") == "vmNode":
            firmware = data.get("firmware", "bios")
            if firmware == "uefi" and data.get("secureBoot"):
                firmware = "uefi-secure"
            vm = {
                "id": data.get("id", node.get("id", "")),
                "name": data.get("label", ""),
                "cpus": data.get("cpus") or data.get("vcpus", 2),
                "memory": data.get("memory") or data.get("ram", 4) * 1024,
                "firmware": firmware,
                "smbiosUuid": data.get("domainUuid", ""),
                "os": data.get("os", ""),
                "powerOnAtDeploy": data.get("powerOnAtDeploy", True),
                "disks": data.get("disks", []),
                "nics": data.get("nics", []),
                "cloudInit": {
                    "userData": data.get("ciGeneratedUserData")
                    or data.get("ciUserData", ""),
                    "networkConfig": data.get("ciNetworkConfig", ""),
                },
                "recertEnabled": data.get("recertEnabled", False),
                "bmcEnabled": data.get("bmcEnabled", False),
                "bmcIp": data.get("bmcIp", ""),
                "bootOrder": data.get("bootDevices", []),
                "videoModel": data.get("videoModel", "virtio"),
                "inputModel": data.get("inputModel", "virtio"),
                "serialModel": data.get("serialModel", "isa"),
                "serialConsole": data.get("serialConsole", True),
                "cdrom": {},
                "guestfishCommands": data.get("guestfishCommands", []),
            }
            if data.get("pxeBootIsoId"):
                vm["cdrom"] = {
                    "libraryIsoId": data.get("pxeBootIsoId", ""),
                    "s3Path": data.get("pxeBootIsoS3Path", ""),
                }
            vms.append(vm)
    return vms


def extract_containers(topology):
    nodes = topology.get("nodes", [])
    containers = []
    for node in nodes:
        data = node.get("data", {})
        if node.get("type") == "containerNode":
            containers.append(
                {
                    "id": data.get("id", node.get("id", "")),
                    "name": data.get("label", ""),
                    "image": data.get("image", ""),
                    "command": data.get("command", ""),
                    "ports": data.get("ports", []),
                    "env": data.get("env", {}),
                    "volumes": data.get("volumes", []),
                    "isPod": data.get("isPod", False),
                    "isShowroom": data.get("isShowroom", False),
                    "infraNetworking": data.get("infraNetworking", False),
                    "initContainers": data.get("initContainers", []),
                    "podContainers": data.get("podContainers", []),
                    "cpus": data.get("cpus", 1),
                    "memory": data.get("memory", 512),
                    "nics": data.get("nics", []),
                    "mounts": data.get("mounts", []),
                }
            )
    return containers


def container_disk_pvc_name(ctr_id, disk_node_id):
    """PVC name for a blank disk attached to a container pod."""
    return f"pod-{ctr_id[:8]}-disk-{disk_node_id[:8]}"


def collect_container_disk_mounts(topology):
    """Return unique (container_id, disk_node_id, size_gb) tuples from mount refs."""
    containers = extract_containers(topology)
    if not containers:
        return []

    node_map = {n["id"]: n for n in topology.get("nodes", [])}
    seen = set()
    mounts = []

    def _add_disk(ctr_id, disk_node_id):
        key = (ctr_id, disk_node_id)
        if key in seen:
            return
        seen.add(key)
        node = node_map.get(disk_node_id, {})
        size = node.get("data", {}).get("size", 5)
        mounts.append((ctr_id, disk_node_id, int(size) if size else 5))

    for ctr in containers:
        ctr_id = ctr["id"]
        for mount in ctr.get("mounts", []):
            if mount.get("diskNodeId"):
                _add_disk(ctr_id, mount["diskNodeId"])
        for ic in ctr.get("initContainers", []):
            for mount in ic.get("mounts", []):
                if mount.get("diskNodeId"):
                    _add_disk(ctr_id, mount["diskNodeId"])
        for pc in ctr.get("podContainers", []):
            for mount in pc.get("mounts", []):
                if mount.get("diskNodeId"):
                    _add_disk(ctr_id, mount["diskNodeId"])
    return mounts


def container_start_delay(topology):
    """Seconds to wait after VMs are ready before starting containers."""
    delay = 0
    for entry in topology.get("startOrder", []):
        if entry.get("entryType") != "container":
            continue
        delay = max(delay, int(entry.get("delaySeconds", 0) or 0))
    return delay


def enrich_container_nics(topology, containers):
    """Attach networkRef and CIDR to container NICs from topology edges."""
    nic_map = resolve_nic_networks(topology)
    net_cidrs = {}
    for node in topology.get("nodes", []):
        if node.get("type") == "networkNode":
            node_id = node.get("id", node.get("data", {}).get("id", ""))
            net_cidrs[f"net-{node_id[:8]}"] = node.get("data", {}).get("cidr", "")

    for ctr in containers:
        for nic in ctr.get("nics", []):
            if not nic.get("networkRef"):
                nic_id = nic.get("id", "")
                nic["networkRef"] = nic_map.get(nic_id, "")
            net_ref = nic.get("networkRef", "")
            if net_ref in net_cidrs:
                nic["cidr"] = net_cidrs[net_ref]


def enrich_vm_nics(topology, spec):
    """Backfill networkRef on VM NICs from topology edges."""
    nic_map = resolve_nic_networks(topology)
    for nic in spec.get("nics", []):
        if not nic.get("networkRef"):
            nic_id = nic.get("id", "")
            ref = nic_map.get(nic_id, "")
            if ref:
                nic["networkRef"] = ref


def collect_vm_nic_network_errors(spec):
    """Fatal errors for VM NICs missing networkRef after topology backfill."""
    errors = []
    for i, nic in enumerate(spec.get("nics", [])):
        if not nic.get("networkRef"):
            nic_id = nic.get("id", f"nic-{i}")
            errors.append(
                f"NIC {nic_id} missing networkRef — cannot attach multus network"
            )
    return errors


def _is_showroom_container(ctr):
    name = (ctr.get("name") or "").strip().lower()
    return ctr.get("isShowroom") or name == "showroom"


def _lab_network_nodes(topology):
    """Lab networks (not gateway/router/LB/BMC) as (node_id, cidr)."""
    nets = []
    for node in topology.get("nodes", []):
        if node.get("type") != "networkNode":
            continue
        data = node.get("data", {})
        subtype = data.get("subtype", "")
        if subtype in ("gateway", "router", "loadbalancer"):
            continue
        if data.get("networkType") == "bmc":
            continue
        node_id = node.get("id", data.get("id", ""))
        if node_id:
            nets.append((node_id, data.get("cidr", "")))
    return nets


def _showroom_ip_for_cidr(cidr, used_ips):
    """Pick a high host IP on the lab subnet for showroom multus (SSH to VMs)."""
    if not cidr or "/" not in cidr:
        return ""
    base = cidr.split("/", 1)[0]
    octets = base.split(".")
    if len(octets) != 4:
        return ""
    for last in range(250, 200, -1):
        ip = f"{octets[0]}.{octets[1]}.{octets[2]}.{last}"
        if ip not in used_ips:
            return ip
    return ""


def _collect_used_ips(topology):
    used = set()
    for node in topology.get("nodes", []):
        if node.get("type") != "vmNode":
            continue
        for nic in node.get("data", {}).get("nics", []):
            ip = nic.get("ip", "")
            if ip:
                used.add(ip)
    return used


def enrich_showroom_infra_networks(topology, containers):
    """KubeVirt: attach showroom pod to all lab NADs so wetty can SSH VM IPs."""
    lab_nets = _lab_network_nodes(topology)
    if not lab_nets:
        return

    used_ips = _collect_used_ips(topology)

    for ctr in containers:
        if not _is_showroom_container(ctr):
            continue
        if not ctr.get("infraNetworking") and ctr.get("nics"):
            continue

        existing_refs = {n.get("networkRef") for n in ctr.get("nics", [])}
        new_nics = list(ctr.get("nics", []))
        for net_id, cidr in lab_nets:
            net_ref = f"net-{net_id[:8]}"
            if net_ref in existing_refs:
                continue
            ip = _showroom_ip_for_cidr(cidr, used_ips)
            if ip:
                used_ips.add(ip)
            new_nics.append(
                {
                    "id": f"infra-{net_id[:8]}",
                    "networkRef": net_ref,
                    "ip": ip,
                    "cidr": cidr,
                    "model": "virtio",
                }
            )
        ctr["nics"] = new_nics


def _extract_nic_id(handle):
    """Extract NIC ID from edge handle like 'nic-nic-UUID-direction'."""
    if not handle or "nic-" not in handle:
        return ""
    for suffix in ("-top", "-bottom", "-left", "-right"):
        if handle.endswith(suffix):
            handle = handle[: -len(suffix)]
            break
    if handle.startswith("nic-"):
        handle = handle[4:]
    if handle.startswith("nic-"):
        return handle
    return f"nic-{handle}" if handle else ""


def resolve_nic_networks(topology):
    """Map NIC IDs to network node IDs by following edges from networkNode → vmNode."""
    edges = topology.get("edges", [])
    nodes = topology.get("nodes", [])

    node_types = {}
    for node in nodes:
        data = node.get("data", {})
        node_id = data.get("id", node.get("id", ""))
        node_types[node_id] = node.get("type")

    nic_to_network = {}

    for edge in edges:
        source = edge.get("source", "")
        target = edge.get("target", "")
        target_handle = edge.get("targetHandle", "")

        if node_types.get(source) == "networkNode" and node_types.get(target) in (
            "vmNode",
            "containerNode",
        ):
            nic_id = _extract_nic_id(target_handle)
            if nic_id:
                nic_to_network[nic_id] = f"net-{source[:8]}"
        elif node_types.get(target) == "networkNode" and node_types.get(source) in (
            "vmNode",
            "containerNode",
        ):
            source_handle = edge.get("sourceHandle", "")
            nic_id = _extract_nic_id(source_handle)
            if nic_id:
                nic_to_network[nic_id] = f"net-{target[:8]}"

    return nic_to_network


def _find_storage_vm_pair(edge, nodes_by_id):
    """Given an edge and node map, return (storage_id, vm_id) or (None, None)."""
    source = edge.get("source", "")
    target = edge.get("target", "")
    source_node = nodes_by_id.get(source, {})
    target_node = nodes_by_id.get(target, {})

    if source_node.get("type") == "storageNode" and target_node.get("type") == "vmNode":
        return source, target
    if target_node.get("type") == "storageNode" and source_node.get("type") == "vmNode":
        return target, source
    return None, None


def _normalize_disk_port_id(handle):
    """Map a React Flow disk port handle to diskControllers[].id."""
    if not handle or not str(handle).startswith("dp-"):
        return None
    body = handle[3:]
    if body.endswith("-left"):
        body = body[:-5]
    elif body.endswith("-right"):
        body = body[:-6]
    if body and not body.startswith("dp-"):
        return f"dp-{body}"
    return body or None


def _extract_disk_edge(edge, vm_node_id):
    """Return (disk_port_handle, storage_node_id) for a VM storage edge."""
    if edge.get("source") == vm_node_id:
        return edge.get("sourceHandle", ""), edge.get("target")
    if edge.get("target") == vm_node_id:
        return edge.get("targetHandle", ""), edge.get("source")
    return None, None


def _resolve_disk_bus(vm_node_id, handle, nodes_by_id):
    """Resolve disk bus and rotation rate from a VM disk controller port."""
    vm_node = nodes_by_id.get(vm_node_id)
    port_id = _normalize_disk_port_id(handle)
    if not vm_node or not port_id:
        return "virtio", None
    for dc in vm_node.get("data", {}).get("diskControllers", []):
        if dc.get("id") == port_id:
            return dc.get("bus", "virtio"), dc.get("rotationRate")
    return "virtio", None


def _apply_pattern_disk_source(disk: dict, sd: dict, central: bool) -> None:
    pattern_id = sd.get("patternId", "")
    disk_id = sd.get("patternDiskId", "")
    resolved_path = sd.get("resolvedS3Path", "")
    if pattern_id and (disk_id or resolved_path):
        disk["patternImage"] = {
            "s3Path": resolved_path or f"patterns/{pattern_id}/{disk_id}.qcow2",
            "format": "qcow2",
            "central": central,
            "source": sd.get("diskSource", "central"),
        }


def _apply_library_disk_source(disk: dict, sd: dict, fmt: str, central: bool) -> None:
    lib_id = sd.get("libraryItemId", "")
    resolved = sd.get("resolvedS3Path", "")
    if lib_id or resolved:
        disk["libraryImage"] = {
            "s3Path": resolved or f"library/{lib_id}.{fmt}",
            "format": fmt,
            "central": central,
        }


def _apply_snapshot_disk_source(disk: dict, sd: dict, fmt: str, central: bool) -> None:
    if sd.get("resolvedS3Path"):
        disk["libraryImage"] = {
            "s3Path": sd["resolvedS3Path"],
            "format": fmt,
            "central": central,
        }


def _build_disk_from_storage(sd, storage_id, bus="virtio", rotation_rate=None):
    """Build a disk dict from storage node data with pattern/library/blank source."""
    fmt = sd.get("format", "qcow2")
    size_gb = sd.get("size", sd.get("sizeGb", 20))
    source_type = sd.get("source", "")
    central = sd.get("centralSource", False)

    if fmt == "iso":
        resolved = sd.get("resolvedS3Path", "")
        return {
            "cdrom": {
                "libraryIsoId": sd.get("libraryItemId", ""),
                "s3Path": resolved or f"library/{sd.get('libraryItemId', '')}.iso",
                "central": central,
            }
        }

    disk = {
        "id": storage_id,
        "sizeGb": int(size_gb) if size_gb else 20,
        "bus": bus,
        "format": fmt,
    }
    if rotation_rate is not None:
        disk["rotationRate"] = rotation_rate

    if source_type == "pattern":
        _apply_pattern_disk_source(disk, sd, central)
    elif source_type == "library":
        _apply_library_disk_source(disk, sd, fmt, central)
    elif source_type == "snapshot":
        if sd.get("resolvedS3Path"):
            _apply_snapshot_disk_source(disk, sd, fmt, central)
        else:
            disk["blank"] = True
    else:
        disk["blank"] = True

    return {"disk": disk}


def resolve_vm_disks(topology):
    """Resolve disks for each VM by following edges from storageNode → vmNode."""
    nodes = topology.get("nodes", [])
    edges = topology.get("edges", [])

    nodes_by_id = {n.get("id"): n for n in nodes}

    vm_disks = {}
    vm_cdroms = {}

    for edge in edges:
        storage_id, vm_id = _find_storage_vm_pair(edge, nodes_by_id)
        if not storage_id or not vm_id:
            continue

        handle, _ = _extract_disk_edge(edge, vm_id)
        bus, rotation_rate = _resolve_disk_bus(vm_id, handle, nodes_by_id)
        sd = nodes_by_id[storage_id].get("data", {})
        result = _build_disk_from_storage(
            sd, storage_id, bus=bus, rotation_rate=rotation_rate
        )

        if "cdrom" in result:
            vm_cdroms[vm_id] = result["cdrom"]
        else:
            if vm_id not in vm_disks:
                vm_disks[vm_id] = []
            vm_disks[vm_id].append(result["disk"])

    return vm_disks, vm_cdroms


def extract_start_order(topology):
    nodes = topology.get("nodes", [])
    for node in nodes:
        data = node.get("data", {})
        so = data.get("startOrder", [])
        if so:
            return so
    vms = extract_vms(topology)
    return [{"vmId": vm["id"]} for vm in vms]


def _build_node_map(nodes):
    """Build a map of node ID to node data."""
    node_map = {}
    for node in nodes:
        data = node.get("data", {})
        node_id = data.get("id", node.get("id", ""))
        node_map[node_id] = data
    return node_map


def _find_vm_and_network_from_edge(edge, node_map):
    """Extract VM data, network ID, and NIC ID from an edge."""
    source = edge.get("source", "")
    target = edge.get("target", "")
    source_handle = edge.get("sourceHandle", "")

    source_data = node_map.get(source, {})
    target_data = node_map.get(target, {})

    if source_data.get("nics"):
        return source_data, target, _extract_nic_id(source_handle)
    if target_data.get("nics"):
        return target_data, source, _extract_nic_id(edge.get("targetHandle", ""))
    return None, None, None


def _add_lease_for_nic(vm_data, nic_id, net_id, network_leases):
    """Add a static lease to network_leases if NIC matches."""
    for nic in vm_data.get("nics", []):
        if nic.get("id") != nic_id:
            continue
        mac = nic.get("mac", "")
        ip = nic.get("ip", "")
        if not mac or not ip:
            continue
        if net_id not in network_leases:
            network_leases[net_id] = []
        network_leases[net_id].append(
            {
                "mac": mac,
                "ip": ip,
                "hostname": vm_data.get("label", ""),
            }
        )


def build_static_leases(topology):
    edges = topology.get("edges", [])
    nodes = topology.get("nodes", [])

    node_map = _build_node_map(nodes)
    network_leases = {}

    for edge in edges:
        vm_data, net_id, nic_id = _find_vm_and_network_from_edge(edge, node_map)
        if vm_data and net_id and nic_id:
            _add_lease_for_nic(vm_data, nic_id, net_id, network_leases)

    return network_leases
