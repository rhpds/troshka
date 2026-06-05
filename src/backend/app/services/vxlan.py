"""
VXLAN mesh networking service.

Manages VNI allocation, mesh topology, and generates network configuration
for the host agent to apply.

VNI ranges:
  1000-16777000: project networks (auto-allocated)
  0-999: reserved
"""
import logging

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.network import Network

logger = logging.getLogger(__name__)

VNI_MIN = 1000
VNI_MAX = 16_777_000


def allocate_vni(db: Session) -> int:
    """Allocate the next available VNI."""
    max_vni = db.query(func.max(Network.vni)).scalar()
    next_vni = max(VNI_MIN, (max_vni or VNI_MIN - 1) + 1)
    if next_vni > VNI_MAX:
        raise ValueError("VNI pool exhausted")
    return next_vni


def allocate_vnis_for_project(db: Session, topology: dict) -> dict[str, int]:
    """Allocate VNIs for all networks in a project topology.

    Returns a mapping of canvas node ID -> VNI.
    Only allocates for 'network' subtype nodes (not routers/gateways).
    """
    nodes = topology.get("nodes", [])
    network_nodes = [
        n for n in nodes
        if n.get("type") == "networkNode"
        and n.get("data", {}).get("subtype") == "network"
    ]

    vni_map = {}
    for node in network_nodes:
        vni = allocate_vni(db)
        vni_map[node["id"]] = vni
        logger.info("Allocated VNI %d for network %s", vni, node["data"].get("name"))

    return vni_map


def build_host_network_config(topology: dict, vni_map: dict[str, int], peer_ips: list[str]) -> dict:
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
        for vm_node in nodes:
            if vm_node["id"] in connected_vm_ids and vm_node.get("type") == "vmNode":
                vm_data = vm_node.get("data", {})
                connected_vms.append({
                    "vm_id": vm_node["id"],
                    "name": vm_data.get("name"),
                })

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
            "connected_vms": connected_vms,
            "peers": peer_ips,
        }

        # DHCP config
        if net_config["dhcp_enabled"]:
            net_config["dhcp_config"] = {
                "range_start": data.get("dhcpRangeStart", ""),
                "range_end": data.get("dhcpRangeEnd", ""),
                "gateway": data.get("dhcpGateway", ""),
                "lease_time": data.get("dhcpLeaseTime", "24h"),
            }

        # PXE config
        if data.get("pxeEnabled"):
            net_config["pxe_config"] = {
                "method": data.get("pxeMethod", "legacy"),
                "server_mode": data.get("pxeServerMode", "builtin"),
                "firmware": data.get("pxeFirmware", "bios"),
                "next_server": data.get("pxeNextServer", ""),
                "boot_file": data.get("pxeBootFile", ""),
                "ipxe_script_url": data.get("ipxeScriptUrl", ""),
                "uefi_boot_url": data.get("uefiBootUrl", ""),
            }

        networks.append(net_config)

    # Build gateway config if present
    gateway_config = None
    for node in nodes:
        if node.get("type") == "networkNode" and node.get("data", {}).get("subtype") == "gateway":
            data = node.get("data", {})
            gateway_config = {
                "name": data.get("name"),
                "mode": data.get("gatewayMode", "nat"),
                "outbound_policy": data.get("outboundPolicy", "allow-all"),
                "outbound_ports": data.get("outboundPorts", ""),
                "port_forwards": data.get("portForwards", []),
            }
            break

    # Build router configs
    router_configs = []
    for node in nodes:
        if node.get("type") == "networkNode" and node.get("data", {}).get("subtype") == "router":
            data = node.get("data", {})

            # Find which networks this router connects to
            connected_net_ids = set()
            for edge in edges:
                other_id = edge["target"] if edge["source"] == node["id"] else edge["source"] if edge["target"] == node["id"] else None
                if other_id:
                    connected_net_ids.add(other_id)

            connected_vnis = [vni_map[nid] for nid in connected_net_ids if nid in vni_map]

            router_configs.append({
                "name": data.get("name"),
                "connected_vnis": connected_vnis,
                "static_routes": data.get("staticRoutes", []),
            })

    return {
        "networks": networks,
        "gateway": gateway_config,
        "routers": router_configs,
        "vni_map": vni_map,
    }


AGENT_SETUP_SCRIPT = """#!/bin/bash
# Auto-generated by troshka — sets up VXLAN mesh networking on a host
set -euo pipefail

# ── VXLAN + Bridge setup ──
{vxlan_commands}

# ── DHCP (dnsmasq) ──
{dhcp_commands}

# ── Routing between networks ──
{routing_commands}

# ── Gateway NAT ──
{gateway_commands}

# ── nftables security rules ──
{nftables_commands}

echo "Network setup complete"
"""


def generate_setup_script(config: dict, host_ip: str) -> str:
    """Generate a shell script that sets up all networking on a host."""
    vxlan_cmds = []
    dhcp_cmds = []
    routing_cmds = []
    gateway_cmds = []
    nft_cmds = ["nft flush ruleset"]

    for net in config.get("networks", []):
        vni = net["vni"]
        bridge = net["bridge_name"]
        vxlan_if = net["vxlan_name"]
        cidr = net.get("cidr", "")

        # Create VXLAN interface
        vxlan_cmds.append(f"ip link add {vxlan_if} type vxlan id {vni} local {host_ip} dstport 4789 nolearning")

        # Add peer destinations (head-end replication)
        for peer in net.get("peers", []):
            if peer != host_ip:
                vxlan_cmds.append(f"bridge fdb append 00:00:00:00:00:00 dev {vxlan_if} dst {peer}")

        # Create bridge and attach VXLAN
        vxlan_cmds.append(f"ip link add {bridge} type bridge")
        vxlan_cmds.append(f"ip link set {vxlan_if} master {bridge}")
        vxlan_cmds.append(f"ip link set {vxlan_if} up")
        vxlan_cmds.append(f"ip link set {bridge} up")

        # Assign bridge IP if DHCP/DNS is enabled (bridge acts as gateway)
        if net.get("dhcp_enabled") or net.get("dns_enabled"):
            gateway_ip = net.get("dhcp_config", {}).get("gateway", "")
            if gateway_ip and cidr:
                prefix = cidr.split("/")[1] if "/" in cidr else "24"
                vxlan_cmds.append(f"ip addr add {gateway_ip}/{prefix} dev {bridge}")

        # DHCP via dnsmasq
        if net.get("dhcp_enabled"):
            dhcp_cfg = net.get("dhcp_config", {})
            range_start = dhcp_cfg.get("range_start", "")
            range_end = dhcp_cfg.get("range_end", "")
            lease = dhcp_cfg.get("lease_time", "24h")
            if range_start and range_end:
                dnsmasq_conf = f"/etc/dnsmasq.d/troshka-{vni}.conf"
                dhcp_cmds.append(f"cat > {dnsmasq_conf} << 'DNSEOF'")
                dhcp_cmds.append(f"interface={bridge}")
                dhcp_cmds.append(f"dhcp-range={range_start},{range_end},{lease}")
                if net.get("dns_enabled") and net.get("dns_domain"):
                    dhcp_cmds.append(f"domain={net['dns_domain']}")
                dhcp_cmds.append("DNSEOF")

        # nftables: isolate this bridge from other project bridges
        nft_cmds.append(f"# Network {net['name']} (VNI {vni})")
        nft_cmds.append(f"nft add rule inet filter forward iifname \"{bridge}\" oifname \"{bridge}\" accept")

    # Router: enable forwarding between connected bridges
    for router in config.get("routers", []):
        for i, vni_a in enumerate(router["connected_vnis"]):
            for vni_b in router["connected_vnis"][i + 1:]:
                br_a = f"br-{vni_a}"
                br_b = f"br-{vni_b}"
                routing_cmds.append(f"nft add rule inet filter forward iifname \"{br_a}\" oifname \"{br_b}\" accept")
                routing_cmds.append(f"nft add rule inet filter forward iifname \"{br_b}\" oifname \"{br_a}\" accept")

    # Gateway NAT
    gw = config.get("gateway")
    if gw and gw.get("mode") in ("nat", "nat-portforward"):
        gateway_cmds.append("sysctl -w net.ipv4.ip_forward=1")
        # Masquerade outbound from all project bridges
        for net in config.get("networks", []):
            bridge = net["bridge_name"]
            gateway_cmds.append(f"nft add rule inet nat postrouting oifname != \"{bridge}\" iifname \"{bridge}\" masquerade")

        # Port forwards
        if gw.get("mode") == "nat-portforward":
            for pf in gw.get("port_forwards", []):
                ext_port = pf.get("extPort", "")
                int_ip = pf.get("intIp", "")
                int_port = pf.get("intPort", "")
                if ext_port and int_ip and int_port:
                    gateway_cmds.append(f"nft add rule inet nat prerouting tcp dport {ext_port} dnat to {int_ip}:{int_port}")

    return AGENT_SETUP_SCRIPT.format(
        vxlan_commands="\n".join(vxlan_cmds) or "# No VXLAN networks",
        dhcp_commands="\n".join(dhcp_cmds) or "# No DHCP configured",
        routing_commands="\n".join(routing_cmds) or "# No inter-network routing",
        gateway_commands="\n".join(gateway_cmds) or "# No gateway configured",
        nftables_commands="\n".join(nft_cmds) or "# No firewall rules",
    )
