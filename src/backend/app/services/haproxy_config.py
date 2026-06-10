def _sanitize_name(name: str) -> str:
    return name.replace(" ", "-").lower()


def generate_haproxy_config(frontends: list[dict], backends: list[dict]) -> str:
    lines = [
        "global",
        "    daemon",
        "    maxconn 4096",
        "",
        "defaults",
        "    mode tcp",
        "    timeout connect 5s",
        "    timeout client 30s",
        "    timeout server 30s",
        "    option tcplog",
        "",
    ]

    for fe in frontends:
        fe_name = _sanitize_name(fe["name"])
        be_name = f"{fe_name}-servers"
        backend_port = fe["backendPort"]

        lines.append(f"frontend {fe_name}")
        lines.append(f"    bind *:{fe['bindPort']}")
        lines.append(f"    default_backend {be_name}")
        lines.append("")

        lines.append(f"backend {be_name}")
        lines.append("    balance roundrobin")
        for be in backends:
            lines.append(f"    server {be['name']} {be['ip']}:{backend_port} check")
        lines.append("")

    return "\n".join(lines)
