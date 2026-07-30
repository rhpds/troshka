"""Tests for placement helper functions."""


from app.models.elastic_ip import ElasticIp
from app.models.host import Host
from app.models.provider import Provider
from tests.conftest import TestSession

# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------
_db = TestSession()

_provider = Provider(
    name="placement-test-provider",
    type="ec2",
    default_region="us-west-2",
    max_eips=5,
)
_provider.set_credentials({"access_key_id": "fake", "secret_access_key": "fake"})
_db.add(_provider)
_db.commit()
_db.refresh(_provider)

_host_with_capacity = Host(
    provider_id=_provider.id,
    instance_id="i-placement-cap",
    ip_address="10.0.0.10",
    private_key="fake-key",
    state="active",
    max_eips=10,
)
_db.add(_host_with_capacity)
_db.commit()
_db.refresh(_host_with_capacity)

_host_no_provider = Host(
    provider_id=None,
    instance_id="i-placement-noprov",
    ip_address="10.0.0.11",
    private_key="fake-key",
    state="active",
    max_eips=5,
)
_db.add(_host_no_provider)
_db.commit()
_db.refresh(_host_no_provider)

_provider_id = _provider.id
_host_with_capacity_id = _host_with_capacity.id
_host_no_provider_id = _host_no_provider.id
_db.close()


# ---------------------------------------------------------------------------
# Tests for _check_eip_capacity
# ---------------------------------------------------------------------------
def test_check_eip_capacity_enough():
    """Host with enough host-level and provider-level EIP capacity returns True."""
    from app.services.placement import _check_eip_capacity

    db = TestSession()
    try:
        host = db.query(Host).filter_by(id=_host_with_capacity_id).first()
        # Host has max_eips=10, no EIPs allocated yet → plenty of room
        assert _check_eip_capacity(db, host, required_eips=3) is True
    finally:
        db.close()


def test_check_eip_capacity_host_full():
    """Host with no remaining host-level capacity returns False."""
    from app.services.placement import _check_eip_capacity

    db = TestSession()
    try:
        host = db.query(Host).filter_by(id=_host_with_capacity_id).first()
        # Create enough associated EIPs to exhaust the host's capacity
        eips = []
        for i in range(host.max_eips):
            eip = ElasticIp(
                provider_id=_provider_id,
                host_id=host.id,
                canvas_eip_id=f"canvas-full-{i}",
                allocation_id=f"eipalloc-full-{i}",
                public_ip=f"54.0.0.{i}",
                state="associated",
            )
            eips.append(eip)
            db.add(eip)
        db.commit()

        # All slots occupied → requesting even 1 more should fail
        assert _check_eip_capacity(db, host, required_eips=1) is False

        # Cleanup
        for eip in eips:
            db.delete(eip)
        db.commit()
    finally:
        db.close()


def test_check_eip_capacity_provider_limit():
    """Provider-level EIP limit exceeded returns False even if host has room."""
    from app.services.placement import _check_eip_capacity

    db = TestSession()
    try:
        host = db.query(Host).filter_by(id=_host_with_capacity_id).first()
        provider = db.query(Provider).filter_by(id=_provider_id).first()

        # Provider max_eips=5. Create 5 associated EIPs on the provider.
        eips = []
        for i in range(provider.max_eips):
            eip = ElasticIp(
                provider_id=provider.id,
                host_id=host.id,
                canvas_eip_id=f"canvas-prov-{i}",
                allocation_id=f"eipalloc-prov-{i}",
                public_ip=f"54.1.0.{i}",
                state="associated",
            )
            eips.append(eip)
            db.add(eip)
        db.commit()

        # Host has max_eips=10 (only 5 used), but provider is at its limit of 5
        assert _check_eip_capacity(db, host, required_eips=1) is False

        # Cleanup
        for eip in eips:
            db.delete(eip)
        db.commit()
    finally:
        db.close()


def test_check_eip_capacity_no_provider():
    """Host without a provider_id handles gracefully and returns True."""
    from app.services.placement import _check_eip_capacity

    db = TestSession()
    try:
        host = db.query(Host).filter_by(id=_host_no_provider_id).first()
        assert host.provider_id is None
        # No provider → only host-level check applies, and it has room
        assert _check_eip_capacity(db, host, required_eips=2) is True
    finally:
        db.close()


def test_check_eip_capacity_zero_required():
    """Requesting zero EIPs always succeeds."""
    from app.services.placement import _check_eip_capacity

    db = TestSession()
    try:
        host = db.query(Host).filter_by(id=_host_with_capacity_id).first()
        assert _check_eip_capacity(db, host, required_eips=0) is True
    finally:
        db.close()
