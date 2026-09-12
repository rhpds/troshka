from app.services.network_mtu import (
    DEFAULT_FALLBACK_MTU,
    MESH_OVERHEAD,
    MTU_FLOOR,
    resolve_network_mtu,
)


def test_auto_single_host_uses_full_uplink():
    mtu, warn = resolve_network_mtu({"mtu": "auto"}, 8900, spans_hosts=False)
    assert mtu == 8900 and warn is None


def test_missing_field_behaves_like_auto():
    assert resolve_network_mtu({}, 8900, spans_hosts=False) == (8900, None)


def test_auto_multi_host_reserves_mesh_overhead():
    mtu, warn = resolve_network_mtu({"mtu": "auto"}, 8900, spans_hosts=True)
    assert mtu == 8900 - MESH_OVERHEAD and warn is None


def test_explicit_within_ceiling_is_honored():
    assert resolve_network_mtu({"mtu": 1500}, 8900, spans_hosts=False) == (1500, None)


def test_explicit_over_ceiling_clamps_and_warns():
    mtu, warn = resolve_network_mtu({"mtu": 9000}, 8900, spans_hosts=False)
    assert mtu == 8900
    assert warn is not None and "reduced" in warn.lower()


def test_unknown_uplink_falls_back_to_1500_with_warning():
    mtu, warn = resolve_network_mtu({"mtu": "auto"}, None, spans_hosts=False)
    assert mtu == DEFAULT_FALLBACK_MTU and warn is not None


def test_floor_is_enforced():
    mtu, _ = resolve_network_mtu({"mtu": 800}, 8900, spans_hosts=False)
    assert mtu == MTU_FLOOR
