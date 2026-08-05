"""Topology helpers — pure functions for parsing, validating, and querying canvas topology JSONB.

Extracted from deploy_service.py for testability. No DB, no troshkad, no Redis dependencies.
"""

import ipaddress
import logging
import os
import random

logger = logging.getLogger(__name__)


def validate_topology_names(topology: dict) -> list[str]:
    """Check for duplicate node names within a topology. Returns list of errors."""
    errors = []
    seen: dict[str, dict[str, str]] = {"vm": {}, "network": {}, "storage": {}}
    type_labels = {"vm": "VM", "network": "Network", "storage": "Disk"}
    for node in topology.get("nodes", []):
        data = node.get("data", {})
        name = data.get("name") or data.get("label", "")
        if not name:
            continue
        if node.get("type") == "vmNode":
            bucket = "vm"
        elif node.get("type") == "networkNode":
            bucket = "network"
        elif node.get("type") == "storageNode":
            bucket = "storage"
        else:
            continue
        if name in seen[bucket]:
            errors.append(f"Duplicate {type_labels[bucket]} name: '{name}'")
        else:
            seen[bucket][name] = node["id"]
    return errors


def validate_topology_ips(topology: dict) -> list[str]:
    """Check for duplicate IP addresses on the same network. Returns list of errors."""
    errors = []
    nodes_by_id: dict[str, dict] = {n["id"]: n for n in topology.get("nodes", [])}

    nic_to_network: dict[str, str] = {}
    for edge in topology.get("edges", []):
        src = edge.get("source", "")
        tgt = edge.get("target", "")
        for handle_key, net_id, vm_id in [
            ("targetHandle", src, tgt),
            ("sourceHandle", tgt, src),
        ]:
            handle = edge.get(handle_key, "")
            if (
                nodes_by_id.get(net_id, {}).get("type") == "networkNode"
                and nodes_by_id.get(vm_id, {}).get("type")
                in ("vmNode", "containerNode")
                and "nic-" in handle
            ):
                raw = handle.replace("-top", "").replace("-bottom", "")
                if raw.startswith("nic-"):
                    nic_id = raw[4:]  # strip handle "nic-" wrapper
                    nic_to_network[nic_id] = net_id

    per_network: dict[str, dict[str, str]] = {}
    for node in topology.get("nodes", []):
        if node.get("type") not in ("vmNode", "containerNode"):
            continue
        vm_name = node.get("data", {}).get("name", "?")
        for nic in node.get("data", {}).get("nics", []):
            ip = nic.get("ip", "")
            if not ip:
                continue
            net_id = nic_to_network.get(nic["id"], "unconnected")
            net_name = (
                nodes_by_id.get(net_id, {}).get("data", {}).get("name", "unconnected")
            )
            if net_id not in per_network:
                per_network[net_id] = {}
            if ip in per_network[net_id]:
                other_vm = per_network[net_id][ip]
                errors.append(
                    f"Duplicate IP {ip} on network '{net_name}': "
                    f"used by both '{other_vm}' and '{vm_name}'"
                )
            else:
                per_network[net_id][ip] = vm_name
    return errors


def validate_topology_passwords(topology: dict) -> list[str]:
    """Check that required passwords are set. Returns list of errors."""
    errors = []
    for node in topology.get("nodes", []):
        data = node.get("data", {})
        if node.get("type") == "networkNode" and data.get("networkType") == "bmc":
            if not data.get("bmcPassword"):
                errors.append(
                    f"BMC network '{data.get('name', '?')}' has no password set"
                )
    return errors


def _filter_topology_for_host(topology: dict, vm_node_ids: set[str]) -> dict:
    """Return a copy of topology with only the specified VM nodes."""
    filtered_nodes = [
        n
        for n in topology.get("nodes", [])
        if n.get("type") != "vmNode" or n["id"] in vm_node_ids
    ]
    start_order = [
        e for e in topology.get("startOrder", []) if e.get("vmId") in vm_node_ids
    ]
    return {**topology, "nodes": filtered_nodes, "startOrder": start_order}


def _extract_vms(topology: dict) -> list[dict]:
    """Extract VM nodes with their properties."""
    vms = []
    for node in topology.get("nodes", []):
        if node.get("type") != "vmNode":
            continue
        data = node.get("data", {})
        vms.append(
            {
                "node_id": node["id"],
                "name": data.get("name", "vm"),
                "vcpus": data.get("vcpus", 2),
                "ram_gb": data.get("ram", 4),
                "os": data.get("os", ""),
                "nics": data.get("nics", []),
                "disk_controllers": data.get("diskControllers", []),
                "boot_devices": data.get("bootDevices", ["hd"]),
                "cloud_init": data.get("cloudInit", False),
                "firmware": data.get("firmware", "bios"),
                "secure_boot": data.get("secureBoot", False),
                "video_model": data.get("videoModel", "virtio"),
                "input_model": data.get("inputModel", "virtio"),
                "uuid": data.get("smbiosUuid") or data.get("uuid"),
                "recertEnabled": data.get("recertEnabled", False),
                "ocpMonitor": data.get("ocpMonitor", False),
                "configureBastionBrowser": data.get("configureBastionBrowser", False),
            }
        )
    return vms


def _extract_containers(topology: dict) -> list[dict]:
    """Extract container nodes with their properties."""
    containers = []
    for node in topology.get("nodes", []):
        if node.get("type") != "containerNode":
            continue
        data = node.get("data", {})
        containers.append(
            {
                "node_id": node["id"],
                "name": data.get("name", "container"),
                "image": data.get("image", ""),
                "registry_credential_id": data.get("registryCredentialId"),
                "registry_credential_name": data.get("registryCredentialName"),
                "cpus": data.get("cpus", 1),
                "memory_mb": data.get("memory", 512),
                "nics": data.get("nics", []),
                "env_vars": data.get("envVars", []),
                "ports": data.get("ports", []),
                "command": data.get("command"),
                "restart_policy": data.get("restartPolicy", "always"),
                "privileged": data.get("privileged", False),
                "mounts": data.get("mounts", []),
                "is_pod": data.get("isPod", False),
                "init_containers": data.get("initContainers", []),
                "pod_containers": data.get("podContainers", []),
            }
        )
    return containers


def _find_vm_networks(
    vm_node_id: str, topology: dict, vni_map: dict, project_id: str = ""
) -> list[dict]:
    """Find networks connected to a VM via NIC handles."""
    edges = topology.get("edges", [])
    nodes = topology.get("nodes", [])
    networks = []

    for edge in edges:
        handle = None
        network_node_id = None

        if edge.get("source") == vm_node_id:
            handle = edge.get("sourceHandle", "")
            network_node_id = edge.get("target")
        elif edge.get("target") == vm_node_id:
            handle = edge.get("targetHandle", "")
            network_node_id = edge.get("source")
        else:
            continue

        if not handle or not handle.startswith("nic-"):
            continue

        # Find the NIC data to get MAC address and model
        # Handle format: "nic-{nicId}-top" or "nic-{nicId}-bottom"
        vm_node = next((n for n in nodes if n["id"] == vm_node_id), None)
        mac = ""
        model = "virtio"
        if vm_node:
            for nic in vm_node.get("data", {}).get("nics", []):
                if nic["id"] in handle:
                    mac = nic.get("mac", "")
                    model = nic.get("model", "virtio")
                    break

        # BMC networks use a dedicated bridge (no VNI)
        net_node = next((n for n in nodes if n["id"] == network_node_id), None)
        if net_node and net_node.get("data", {}).get("networkType") == "bmc":
            # Use the NIC's MAC from the edge handle, otherwise generate one
            bmc_mac = mac  # mac was already resolved from the handle above
            if not bmc_mac:
                bmc_mac = "52:54:01:%02x:%02x:%02x" % (
                    random.randint(0, 255),
                    random.randint(0, 255),
                    random.randint(0, 255),
                )
            networks.append(
                {
                    "bridge": f"br-bmc-{project_id[:8]}",
                    "mac": bmc_mac,
                    "nic_id": handle,
                    "model": model,
                }
            )
            continue

        if network_node_id not in vni_map:
            continue

        vni = vni_map[network_node_id]
        networks.append(
            {
                "bridge": f"br-{vni}",
                "mac": mac,
                "nic_id": handle,
                "model": model,
            }
        )

    return networks


def _find_container_networks(
    container_node_id: str, topology: dict, vni_map: dict, project_id: str = ""
) -> list[dict]:
    """Find networks connected to a container via NIC handles."""
    results: list[dict] = []
    container_node = next(
        (n for n in topology.get("nodes", []) if n["id"] == container_node_id), None
    )
    if not container_node:
        return results

    nics_by_id = {
        nic["id"]: nic for nic in container_node.get("data", {}).get("nics", [])
    }

    for edge in topology.get("edges", []):
        src, tgt = edge.get("source"), edge.get("target")
        src_h, tgt_h = edge.get("sourceHandle", ""), edge.get("targetHandle", "")

        nic_id = None
        net_node_id = None
        if src == container_node_id and src_h.startswith("nic-"):
            nic_id = src_h.split("-", 1)[1].rsplit("-", 1)[0]
            net_node_id = tgt
        elif tgt == container_node_id and tgt_h.startswith("nic-"):
            nic_id = tgt_h.split("-", 1)[1].rsplit("-", 1)[0]
            net_node_id = src

        if not nic_id or not net_node_id:
            continue

        nic = nics_by_id.get(nic_id, {})
        vni = vni_map.get(net_node_id)
        if not vni:
            continue

        net_node = next(
            (n for n in topology.get("nodes", []) if n["id"] == net_node_id), None
        )
        cidr = net_node.get("data", {}).get("cidr", "") if net_node else ""

        results.append(
            {
                "bridge": f"br-{vni}",
                "mac": nic.get("mac", ""),
                "nic_id": nic_id,
                "model": nic.get("model", "virtio"),
                "ip": nic.get("ip", ""),
                "cidr": cidr,
            }
        )

    return results


def _find_vm_name_by_ip(topology, ip):
    """Find the VM name that has a NIC with the given IP address."""
    for node in topology.get("nodes", []):
        if node.get("type") != "vmNode":
            continue
        data = node.get("data", {})
        for nic in data.get("nics", []):
            if nic.get("ip") == ip:
                return data.get("name", node["id"][:8])
    return ip.replace(".", "-")


def _find_vm_disks(vm_node_id: str, topology: dict) -> list[dict]:
    """Find storage nodes connected to a VM via disk controller handles."""
    edges = topology.get("edges", [])
    nodes = topology.get("nodes", [])
    disks = []

    for edge in edges:
        handle = None
        storage_node_id = None

        if edge.get("source") == vm_node_id:
            handle = edge.get("sourceHandle", "")
            storage_node_id = edge.get("target")
        elif edge.get("target") == vm_node_id:
            handle = edge.get("targetHandle", "")
            storage_node_id = edge.get("source")
        else:
            continue

        if not handle or not handle.startswith("dp-"):
            continue

        storage_node = next(
            (
                n
                for n in nodes
                if n["id"] == storage_node_id and n.get("type") == "storageNode"
            ),
            None,
        )
        if not storage_node:
            continue

        sdata = storage_node.get("data", {})

        # Find bus type and rotation_rate from the disk controller
        vm_node = next((n for n in nodes if n["id"] == vm_node_id), None)
        bus = "virtio"
        rotation_rate = None
        if vm_node:
            for dc in vm_node.get("data", {}).get("diskControllers", []):
                if dc["id"] == handle:
                    bus = dc.get("bus", "virtio")
                    rotation_rate = dc.get("rotationRate")
                    break

        disk_entry = {
            "node_id": storage_node_id,
            "name": sdata.get("name", "disk"),
            "size_gb": sdata.get("size", 10),
            "format": sdata.get("format", "qcow2"),
            "bus": bus,
            "source": sdata.get("source", "blank"),
            "library_item_id": sdata.get("libraryItemId"),
            "patternId": sdata.get("patternId"),
            "patternDiskId": sdata.get("patternDiskId"),
            "snapshotItemId": sdata.get("snapshotItemId"),
        }
        if rotation_rate is not None:
            disk_entry["rotation_rate"] = rotation_rate
        disks.append(disk_entry)

    return disks


def _find_container_volumes(
    container_node_id: str, topology: dict, project_id: str, pool=None
) -> list[dict]:
    """Find storage nodes connected to a container via mount handles."""
    container_node = next(
        (n for n in topology.get("nodes", []) if n["id"] == container_node_id), None
    )
    if not container_node:
        return []

    mounts = container_node.get("data", {}).get("mounts", [])
    mounts_by_disk = {m["diskNodeId"]: m for m in mounts}

    results = []
    for edge in topology.get("edges", []):
        src, tgt = edge.get("source"), edge.get("target")
        src_h, tgt_h = edge.get("sourceHandle", ""), edge.get("targetHandle", "")

        disk_node_id = None
        if src == container_node_id and (tgt_h or "").startswith("mnt-"):
            disk_node_id = tgt
        elif tgt == container_node_id and (src_h or "").startswith("mnt-"):
            disk_node_id = src
        elif tgt == container_node_id and (tgt_h or "").startswith("mnt-"):
            disk_node_id = src
        elif src == container_node_id and (src_h or "").startswith("mnt-"):
            disk_node_id = tgt

        if not disk_node_id:
            continue

        disk_node = next(
            (
                n
                for n in topology.get("nodes", [])
                if n["id"] == disk_node_id and n.get("type") == "storageNode"
            ),
            None,
        )
        if not disk_node:
            continue

        mount_info = mounts_by_disk.get(disk_node_id, {})
        dd = disk_node.get("data", {})
        disk_path = _disk_path(project_id, container_node_id, disk_node_id, "raw", pool)
        mount_dir = os.path.join(_vm_dir(project_id, pool), f"mnt-{disk_node_id[:8]}")
        results.append(
            {
                "disk_path": disk_path,
                "mount_dir": mount_dir,
                "mount_path": mount_info.get("mountPath", "/data"),
                "size_gb": dd.get("size", 10),
                "node_id": disk_node_id,
            }
        )

    return results


def _vm_domain_name(project_id: str, node_id: str) -> str:
    return f"troshka-{project_id[:8]}-{node_id[:8]}"


def _extract_bmc_config(topology: dict, project_id: str) -> dict | None:
    """Extract BMC configuration from topology if any VMs have BMC enabled."""
    bmc_network = None
    for node in topology.get("nodes", []):
        if (
            node.get("type") == "networkNode"
            and node.get("data", {}).get("networkType") == "bmc"
        ):
            bmc_network = node
            break

    if not bmc_network:
        return None

    bmc_vms = []
    for node in topology.get("nodes", []):
        if node.get("type") == "vmNode" and node.get("data", {}).get("bmcEnabled"):
            bmc_ip = node["data"].get("bmcIp", "")
            if bmc_ip:
                bmc_vms.append(
                    {
                        "node_id": node["id"],
                        "domain_name": _vm_domain_name(project_id, node["id"]),
                        "bmc_ip": bmc_ip,
                    }
                )

    if not bmc_vms:
        return None

    # Collect DHCP hosts — VMs with a static IP on their BMC NIC
    dhcp_hosts = []
    bmc_net_id = bmc_network["id"]
    edges = topology.get("edges", [])
    nodes = topology.get("nodes", [])
    for node in nodes:
        if node.get("type") != "vmNode":
            continue
        for edge in edges:
            vm_id = node["id"]
            if edge.get("source") == vm_id:
                handle = edge.get("sourceHandle", "")
                net_id = edge.get("target")
            elif edge.get("target") == vm_id:
                handle = edge.get("targetHandle", "")
                net_id = edge.get("source")
            else:
                continue
            if net_id != bmc_net_id or not handle.startswith("nic-"):
                continue
            for nic in node.get("data", {}).get("nics", []):
                if nic["id"] in handle and nic.get("ip") and nic.get("mac"):
                    dhcp_hosts.append(
                        {
                            "mac": nic["mac"],
                            "ip": nic["ip"],
                            "name": node["data"].get("name", ""),
                        }
                    )

    return {
        "bmc_network": bmc_network["data"],
        "vms": bmc_vms,
        "dhcp_hosts": dhcp_hosts,
    }


def _vm_dir(project_id: str, pool=None) -> str:
    if pool and pool.mode.startswith("shared"):
        return f"/var/lib/troshka/shared/vms/{project_id}"
    return f"/var/lib/troshka/vms/{project_id}"


def _disk_path(
    project_id: str, vm_node_id: str, disk_node_id: str, fmt: str, pool=None
) -> str:
    return f"{_vm_dir(project_id, pool)}/{vm_node_id[:8]}-{disk_node_id[:8]}.{fmt}"


def _seed_path(project_id: str, vm_node_id: str, pool=None) -> str:
    return f"{_vm_dir(project_id, pool)}/{vm_node_id[:8]}-seed.iso"


def _image_cache_path(item_id: str, fmt: str, pool=None) -> str:
    if pool and pool.mode.startswith("shared"):
        return f"/var/lib/troshka/shared/images/{item_id}.{fmt}"
    return f"/var/lib/troshka/images/{item_id}.{fmt}"


def _pattern_cache_path(pattern_id: str, disk_id: str, fmt: str, pool=None) -> str:
    return f"/var/lib/troshka/local/cache/patterns/{pattern_id}/{disk_id}.{fmt}"


def _snapshot_cache_path(item_id: str, disk_id: str, fmt: str) -> str:
    return f"/var/lib/troshka/cache/snapshots/{item_id}/{disk_id}.{fmt}"


def _resolve_boot_devs(vm: dict, vm_disks: list[dict], topology: dict) -> list[str]:
    boot_type_map = {"hd": "hd", "disk": "hd", "network": "network", "cdrom": "cdrom"}
    all_nodes = topology.get("nodes", [])
    storage_nodes = {n["id"]: n for n in all_nodes if n.get("type") == "storageNode"}

    raw_boot_devs = vm.get("boot_devices") or None
    has_iso = any(d["format"] == "iso" for d in vm_disks)
    has_disk = any(d["format"] != "iso" for d in vm_disks)
    has_cdrom_controller = any(
        dc.get("bus") == "sata" and "cdrom" in dc.get("name", "")
        for dc in vm.get("disk_controllers", [])
    )
    if raw_boot_devs is None or (raw_boot_devs == ["hd"] and has_iso):
        if has_iso and has_disk:
            return ["cdrom", "hd"]
        elif has_iso:
            return ["cdrom"]
        elif has_disk:
            return ["hd"]
        else:
            return ["network"]
    boot_devs = []
    seen = set()
    for d in raw_boot_devs:
        if d in boot_type_map:
            dev = boot_type_map[d]
        elif d in storage_nodes:
            dev = (
                "cdrom"
                if storage_nodes[d].get("data", {}).get("format") == "iso"
                else "hd"
            )
        else:
            continue
        if dev not in seen:
            boot_devs.append(dev)
            seen.add(dev)
    # Add cdrom fallback if VM has a cdrom controller but no cdrom in boot order
    if has_cdrom_controller and "cdrom" not in seen:
        boot_devs.append("cdrom")
    return boot_devs or ["hd"]


def diff_topologies(current: dict, deployed: dict) -> dict:
    """Diff current topology against what was deployed. Returns changes."""
    cur_nodes = {n["id"]: n for n in current.get("nodes", [])}
    dep_nodes = {n["id"]: n for n in deployed.get("nodes", [])}

    added_vms = []
    removed_vms = []
    changed_vms = []
    added_networks = []
    removed_networks = []

    for nid, node in cur_nodes.items():
        if nid not in dep_nodes:
            if node.get("type") == "vmNode":
                added_vms.append(node)
            elif node.get("type") == "networkNode":
                added_networks.append(node)

    for nid, node in dep_nodes.items():
        if nid not in cur_nodes:
            if node.get("type") == "vmNode":
                removed_vms.append(node)
            elif node.get("type") == "networkNode":
                removed_networks.append(node)

    skip_keys = {"status", "redeployStep", "redeployDetail", "liveBootDevs"}
    for nid, node in cur_nodes.items():
        if nid in dep_nodes and node.get("type") == "vmNode":
            cur_data = {
                k: v for k, v in node.get("data", {}).items() if k not in skip_keys
            }
            dep_data = {
                k: v
                for k, v in dep_nodes[nid].get("data", {}).items()
                if k not in skip_keys
            }
            if cur_data != dep_data:
                changed_vms.append(node)

    return {
        "added_vms": added_vms,
        "removed_vms": removed_vms,
        "changed_vms": changed_vms,
        "added_networks": added_networks,
        "removed_networks": removed_networks,
        "has_changes": bool(
            added_vms
            or removed_vms
            or changed_vms
            or added_networks
            or removed_networks
        ),
    }


def _auto_assign_container_ips(topology: dict) -> None:
    """Assign IPs to container NICs that don't have static IPs.

    Mutates topology in-place. Picks IPs from the connected network's CIDR,
    avoiding all IPs already used by VMs or other containers.
    """
    nodes = topology.get("nodes", [])
    edges = topology.get("edges", [])
    used_ips = _collect_used_ips(topology)

    # Also reserve .1 (gateway) and DHCP range for each network
    net_nodes = {n["id"]: n for n in nodes if n.get("type") == "networkNode"}

    for node in nodes:
        if node.get("type") != "containerNode":
            continue
        data = node.get("data", {})
        for nic in data.get("nics", []):
            if nic.get("ip"):
                continue

            # Find connected network via edges
            nic_handle_top = f"nic-{nic['id']}-top"
            nic_handle_bottom = f"nic-{nic['id']}-bottom"
            net_node = None
            for edge in edges:
                src, tgt = edge.get("source"), edge.get("target")
                sh, th = edge.get("sourceHandle", ""), edge.get("targetHandle", "")
                if src == node["id"] and sh in (nic_handle_top, nic_handle_bottom):
                    net_node = net_nodes.get(tgt)
                elif tgt == node["id"] and th in (nic_handle_top, nic_handle_bottom):
                    net_node = net_nodes.get(src)
                if net_node:
                    break

            if not net_node:
                continue

            cidr = net_node.get("data", {}).get("cidr", "")
            if not cidr:
                continue

            net_data = net_node.get("data", {})
            dhcp_range = _get_dhcp_range(net_data)
            if not dhcp_range:
                continue
            start_int, end_int = dhcp_range
            for addr_int in range(start_int, end_int + 1):
                candidate_str = str(ipaddress.ip_address(addr_int))
                if candidate_str not in used_ips:
                    nic["ip"] = candidate_str
                    used_ips.add(candidate_str)
                    logger.info(
                        "Auto-assigned %s to container %s NIC %s (from DHCP range)",
                        candidate_str,
                        data.get("name"),
                        nic.get("name"),
                    )
                    break


def _collect_used_ips(topology: dict) -> set[str]:
    """Collect all IPs already assigned: static IPs on VMs/containers + gateway IPs."""
    used = set()
    for node in topology.get("nodes", []):
        data = node.get("data", {})
        for nic in data.get("nics", []):
            ip = nic.get("ip", "")
            if ip:
                used.add(ip)
        if node.get("type") == "networkNode":
            cidr = data.get("cidr", "")
            if cidr:
                try:
                    net = ipaddress.ip_network(cidr, strict=False)
                    used.add(str(net.network_address + 1))
                except ValueError:
                    pass
    return used


def _get_dhcp_range(net_data: dict) -> tuple[int, int] | None:
    """Return the DHCP range as (start_int, end_int) for a network node's data.

    Matches the auto-generation logic in vxlan.py: hosts[9] to hosts[-1].
    """
    range_start = net_data.get("dhcpRangeStart", "")
    range_end = net_data.get("dhcpRangeEnd", "")
    if not range_start or not range_end:
        cidr = net_data.get("cidr", "")
        if cidr:
            try:
                net = ipaddress.ip_network(cidr, strict=False)
                hosts = list(net.hosts())
                if len(hosts) > 10:
                    if not range_start:
                        range_start = str(hosts[min(9, len(hosts) - 2)])
                    if not range_end:
                        range_end = str(hosts[-1])
            except ValueError:
                pass
    if range_start and range_end:
        try:
            return (
                int(ipaddress.ip_address(range_start)),
                int(ipaddress.ip_address(range_end)),
            )
        except ValueError:
            pass
    return None
