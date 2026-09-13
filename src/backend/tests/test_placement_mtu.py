"""Tests for MTU-aware host placement."""

from app.models.host import Host
from app.models.provider import Provider
from tests.conftest import TestSession

# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------
_db = TestSession()

_provider = Provider(
    name="mtu-test-provider",
    type="ec2",
    default_region="us-west-2",
    max_eips=5,
)
_provider.set_credentials({"access_key_id": "fake", "secret_access_key": "fake"})
_db.add(_provider)
_db.commit()
_db.refresh(_provider)

_host_jumbo = Host(
    provider_id=_provider.id,
    instance_id="i-jumbo",
    ip_address="10.0.0.10",
    private_key="fake-key",
    state="active",
    agent_status="connected",
    host_type="shared",
    total_vcpus=32,
    total_ram_mb=65536,
    used_vcpus=0,
    used_ram_mb=0,
    max_eips=10,
    uplink_mtu=8900,
)
_db.add(_host_jumbo)
_db.commit()
_db.refresh(_host_jumbo)

_host_standard = Host(
    provider_id=_provider.id,
    instance_id="i-standard",
    ip_address="10.0.0.11",
    private_key="fake-key",
    state="active",
    agent_status="connected",
    host_type="shared",
    total_vcpus=16,
    total_ram_mb=32768,
    used_vcpus=0,
    used_ram_mb=0,
    max_eips=5,
    uplink_mtu=1500,
)
_db.add(_host_standard)
_db.commit()
_db.refresh(_host_standard)

_host_unknown_mtu = Host(
    provider_id=_provider.id,
    instance_id="i-unknown",
    ip_address="10.0.0.12",
    private_key="fake-key",
    state="active",
    agent_status="connected",
    host_type="shared",
    total_vcpus=8,
    total_ram_mb=16384,
    used_vcpus=0,
    used_ram_mb=0,
    max_eips=3,
    uplink_mtu=None,
)
_db.add(_host_unknown_mtu)
_db.commit()
_db.refresh(_host_unknown_mtu)

_provider_id = _provider.id
_host_jumbo_id = _host_jumbo.id
_host_standard_id = _host_standard.id
_host_unknown_mtu_id = _host_unknown_mtu.id
_db.close()


# ---------------------------------------------------------------------------
# Tests for required_network_mtu
# ---------------------------------------------------------------------------
def test_required_network_mtu_with_concrete():
    """Single concrete MTU value is returned."""
    from app.services.placement import required_network_mtu

    topology = {
        "nodes": [
            {"id": "net1", "type": "networkNode", "data": {"mtu": 8900}},
        ]
    }
    assert required_network_mtu(topology) == 8900


def test_required_network_mtu_with_auto():
    """'auto' MTU is not concrete, returns None."""
    from app.services.placement import required_network_mtu

    topology = {
        "nodes": [
            {"id": "net1", "type": "networkNode", "data": {"mtu": "auto"}},
        ]
    }
    assert required_network_mtu(topology) is None


def test_required_network_mtu_mixed():
    """Max of concrete MTUs is returned, ignoring 'auto'."""
    from app.services.placement import required_network_mtu

    topology = {
        "nodes": [
            {"id": "net1", "type": "networkNode", "data": {"mtu": 1500}},
            {"id": "net2", "type": "networkNode", "data": {"mtu": "auto"}},
            {"id": "net3", "type": "networkNode", "data": {"mtu": 8900}},
            {"id": "vm1", "type": "vmNode", "data": {}},
        ]
    }
    assert required_network_mtu(topology) == 8900


def test_required_network_mtu_none():
    """No network nodes or all auto/missing MTU returns None."""
    from app.services.placement import required_network_mtu

    topology = {
        "nodes": [
            {"id": "vm1", "type": "vmNode", "data": {}},
        ]
    }
    assert required_network_mtu(topology) is None


def test_required_network_mtu_empty():
    """Empty or None topology returns None."""
    from app.services.placement import required_network_mtu

    assert required_network_mtu(None) is None
    assert required_network_mtu({}) is None
    assert required_network_mtu({"nodes": []}) is None


# ---------------------------------------------------------------------------
# Tests for _host_mtu_ok
# ---------------------------------------------------------------------------
def test_host_mtu_ok_none_required():
    """None required MTU always passes."""
    from app.services.placement import _host_mtu_ok

    db = TestSession()
    try:
        host = db.query(Host).filter_by(id=_host_standard_id).first()
        assert _host_mtu_ok(host, None) is True
    finally:
        db.close()


def test_host_mtu_ok_satisfying():
    """Host with sufficient uplink_mtu passes."""
    from app.services.placement import _host_mtu_ok

    db = TestSession()
    try:
        host = db.query(Host).filter_by(id=_host_jumbo_id).first()
        assert _host_mtu_ok(host, 8900) is True
        assert _host_mtu_ok(host, 1500) is True
    finally:
        db.close()


def test_host_mtu_ok_too_small():
    """Host with insufficient uplink_mtu fails."""
    from app.services.placement import _host_mtu_ok

    db = TestSession()
    try:
        host = db.query(Host).filter_by(id=_host_standard_id).first()
        assert _host_mtu_ok(host, 8900) is False
    finally:
        db.close()


def test_host_mtu_ok_unknown_uplink():
    """Host with unknown uplink_mtu (None) fails concrete requirement."""
    from app.services.placement import _host_mtu_ok

    db = TestSession()
    try:
        host = db.query(Host).filter_by(id=_host_unknown_mtu_id).first()
        assert _host_mtu_ok(host, 1500) is False
        assert _host_mtu_ok(host, None) is True
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Tests for find_available_host with MTU filtering
# ---------------------------------------------------------------------------
def test_find_available_host_mtu_filters():
    """Auto-placement excludes hosts that can't meet required MTU."""
    from app.services.placement import find_available_host

    db = TestSession()
    try:
        # Required MTU 8900 → only jumbo host qualifies
        host = find_available_host(
            db,
            required_vcpus=4,
            required_ram_mb=4096,
            required_mtu=8900,
        )
        assert host is not None
        assert host.id == _host_jumbo_id

        # Required MTU None → all hosts qualify (picks least loaded)
        host = find_available_host(
            db,
            required_vcpus=4,
            required_ram_mb=4096,
            required_mtu=None,
        )
        assert host is not None
    finally:
        db.close()


def test_find_available_host_mtu_no_match():
    """No host meets required MTU returns None."""
    from app.services.placement import find_available_host

    db = TestSession()
    try:
        # Required MTU 9000 → no host qualifies
        host = find_available_host(
            db,
            required_vcpus=4,
            required_ram_mb=4096,
            required_mtu=9000,
        )
        assert host is None
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Tests for _select_host explicit host validation
# ---------------------------------------------------------------------------
def test_select_host_explicit_mtu_ok():
    """Explicit host that meets MTU requirement succeeds."""
    from app.models.project import Project
    from app.models.user import User
    from app.services.placement import _select_host

    db = TestSession()
    try:
        # Create a test user
        user = User(
            email="mtu-test-ok@test.com",
            password_hash="fake",
        )
        db.add(user)
        db.commit()
        db.refresh(user)

        project = Project(
            name="mtu-test-ok",
            owner_id=user.id,
            topology={"nodes": []},
        )

        reqs = {"total_vcpus": 4, "total_ram_mb": 4096, "requested_eips": 0}
        host, pool, error = _select_host(
            db,
            project,
            reqs,
            has_anti_affinity=False,
            storage_pool_id=None,
            host_id=_host_jumbo_id,
            pattern_disk_ids=None,
            required_mtu=8900,
        )
        assert error is None
        assert host is not None
        assert host.id == _host_jumbo_id

        db.delete(user)
        db.commit()
    finally:
        db.close()


def test_select_host_explicit_mtu_fail():
    """Explicit host that can't meet MTU requirement returns error."""
    from app.models.project import Project
    from app.models.user import User
    from app.services.placement import _select_host

    db = TestSession()
    try:
        # Create a test user
        user = User(
            email="mtu-test-fail@test.com",
            password_hash="fake",
        )
        db.add(user)
        db.commit()
        db.refresh(user)

        project = Project(
            name="mtu-test-fail",
            owner_id=user.id,
            topology={"nodes": []},
        )

        reqs = {"total_vcpus": 4, "total_ram_mb": 4096, "requested_eips": 0}
        host, pool, error = _select_host(
            db,
            project,
            reqs,
            has_anti_affinity=False,
            storage_pool_id=None,
            host_id=_host_standard_id,
            pattern_disk_ids=None,
            required_mtu=8900,
        )
        assert error is not None
        assert "error" in error
        assert "MTU" in error["error"]
        assert "8900" in error["error"]
        assert host is None

        db.delete(user)
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Tests for _prepare_hosts MTU filtering
# ---------------------------------------------------------------------------
def test_prepare_hosts_filters_mtu():
    """_prepare_hosts excludes hosts that can't meet required MTU."""
    from app.services.placement import _prepare_hosts

    db = TestSession()
    try:
        # Required MTU 8900 → only jumbo host should be in available_hosts
        hosts, remaining = _prepare_hosts(
            db, pool_id=None, provider_id=_provider_id, required_mtu=8900
        )
        assert hosts is not None
        assert remaining is not None
        # Should have only jumbo host (standard and unknown MTU filtered out)
        assert len([h for h in hosts if h.uplink_mtu and h.uplink_mtu >= 8900]) >= 1
        assert all(h.uplink_mtu and h.uplink_mtu >= 8900 for h in hosts)
    finally:
        db.close()


def test_prepare_hosts_no_mtu_requirement():
    """_prepare_hosts with None required_mtu includes all hosts."""
    from app.services.placement import _prepare_hosts

    db = TestSession()
    try:
        hosts, remaining = _prepare_hosts(
            db, pool_id=None, provider_id=_provider_id, required_mtu=None
        )
        assert hosts is not None
        assert remaining is not None
        # Should have multiple hosts (jumbo + standard at minimum)
        assert len(hosts) >= 2
    finally:
        db.close()
