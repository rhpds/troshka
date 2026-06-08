"""
VXLAN mesh networking service.

Manages VNI allocation, mesh topology, and generates network configuration
for the host agent to apply.

VNI ranges:
  1000-16777000: project networks (auto-allocated)
  0-999: reserved
"""
import logging

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
        n for n in nodes
        if n.get("type") == "networkNode"
        and n.get("data", {}).get("subtype") == "network"
    ]

    used_vnis = _get_all_used_vnis(db)
    next_vni = VNI_MIN

    vni_map = {}
    for node in network_nodes:
        while next_vni in used_vnis:
            next_vni += 1
        if next_vni > VNI_MAX:
            raise ValueError("VNI pool exhausted")
        vni_map[node["id"]] = next_vni
        used_vnis.add(next_vni)
        logger.info("Allocated VNI %d for network %s", next_vni, node["data"].get("name"))
        next_vni += 1

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
        dhcp_hosts = []
        for vm_node in nodes:
            if vm_node["id"] in connected_vm_ids and vm_node.get("type") == "vmNode":
                vm_data = vm_node.get("data", {})
                connected_vms.append({
                    "vm_id": vm_node["id"],
                    "name": vm_data.get("name"),
                })
                for vm_nic in vm_data.get("nics", []):
                    if vm_nic.get("ip") and vm_nic.get("mac"):
                        nic_handle_top = f"nic-{vm_nic['id']}-top"
                        nic_handle_bottom = f"nic-{vm_nic['id']}-bottom"
                        on_this_net = any(
                            ((e.get("source") == vm_node["id"] and e.get("target") == node_id and
                              (e.get("sourceHandle") == nic_handle_top or e.get("sourceHandle") == nic_handle_bottom)) or
                             (e.get("target") == vm_node["id"] and e.get("source") == node_id and
                              (e.get("targetHandle") == nic_handle_top or e.get("targetHandle") == nic_handle_bottom)))
                            for e in edges
                        )
                        if on_this_net:
                            dhcp_hosts.append({
                                "mac": vm_nic["mac"],
                                "ip": vm_nic["ip"],
                                "name": vm_data.get("name", ""),
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
            external_ips = topology.get("externalIps", [])
            eip_map = {eip["id"]: eip for eip in external_ips}

            port_forwards = []
            for pf in data.get("portForwards", []):
                pf_entry = dict(pf)
                ext_ip = eip_map.get(pf.get("extIpId", ""), {})
                pf_entry["_private_ip"] = ext_ip.get("_private_ip", "")
                port_forwards.append(pf_entry)

            gateway_config = {
                "name": data.get("name"),
                "mode": data.get("gatewayMode", "nat"),
                "outbound_policy": data.get("outboundPolicy", "allow-all"),
                "outbound_ports": data.get("outboundPorts", ""),
                "port_forwards": port_forwards,
                "eip_private_ips": [eip.get("_private_ip", "") for eip in external_ips if eip.get("_private_ip")],
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


def generate_setup_script(config: dict, host_ip: str, project_id: str = "") -> str:
    """Generate a shell script that sets up all networking on a host.

    Uses per-project network namespaces for full isolation.  Each project
    gets its own namespace with VXLAN interfaces, bridges, dnsmasq, and
    nftables rules.  A veth pair connects the namespace to the host for
    outbound connectivity and port forwarding.
    """
    pid = project_id[:8] if project_id else "default"
    ns = f"troshka-{pid}"
    veth_host = f"ve{pid}h"
    veth_ns = f"ve{pid}n"

    # Derive transit subnet from the first VNI
    # Use /24 to allow multiple transit IPs (one per EIP port forward)
    all_vnis = [net["vni"] for net in config.get("networks", [])]
    first_vni = all_vnis[0] if all_vnis else 1000
    transit_octet3 = first_vni & 0xFF
    transit_host_ip = f"172.30.{transit_octet3}.1"
    transit_ns_ip = f"172.30.{transit_octet3}.2"
    transit_cidr = f"172.30.{transit_octet3}.0/24"

    # ── Namespace + veth ──
    ns_cmds = [
        f"ip netns del {ns} 2>/dev/null || true",
        f"ip link del {veth_host} 2>/dev/null || true",
        f"ip netns add {ns}",
        f"ip link add {veth_host} type veth peer name {veth_ns}",
        f"ip link set {veth_ns} netns {ns}",
        f"ip addr add {transit_host_ip}/24 dev {veth_host}",
        f"ip link set {veth_host} up",
        f"ip netns exec {ns} ip addr add {transit_ns_ip}/24 dev {veth_ns}",
        f"ip netns exec {ns} ip link set {veth_ns} up",
        f"ip netns exec {ns} ip link set lo up",
        f"ip netns exec {ns} ip route add default via {transit_host_ip}",
        f"ip route add {transit_cidr} dev {veth_host} 2>/dev/null || true",
        "sysctl -w net.ipv4.ip_forward=1",
    ]

    # ── VXLAN + Bridge (inside namespace) ──
    vxlan_cmds = []
    for net in config.get("networks", []):
        vni = net["vni"]
        bridge = net["bridge_name"]
        vxlan_if = net["vxlan_name"]
        cidr = net.get("cidr", "")

        # Create VXLAN in host namespace, add peers, then move into netns
        vxlan_cmds.append(f"ip link del {vxlan_if} 2>/dev/null || true")
        vxlan_cmds.append(f"ip netns exec {ns} ip link del {vxlan_if} 2>/dev/null || true")
        vxlan_cmds.append(f"ip link add {vxlan_if} type vxlan id {vni} local {host_ip} dstport 4789 nolearning")
        for peer in net.get("peers", []):
            if peer != host_ip:
                vxlan_cmds.append(f"bridge fdb append 00:00:00:00:00:00 dev {vxlan_if} dst {peer}")
        vxlan_cmds.append(f"ip link set {vxlan_if} netns {ns}")

        # Create bridge inside namespace and attach VXLAN
        vxlan_cmds.append(f"ip netns exec {ns} ip link add {bridge} type bridge")
        vxlan_cmds.append(f"ip netns exec {ns} ip link set {vxlan_if} master {bridge}")
        vxlan_cmds.append(f"ip netns exec {ns} ip link set {vxlan_if} up")
        vxlan_cmds.append(f"ip netns exec {ns} ip link set {bridge} up")

        # Create dummy bridge in host namespace for libvirt validation
        # (libvirt checks bridge exists before starting VM; qemu hook moves tap to real bridge)
        vxlan_cmds.append(f"ip link show {bridge} &>/dev/null || ip link add {bridge} type bridge")
        vxlan_cmds.append(f"ip link set {bridge} up")

        # Assign bridge IP if DHCP/DNS is enabled (bridge acts as gateway)
        if net.get("dhcp_enabled") or net.get("dns_enabled"):
            gateway_ip = net.get("dhcp_config", {}).get("gateway", "")
            if gateway_ip and cidr:
                prefix = cidr.split("/")[1] if "/" in cidr else "24"
                vxlan_cmds.append(f"ip netns exec {ns} ip addr add {gateway_ip}/{prefix} dev {bridge}")

    # ── DHCP (dnsmasq inside namespace) ──
    dhcp_cmds = []
    for net in config.get("networks", []):
        if not net.get("dhcp_enabled"):
            continue
        vni = net["vni"]
        bridge = net["bridge_name"]
        dhcp_cfg = net.get("dhcp_config", {})
        range_start = dhcp_cfg.get("range_start", "")
        range_end = dhcp_cfg.get("range_end", "")
        lease = dhcp_cfg.get("lease_time", "24h")
        if not (range_start and range_end):
            continue

        dnsmasq_conf = f"/etc/dnsmasq.d/troshka-{vni}.conf"
        dnsmasq_pid = f"/run/troshka-dnsmasq-{vni}.pid"
        dnsmasq_lease = f"/var/lib/troshka/dnsmasq-{vni}.leases"
        dhcp_cmds.append(f"cat > {dnsmasq_conf} << 'DNSEOF'")
        dhcp_cmds.append(f"interface={bridge}")
        dhcp_cmds.append("bind-dynamic")
        dhcp_cmds.append("except-interface=lo")
        dhcp_cmds.append("no-resolv")
        dhcp_cmds.append("no-hosts")
        dhcp_cmds.append(f"pid-file={dnsmasq_pid}")
        dhcp_cmds.append(f"dhcp-leasefile={dnsmasq_lease}")
        dhcp_cmds.append(f"dhcp-range={range_start},{range_end},{lease}")
        for dh in net.get("dhcp_hosts", []):
            safe_name = (dh.get("name") or "").replace(" ", "-").replace("_", "-")
            hostname_part = f",{safe_name}" if safe_name else ""
            dhcp_cmds.append(f"dhcp-host={dh['mac']},{dh['ip']}{hostname_part}")
        if net.get("dns_enabled") and net.get("dns_domain"):
            dhcp_cmds.append(f"domain={net['dns_domain']}")
        dhcp_cmds.append("DNSEOF")
        dhcp_cmds.append(f"[ -f {dnsmasq_pid} ] && kill $(cat {dnsmasq_pid}) 2>/dev/null; rm -f {dnsmasq_pid}")
        dhcp_cmds.append(f"ip netns exec {ns} dnsmasq --conf-file={dnsmasq_conf}")

    # ── nftables INSIDE namespace ──
    nft_cmds = [
        f"ip netns exec {ns} nft add table inet filter",
        f"ip netns exec {ns} nft add chain inet filter forward '{{ type filter hook forward priority 0; policy drop; }}'",
        f"ip netns exec {ns} nft add table inet nat",
        f"ip netns exec {ns} nft add chain inet nat postrouting '{{ type nat hook postrouting priority 100; }}'",
        f"ip netns exec {ns} nft add chain inet nat prerouting '{{ type nat hook prerouting priority -100; }}'",
        # Masquerade outbound traffic from bridges (not bridge-to-bridge)
        f"ip netns exec {ns} nft add rule inet nat postrouting oifname \"{veth_ns}\" masquerade",
    ]

    # Intra-bridge forwarding (allow traffic within each bridge)
    for net in config.get("networks", []):
        bridge = net["bridge_name"]
        nft_cmds.append(f"ip netns exec {ns} nft add rule inet filter forward iifname \"{bridge}\" oifname \"{bridge}\" accept")

    # Router: enable forwarding between connected bridges
    for router in config.get("routers", []):
        for i, vni_a in enumerate(router["connected_vnis"]):
            for vni_b in router["connected_vnis"][i + 1:]:
                br_a = f"br-{vni_a}"
                br_b = f"br-{vni_b}"
                nft_cmds.append(f"ip netns exec {ns} nft add rule inet filter forward iifname \"{br_a}\" oifname \"{br_b}\" accept")
                nft_cmds.append(f"ip netns exec {ns} nft add rule inet filter forward iifname \"{br_b}\" oifname \"{br_a}\" accept")

    # Allow established/related return traffic and bridge→veth outbound
    nft_cmds.append(f"ip netns exec {ns} nft add rule inet filter forward ct state established,related accept")
    for net in config.get("networks", []):
        bridge = net["bridge_name"]
        nft_cmds.append(f"ip netns exec {ns} nft add rule inet filter forward iifname \"{bridge}\" oifname \"{veth_ns}\" accept")

    # Port forward DNAT inside namespace — each EIP gets a unique transit IP
    gw = config.get("gateway")
    if gw and gw.get("mode") == "nat-portforward":
        for pf_idx, pf in enumerate(gw.get("port_forwards", [])):
            ext_port = pf.get("extPort", "")
            int_ip = pf.get("intIp", "")
            int_port = pf.get("intPort", "")
            if ext_port and int_ip and int_port:
                pf_transit_ip = f"172.30.{transit_octet3}.{10 + pf_idx}"
                pf["_transit_ip"] = pf_transit_ip
                ns_cmds.append(f"ip netns exec {ns} ip addr add {pf_transit_ip}/24 dev {veth_ns} 2>/dev/null || true")
                nft_cmds.append(f"ip netns exec {ns} nft add rule inet nat prerouting ip daddr {pf_transit_ip} tcp dport {ext_port} dnat ip to {int_ip}:{int_port}")
                nft_cmds.append(f"ip netns exec {ns} nft add rule inet filter forward iifname \"{veth_ns}\" tcp dport {int_port} accept")

    # ── nftables in HOST namespace ──
    host_nft_cmds = []
    if gw and gw.get("mode") in ("nat", "nat-portforward"):
        fwd_chain = f"troshka-fwd-{pid}"
        post_chain = f"troshka-post-{pid}"
        pre_chain = f"troshka-pre-{pid}"

        host_nft_cmds.extend([
            "nft add table inet filter 2>/dev/null || true",
            "nft add chain inet filter forward '{ type filter hook forward priority 0; policy accept; }' 2>/dev/null || true",
            "nft add table inet nat 2>/dev/null || true",
            "nft add chain inet nat postrouting '{ type nat hook postrouting priority 100; }' 2>/dev/null || true",
            "nft add chain inet nat prerouting '{ type nat hook prerouting priority -100; }' 2>/dev/null || true",
            f"nft add chain inet filter {fwd_chain} 2>/dev/null || true",
            f"nft flush chain inet filter {fwd_chain}",
            f"nft add chain inet nat {post_chain} 2>/dev/null || true",
            f"nft flush chain inet nat {post_chain}",
            f"nft add chain inet nat {pre_chain} 2>/dev/null || true",
            f"nft flush chain inet nat {pre_chain}",
            f"nft list chain inet filter forward 2>/dev/null | grep -q 'jump {fwd_chain}' || nft add rule inet filter forward jump {fwd_chain}",
            f"nft list chain inet nat postrouting 2>/dev/null | grep -q 'jump {post_chain}' || nft add rule inet nat postrouting jump {post_chain}",
            f"nft list chain inet nat prerouting 2>/dev/null | grep -q 'jump {pre_chain}' || nft add rule inet nat prerouting jump {pre_chain}",
            # Forward traffic through veth
            f"nft add rule inet filter {fwd_chain} iifname \"{veth_host}\" accept",
            f"nft add rule inet filter {fwd_chain} oifname \"{veth_host}\" accept",
            # Masquerade transit traffic for outbound internet
            f"nft add rule inet nat {post_chain} ip saddr {transit_cidr} masquerade",
        ])

        # EIP-specific port forward DNAT in host namespace → per-EIP transit IP
        if gw.get("mode") == "nat-portforward":
            for pf in gw.get("port_forwards", []):
                ext_port = pf.get("extPort", "")
                int_ip = pf.get("intIp", "")
                int_port = pf.get("intPort", "")
                priv_ip = pf.get("_private_ip", "")
                pf_transit_ip = pf.get("_transit_ip", transit_ns_ip)
                if ext_port and int_ip and int_port:
                    if priv_ip:
                        host_nft_cmds.append(f"nft add rule inet nat {pre_chain} ip daddr {priv_ip} tcp dport {ext_port} dnat ip to {pf_transit_ip}:{ext_port}")
                    else:
                        host_nft_cmds.append(f"nft add rule inet nat {pre_chain} tcp dport {ext_port} dnat ip to {pf_transit_ip}:{ext_port}")

    return AGENT_SETUP_SCRIPT.format(
        namespace_commands="\n".join(ns_cmds) or "# No namespace setup",
        vxlan_commands="\n".join(vxlan_cmds) or "# No VXLAN networks",
        dhcp_commands="\n".join(dhcp_cmds) or "# No DHCP configured",
        nft_commands="\n".join(nft_cmds) or "# No namespace firewall rules",
        host_nft_commands="\n".join(host_nft_cmds) or "# No host firewall rules",
    )
