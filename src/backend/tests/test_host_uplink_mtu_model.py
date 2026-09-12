from app.models.host import Host


def test_host_has_uplink_mtu_default_none():
    h = Host()
    assert h.uplink_mtu is None
