def extract_networks(topology):
    nodes = topology.get("nodes", [])
    networks = []
    for node in nodes:
        data = node.get("data", {})
        if node.get("type") == "networkNode":
            networks.append(
                {
                    "id": data.get("id", node.get("id", "")),
                    "label": data.get("label", ""),
                    "cidr": data.get("cidr", ""),
                    "gateway": data.get("gatewayIp", ""),
                    "dhcpRange": data.get("dhcpRange", ""),
                    "networkType": data.get("networkType", "standard"),
                    "dnsForwarders": data.get("dnsForwarders", []),
                    "externalAccess": data.get("externalAccess", False),
                    "pxeConfig": data.get("pxeConfig", {}),
                    "staticLeases": [],
                }
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
                "cpus": data.get("cpus", 2),
                "memory": data.get("memory", 4096),
                "firmware": data.get("firmware", "bios"),
                "machineType": data.get("machineType", "q35"),
                "smbiosUuid": data.get("domainUuid", ""),
                "powerOnAtDeploy": data.get("powerOnAtDeploy", True),
                "disks": data.get("disks", []),
                "nics": data.get("nics", []),
                "cloudInit": {
                    "userData": data.get("ciUserData", ""),
                    "networkConfig": data.get("ciNetworkConfig", ""),
                },
                "bmcEnabled": data.get("bmcEnabled", False),
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

        if node_types.get(source) == "networkNode" and node_types.get(target) == "vmNode":
            nic_id = _extract_nic_id(target_handle)
            if nic_id:
                nic_to_network[nic_id] = f"net-{source[:8]}"
        elif node_types.get(target) == "networkNode" and node_types.get(source) == "vmNode":
            source_handle = edge.get("sourceHandle", "")
            nic_id = _extract_nic_id(source_handle)
            if nic_id:
                nic_to_network[nic_id] = f"net-{target[:8]}"

    return nic_to_network


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
        source = edge.get("source", "")
        target = edge.get("target", "")

        source_info = node_map.get(source, {})
        target_info = node_map.get(target, {})

        storage_id = None
        vm_id = None
        if source_info.get("type") == "storageNode" and target_info.get("type") == "vmNode":
            storage_id = source
            vm_id = target
        elif target_info.get("type") == "storageNode" and source_info.get("type") == "vmNode":
            storage_id = target
            vm_id = source

        if not storage_id or not vm_id:
            continue

        sd = node_map[storage_id]["data"]
        fmt = sd.get("format", "qcow2")
        size_gb = sd.get("size", sd.get("sizeGb", 20))
        source_type = sd.get("source", "")

        if fmt == "iso":
            cdrom = {"libraryIsoId": sd.get("libraryItemId", ""), "s3Path": ""}
            if cdrom["libraryIsoId"]:
                cdrom["s3Path"] = f"library/{cdrom['libraryIsoId']}.iso"
            vm_cdroms[vm_id] = cdrom
            continue

        disk = {
            "id": storage_id,
            "sizeGb": int(size_gb) if size_gb else 20,
            "bus": "virtio",
            "format": fmt,
        }

        presigned_url = sd.get("presignedUrl", "")

        if source_type == "pattern":
            pattern_id = sd.get("patternId", "")
            disk_id = sd.get("patternDiskId", "")
            resolved_path = sd.get("resolvedS3Path", "")
            if pattern_id and (disk_id or resolved_path):
                disk["patternImage"] = {
                    "s3Path": resolved_path or f"patterns/{pattern_id}/{disk_id}.qcow2",
                    "format": "qcow2",
                }
                if presigned_url:
                    disk["patternImage"]["presignedUrl"] = presigned_url
        elif source_type == "library":
            lib_id = sd.get("libraryItemId", "")
            if lib_id:
                disk["libraryImage"] = {
                    "s3Path": f"library/{lib_id}.{fmt}",
                    "format": fmt,
                }
                if presigned_url:
                    disk["libraryImage"]["presignedUrl"] = presigned_url
        else:
            disk["blank"] = True

        if vm_id not in vm_disks:
            vm_disks[vm_id] = []
        vm_disks[vm_id].append(disk)

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


def build_static_leases(topology):
    edges = topology.get("edges", [])
    nodes = topology.get("nodes", [])

    node_map = {}
    for node in nodes:
        data = node.get("data", {})
        node_id = data.get("id", node.get("id", ""))
        node_map[node_id] = data

    network_leases = {}

    for edge in edges:
        source = edge.get("source", "")
        target = edge.get("target", "")
        source_handle = edge.get("sourceHandle", "")

        source_data = node_map.get(source, {})
        target_data = node_map.get(target, {})

        vm_data = None
        net_id = None
        nic_id = None

        if source_data.get("nics"):
            vm_data = source_data
            net_id = target
            nic_id = source_handle
        elif target_data.get("nics"):
            vm_data = target_data
            net_id = source
            nic_id = edge.get("targetHandle", "")

        if vm_data and net_id and nic_id:
            for nic in vm_data.get("nics", []):
                if nic.get("id") == nic_id:
                    mac = nic.get("mac", "")
                    ip = nic.get("ip", "")
                    hostname = vm_data.get("label", "")
                    if mac and ip:
                        if net_id not in network_leases:
                            network_leases[net_id] = []
                        network_leases[net_id].append(
                            {
                                "mac": mac,
                                "ip": ip,
                                "hostname": hostname,
                            }
                        )

    return network_leases
