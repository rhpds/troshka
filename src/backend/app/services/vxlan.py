"""
VXLAN mesh networking service.

Manages VNI allocation, mesh topology, and generates network configuration
for the host agent to apply.

VNI ranges:
  1000-16777000: project networks (auto-allocated)
  0-999: reserved
"""

import logging
import os

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

VNI_MIN = 1000
VNI_MAX = 16_777_000


def _transit_subnet(vni: int) -> dict:
    """Calculate the unique /30 transit subnet for a VNI's veth pair.

    Returns dict with host_ip, ns_ip, and cidr for the transit link.
    Uses 172.30.0.0/16 space, derived deterministically from VNI.
    """
    octet3 = (vni >> 2) & 0xFF
    octet4_base = (vni & 0x03) * 4
    return {
        "host_ip": f"172.30.{octet3}.{octet4_base + 1}",
        "ns_ip": f"172.30.{octet3}.{octet4_base + 2}",
        "cidr": f"172.30.{octet3}.{octet4_base}/30",
    }


def _get_all_used_vnis(db: Session) -> set[int]:
    """Collect all VNIs currently in use across all projects."""
    from app.models.project import Project

    used = set()
    for project in db.query(Project).filter(Project.vni_map.isnot(None)).all():
        for vni in (project.vni_map or {}).values():
            used.add(int(vni))
    return used


def allocate_vnis_for_project(db: Session, topology: dict) -> dict[str, int]:
    """Allocate VNIs for all networks in a project topology.

    Returns a mapping of canvas node ID -> VNI.
    Only allocates for 'network' subtype nodes (not routers/gateways).
    """
    nodes = topology.get("nodes", [])
    network_nodes = [
        n
        for n in nodes
        if n.get("type") == "networkNode"
        and n.get("data", {}).get("subtype") == "network"
        and n.get("data", {}).get("networkType") != "bmc"
    ]

    used_vnis = _get_all_used_vnis(db)
    db_max = max(used_vnis, default=VNI_MIN - 1)

    # Read high-water mark from host file (survives DB wipes)
    hwm_file = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        ".vni_hwm",
    )
    file_hwm = VNI_MIN - 1
    try:
        with open(hwm_file) as f:
            file_hwm = int(f.read().strip())
    except (FileNotFoundError, ValueError):
        pass

    next_vni = max(db_max, file_hwm) + 1

    vni_map = {}
    for node in network_nodes:
        if next_vni > VNI_MAX:
            raise ValueError("VNI pool exhausted")
        vni_map[node["id"]] = next_vni
        used_vnis.add(next_vni)
        logger.info(
            "Allocated VNI %d for network %s", next_vni, node["data"].get("name")
        )
        next_vni += 1

    # Persist high-water mark
    if vni_map:
        hwm = max(vni_map.values())
        try:
            with open(hwm_file, "w") as f:
                f.write(str(hwm))
        except OSError:
            pass

    return vni_map


def build_host_network_config(
    topology: dict, vni_map: dict[str, int], peer_ips: list[str]
) -> dict:
    """Build the network configuration a host agent needs to set up VXLAN.

    Returns a structure the agent can use to create bridges, VXLAN interfaces,
    and configure DHCP/DNS/nftables.
    """
    nodes = topology.get("nodes", [])
    edges = topology.get("edges", [])

    networks = []
    for node in nodes:
        if node.get("type") != "networkNode":
            continue
        data = node.get("data", {})
        subtype = data.get("subtype", "network")

        if subtype != "network":
            continue

        node_id = node["id"]
        vni = vni_map.get(node_id)
        if not vni:
            continue

        # Find connected VMs
        connected_vm_ids = set()
        for edge in edges:
            if edge["source"] == node_id:
                connected_vm_ids.add(edge["target"])
            elif edge["target"] == node_id:
                connected_vm_ids.add(edge["source"])

        connected_vms = []
        dhcp_hosts = []
        pxe_boot_iso_ids = set()
        pxe_vm_boot_config = {}
        for vm_node in nodes:
            if vm_node["id"] in connected_vm_ids and vm_node.get("type") == "vmNode":
                vm_data = vm_node.get("data", {})
                connected_vms.append(
                    {
                        "vm_id": vm_node["id"],
                        "name": vm_data.get("name"),
                    }
                )
                pxe_iso_id = vm_data.get("pxeBootIsoId")
                if pxe_iso_id:
                    pxe_boot_iso_ids.add(pxe_iso_id)
                if not pxe_vm_boot_config:
                    for field in (
                        "pxeMethod",
                        "pxeNextServer",
                        "pxeBootFile",
                        "ipxeScriptUrl",
                        "uefiBootUrl",
                    ):
                        if vm_data.get(field):
                            pxe_vm_boot_config[field] = vm_data[field]
                for vm_nic in vm_data.get("nics", []):
                    if vm_nic.get("ip") and vm_nic.get("mac"):
                        nic_handle_top = f"nic-{vm_nic['id']}-top"
                        nic_handle_bottom = f"nic-{vm_nic['id']}-bottom"
                        on_this_net = any(
                            (
                                (
                                    e.get("source") == vm_node["id"]
                                    and e.get("target") == node_id
                                    and (
                                        e.get("sourceHandle") == nic_handle_top
                                        or e.get("sourceHandle") == nic_handle_bottom
                                    )
                                )
                                or (
                                    e.get("target") == vm_node["id"]
                                    and e.get("source") == node_id
                                    and (
                                        e.get("targetHandle") == nic_handle_top
                                        or e.get("targetHandle") == nic_handle_bottom
                                    )
                                )
                            )
                            for e in edges
                        )
                        if on_this_net:
                            dhcp_hosts.append(
                                {
                                    "mac": vm_nic["mac"],
                                    "ip": vm_nic["ip"],
                                    "name": vm_data.get("name", ""),
                                }
                            )

        net_config = {
            "node_id": node_id,
            "name": data.get("name"),
            "cidr": data.get("cidr"),
            "vni": vni,
            "bridge_name": f"br-{vni}",
            "vxlan_name": f"vxlan-{vni}",
            "dhcp_enabled": data.get("dhcp", False),
            "dns_enabled": data.get("dns", False),
            "dns_domain": data.get("dnsDomain", ""),
            "dns_records": data.get("dnsRecords", []),
            "connected_vms": connected_vms,
            "dhcp_hosts": dhcp_hosts,
            "peers": peer_ips,
        }

        # DHCP config — auto-generate from CIDR if not explicitly set
        if net_config["dhcp_enabled"]:
            range_start = data.get("dhcpRangeStart", "")
            range_end = data.get("dhcpRangeEnd", "")
            gateway = data.get("dhcpGateway", "")

            net_cidr = data.get("cidr", "")
            if net_cidr and (not range_start or not range_end or not gateway):
                import ipaddress

                try:
                    network = ipaddress.ip_network(net_cidr, strict=False)
                    hosts = list(network.hosts())
                    if not gateway:
                        gateway = str(hosts[0])
                    if not range_start:
                        range_start = str(hosts[min(9, len(hosts) - 2)])
                    if not range_end:
                        range_end = str(hosts[-1])
                except (ValueError, IndexError):
                    pass

            net_config["dhcp_config"] = {
                "range_start": range_start,
                "range_end": range_end,
                "gateway": gateway,
                "lease_time": data.get("dhcpLeaseTime", "24h"),
            }

        # PXE config
        if data.get("pxeEnabled"):
            server_mode = data.get("pxeServerMode", "builtin")
            pxe_config = {
                "method": pxe_vm_boot_config.get("pxeMethod", "legacy"),
                "server_mode": server_mode,
                "next_server": pxe_vm_boot_config.get("pxeNextServer", ""),
                "boot_file": pxe_vm_boot_config.get("pxeBootFile", ""),
                "ipxe_script_url": pxe_vm_boot_config.get("ipxeScriptUrl", ""),
                "uefi_boot_url": pxe_vm_boot_config.get("uefiBootUrl", ""),
            }
            if server_mode == "builtin" and pxe_boot_iso_ids:
                iso_id = next(iter(pxe_boot_iso_ids))
                pxe_config["iso_path"] = f"/var/lib/troshka/images/{iso_id}.iso"
                pxe_config["tftp_root"] = f"/var/lib/troshka/pxe/{vni}/tftpboot"
                pxe_config["http_port"] = 8080 + (vni % 1000)
            net_config["pxe_config"] = pxe_config

        networks.append(net_config)

    # Build gateway config if present
    gateway_config = None
    for node in nodes:
        if (
            node.get("type") == "networkNode"
            and node.get("data", {}).get("subtype") == "gateway"
        ):
            data = node.get("data", {})
            external_ips = topology.get("externalIps", [])
            eip_map = {eip["id"]: eip for eip in external_ips}

            port_forwards = []
            for pf in data.get("portForwards", []):
                pf_entry = dict(pf)
                ext_ip = eip_map.get(pf.get("extIpId", ""), {})
                pf_entry["_private_ip"] = ext_ip.get("_private_ip", "")
                transit_map = ext_ip.get("_transit_port_map")
                if transit_map:
                    ext_port_str = str(pf.get("extPort", ""))
                    pf_entry["_transit_port"] = transit_map.get(ext_port_str)
                port_forwards.append(pf_entry)

            gateway_config = {
                "name": data.get("name"),
                "mode": data.get("gatewayMode", "nat"),
                "outbound_policy": data.get("outboundPolicy", "allow-all"),
                "outbound_ports": data.get("outboundPorts", ""),
                "port_forwards": port_forwards,
                "eip_private_ips": [
                    eip.get("_private_ip", "")
                    for eip in external_ips
                    if eip.get("_private_ip")
                ],
            }
            # Add transit subnet info for host-side EIP DNAT
            first_vni = networks[0]["vni"] if networks else None
            if first_vni:
                transit = _transit_subnet(first_vni)
                gateway_config["transit_ns_ip"] = transit["ns_ip"]
            break

    # Build router configs
    router_configs = []
    for node in nodes:
        if (
            node.get("type") == "networkNode"
            and node.get("data", {}).get("subtype") == "router"
        ):
            data = node.get("data", {})

            # Find which networks this router connects to
            connected_net_ids = set()
            for edge in edges:
                other_id = (
                    edge["target"]
                    if edge["source"] == node["id"]
                    else edge["source"]
                    if edge["target"] == node["id"]
                    else None
                )
                if other_id:
                    connected_net_ids.add(other_id)

            connected_vnis = [
                vni_map[nid] for nid in connected_net_ids if nid in vni_map
            ]

            router_configs.append(
                {
                    "name": data.get("name"),
                    "connected_vnis": connected_vnis,
                    "static_routes": data.get("staticRoutes", []),
                }
            )

    # Build load balancer config if present
    lb_config = None
    for node in nodes:
        if (
            node.get("type") == "networkNode"
            and node.get("data", {}).get("networkType") == "loadbalancer"
        ):
            data = node.get("data", {})
            node_id = node["id"]

            connected_vm_ids = set()
            for edge in edges:
                other_id = (
                    edge["target"]
                    if edge["source"] == node_id
                    else edge["source"]
                    if edge["target"] == node_id
                    else None
                )
                if other_id:
                    other_node = next((n for n in nodes if n["id"] == other_id), None)
                    if other_node and other_node.get("type") == "vmNode":
                        connected_vm_ids.add(other_id)

            backends = []
            for vm_id in connected_vm_ids:
                vm_node = next((n for n in nodes if n["id"] == vm_id), None)
                if not vm_node:
                    continue
                vm_data = vm_node.get("data", {})
                vm_name = vm_data.get("name", vm_id[:8])
                for nic in vm_data.get("nics", []):
                    ip = nic.get("ip")
                    if ip:
                        backends.append({"name": vm_name, "ip": ip})
                        break

            lb_config = {
                "name": data.get("name"),
                "frontends": data.get("frontends", []),
                "lb_ip": data.get("lbIp", ""),
                "external": data.get("external", True),
                "ext_ip_id": data.get("extIpId", ""),
                "backends": backends,
                "dns_records": data.get("dnsRecords", []),
                "dns_ttl": data.get("dnsTtl", 30),
            }
            break

    return {
        "networks": networks,
        "gateway": gateway_config,
        "routers": router_configs,
        "loadbalancer": lb_config,
        "vni_map": vni_map,
    }


AGENT_SETUP_SCRIPT = """#!/bin/bash
# Auto-generated by troshka — sets up VXLAN mesh networking on a host
# Uses network namespaces for per-project isolation.
set -uo pipefail

# ── Namespace + veth setup ──
{namespace_commands}

# ── VXLAN + Bridge setup (inside namespace) ──
{vxlan_commands}

# ── DHCP (dnsmasq inside namespace) ──
{dhcp_commands}

# ── nftables inside namespace ──
{nft_commands}

# ── nftables in host namespace ──
{host_nft_commands}

echo "Network setup complete"
"""
