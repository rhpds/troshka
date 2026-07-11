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

    if network_spec.get("dhcpRange"):
        cidr = network_spec["cidr"]
        netmask = _cidr_to_netmask(cidr)
        dhcp_range = network_spec["dhcpRange"]
        lines.append(f"dhcp-range={dhcp_range},{netmask},12h")

    if network_spec.get("gateway"):
        lines.append(f"dhcp-option=3,{network_spec['gateway']}")

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
