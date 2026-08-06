import re

_IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def _auto_dhcp_range(cidr):
    """Generate a DHCP range from a CIDR, reserving .1 for the gateway."""
    parts = cidr.split("/")
    base = parts[0]
    prefix = int(parts[1]) if len(parts) > 1 else 24
    octets = base.split(".")
    octets[3] = "10"
    start = ".".join(octets)
    octets[3] = str(min(254, (1 << (32 - prefix)) - 2))
    end = ".".join(octets)
    return f"{start},{end}"


def _add_dns_forwarder_config(lines, network_spec):
    """Add DNS forwarder configuration lines."""
    if network_spec.get("dnsForwarders"):
        lines.append("port=53")
        for fwd in network_spec["dnsForwarders"]:
            lines.append(f"server={fwd}")
    else:
        lines.append("port=0")


def _add_dhcp_range_config(lines, cidr, dhcp_range):
    """Add DHCP range configuration line."""
    if not dhcp_range and cidr:
        dhcp_range = _auto_dhcp_range(cidr)
    if dhcp_range:
        netmask = _cidr_to_netmask(cidr)
        lines.append(f"dhcp-range={dhcp_range},{netmask},12h")


def _add_gateway_config(lines, gateway, cidr):
    """Add gateway DHCP option line."""
    if not gateway and cidr:
        octets = cidr.split("/")[0].split(".")
        octets[3] = "1"
        gateway = ".".join(octets)
    if gateway and _IPV4_RE.match(gateway):
        lines.append(f"dhcp-option=3,{gateway}")


def _add_dns_server_config(lines, network_spec, cidr):
    """Add DNS server DHCP option line."""
    if network_spec.get("dnsForwarders") and cidr:
        dns_ip = cidr.split("/")[0].split(".")
        dns_ip[3] = "2"
        lines.append(f"dhcp-option=6,{'.'.join(dns_ip)}")


def _add_static_leases(lines, network_spec):
    """Add static DHCP lease configuration lines."""
    for lease in network_spec.get("staticLeases", []):
        mac = lease.get("mac", "")
        ip = lease.get("ip", "")
        hostname = lease.get("hostname", "")
        if not mac or not ip:
            continue
        if hostname:
            lines.append(f"dhcp-host={mac},{ip},{hostname}")
        else:
            lines.append(f"dhcp-host={mac},{ip}")


def _add_dns_records(lines, network_spec):
    """Add DNS address records."""
    for rec in network_spec.get("dnsRecords", []):
        name = rec.get("name", "")
        ip = rec.get("ip", "")
        if name and ip and _IPV4_RE.match(ip):
            lines.append(f"address=/{name}/{ip}")


def _add_pxe_config(lines, network_spec):
    """Add PXE boot configuration lines."""
    pxe = network_spec.get("pxeConfig", {})
    if pxe.get("enabled"):
        lines.append("enable-tftp")
        lines.append("tftp-root=/var/lib/tftpboot")
        lines.append("dhcp-boot=pxelinux.0")


def generate_dnsmasq_config(network_spec):
    lines = []

    _add_dns_forwarder_config(lines, network_spec)
    lines.append("bind-interfaces")
    lines.append("except-interface=lo")
    lines.append("log-dhcp")

    cidr = network_spec.get("cidr", "")
    dhcp_range = network_spec.get("dhcpRange", "")
    _add_dhcp_range_config(lines, cidr, dhcp_range)

    gateway = network_spec.get("gateway", "")
    _add_gateway_config(lines, gateway, cidr)
    _add_dns_server_config(lines, network_spec, cidr)
    _add_static_leases(lines, network_spec)
    _add_dns_records(lines, network_spec)
    _add_pxe_config(lines, network_spec)

    return "\n".join(lines) + "\n"


def _cidr_to_netmask(cidr):
    prefix = int(cidr.split("/")[1])
    mask = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF
    return f"{(mask >> 24) & 0xFF}.{(mask >> 16) & 0xFF}.{(mask >> 8) & 0xFF}.{mask & 0xFF}"
