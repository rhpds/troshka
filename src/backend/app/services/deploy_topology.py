"""Topology helpers — pure functions for parsing, validating, and querying canvas topology JSONB.

Extracted from deploy_service.py for testability. No DB, no troshkad, no Redis dependencies.
"""

import ipaddress
import logging
import os
import random
import uuid

logger = logging.getLogger(__name__)

# Providers with OpenShift ingress: 443/80 forwards are served by an OpenShift
# Route, never bound to the EIP LoadBalancer. Cloud providers (ec2/gcp/azure)
# have no ingress, so their 443/80 forwards must stay on the EIP.
_ROUTE_PROVIDERS = frozenset({"ocpvirt", "kubevirt"})


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
    nodes_by_id: dict[str, dict] = {n["id"]: n for n in topology.get("nodes", [])}
    nic_to_network = _build_nic_to_network_map(topology, nodes_by_id)
    return _check_duplicate_ips(topology, nodes_by_id, nic_to_network)


def _build_nic_to_network_map(
    topology: dict, nodes_by_id: dict[str, dict]
) -> dict[str, str]:
    nic_to_network: dict[str, str] = {}
    for edge in topology.get("edges", []):
        src = edge.get("source", "")
        tgt = edge.get("target", "")
        for handle_key, net_id, vm_id in [
            ("targetHandle", src, tgt),
            ("sourceHandle", tgt, src),
        ]:
            handle = edge.get(handle_key, "")
            if not _is_nic_edge(nodes_by_id, net_id, vm_id, handle):
                continue
            raw = handle.replace("-top", "").replace("-bottom", "")
            if raw.startswith("nic-"):
                nic_to_network[raw[4:]] = net_id
    return nic_to_network


def _is_nic_edge(
    nodes_by_id: dict[str, dict], net_id: str, vm_id: str, handle: str
) -> bool:
    return (
        nodes_by_id.get(net_id, {}).get("type") == "networkNode"
        and nodes_by_id.get(vm_id, {}).get("type") in ("vmNode", "containerNode")
        and "nic-" in handle
    )


def _check_duplicate_ips(
    topology: dict,
    nodes_by_id: dict[str, dict],
    nic_to_network: dict[str, str],
) -> list[str]:
    errors = []
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


def _is_showroom_node(node: dict) -> bool:
    if node.get("type") != "containerNode":
        return False
    data = node.get("data", {})
    return bool(data.get("isShowroom") or data.get("name") == "showroom")


def _is_gateway_node(node: dict | None) -> bool:
    return bool(
        node
        and node.get("type") == "networkNode"
        and node.get("data", {}).get("subtype") == "gateway"
    )


def _is_plain_network(node: dict | None) -> bool:
    if not node or node.get("type") != "networkNode":
        return False
    subtype = node.get("data", {}).get("subtype")
    return subtype in (None, "network", "dhcp", "dns")


def _showroom_content_repo(topology: dict, showroom_node: dict) -> str:
    """Content repo from topology.showroom or the showroom container node."""
    showroom_cfg = topology.get("showroom") or {}
    repo = str(showroom_cfg.get("content_repo") or "").strip()
    if repo:
        return repo
    data = showroom_node.get("data", {})
    return str(data.get("contentRepo") or "").strip()


def validate_showroom_topology(topology: dict) -> list[str]:
    """Validate showroom content config (infra networking is deploy-managed)."""
    showroom_nodes = [n for n in topology.get("nodes", []) if _is_showroom_node(n)]
    if not showroom_nodes:
        return []
    if len(showroom_nodes) > 1:
        return ["Only one showroom is allowed per project"]

    showroom = showroom_nodes[0]
    errors: list[str] = []

    gateway = next(
        (n for n in topology.get("nodes", []) if _is_gateway_node(n)),
        None,
    )
    if not gateway:
        errors.append("A gateway is required for external showroom access")

    showroom_cfg = topology.get("showroom") or {}
    enabled = showroom_cfg.get("enabled", True)
    if enabled and not _showroom_content_repo(topology, showroom):
        errors.append("Showroom content repo URL is required")

    if enabled:
        dns_net_name = _showroom_dns_network_name(topology)
        if not dns_net_name:
            errors.append("Enable DNS on at least one network connected to the gateway")
        else:
            dns_net = _find_lab_network_by_name(topology, dns_net_name)
            if not dns_net:
                errors.append(
                    f"Showroom DNS network '{dns_net_name}' was not found on the canvas"
                )
            else:
                dns_data = dns_net.get("data", {})
                if not _network_dns_enabled(dns_data):
                    errors.append(
                        f"Showroom DNS network '{dns_net_name}' must have DNS enabled"
                    )
                elif not dns_data.get("cidr"):
                    errors.append(
                        f"Showroom DNS network '{dns_net_name}' must have a CIDR"
                    )
                elif not _is_network_gateway_connected(topology, dns_net["id"]):
                    errors.append(
                        f"Showroom DNS network '{dns_net_name}' must be connected to the gateway"
                    )

        if gateway and not _gateway_allows_dns_upstream(gateway.get("data", {})):
            errors.append(
                "Gateway outbound must allow DNS (port 53) so showroom can resolve external names"
            )

    return errors


def ensure_showroom_external_ips(topology: dict) -> bool:
    """Add a canvas external IP slot when showroom + gateway need public access."""
    from app.services.vxlan import _topology_has_showroom

    if not _topology_has_showroom(topology):
        return False
    gateway = next(
        (
            n
            for n in topology.get("nodes", [])
            if n.get("type") == "networkNode"
            and n.get("data", {}).get("subtype") == "gateway"
        ),
        None,
    )
    if not gateway:
        return False
    if topology.get("externalIps"):
        return False
    topology["externalIps"] = [{"id": str(uuid.uuid4()), "name": "IP-1", "ip": ""}]
    return True


def strip_showroom_gateway_access(topology: dict) -> bool:
    """Remove auto-managed showroom gateway port forward, outbound ports, and IP-1."""
    from app.services.vxlan import _is_showroom_infra_forward

    changed = False
    gateway = next(
        (
            n
            for n in topology.get("nodes", [])
            if n.get("type") == "networkNode"
            and n.get("data", {}).get("subtype") == "gateway"
        ),
        None,
    )
    gateway_pfs: list = []
    if gateway:
        data = gateway.setdefault("data", {})
        existing = list(data.get("portForwards") or [])
        stripped = [pf for pf in existing if not _is_showroom_infra_forward(pf)]
        if stripped != existing:
            data["portForwards"] = stripped
            gateway_pfs = stripped
            if not stripped and data.get("gatewayMode") == "nat-portforward":
                data["gatewayMode"] = "nat"
            changed = True
        else:
            gateway_pfs = existing

        if data.get("outboundPolicy") == "restrict":
            managed = list(data.get("showroomManagedOutbound") or [])
            if managed:
                new_ports = _strip_showroom_outbound_ports(
                    str(data.get("outboundPorts") or ""), managed
                )
                if new_ports != str(data.get("outboundPorts") or ""):
                    data["outboundPorts"] = new_ports
                    changed = True
                data.pop("showroomManagedOutbound", None)

    ext_ips = topology.get("externalIps") or []
    if (
        len(ext_ips) == 1
        and not ext_ips[0].get("ip")
        and ext_ips[0].get("name") == "IP-1"
        and not any(pf.get("extIpId") == ext_ips[0].get("id") for pf in gateway_pfs)
    ):
        topology["externalIps"] = []
        changed = True
    return changed


SHOWROOM_MANAGED_OUTBOUND_PORTS = ("53", "443")


def _parse_outbound_port_entries(outbound_ports: str) -> list[str]:
    return [p.strip() for p in str(outbound_ports or "").split(",") if p.strip()]


def _outbound_entries_include_port(entries: list[str], port: str) -> bool:
    for entry in entries:
        if entry == port:
            return True
        if "/" in entry and entry.split("/", 1)[0] == port:
            return True
    return False


def _gateway_allows_dns_upstream(gateway_data: dict) -> bool:
    """dnsmasq upstream (e.g. github.com) needs outbound 53 or allow-all."""
    policy = gateway_data.get("outboundPolicy", "allow-all")
    if policy != "restrict":
        return True
    entries = _parse_outbound_port_entries(str(gateway_data.get("outboundPorts") or ""))
    return _outbound_entries_include_port(entries, "53")


def _inject_showroom_outbound_ports(outbound_ports: str) -> tuple[str, list[str]]:
    """Ensure DNS (53) and git (443) when showroom uses restrict outbound."""
    entries = _parse_outbound_port_entries(outbound_ports)
    added: list[str] = []
    for port in SHOWROOM_MANAGED_OUTBOUND_PORTS:
        if not _outbound_entries_include_port(entries, port):
            entries.append(port)
            added.append(port)
    return ",".join(entries), added


def _strip_showroom_outbound_ports(outbound_ports: str, managed: list[str]) -> str:
    if not managed:
        return outbound_ports
    managed_set = set(managed)
    stripped = [
        entry
        for entry in _parse_outbound_port_entries(outbound_ports)
        if entry not in managed_set
        and not ("/" in entry and entry.split("/", 1)[0] in managed_set)
    ]
    return ",".join(stripped)


def inject_showroom_gateway_port_forwards(
    topology: dict, vni_map: dict, provider_type: str | None = None
) -> bool:
    """Sync gateway external IP + 443→showroom forward with showroom presence.

    On OpenShift-ingress providers (ocpvirt/kubevirt) 443/80 forwards are served
    by a Route and must NOT be bound to the EIP; on cloud providers they stay on
    the EIP.
    """
    from app.services.vxlan import (
        _inject_showroom_port_forward,
        _topology_has_showroom,
    )

    if not _topology_has_showroom(topology):
        return strip_showroom_gateway_access(topology)

    changed = ensure_showroom_external_ips(topology)

    gateway = next(
        (
            n
            for n in topology.get("nodes", [])
            if n.get("type") == "networkNode"
            and n.get("data", {}).get("subtype") == "gateway"
        ),
        None,
    )
    if not gateway:
        return changed

    first_vni = min(vni_map.values()) if vni_map else None
    data = gateway.setdefault("data", {})
    existing = list(data.get("portForwards") or [])
    merged = _inject_showroom_port_forward(existing, topology, first_vni)

    ext_ips = topology.get("externalIps") or []
    eip_id = str(ext_ips[0].get("id", "")) if ext_ips else ""
    if eip_id:
        route_web = provider_type in _ROUTE_PROVIDERS
        new_merged = []
        for pf in merged:
            is_showroom = pf.get("managedByShowroom") or (
                str(pf.get("extPort")) == "443"
                and str(pf.get("intPort")) == "80"
                and (pf.get("intIp") or "").strip().startswith("172.30.")
                and (pf.get("intIp") or "").strip().endswith(".3")
            )
            entry = {**pf}
            if is_showroom:
                entry["managedByShowroom"] = True
            # On OpenShift-ingress providers, 443/80 are served by a Route — strip
            # any extIpId so they stay Route-only (also self-heals topologies where
            # an EIP was wrongly assigned to the showroom 443). On cloud providers
            # there is no ingress, so 443/80 stay bound to the EIP like everything
            # else.
            if route_web and str(pf.get("extPort")) in ("443", "80"):
                entry.pop("extIpId", None)
            else:
                entry["extIpId"] = pf.get("extIpId") or eip_id
            new_merged.append(entry)
        merged = new_merged

    if merged != existing:
        data["portForwards"] = merged
        changed = True

    if data.get("outboundPolicy") == "restrict":
        new_ports, added = _inject_showroom_outbound_ports(
            str(data.get("outboundPorts") or "")
        )
        if added:
            data["outboundPorts"] = new_ports
            managed = list(data.get("showroomManagedOutbound") or [])
            data["showroomManagedOutbound"] = list(dict.fromkeys(managed + added))
            changed = True

    return changed


def showroom_transit_octet3(vni_map: dict) -> int | None:
    if not vni_map:
        return None
    # Match inject_showroom_gateway_port_forwards (min VNI → stable octet).
    vni = min(int(v) for v in vni_map.values())
    return vni & 0xFF


def is_showroom_infra_ip(ip: str) -> bool:
    """Transit-side showroom address (172.30.{vni}.3), not a lab NIC."""
    parts = (ip or "").split(".")
    return (
        len(parts) == 4 and parts[0] == "172" and parts[1] == "30" and parts[3] == "3"
    )


def showroom_infra_ip(vni_map: dict) -> str:
    octet3 = showroom_transit_octet3(vni_map)
    if octet3 is None:
        return ""
    return f"172.30.{octet3}.3"


def _network_dns_enabled(data: dict) -> bool:
    return bool(data.get("dns") or data.get("subtype") == "dns")


def _peer_lab_network_on_gateway_edge(
    edge: dict, gateway_id: str, nodes_by_id: dict[str, dict]
) -> dict | None:
    src_id, tgt_id = edge.get("source", ""), edge.get("target", "")
    if src_id == gateway_id:
        peer = nodes_by_id.get(tgt_id)
    elif tgt_id == gateway_id:
        peer = nodes_by_id.get(src_id)
    else:
        return None
    return peer if _is_plain_network(peer) else None


def _resolve_network_dns_nameserver(net_data: dict, cidr: str) -> str:
    """IP where project dnsmasq listens (explicit dnsServerIp or DHCP gateway)."""
    server = str((net_data or {}).get("dnsServerIp") or "").strip()
    if server:
        return server
    return _resolve_network_gateway(net_data, cidr)


def _gateway_connected_dns_network_name(topology: dict) -> str:
    """First gateway-connected lab network with DNS (gateway must allow upstream DNS)."""
    gateway = next(
        (n for n in topology.get("nodes", []) if _is_gateway_node(n)),
        None,
    )
    if not gateway:
        return ""
    gateway_data = gateway.get("data", {})
    if not _gateway_allows_dns_upstream(gateway_data):
        return ""
    nodes_by_id = {n["id"]: n for n in topology.get("nodes", [])}
    gateway_id = gateway["id"]
    for edge in topology.get("edges", []):
        peer = _peer_lab_network_on_gateway_edge(edge, gateway_id, nodes_by_id)
        if not peer:
            continue
        data = peer.get("data", {})
        if not _network_dns_enabled(data):
            continue
        name = str(data.get("name") or data.get("label") or "").strip()
        if name:
            return name
    return ""


def _is_network_gateway_connected(topology: dict, network_id: str) -> bool:
    gateway = next(
        (n for n in topology.get("nodes", []) if _is_gateway_node(n)),
        None,
    )
    if not gateway:
        return False
    nodes_by_id = {n["id"]: n for n in topology.get("nodes", [])}
    gateway_id = gateway["id"]
    for edge in topology.get("edges", []):
        peer = _peer_lab_network_on_gateway_edge(edge, gateway_id, nodes_by_id)
        if peer and peer.get("id") == network_id:
            return True
    return False


def _first_dns_enabled_network_name(topology: dict) -> str:
    """Implicit showroom DNS network: gateway-outbound with DNS enabled."""
    return _gateway_connected_dns_network_name(topology)


def _showroom_dns_network_name(topology: dict) -> str:
    """Configured lab network name for showroom DNS, or first DNS-enabled network."""
    showroom_cfg = topology.get("showroom") or {}
    name = str(showroom_cfg.get("dns_network") or "").strip()
    if name:
        return name
    for node in topology.get("nodes", []):
        if not _is_showroom_node(node):
            continue
        name = str(node.get("data", {}).get("dnsNetwork") or "").strip()
        if name:
            return name
    return _first_dns_enabled_network_name(topology)


def _find_lab_network_by_name(topology: dict, name: str) -> dict | None:
    name = (name or "").strip()
    if not name:
        return None
    for node in topology.get("nodes", []):
        if not _is_plain_network(node):
            continue
        data = node.get("data", {})
        if data.get("name") == name or data.get("label") == name:
            return node
    return None


def _nameserver_for_lab_network(node: dict | None) -> str:
    if not node:
        return ""
    data = node.get("data", {})
    cidr = data.get("cidr", "")
    if not cidr:
        return ""
    return _resolve_network_dns_nameserver(data, cidr)


def _gateway_connected_dns_nameserver(topology: dict) -> str:
    """Fallback nameserver from first gateway-connected DNS network."""
    name = _gateway_connected_dns_network_name(topology)
    if not name:
        return ""
    return _nameserver_for_lab_network(_find_lab_network_by_name(topology, name))


def _showroom_dns_nameserver(topology: dict) -> str:
    """Showroom pod DNS: configured dns_network gateway, not transit .2."""
    name = _showroom_dns_network_name(topology)
    if name:
        return _nameserver_for_lab_network(_find_lab_network_by_name(topology, name))
    return _gateway_connected_dns_nameserver(topology)


def showroom_infra_network(
    vni_map: dict, mac: str = "", dns_nameserver: str = ""
) -> list[dict]:
    """Transit-side showroom addressing in the project netns (not lab DHCP)."""
    octet3 = showroom_transit_octet3(vni_map)
    if octet3 is None:
        return []
    net: dict = {
        "bridge": "",
        "mac": mac,
        "ip": f"172.30.{octet3}.3",
        "cidr": f"172.30.{octet3}.0/24",
        "gateway": f"172.30.{octet3}.2",
        "infra_transit": True,
    }
    if dns_nameserver:
        net["dns_nameserver"] = dns_nameserver
    return [net]


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
                "machine_type": data.get("machineType") or "",
                "video_model": data.get("videoModel", "virtio"),
                "input_model": data.get("inputModel", "virtio"),
                "serial_exec_type": data.get("serialExecType") or "",
                "headless": data.get("headless"),
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
                "build_content": data.get("buildContent", True),
            }
        )
    return containers


def metadata_bridges_for_topology(topology: dict, vni_map: dict) -> list[str]:
    """Bridges that receive the cloud-init metadata IP (169.254.169.254).

    Only the project's first network (canvas order) gets the metadata address.
    Putting it on every bridge breaks multi-network projects: each dnsmasq binds
    169.254.169.254:53 and only one process can listen there.
    """
    if not vni_map:
        return []
    for node in topology.get("nodes", []):
        if node.get("type") != "networkNode":
            continue
        net_id = node["id"]
        if net_id in vni_map:
            return [f"br-{vni_map[net_id]}"]
    first_vni = next(iter(vni_map.values()))
    return [f"br-{first_vni}"]


def _find_vm_networks(
    vm_node_id: str, topology: dict, vni_map: dict, project_id: str = ""
) -> list[dict]:
    """Find networks connected to a VM via NIC handles."""
    edges = topology.get("edges", [])
    nodes = topology.get("nodes", [])
    networks = []

    for edge in edges:
        entry = _resolve_vm_network_entry(edge, vm_node_id, nodes, vni_map, project_id)
        if entry is not None:
            networks.append(entry)

    return networks


def _resolve_vm_network_entry(
    edge: dict,
    vm_node_id: str,
    nodes: list[dict],
    vni_map: dict,
    project_id: str,
) -> dict | None:
    handle, network_node_id = _extract_nic_edge(edge, vm_node_id)
    if not handle:
        return None

    mac, model = _resolve_nic_mac_model(vm_node_id, handle, nodes)

    net_node = next((n for n in nodes if n["id"] == network_node_id), None)
    if net_node and net_node.get("data", {}).get("networkType") == "bmc":
        return _build_bmc_network_entry(mac, model, handle, project_id)

    if network_node_id not in vni_map:
        return None

    vni = vni_map[network_node_id]
    return {"bridge": f"br-{vni}", "mac": mac, "nic_id": handle, "model": model}


def _extract_nic_edge(edge: dict, vm_node_id: str) -> tuple[str | None, str | None]:
    if edge.get("source") == vm_node_id:
        handle = edge.get("sourceHandle", "")
        network_node_id = edge.get("target")
    elif edge.get("target") == vm_node_id:
        handle = edge.get("targetHandle", "")
        network_node_id = edge.get("source")
    else:
        return None, None

    if not handle or not handle.startswith("nic-"):
        return None, None
    return handle, network_node_id


def _resolve_nic_mac_model(
    vm_node_id: str, handle: str, nodes: list[dict]
) -> tuple[str, str]:
    vm_node = next((n for n in nodes if n["id"] == vm_node_id), None)
    if not vm_node:
        return "", "virtio"
    for nic in vm_node.get("data", {}).get("nics", []):
        if nic["id"] in handle:
            return nic.get("mac", ""), nic.get("model", "virtio")
    return "", "virtio"


def _build_bmc_network_entry(
    mac: str, model: str, handle: str, project_id: str
) -> dict:
    bmc_mac = mac
    if not bmc_mac:
        bmc_mac = "52:54:01:%02x:%02x:%02x" % (
            random.randint(0, 255),
            random.randint(0, 255),
            random.randint(0, 255),
        )
    return {
        "bridge": f"br-bmc-{project_id[:8]}",
        "mac": bmc_mac,
        "nic_id": handle,
        "model": model,
    }


def _resolve_network_gateway(net_data: dict, cidr: str) -> str:
    """Return DHCP gateway from network node data, or derive from CIDR."""
    gateway = (net_data or {}).get("dhcpGateway", "")
    if gateway:
        return gateway
    if not cidr:
        return ""
    try:
        network = ipaddress.ip_network(cidr, strict=False)
        return str(next(network.hosts()))
    except (ValueError, StopIteration):
        return ""


def _find_container_networks(
    container_node_id: str, topology: dict, vni_map: dict, project_id: str = ""
) -> list[dict]:
    """Find networks connected to a container via NIC handles."""
    _ = project_id  # reserved for future use; callers pass it
    results: list[dict] = []
    container_node = next(
        (n for n in topology.get("nodes", []) if n["id"] == container_node_id), None
    )
    if not container_node:
        return results

    if _is_showroom_node(container_node):
        nics = container_node.get("data", {}).get("nics", [])
        mac = nics[0].get("mac", "") if nics else ""
        dns = _showroom_dns_nameserver(topology)
        return showroom_infra_network(vni_map, mac, dns_nameserver=dns)

    nics_by_id = {
        nic["id"]: nic for nic in container_node.get("data", {}).get("nics", [])
    }

    for edge in topology.get("edges", []):
        src, tgt = edge.get("source"), edge.get("target")
        src_h, tgt_h = edge.get("sourceHandle", ""), edge.get("targetHandle", "")

        nic_id = None
        net_node_id = None
        if src == container_node_id and src_h.startswith("nic-"):
            nic_id = _extract_nic_id(src_h)
            net_node_id = tgt
        elif tgt == container_node_id and tgt_h.startswith("nic-"):
            nic_id = _extract_nic_id(tgt_h)
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
        net_data = net_node.get("data", {}) if net_node else {}
        cidr = net_data.get("cidr", "")

        results.append(
            {
                "bridge": f"br-{vni}",
                "mac": nic.get("mac", ""),
                "nic_id": nic_id,
                "model": nic.get("model", "virtio"),
                "ip": nic.get("ip", ""),
                "cidr": cidr,
                "gateway": _resolve_network_gateway(net_data, cidr),
            }
        )

    return results


def _find_vm_name_by_ip(topology, ip):
    """Find the VM/showroom name for a port-forward internal IP."""
    if is_showroom_infra_ip(ip):
        for node in topology.get("nodes", []):
            if _is_showroom_node(node):
                data = node.get("data", {})
                return data.get("name", "showroom")
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
        handle, storage_node_id = _extract_disk_edge(edge, vm_node_id)
        if not handle:
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
        bus, rotation_rate = _resolve_disk_bus(vm_node_id, handle, nodes)

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
        if sdata.get("format") == "iso":
            disk_entry["bootable_iso"] = bool(sdata.get("bootableIso", False))
        disks.append(disk_entry)

    return disks


def _normalize_disk_port_id(handle: str) -> str | None:
    """Map a React Flow disk port handle to diskControllers[].id."""
    if not handle or not handle.startswith("dp-"):
        return None
    body = handle[3:]
    if body.endswith("-left"):
        body = body[:-5]
    elif body.endswith("-right"):
        body = body[:-6]
    # Canvas handles are dp-{controllerId}-left where controllerId is dp-{uuid}.
    # Legacy edges may use the bare controller id without side suffix.
    if body and not body.startswith("dp-"):
        return f"dp-{body}"
    return body or None


def _extract_nic_id(handle: str) -> str:
    """Extract NIC ID from edge handle 'nic-{nicId}-{direction}'.

    Handles are built as f"nic-{nic.id}-{dir}" (nic.id is itself "nic-<uuid>"),
    so stripping the leading "nic-" prefix and the trailing direction suffix
    yields the original nic id. Do not re-prepend "nic-": that corrupts ids
    that do not already start with the prefix.
    """
    if not handle or "nic-" not in handle:
        return ""
    for suffix in ("-top", "-bottom", "-left", "-right"):
        if handle.endswith(suffix):
            handle = handle[: -len(suffix)]
            break
    if handle.startswith("nic-"):
        handle = handle[4:]
    return handle


def _extract_disk_edge(edge: dict, vm_node_id: str) -> tuple[str | None, str | None]:
    if edge.get("source") == vm_node_id:
        handle = edge.get("sourceHandle", "")
        storage_node_id = edge.get("target")
    elif edge.get("target") == vm_node_id:
        handle = edge.get("targetHandle", "")
        storage_node_id = edge.get("source")
    else:
        return None, None

    if not handle or not handle.startswith("dp-"):
        return None, None
    return handle, storage_node_id


def _resolve_disk_bus(
    vm_node_id: str, handle: str, nodes: list[dict]
) -> tuple[str, int | None]:
    vm_node = next((n for n in nodes if n["id"] == vm_node_id), None)
    port_id = _normalize_disk_port_id(handle)
    if not vm_node or not port_id:
        return "virtio", None
    for dc in vm_node.get("data", {}).get("diskControllers", []):
        if dc["id"] == port_id:
            return dc.get("bus", "virtio"), dc.get("rotationRate")
    return "virtio", None


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
    nodes = topology.get("nodes", [])

    results = []
    for edge in topology.get("edges", []):
        disk_node_id = _resolve_mount_edge(edge, container_node_id)
        if not disk_node_id:
            continue

        disk_node = next(
            (
                n
                for n in nodes
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


def _resolve_mount_edge(edge: dict, container_node_id: str) -> str | None:
    src, tgt = edge.get("source"), edge.get("target")
    src_h, tgt_h = edge.get("sourceHandle", ""), edge.get("targetHandle", "")

    if src == container_node_id and (tgt_h or "").startswith("mnt-"):
        return tgt
    if tgt == container_node_id and (src_h or "").startswith("mnt-"):
        return src
    if tgt == container_node_id and (tgt_h or "").startswith("mnt-"):
        return src
    if src == container_node_id and (src_h or "").startswith("mnt-"):
        return tgt
    return None


def _vm_domain_name(project_id: str, node_id: str) -> str:
    return f"troshka-{project_id[:8]}-{node_id[:8]}"


def _extract_bmc_config(topology: dict, project_id: str) -> dict | None:
    """Extract BMC configuration from topology if any VMs have BMC enabled."""
    bmc_network = _find_bmc_network(topology)
    if not bmc_network:
        return None

    bmc_vms = _find_bmc_vms(topology, project_id)
    if not bmc_vms:
        return None

    dhcp_hosts = _collect_bmc_dhcp_hosts(topology, bmc_network["id"])

    return {
        "bmc_network": bmc_network["data"],
        "vms": bmc_vms,
        "dhcp_hosts": dhcp_hosts,
    }


def _find_bmc_network(topology: dict) -> dict | None:
    for node in topology.get("nodes", []):
        if (
            node.get("type") == "networkNode"
            and node.get("data", {}).get("networkType") == "bmc"
        ):
            return node
    return None


def _find_bmc_vms(topology: dict, project_id: str) -> list[dict]:
    bmc_vms = []
    for node in topology.get("nodes", []):
        if node.get("type") != "vmNode":
            continue
        if not node.get("data", {}).get("bmcEnabled"):
            continue
        bmc_ip = node["data"].get("bmcIp", "")
        if bmc_ip:
            bmc_vms.append(
                {
                    "node_id": node["id"],
                    "domain_name": _vm_domain_name(project_id, node["id"]),
                    "bmc_ip": bmc_ip,
                }
            )
    return bmc_vms


def _collect_bmc_dhcp_hosts(topology: dict, bmc_net_id: str) -> list[dict]:
    dhcp_hosts = []
    edges = topology.get("edges", [])
    nodes = topology.get("nodes", [])
    for node in nodes:
        if node.get("type") != "vmNode":
            continue
        dhcp_hosts.extend(_collect_vm_bmc_dhcp_entries(node, edges, bmc_net_id))
    return dhcp_hosts


def _collect_vm_bmc_dhcp_entries(
    node: dict, edges: list[dict], bmc_net_id: str
) -> list[dict]:
    entries = []
    vm_id = node["id"]
    for edge in edges:
        handle, net_id = _extract_nic_edge(edge, vm_id)
        if not handle or net_id != bmc_net_id:
            continue
        for nic in node.get("data", {}).get("nics", []):
            if nic["id"] in handle and nic.get("ip") and nic.get("mac"):
                entries.append(
                    {
                        "mac": nic["mac"],
                        "ip": nic["ip"],
                        "name": node["data"].get("name", ""),
                    }
                )
    return entries


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
    _ = pool  # reserved for future use; callers pass it
    return f"/var/lib/troshka/local/cache/patterns/{pattern_id}/{disk_id}.{fmt}"


def _snapshot_cache_path(item_id: str, disk_id: str, fmt: str) -> str:
    return f"/var/lib/troshka/cache/snapshots/{item_id}/{disk_id}.{fmt}"


def _resolve_boot_devs(vm: dict, vm_disks: list[dict], topology: dict) -> list[str]:
    all_nodes = topology.get("nodes", [])
    storage_nodes = {n["id"]: n for n in all_nodes if n.get("type") == "storageNode"}

    raw_boot_devs = vm.get("boot_devices") or None
    has_bootable_iso = any(
        d["format"] == "iso" and d.get("bootable_iso", False) for d in vm_disks
    )
    has_disk = any(d["format"] != "iso" for d in vm_disks)
    has_cdrom_controller = any(
        dc.get("bus") == "sata" and "cdrom" in dc.get("name", "")
        for dc in vm.get("disk_controllers", [])
    )

    if raw_boot_devs is None or (raw_boot_devs == ["hd"] and has_bootable_iso):
        return _auto_detect_boot_devs(has_bootable_iso, has_disk)

    return _map_boot_dev_entries(
        raw_boot_devs, storage_nodes, has_cdrom_controller, has_bootable_iso
    )


def _auto_detect_boot_devs(has_iso: bool, has_disk: bool) -> list[str]:
    if has_iso and has_disk:
        return ["cdrom", "hd"]
    if has_iso:
        return ["cdrom"]
    if has_disk:
        return ["hd"]
    return ["network"]


def _map_boot_dev_entries(
    raw_boot_devs: list[str],
    storage_nodes: dict[str, dict],
    has_cdrom_controller: bool,
    has_bootable_iso: bool = False,
) -> list[str]:
    boot_type_map = {"hd": "hd", "disk": "hd", "network": "network", "cdrom": "cdrom"}
    boot_devs = []
    seen: set[str] = set()
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
    if has_cdrom_controller and has_bootable_iso and "cdrom" not in seen:
        boot_devs.append("cdrom")
    return boot_devs or ["hd"]


def diff_topologies(current: dict, deployed: dict) -> dict:
    """Diff current topology against what was deployed. Returns changes."""
    cur_nodes = {n["id"]: n for n in current.get("nodes", [])}
    dep_nodes = {n["id"]: n for n in deployed.get("nodes", [])}

    added_vms, added_networks = _categorize_new_nodes(cur_nodes, dep_nodes)
    removed_vms, removed_networks = _categorize_new_nodes(dep_nodes, cur_nodes)
    changed_vms = _find_changed_vms(cur_nodes, dep_nodes)

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


def _categorize_new_nodes(
    source: dict[str, dict], reference: dict[str, dict]
) -> tuple[list[dict], list[dict]]:
    vms = []
    networks = []
    for nid, node in source.items():
        if nid in reference:
            continue
        ntype = node.get("type")
        if ntype == "vmNode":
            vms.append(node)
        elif ntype == "networkNode":
            networks.append(node)
    return vms, networks


def _storage_node_data(topology: dict, storage_id: str) -> dict:
    """Return storage node data dict for a canvas storage node id."""
    for node in topology.get("nodes", []):
        if node.get("id") == storage_id and node.get("type") == "storageNode":
            return node.get("data", {})
    return {}


def build_troshkavm_disk_spec(disk: dict, topology: dict) -> dict:
    """Build a TroshkaVM CR disk entry from _find_vm_disks() output."""
    storage_id = disk.get("node_id", "")
    sd = dict(_storage_node_data(topology, storage_id))
    if disk.get("resolvedS3Path") and not sd.get("resolvedS3Path"):
        sd["resolvedS3Path"] = disk["resolvedS3Path"]
    if disk.get("centralSource") is not None and "centralSource" not in sd:
        sd["centralSource"] = disk["centralSource"]
    if disk.get("diskSource") and "diskSource" not in sd:
        sd["diskSource"] = disk["diskSource"]
    fmt = disk.get("format", sd.get("format", "qcow2"))
    size_gb = disk.get("size_gb", disk.get("size", sd.get("size", 20)))
    source = disk.get("source") or sd.get("source", "blank")
    central = sd.get("centralSource", False)

    spec: dict = {
        "id": storage_id,
        "sizeGb": int(size_gb) if size_gb else 20,
        "bus": disk.get("bus", "virtio"),
        "format": fmt,
    }
    if disk.get("rotation_rate") is not None:
        spec["rotationRate"] = disk["rotation_rate"]

    source_size_gb = int(sd.get("sourceSizeGb", 0) or 0)
    if source == "pattern" and (disk.get("patternId") or sd.get("patternId")):
        pattern_id = disk.get("patternId") or sd.get("patternId", "")
        pattern_disk_id = sd.get("patternDiskId", "")
        spec["patternImage"] = {
            "s3Path": sd.get("resolvedS3Path")
            or f"patterns/{pattern_id}/{pattern_disk_id}.qcow2",
            "format": "qcow2",
            "central": central,
            "source": sd.get("diskSource", "central"),
        }
        if source_size_gb > 0:
            spec["sourceSizeGb"] = source_size_gb
    elif source == "library" and (
        disk.get("library_item_id")
        or disk.get("libraryItemId")
        or sd.get("libraryItemId")
    ):
        lib_id = (
            disk.get("library_item_id")
            or disk.get("libraryItemId")
            or sd.get("libraryItemId", "")
        )
        spec["libraryImage"] = {
            "s3Path": sd.get("resolvedS3Path") or f"library/{lib_id}.{fmt}",
            "format": fmt,
            "central": central,
        }
        if source_size_gb > 0:
            spec["sourceSizeGb"] = source_size_gb
    elif source == "snapshot" and sd.get("resolvedS3Path"):
        spec["libraryImage"] = {
            "s3Path": sd["resolvedS3Path"],
            "format": fmt,
            "central": central,
        }
    else:
        spec["blank"] = True
    return spec


def _nic_network_ref(nic_id: str, topology: dict) -> str:
    """Map a canvas NIC id to TroshkaNetwork CR name (net-{node_id[:8]})."""
    nodes_by_id = {n["id"]: n for n in topology.get("nodes", [])}
    nic_map = _build_nic_to_network_map(topology, nodes_by_id)
    net_node_id = nic_map.get(nic_id, "")
    if not net_node_id:
        return ""
    return f"net-{net_node_id[:8]}"


def build_troshkavm_nic_specs(
    vm_data: dict, topology: dict | None = None
) -> list[dict]:
    """Build TroshkaVM CR NIC entries from canvas VM node data."""
    specs = []
    for nic in vm_data.get("nics", []):
        entry = {
            "id": nic.get("id", ""),
            "mac": nic.get("mac", ""),
            "model": nic.get("model", "virtio"),
        }
        if topology:
            net_ref = _nic_network_ref(nic.get("id", ""), topology)
            if net_ref:
                entry["networkRef"] = net_ref
        specs.append(entry)
    return specs


def _resolve_vm_firmware(vm: dict, vm_data: dict) -> str:
    """Map canvas firmware + secureBoot to TroshkaVM firmware enum."""
    firmware = vm.get("firmware") or vm_data.get("firmware", "bios")
    if firmware == "uefi" and vm_data.get("secureBoot"):
        return "uefi-secure"
    return firmware


def build_troshkavm_vm_spec(vm_id: str, vm: dict, topology: dict) -> dict:
    """Build a TroshkaVM CR spec from canvas topology (KubeVirt + shared semantics)."""
    vm_data: dict = {}
    for node in topology.get("nodes", []):
        if node.get("id") == vm_id and node.get("type") == "vmNode":
            vm_data = node.get("data", {})
            break

    disk_specs = [
        build_troshkavm_disk_spec(d, topology) for d in _find_vm_disks(vm_id, topology)
    ]

    spec: dict = {
        "vmId": vm_data.get("id", vm_id),
        "name": vm.get("name", "vm"),
        "cpus": vm.get("vcpus", 2),
        "memory": vm.get("ram_gb", 4) * 1024,
        "firmware": _resolve_vm_firmware(vm, vm_data),
        "smbiosUuid": vm_data.get("smbiosUuid") or vm_data.get("domainUuid", ""),
        "os": vm.get("os", ""),
        "powerOnAtDeploy": vm_data.get("powerOnAtDeploy", True),
        "recertEnabled": vm_data.get("recertEnabled", False),
        "ocpMonitor": vm_data.get("ocpMonitor", False),
        "configureBastionBrowser": vm_data.get("configureBastionBrowser", False),
        "bmcEnabled": vm_data.get("bmcEnabled", False),
        "videoModel": vm_data.get("videoModel", "virtio"),
        "inputModel": vm_data.get("inputModel", "virtio"),
        "serialModel": vm_data.get("serialModel", "isa"),
        "serialConsole": vm_data.get("serialConsole", True),
        "serialExecType": vm_data.get("serialExecType", ""),
        "disks": disk_specs,
        "nics": build_troshkavm_nic_specs(vm_data, topology),
        "bootOrder": vm_data.get("bootDevices", []),
        "cloudInit": {
            "userData": vm_data.get("ciGeneratedUserData", "")
            or vm_data.get("ciUserData", ""),
            "networkConfig": vm_data.get("ciNetworkConfig", ""),
        },
    }
    if vm_data.get("bmcIp"):
        spec["bmcIp"] = vm_data["bmcIp"]
    if vm_data.get("guestfishCommands"):
        spec["guestfishCommands"] = vm_data["guestfishCommands"]
    if vm_data.get("legacyRootBus"):
        spec["legacyRootBus"] = True
    if vm_data.get("nestedVirt"):
        spec["nestedVirt"] = True
    pxe_path = vm_data.get("pxeBootIsoS3Path") or vm_data.get("pxeBootIsoResolvedPath")
    if vm_data.get("pxeBootIsoId") and pxe_path:
        spec["cdrom"] = {
            "libraryIsoId": vm_data.get("pxeBootIsoId", ""),
            "s3Path": pxe_path,
        }
    return spec


_KUBEVIRT_UNSUPPORTED_DISK_BUSES = frozenset({"ide"})


def validate_kubevirt_vm_disk_buses(
    current: dict, vm_ids: list[str], capabilities: dict | None = None
) -> str | None:
    """Return an error message when a VM uses disk buses rejected by KubeVirt."""
    if capabilities:
        from app.services.providers.kubevirt_capabilities import (
            validate_vm_disk_buses_against_capabilities,
        )

        return validate_vm_disk_buses_against_capabilities(
            current, vm_ids, capabilities
        )

    vms = {v["node_id"]: v for v in _extract_vms(current)}
    for vm_id in vm_ids:
        vm = vms.get(vm_id)
        if not vm:
            continue
        spec = build_troshkavm_vm_spec(vm_id, vm, current)
        for disk in spec.get("disks", []):
            bus = disk.get("bus", "virtio")
            if bus in _KUBEVIRT_UNSUPPORTED_DISK_BUSES:
                vm_name = vm.get("name", vm_id[:8])
                return (
                    f"VM {vm_name}: {bus.upper()} disk bus is not supported on KubeVirt"
                )
    return None


def _normalize_disk_controllers(controllers: list | None) -> list[dict]:
    """Normalize disk controller list for stable redeploy comparison."""
    return sorted(
        (
            {
                "id": dc.get("id"),
                "bus": dc.get("bus", "virtio"),
                "rotationRate": dc.get("rotationRate"),
            }
            for dc in (controllers or [])
        ),
        key=lambda dc: dc.get("id") or "",
    )


def _vm_redeploy_spec(data: dict) -> dict:
    """Domain-defining VM fields that require destroy+recreate when changed."""
    return {
        "firmware": data.get("firmware", "bios"),
        "secureBoot": data.get("secureBoot", False),
        "videoModel": data.get("videoModel", "virtio"),
        "inputModel": data.get("inputModel", "virtio"),
        "smbiosUuid": data.get("smbiosUuid")
        or data.get("uuid")
        or data.get("domainUuid"),
        "diskControllers": _normalize_disk_controllers(data.get("diskControllers")),
        "pxeBootIsoId": data.get("pxeBootIsoId"),
        "pxeBootIsoName": data.get("pxeBootIsoName"),
    }


def vm_ids_needing_redeploy(current: dict, deployed: dict) -> set[str]:
    """VM node IDs whose domain-defining properties changed and need redeploy."""
    cur_nodes = {
        n["id"]: n for n in current.get("nodes", []) if n.get("type") == "vmNode"
    }
    dep_nodes = {
        n["id"]: n for n in deployed.get("nodes", []) if n.get("type") == "vmNode"
    }
    need: set[str] = set()
    for nid, node in cur_nodes.items():
        if nid not in dep_nodes:
            continue
        cur = node.get("data", {})
        dep = dep_nodes[nid].get("data", {})
        if _vm_redeploy_spec(cur) != _vm_redeploy_spec(dep):
            need.add(nid)
    return need


# Backward-compatible alias for callers/tests added before the broader check.
vm_ids_needing_firmware_redeploy = vm_ids_needing_redeploy


def _find_changed_vms(
    cur_nodes: dict[str, dict], dep_nodes: dict[str, dict]
) -> list[dict]:
    skip_keys = {"status", "redeployStep", "redeployDetail", "liveBootDevs"}
    changed = []
    for nid, node in cur_nodes.items():
        if nid not in dep_nodes or node.get("type") != "vmNode":
            continue
        cur_data = {k: v for k, v in node.get("data", {}).items() if k not in skip_keys}
        dep_data = {
            k: v
            for k, v in dep_nodes[nid].get("data", {}).items()
            if k not in skip_keys
        }
        if cur_data != dep_data:
            changed.append(node)
    return changed


def _auto_assign_container_ips(topology: dict) -> None:
    """Assign IPs to container NICs that don't have static IPs.

    Mutates topology in-place. Picks IPs from the connected network's CIDR,
    avoiding all IPs already used by VMs or other containers.
    """
    nodes = topology.get("nodes", [])
    edges = topology.get("edges", [])
    used_ips = _collect_used_ips(topology)
    net_nodes = {n["id"]: n for n in nodes if n.get("type") == "networkNode"}

    for node in nodes:
        if node.get("type") != "containerNode":
            continue
        data = node.get("data", {})
        for nic in data.get("nics", []):
            if nic.get("ip"):
                continue
            _assign_container_nic_ip(node, nic, edges, net_nodes, used_ips, data)


def _assign_container_nic_ip(
    node: dict,
    nic: dict,
    edges: list[dict],
    net_nodes: dict[str, dict],
    used_ips: set[str],
    data: dict,
) -> None:
    net_node = _find_container_network(node["id"], nic, edges, net_nodes)
    if not net_node:
        return

    cidr = net_node.get("data", {}).get("cidr", "")
    if not cidr:
        return

    net_data = net_node.get("data", {})
    dhcp_range = _get_dhcp_range(net_data)
    if not dhcp_range:
        return

    candidate = _pick_available_ip(dhcp_range, used_ips)
    if candidate:
        nic["ip"] = candidate
        used_ips.add(candidate)
        logger.info(
            "Auto-assigned %s to container %s NIC %s (from DHCP range)",
            candidate,
            data.get("name"),
            nic.get("name"),
        )


def _find_container_network(
    node_id: str, nic: dict, edges: list[dict], net_nodes: dict[str, dict]
) -> dict | None:
    nic_handle_top = f"nic-{nic['id']}-top"
    nic_handle_bottom = f"nic-{nic['id']}-bottom"
    for edge in edges:
        src, tgt = edge.get("source", ""), edge.get("target", "")
        sh, th = edge.get("sourceHandle", ""), edge.get("targetHandle", "")
        if src == node_id and sh in (nic_handle_top, nic_handle_bottom):
            net_node = net_nodes.get(tgt)
            if net_node:
                return net_node
        elif tgt == node_id and th in (nic_handle_top, nic_handle_bottom):
            net_node = net_nodes.get(src)
            if net_node:
                return net_node
    return None


def _pick_available_ip(dhcp_range: tuple[int, int], used_ips: set[str]) -> str | None:
    start_int, end_int = dhcp_range
    for addr_int in range(start_int, end_int + 1):
        candidate = str(ipaddress.ip_address(addr_int))
        if candidate not in used_ips:
            return candidate
    return None


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
        range_start, range_end = _compute_dhcp_bounds(
            net_data.get("cidr", ""), range_start, range_end
        )
    if range_start and range_end:
        try:
            return (
                int(ipaddress.ip_address(range_start)),
                int(ipaddress.ip_address(range_end)),
            )
        except ValueError:
            pass
    return None


def _compute_dhcp_bounds(
    cidr: str, range_start: str, range_end: str
) -> tuple[str, str]:
    if not cidr:
        return range_start, range_end
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
    return range_start, range_end
