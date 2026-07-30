"""Tests for eip_service.py — allocate, associate, disassociate, release, transit ports."""

import uuid
from unittest.mock import MagicMock, patch

from app.models.elastic_ip import ElasticIp
from app.models.host import Host
from app.models.provider import Provider
from tests.conftest import TestSession


def _make_provider(db, ptype="ec2"):
    p = Provider(
        id=str(uuid.uuid4()),
        name=f"test-provider-{uuid.uuid4().hex[:6]}",
        type=ptype,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _make_host(db, provider):
    h = Host(
        id=str(uuid.uuid4()),
        provider_id=provider.id,
        ip_address="10.0.0.1",
        instance_id="i-test123",
        agent_status="connected",
        state="active",
    )
    db.add(h)
    db.commit()
    db.refresh(h)
    return h


def _make_eip(db, provider, host=None, project_id=None, state="allocated"):
    eip = ElasticIp(
        id=str(uuid.uuid4()),
        provider_id=provider.id,
        project_id=project_id or str(uuid.uuid4()),
        canvas_eip_id="canvas-eip-1",
        allocation_id=f"eipalloc-{uuid.uuid4().hex[:8]}",
        public_ip="54.1.2.3",
        host_id=host.id if host else None,
        state=state,
    )
    db.add(eip)
    db.commit()
    db.refresh(eip)
    return eip


# ---------------------------------------------------------------------------
# allocate_eip
# ---------------------------------------------------------------------------


@patch("app.services.eip_service.get_provider_driver")
def test_allocate_eip(mock_get_driver):
    """Allocate EIP creates DB record with provider result."""
    db = TestSession()
    try:
        provider = _make_provider(db)
        host = _make_host(db, provider)

        mock_driver = MagicMock()
        mock_driver.allocate_eip.return_value = {
            "allocation_id": "eipalloc-abc",
            "public_ip": "1.2.3.4",
        }
        mock_get_driver.return_value = mock_driver

        from app.services.eip_service import allocate_eip

        project_id = str(uuid.uuid4())
        eip = allocate_eip(db, provider, project_id, "canvas-1", host)

        assert eip.public_ip == "1.2.3.4"
        assert eip.allocation_id == "eipalloc-abc"
        assert eip.state == "allocated"
        assert eip.project_id == project_id
        assert eip.canvas_eip_id == "canvas-1"
        mock_driver.allocate_eip.assert_called_once()
    finally:
        db.rollback()
        db.close()


# ---------------------------------------------------------------------------
# associate_eip
# ---------------------------------------------------------------------------


@patch("app.services.eip_service.get_provider_driver")
def test_associate_eip(mock_get_driver):
    """Associate EIP updates DB record with host binding."""
    db = TestSession()
    try:
        provider = _make_provider(db)
        host = _make_host(db, provider)
        eip = _make_eip(db, provider)

        mock_driver = MagicMock()
        mock_driver.associate_eip.return_value = {
            "private_ip": "10.0.0.99",
            "association_id": "eipassoc-xyz",
        }
        mock_get_driver.return_value = mock_driver

        from app.services.eip_service import associate_eip

        associate_eip(db, eip, host)

        assert eip.state == "associated"
        assert eip.private_ip == "10.0.0.99"
        assert eip.association_id == "eipassoc-xyz"
        assert eip.host_id == host.id
    finally:
        db.rollback()
        db.close()


@patch("app.services.eip_service.get_provider_driver")
def test_associate_eip_provider_not_found(mock_get_driver):
    """associate_eip raises ValueError when provider is missing."""
    db = TestSession()
    try:
        provider = _make_provider(db)
        host = _make_host(db, provider)
        eip = _make_eip(db, provider)
        # Set provider_id to something nonexistent
        eip.provider_id = str(uuid.uuid4())
        db.commit()

        from app.services.eip_service import associate_eip

        try:
            associate_eip(db, eip, host)
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "not found" in str(e)
    finally:
        db.rollback()
        db.close()


# ---------------------------------------------------------------------------
# disassociate_eip
# ---------------------------------------------------------------------------


def test_disassociate_eip_not_associated():
    """Disassociate on non-associated EIP is a no-op."""
    db = TestSession()
    try:
        provider = _make_provider(db)
        host = _make_host(db, provider)
        eip = _make_eip(db, provider, state="allocated")

        from app.services.eip_service import disassociate_eip

        disassociate_eip(db, eip, host)
        # Should remain allocated (no change)
        assert eip.state == "allocated"
    finally:
        db.rollback()
        db.close()


@patch("app.services.provisioner._get_ec2_client")
def test_disassociate_eip_ec2(mock_ec2_client):
    """Disassociate EIP on EC2 calls disassociate_address and clears DB fields."""
    db = TestSession()
    try:
        provider = _make_provider(db, ptype="ec2")
        host = _make_host(db, provider)
        eip = _make_eip(db, provider, host=host, state="associated")
        eip.association_id = "eipassoc-abc"
        eip.private_ip = "10.0.0.50"
        db.commit()

        mock_ec2 = MagicMock()
        mock_ec2.describe_instances.return_value = {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "NetworkInterfaces": [
                                {
                                    "Attachment": {"DeviceIndex": 0},
                                    "NetworkInterfaceId": "eni-abc123",
                                }
                            ]
                        }
                    ]
                }
            ]
        }
        mock_ec2_client.return_value = mock_ec2

        from app.services.eip_service import disassociate_eip

        disassociate_eip(db, eip, host)

        assert eip.state == "allocated"
        assert eip.private_ip is None
        assert eip.host_id is None
        assert eip.association_id is None
        mock_ec2.disassociate_address.assert_called_once_with(
            AssociationId="eipassoc-abc"
        )
        mock_ec2.unassign_private_ip_addresses.assert_called_once()
    finally:
        db.rollback()
        db.close()


# ---------------------------------------------------------------------------
# release_eip
# ---------------------------------------------------------------------------


@patch("app.services.eip_service.get_provider_driver")
def test_release_eip_allocated(mock_get_driver):
    """Release an allocated EIP calls driver and deletes DB record."""
    db = TestSession()
    try:
        provider = _make_provider(db)
        eip = _make_eip(db, provider, state="allocated")
        eip_id = eip.id

        mock_driver = MagicMock()
        mock_get_driver.return_value = mock_driver

        from app.services.eip_service import release_eip

        release_eip(db, eip)

        mock_driver.release_eip.assert_called_once()
        assert db.query(ElasticIp).filter_by(id=eip_id).first() is None
    finally:
        db.rollback()
        db.close()


@patch("app.services.eip_service.get_provider_driver")
def test_release_eip_provider_not_found(mock_get_driver):
    """release_eip raises ValueError when provider is missing."""
    db = TestSession()
    try:
        provider = _make_provider(db)
        eip = _make_eip(db, provider, state="allocated")
        eip.provider_id = str(uuid.uuid4())
        db.commit()

        from app.services.eip_service import release_eip

        try:
            release_eip(db, eip)
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "not found" in str(e)
    finally:
        db.rollback()
        db.close()


# ---------------------------------------------------------------------------
# get_host_eip_usage
# ---------------------------------------------------------------------------


def test_get_host_eip_usage_empty():
    """No EIPs associated returns 0."""
    db = TestSession()
    try:
        from app.services.eip_service import get_host_eip_usage

        count = get_host_eip_usage(db, str(uuid.uuid4()))
        assert count == 0
    finally:
        db.close()


def test_get_host_eip_usage_with_eips():
    """Count of associated EIPs for a host."""
    db = TestSession()
    try:
        provider = _make_provider(db)
        host = _make_host(db, provider)

        # Add two associated EIPs
        for _ in range(2):
            _make_eip(db, provider, host=host, state="associated")
        # Add one allocated (not associated) — should not count
        _make_eip(db, provider, state="allocated")

        from app.services.eip_service import get_host_eip_usage

        count = get_host_eip_usage(db, host.id)
        assert count == 2
    finally:
        db.rollback()
        db.close()


# ---------------------------------------------------------------------------
# allocate_transit_ports
# ---------------------------------------------------------------------------


def test_allocate_transit_ports_basic():
    """Transit ports are allocated sequentially from TRANSIT_PORT_START."""
    db = TestSession()
    try:
        provider = _make_provider(db)
        host = _make_host(db, provider)
        eip = _make_eip(db, provider, host=host, state="associated")

        from app.services.eip_service import TRANSIT_PORT_START, allocate_transit_ports

        port_forwards = [{"extPort": 443}, {"extPort": 80}]
        port_map = allocate_transit_ports(db, eip, host, port_forwards)

        assert port_map["443"] == TRANSIT_PORT_START
        assert port_map["80"] == TRANSIT_PORT_START + 1
        assert eip.port_map == port_map
    finally:
        db.rollback()
        db.close()


def test_allocate_transit_ports_avoids_existing():
    """Transit port allocation skips ports already in use by other EIPs."""
    db = TestSession()
    try:
        provider = _make_provider(db)
        host = _make_host(db, provider)

        # Pre-allocate a port on another EIP
        from app.services.eip_service import TRANSIT_PORT_START

        other_eip = _make_eip(db, provider, host=host, state="associated")
        other_eip.port_map = {"443": TRANSIT_PORT_START}
        db.commit()

        eip = _make_eip(db, provider, host=host, state="associated")

        from app.services.eip_service import allocate_transit_ports

        port_map = allocate_transit_ports(db, eip, host, [{"extPort": 8080}])

        # Should skip TRANSIT_PORT_START and use the next one
        assert port_map["8080"] == TRANSIT_PORT_START + 1
    finally:
        db.rollback()
        db.close()


# ---------------------------------------------------------------------------
# migrate_eip
# ---------------------------------------------------------------------------


@patch("app.services.eip_service.associate_eip")
@patch("app.services.eip_service.disassociate_eip")
def test_migrate_eip(mock_disassociate, mock_associate):
    """migrate_eip disassociates from source and associates to target."""
    db = TestSession()
    try:
        provider = _make_provider(db)
        host_a = _make_host(db, provider)
        host_b = _make_host(db, provider)
        eip = _make_eip(db, provider, host=host_a, state="associated")

        from app.services.eip_service import migrate_eip

        migrate_eip(db, eip, host_a, host_b)

        mock_disassociate.assert_called_once_with(db, eip, host_a)
        mock_associate.assert_called_once_with(db, eip, host_b)
    finally:
        db.rollback()
        db.close()
