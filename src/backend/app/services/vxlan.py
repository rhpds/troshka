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
import threading

from sqlalchemy.orm import Session

_vni_lock = threading.Lock()

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
    Thread-safe: uses _vni_lock to prevent concurrent allocations from
    getting the same VNIs.
    """
    nodes = topology.get("nodes", [])
    network_nodes = [
        n
        for n in nodes
        if n.get("type") == "networkNode"
        and n.get("data", {}).get("subtype") == "network"
        and n.get("data", {}).get("networkType") != "bmc"
    ]

    with _vni_lock:
        used_vnis = _get_all_used_vnis(db)
        db_max = max(used_vnis, default=VNI_MIN - 1)

        hwm_file = os.path.join(
            os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            ),
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
                "Allocated VNI %d for network %s",
                next_vni,
                node["data"].get("name"),
            )
            next_vni += 1

        if vni_map:
            hwm = max(vni_map.values())
            try:
                with open(hwm_file, "w") as f:
                    f.write(str(hwm))
            except OSError:
                pass

    return vni_map


def _is_nic_on_network(
    vm_node_id: str, network_node_id: str, nic_id: str, edges: list
) -> bool:
    """Check if a NIC is connected to a specific network via edge handles."""
    handle_top = f"nic-{nic_id}-top"
    handle_bottom = f"nic-{nic_id}-bottom"
    for e in edges:
        src, tgt = e.get("source"), e.get("target")
        if src == vm_node_id and tgt == network_node_id:
            sh = e.get("sourceHandle")
            if sh == handle_top or sh == handle_bottom:
                return True
        elif tgt == vm_node_id and src == network_node_id:
            th = e.get("targetHandle")
            if th == handle_top or th == handle_bottom:
                return True
    return False


def _extract_pxe_boot_config(vm_data: dict) -> dict:
    """Extract PXE boot config fields from VM node data."""
    config = {}
    for field in (
        "pxeMethod",
        "pxeNextServer",
        "pxeBootFile",
        "ipxeScriptUrl",
        "uefiBootUrl",
    ):
        if vm_data.get(field):
            config[field] = vm_data[field]
    return config


def _collect_dhcp_hosts_for_vm(
    vm_node: dict, vm_data: dict, network_node_id: str, edges: list
) -> list[dict]:
    """Collect DHCP host entries for NICs connected to a specific network."""
    hosts = []
    for vm_nic in vm_data.get("nics", []):
        if not vm_nic.get("ip") or not vm_nic.get("mac"):
            continue
        if _is_nic_on_network(vm_node["id"], network_node_id, vm_nic["id"], edges):
            hosts.append(
                {
                    "mac": vm_nic["mac"],
                    "ip": vm_nic["ip"],
                    "name": vm_data.get("name", ""),
                }
            )
    return hosts


def _find_connected_workloads(
    node_id: str, nodes: list, edges: list
) -> tuple[list, list, set, dict]:
    """Find VMs/containers connected to a network node.

    Returns (connected_vms, dhcp_hosts, pxe_boot_iso_ids, pxe_vm_boot_config).
    """
    connected_vm_ids = set()
    for edge in edges:
        if edge["source"] == node_id:
            connected_vm_ids.add(edge["target"])
        elif edge["target"] == node_id:
            connected_vm_ids.add(edge["source"])

    connected_vms = []
    dhcp_hosts = []
    pxe_boot_iso_ids: set[str] = set()
    pxe_vm_boot_config: dict = {}
    for vm_node in nodes:
        if vm_node["id"] not in connected_vm_ids:
            continue
        if vm_node.get("type") not in ("vmNode", "containerNode"):
            continue
        vm_data = vm_node.get("data", {})
        connected_vms.append({"vm_id": vm_node["id"], "name": vm_data.get("name")})
        pxe_iso_id = vm_data.get("pxeBootIsoId")
        if pxe_iso_id:
            pxe_boot_iso_ids.add(pxe_iso_id)
        if not pxe_vm_boot_config:
            pxe_vm_boot_config = _extract_pxe_boot_config(vm_data)
        dhcp_hosts.extend(_collect_dhcp_hosts_for_vm(vm_node, vm_data, node_id, edges))
    return connected_vms, dhcp_hosts, pxe_boot_iso_ids, pxe_vm_boot_config


def _bogus_mac_for_ip(ip: str) -> str:
    """Deterministic locally-administered (02:) MAC for a VIP reservation — never
    collides with a real VM NIC (52:54:) and is stable across redeploys."""
    parts = [int(p) & 0xFF for p in ip.split(".")]
    return "02:00:%02x:%02x:%02x:%02x" % (parts[0], parts[1], parts[2], parts[3])


def _cluster_vip_reservations(
    net_node_id: str, nodes: list[dict], edges: list[dict]
) -> list[dict]:
    """dhcp-host reservations (bogus MAC) for each cluster VIP whose members are
    on this network, so dnsmasq excludes the VIPs from the dynamic pool."""
    reservations: list[dict] = []
    for boundary in nodes:
        if boundary.get("type") != "clusterNode":
            continue
        d = boundary.get("data", {})
        vips = [
            (label, str(d.get(key) or "").strip())
            for label, key in (("api", "apiVip"), ("ingress", "ingressVip"))
        ]
        vips = [(label, vip) for label, vip in vips if vip]
        if not vips:
            continue
        members = {
            n["id"]
            for n in nodes
            if n.get("type") == "vmNode" and n.get("parentId") == boundary["id"]
        }
        on_net = any(
            (e.get("source") == net_node_id and e.get("target") in members)
            or (e.get("target") == net_node_id and e.get("source") in members)
            for e in edges
        )
        if not on_net:
            continue
        name = d.get("name", "cluster")
        for label, vip in vips:
            reservations.append(
                {"mac": _bogus_mac_for_ip(vip), "ip": vip, "name": f"{name}-{label}"}
            )
    return reservations


def _build_dhcp_config(data: dict) -> dict:
    """Build DHCP config from network data, auto-generating from CIDR if needed."""
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

    return {
        "range_start": range_start,
        "range_end": range_end,
        "gateway": gateway,
        "lease_time": data.get("dhcpLeaseTime", "24h"),
    }


def _build_pxe_config(
    data: dict, pxe_vm_boot_config: dict, pxe_boot_iso_ids: set, vni: int
) -> dict | None:
    """Build PXE config if PXE is enabled on this network."""
    if not data.get("pxeEnabled"):
        return None
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
    return pxe_config


def _resolved_dns_records_for_network(
    net_data: dict, nodes: list, vni_map: dict[str, int]
) -> list[dict]:
    """Resolve dnsRecords targets (incl. showroom infra IP) for dnsmasq."""
    from app.services.deploy_topology import _is_showroom_node, showroom_infra_ip
    from app.services.template_loader import (
        _collect_workload_ips,
        _resolve_dns_record_entry,
    )

    workload_ips = _collect_workload_ips(nodes)
    if any(_is_showroom_node(n) for n in nodes):
        infra_ip = showroom_infra_ip(vni_map)
        if infra_ip:
            workload_ips["showroom"] = infra_ip
    out: list[dict] = []
    for rec in net_data.get("dnsRecords", []):
        resolved = _resolve_dns_record_entry(rec, workload_ips)
        name = resolved.get("name", "")
        ip = resolved.get("ip", "")
        if name and ip:
            out.append({"name": name, "ip": ip})
    return out


def _build_network_configs(
    nodes: list, edges: list, vni_map: dict, peer_ips: list
) -> list:
    """Build configuration for all network-subtype nodes."""
    networks = []
    for node in nodes:
        if node.get("type") != "networkNode":
            continue
        data = node.get("data", {})
        if data.get("subtype", "network") != "network":
            continue

        node_id = node["id"]
        vni = vni_map.get(node_id)
        if not vni:
            continue

        (
            connected_vms,
            dhcp_hosts,
            pxe_boot_iso_ids,
            pxe_vm_boot_config,
        ) = _find_connected_workloads(node_id, nodes, edges)

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
            "dns_records": _resolved_dns_records_for_network(data, nodes, vni_map),
            "connected_vms": connected_vms,
            "dhcp_hosts": dhcp_hosts,
            "peers": peer_ips,
        }

        if net_config["dhcp_enabled"]:
            net_config["dhcp_config"] = _build_dhcp_config(data)
            # Reserve each cluster VIP on this network with a deterministic bogus
            # (locally-administered) MAC, so dnsmasq holds the address and never
            # hands it out from the dynamic pool. (Node static IPs are already
            # reserved via their real NIC MAC in dhcp_hosts.)
            net_config["dhcp_hosts"].extend(
                _cluster_vip_reservations(node_id, nodes, edges)
            )

        pxe_config = _build_pxe_config(data, pxe_vm_boot_config, pxe_boot_iso_ids, vni)
        if pxe_config:
            net_config["pxe_config"] = pxe_config

        networks.append(net_config)
    return networks


def _topology_has_showroom(topology: dict) -> bool:
    from app.services.deploy_topology import _is_showroom_node

    return any(_is_showroom_node(n) for n in topology.get("nodes", []))


def _is_showroom_infra_forward(pf: dict) -> bool:
    """Gateway PF 443→172.30.{vni}.3:80 auto-managed for showroom."""
    int_ip = (pf.get("intIp") or "").strip()
    if not int_ip.startswith("172.30.") or not int_ip.endswith(".3"):
        return False
    return str(pf.get("extPort")) == "443" and str(pf.get("intPort")) == "80"


def _inject_showroom_port_forward(
    port_forwards: list, topology: dict, first_vni: int | None
) -> list:
    """Auto-add gateway PF 443→showroom infra:80 (transit netns, not lab DHCP)."""
    if not first_vni or not _topology_has_showroom(topology):
        return port_forwards
    octet3 = int(first_vni) & 0xFF
    infra_ip = f"172.30.{octet3}.3"

    out = [
        pf
        for pf in port_forwards
        if not (
            str(pf.get("extPort")) == "443"
            and str(pf.get("intPort")) == "80"
            and (pf.get("intIp") or "").strip() != infra_ip
        )
    ]

    if not any(
        str(pf.get("extPort")) == "443"
        and (pf.get("intIp") or "").strip() == infra_ip
        and str(pf.get("intPort")) == "80"
        for pf in out
    ):
        out.append(
            {
                "extPort": "443",
                "intIp": infra_ip,
                "intPort": "80",
                "proto": "tcp",
                "extIpId": "",
                "managedByShowroom": True,
            }
        )
    return out


def _build_gateway_config(nodes: list, topology: dict, networks: list) -> dict | None:
    """Build gateway config from the gateway node, if present."""
    for node in nodes:
        if (
            node.get("type") != "networkNode"
            or node.get("data", {}).get("subtype") != "gateway"
        ):
            continue
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

        first_vni = networks[0]["vni"] if networks else None
        port_forwards = _inject_showroom_port_forward(
            port_forwards, topology, first_vni
        )

        gw_config = {
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
        if first_vni:
            transit = _transit_subnet(first_vni)
            gw_config["transit_ns_ip"] = transit["ns_ip"]
        return gw_config
    return None


def _build_router_configs(nodes: list, edges: list, vni_map: dict) -> list:
    """Build configuration for all router nodes."""
    router_configs = []
    for node in nodes:
        if (
            node.get("type") != "networkNode"
            or node.get("data", {}).get("subtype") != "router"
        ):
            continue
        data = node.get("data", {})

        connected_net_ids = set()
        for edge in edges:
            if edge["source"] == node["id"]:
                connected_net_ids.add(edge["target"])
            elif edge["target"] == node["id"]:
                connected_net_ids.add(edge["source"])

        connected_vnis = [vni_map[nid] for nid in connected_net_ids if nid in vni_map]
        router_configs.append(
            {
                "name": data.get("name"),
                "connected_vnis": connected_vnis,
                "static_routes": data.get("staticRoutes", []),
            }
        )
    return router_configs


def _find_connected_lb_backend_ids(node_id: str, nodes: list, edges: list) -> set[str]:
    """Find VM and pod IDs connected to a load balancer via canvas edges."""
    backend_ids: set[str] = set()
    for edge in edges:
        if edge["source"] == node_id:
            other_id = edge["target"]
        elif edge["target"] == node_id:
            other_id = edge["source"]
        else:
            continue
        other_node = next((n for n in nodes if n["id"] == other_id), None)
        if not other_node:
            continue
        if other_node.get("type") == "vmNode":
            backend_ids.add(other_id)
        elif other_node.get("type") == "containerNode":
            data = other_node.get("data", {})
            if data.get("isShowroom") or data.get("name") == "showroom":
                continue
            if data.get("isPod"):
                backend_ids.add(other_id)
    return backend_ids


def _build_lb_backends(backend_ids: set[str], nodes: list) -> list[dict]:
    """Build backend entries from connected VM and pod nodes."""
    backends = []
    for node_id in backend_ids:
        node = next((n for n in nodes if n["id"] == node_id), None)
        if not node:
            continue
        data = node.get("data", {})
        name = data.get("name", node_id[:8])
        for nic in data.get("nics", []):
            ip = nic.get("ip")
            if ip:
                backends.append({"name": name, "ip": ip})
                break
    return backends


def _build_lb_config(nodes: list, edges: list) -> dict | None:
    """Build load balancer config from the LB node, if present."""
    for node in nodes:
        if (
            node.get("type") != "networkNode"
            or node.get("data", {}).get("networkType") != "loadbalancer"
        ):
            continue
        data = node.get("data", {})
        connected_backend_ids = _find_connected_lb_backend_ids(node["id"], nodes, edges)
        backends = _build_lb_backends(connected_backend_ids, nodes)

        return {
            "name": data.get("name"),
            "frontends": data.get("frontends", []),
            "lb_ip": data.get("lbIp", ""),
            "external": data.get("external", True),
            "ext_ip_id": data.get("extIpId", ""),
            "backends": backends,
            "dns_records": data.get("dnsRecords", []),
            "dns_ttl": data.get("dnsTtl", 30),
        }
    return None


def build_host_network_config(
    topology: dict, vni_map: dict[str, int], peer_ips: list[str]
) -> dict:
    """Build the network configuration a host agent needs to set up VXLAN.

    Returns a structure the agent can use to create bridges, VXLAN interfaces,
    and configure DHCP/DNS/nftables.
    """
    nodes = topology.get("nodes", [])
    edges = topology.get("edges", [])

    networks = _build_network_configs(nodes, edges, vni_map, peer_ips)
    gateway = _build_gateway_config(nodes, topology, networks)
    routers = _build_router_configs(nodes, edges, vni_map)
    lb = _build_lb_config(nodes, edges)

    return {
        "networks": networks,
        "gateway": gateway,
        "routers": routers,
        "loadbalancer": lb,
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
