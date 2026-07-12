import re

_IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def _auto_dhcp_range(cidr):
    """Generate a DHCP range from a CIDR, reserving .1 for the gateway."""
    parts = cidr.split("/")
    base = parts[0]
    prefix = int(parts[1]) if len(parts) > 1 else 24
    octets = base.split(".")
    octets[3] = "2"
    start = ".".join(octets)
    octets[3] = str(min(254, (1 << (32 - prefix)) - 2))
    end = ".".join(octets)
    return f"{start},{end}"


def generate_dnsmasq_config(network_spec):
    lines = []

    if network_spec.get("dnsForwarders"):
        lines.append("port=53")
        for fwd in network_spec["dnsForwarders"]:
            lines.append(f"server={fwd}")
    else:
        lines.append("port=0")

    lines.append("bind-interfaces")
    lines.append("except-interface=lo")
    lines.append("log-dhcp")

    cidr = network_spec.get("cidr", "")
    dhcp_range = network_spec.get("dhcpRange", "")
    if not dhcp_range and cidr:
        dhcp_range = _auto_dhcp_range(cidr)

    if dhcp_range:
        netmask = _cidr_to_netmask(cidr)
        lines.append(f"dhcp-range={dhcp_range},{netmask},12h")

    gateway = network_spec.get("gateway", "")
    if not gateway and cidr:
        octets = cidr.split("/")[0].split(".")
        octets[3] = "1"
        gateway = ".".join(octets)
    if gateway and _IPV4_RE.match(gateway):
        lines.append(f"dhcp-option=3,{gateway}")

    for lease in network_spec.get("staticLeases", []):
        mac = lease.get("mac", "")
        ip = lease.get("ip", "")
        hostname = lease.get("hostname", "")
        if mac and ip:
            if hostname:
                lines.append(f"dhcp-host={mac},{ip},{hostname}")
            else:
                lines.append(f"dhcp-host={mac},{ip}")

    pxe = network_spec.get("pxeConfig", {})
    if pxe.get("enabled"):
        lines.append("enable-tftp")
        lines.append("tftp-root=/var/lib/tftpboot")
        lines.append("dhcp-boot=pxelinux.0")

    return "\n".join(lines) + "\n"


def _cidr_to_netmask(cidr):
    prefix = int(cidr.split("/")[1])
    mask = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF
    return f"{(mask >> 24) & 0xFF}.{(mask >> 16) & 0xFF}.{(mask >> 8) & 0xFF}.{mask & 0xFF}"
