"""Single resolution point for a network's underlay MTU (see
docs/superpowers/specs/2026-09-11-per-network-mtu-design.md)."""

MESH_OVERHEAD = 110  # WireGuard (~60) + VXLAN (~50) for multi-host mesh
OVN_GENEVE_OVERHEAD = 100  # OVN-Kubernetes geneve overhead (interface -> overlay)
MTU_FLOOR = 1280
DEFAULT_FALLBACK_MTU = 1500  # host uplink unknown -> safe/portable


def resolve_network_mtu(network_data, host_uplink_mtu, spans_hosts):
    """Resolve a network's underlay MTU. Returns (mtu, warning_or_None)."""
    requested = (network_data or {}).get("mtu")
    if host_uplink_mtu is None:
        return DEFAULT_FALLBACK_MTU, (
            "host uplink MTU unknown; defaulting to "
            f"{DEFAULT_FALLBACK_MTU} (agent may not have reported yet)"
        )

    overhead = MESH_OVERHEAD if spans_hosts else 0
    ceiling = host_uplink_mtu - overhead

    is_auto = requested in (None, "", "auto")
    candidate = ceiling if is_auto else int(requested)

    warning = None
    if candidate > ceiling:
        warning = (
            f"MTU {candidate} reduced to {ceiling}: exceeds host uplink "
            f"{host_uplink_mtu} (overhead {overhead})"
        )
        candidate = ceiling

    if candidate < MTU_FLOOR:
        candidate = MTU_FLOOR
    return candidate, warning
