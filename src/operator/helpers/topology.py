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
            vm = {
                "id": data.get("id", node.get("id", "")),
                "name": data.get("label", ""),
                "cpus": data.get("cpus") or data.get("vcpus", 2),
                "memory": data.get("memory") or data.get("ram", 4) * 1024,
                "firmware": data.get("firmware", "bios"),
                "machineType": data.get("machineType", "q35"),
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
                    "initContainers": data.get("initContainers", []),
                    "podContainers": data.get("podContainers", []),
                    "cpus": data.get("cpus", 1),
                    "memory": data.get("memory", 512),
                    "nics": data.get("nics", []),
                }
            )
    return containers


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

        if (
            node_types.get(source) == "networkNode"
            and node_types.get(target) == "vmNode"
        ):
            nic_id = _extract_nic_id(target_handle)
            if nic_id:
                nic_to_network[nic_id] = f"net-{source[:8]}"
        elif (
            node_types.get(target) == "networkNode"
            and node_types.get(source) == "vmNode"
        ):
            source_handle = edge.get("sourceHandle", "")
            nic_id = _extract_nic_id(source_handle)
            if nic_id:
                nic_to_network[nic_id] = f"net-{target[:8]}"

    return nic_to_network


def _find_storage_vm_pair(edge, node_map):
    """Given an edge and node_map, return (storage_id, vm_id) or (None, None)."""
    source = edge.get("source", "")
    target = edge.get("target", "")
    source_info = node_map.get(source, {})
    target_info = node_map.get(target, {})

    if source_info.get("type") == "storageNode" and target_info.get("type") == "vmNode":
        return source, target
    if target_info.get("type") == "storageNode" and source_info.get("type") == "vmNode":
        return target, source
    return None, None


def _build_disk_from_storage(sd, storage_id):
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
        "bus": "virtio",
        "format": fmt,
    }

    if source_type == "pattern":
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
    elif source_type == "library":
        lib_id = sd.get("libraryItemId", "")
        resolved = sd.get("resolvedS3Path", "")
        if lib_id or resolved:
            disk["libraryImage"] = {
                "s3Path": resolved or f"library/{lib_id}.{fmt}",
                "format": fmt,
                "central": central,
            }
    elif source_type == "snapshot" and sd.get("resolvedS3Path"):
        # Snapshot data disks clone from their resolved key. No fabricated
        # fallback — an unresolved snapshot must not guess a library path.
        disk["libraryImage"] = {
            "s3Path": sd["resolvedS3Path"],
            "format": fmt,
            "central": central,
        }
    else:
        disk["blank"] = True

    return {"disk": disk}


def resolve_vm_disks(topology):
    """Resolve disks for each VM by following edges from storageNode → vmNode."""
    nodes = topology.get("nodes", [])
    edges = topology.get("edges", [])

    node_map = {}
    for node in nodes:
        data = node.get("data", {})
        node_id = data.get("id", node.get("id", ""))
        node_map[node_id] = {"type": node.get("type"), "data": data}

    vm_disks = {}
    vm_cdroms = {}

    for edge in edges:
        storage_id, vm_id = _find_storage_vm_pair(edge, node_map)
        if not storage_id or not vm_id:
            continue

        sd = node_map[storage_id]["data"]
        result = _build_disk_from_storage(sd, storage_id)

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
