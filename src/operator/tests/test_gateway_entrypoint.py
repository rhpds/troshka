"""Gateway entrypoint nftables rules must not hijack VM egress."""

from pathlib import Path


def test_port_forward_dnat_restricted_to_eth0():
    entrypoint = (
        Path(__file__).resolve().parent.parent / "images" / "gateway" / "entrypoint.sh"
    ).read_text()
    assert 'iifname "eth0"' in entrypoint
    assert "prerouting iifname" in entrypoint
    # Old unqualified rule would break outbound HTTPS from lab VMs.
    assert 'prerouting "$proto" dport' not in entrypoint
