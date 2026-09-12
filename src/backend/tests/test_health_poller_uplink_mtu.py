# src/backend/tests/test_health_poller_uplink_mtu.py
from app.services import health_poller


def test_persist_uplink_mtu_sets_host_field():
    class H:
        uplink_mtu = None

    host = H()
    health_poller._apply_uplink_mtu(host, {"version": "x", "uplink_mtu": 8900})
    assert host.uplink_mtu == 8900


def test_persist_uplink_mtu_ignores_missing():
    class H:
        uplink_mtu = 1500

    host = H()
    health_poller._apply_uplink_mtu(host, {"version": "x"})
    assert host.uplink_mtu == 1500
