"""
Network handler — sets up VXLAN interfaces, bridges, DHCP, DNS, and NAT.

Receives a network config from the API server and applies it to the host.
All operations are idempotent — safe to run multiple times.
"""
import logging
import re
import subprocess

logger = logging.getLogger(__name__)

_SAFE_NAME = re.compile(r"^[a-zA-Z0-9._-]+$")
_SAFE_IP = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")
_SAFE_CIDR = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}/\d{1,2}$")
_SAFE_PORT = re.compile(r"^\d{1,5}$")


def _validate_name(value: str, label: str = "name") -> str:
    if not _SAFE_NAME.match(value):
        raise ValueError(f"Invalid {label}: {value!r}")
    return value


def _validate_ip(value: str) -> str:
    if not _SAFE_IP.match(value):
        raise ValueError(f"Invalid IP: {value!r}")
    return value


def _validate_port(value: str) -> str:
    if not _SAFE_PORT.match(value):
        raise ValueError(f"Invalid port: {value!r}")
    return value


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    logger.debug("Running: %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def link_exists(name: str) -> bool:
    name = _validate_name(name, "interface")
    result = run(["ip", "link", "show", name], check=False)
    return result.returncode == 0


def setup_vxlan(vni: int, local_ip: str, peers: list[str], bridge_name: str, vxlan_name: str):
    """Create a VXLAN interface and bridge, attach VXLAN to bridge."""
    _validate_name(vxlan_name, "vxlan")
    _validate_name(bridge_name, "bridge")
    _validate_ip(local_ip)

    if not link_exists(vxlan_name):
        run(["ip", "link", "add", vxlan_name, "type", "vxlan", "id", str(vni), "local", local_ip, "dstport", "4789", "nolearning"])
        logger.info("Created VXLAN interface %s (VNI %d)", vxlan_name, vni)

    for peer in peers:
        if peer != local_ip:
            _validate_ip(peer)
            run(["bridge", "fdb", "replace", "00:00:00:00:00:00", "dev", vxlan_name, "dst", peer], check=False)

    if not link_exists(bridge_name):
        run(["ip", "link", "add", bridge_name, "type", "bridge"])
        run(["ip", "link", "set", bridge_name, "up"])
        logger.info("Created bridge %s", bridge_name)

    result = run(["ip", "link", "show", vxlan_name], check=False)
    if "master" not in result.stdout:
        run(["ip", "link", "set", vxlan_name, "master", bridge_name])

    run(["ip", "link", "set", vxlan_name, "up"])


def assign_bridge_ip(bridge_name: str, ip: str, prefix: str):
    """Assign an IP to the bridge (for DHCP/DNS gateway)."""
    _validate_name(bridge_name, "bridge")
    _validate_ip(ip)
    result = run(["ip", "addr", "show", bridge_name], check=False)
    if ip not in result.stdout:
        run(["ip", "addr", "add", f"{ip}/{prefix}", "dev", bridge_name])
        logger.info("Assigned %s/%s to %s", ip, prefix, bridge_name)


def setup_dhcp(vni: int, bridge_name: str, dhcp_config: dict, dns_config: dict | None = None):
    """Write a dnsmasq config file for DHCP on a bridge."""
    conf_path = f"/etc/dnsmasq.d/troshka-{vni}.conf"

    lines = [
        f"interface={bridge_name}",
        f"bind-interfaces",
    ]

    range_start = dhcp_config.get("range_start", "")
    range_end = dhcp_config.get("range_end", "")
    lease = dhcp_config.get("lease_time", "24h")
    if range_start and range_end:
        lines.append(f"dhcp-range={range_start},{range_end},{lease}")

    gateway = dhcp_config.get("gateway", "")
    if gateway:
        lines.append(f"dhcp-option=option:router,{gateway}")

    if dns_config:
        domain = dns_config.get("domain", "")
        if domain:
            lines.append(f"domain={domain}")
            lines.append(f"local=/{domain}/")

    content = "\n".join(lines) + "\n"

    with open(conf_path, "w") as f:
        f.write(content)

    logger.info("Wrote DHCP config %s", conf_path)
    run(["systemctl", "restart", "dnsmasq"], check=False)


def setup_nat(bridge_name: str, port_forwards: list[dict] | None = None):
    """Configure NAT masquerade for a bridge and optional port forwards."""
    _validate_name(bridge_name, "bridge")
    run(["sysctl", "-w", "net.ipv4.ip_forward=1"])

    run(["nft", "add", "rule", "inet", "nat", "postrouting", "oifname", "!=", bridge_name, "iifname", bridge_name, "masquerade"], check=False)

    for pf in (port_forwards or []):
        ext_port = _validate_port(pf.get("extPort", ""))
        int_ip = _validate_ip(pf.get("intIp", ""))
        int_port = _validate_port(pf.get("intPort", ""))
        if ext_port and int_ip and int_port:
            run(["nft", "add", "rule", "inet", "nat", "prerouting", "tcp", "dport", ext_port, "dnat", "to", f"{int_ip}:{int_port}"], check=False)
            logger.info("Port forward :%s -> %s:%s", ext_port, int_ip, int_port)


def setup_routing(bridge_a: str, bridge_b: str):
    """Allow forwarding between two bridges (router function)."""
    _validate_name(bridge_a, "bridge")
    _validate_name(bridge_b, "bridge")
    run(["nft", "add", "rule", "inet", "filter", "forward", "iifname", bridge_a, "oifname", bridge_b, "accept"], check=False)
    run(["nft", "add", "rule", "inet", "filter", "forward", "iifname", bridge_b, "oifname", bridge_a, "accept"], check=False)
    logger.info("Routing enabled: %s <-> %s", bridge_a, bridge_b)


def teardown_network(vni: int):
    """Remove a VXLAN interface and its bridge."""
    vxlan_name = f"vxlan-{vni}"
    bridge_name = f"br-{vni}"

    if link_exists(vxlan_name):
        run(["ip", "link", "del", vxlan_name])
        logger.info("Removed VXLAN %s", vxlan_name)

    if link_exists(bridge_name):
        run(["ip", "link", "del", bridge_name])
        logger.info("Removed bridge %s", bridge_name)

    import os
    conf_path = f"/etc/dnsmasq.d/troshka-{vni}.conf"
    if os.path.exists(conf_path):
        os.remove(conf_path)


def apply_network_config(config: dict, host_ip: str):
    """Apply a full network config from the API server."""
    logger.info("Applying network config (%d networks)", len(config.get("networks", [])))

    _validate_ip(host_ip)

    # Initialize nftables
    run(["nft", "add", "table", "inet", "filter"], check=False)
    run(["nft", "add", "chain", "inet", "filter", "forward", "{ type filter hook forward priority 0; policy drop; }"], check=False)
    run(["nft", "add", "rule", "inet", "filter", "forward", "ct", "state", "established,related", "accept"], check=False)
    run(["nft", "add", "table", "inet", "nat"], check=False)
    run(["nft", "add", "chain", "inet", "nat", "postrouting", "{ type nat hook postrouting priority 100; }"], check=False)
    run(["nft", "add", "chain", "inet", "nat", "prerouting", "{ type nat hook prerouting priority -100; }"], check=False)

    # Set up each network
    for net in config.get("networks", []):
        setup_vxlan(
            vni=net["vni"],
            local_ip=host_ip,
            peers=net.get("peers", []),
            bridge_name=net["bridge_name"],
            vxlan_name=net["vxlan_name"],
        )

        # Bridge IP for gateway
        if net.get("dhcp_enabled") or net.get("dns_enabled"):
            dhcp_cfg = net.get("dhcp_config", {})
            gateway_ip = dhcp_cfg.get("gateway", "")
            cidr = net.get("cidr", "")
            if gateway_ip and cidr:
                prefix = cidr.split("/")[1] if "/" in cidr else "24"
                assign_bridge_ip(net["bridge_name"], gateway_ip, prefix)

        # DHCP
        if net.get("dhcp_enabled"):
            dns_cfg = None
            if net.get("dns_enabled"):
                dns_cfg = {"domain": net.get("dns_domain", "")}
            setup_dhcp(net["vni"], net["bridge_name"], net.get("dhcp_config", {}), dns_cfg)

        # Intra-network forwarding
        run(["nft", "add", "rule", "inet", "filter", "forward", "iifname", net["bridge_name"], "oifname", net["bridge_name"], "accept"], check=False)

    # Routers
    for router in config.get("routers", []):
        vnis = router.get("connected_vnis", [])
        for i, vni_a in enumerate(vnis):
            for vni_b in vnis[i + 1:]:
                setup_routing(f"br-{vni_a}", f"br-{vni_b}")

    # Gateway NAT
    gw = config.get("gateway")
    if gw:
        for net in config.get("networks", []):
            setup_nat(net["bridge_name"], gw.get("port_forwards") if gw.get("mode") == "nat-portforward" else None)

    logger.info("Network config applied successfully")
