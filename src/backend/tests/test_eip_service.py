"""Tests for EIP service lifecycle operations."""

import uuid
from unittest.mock import MagicMock, patch

from app.core.auth import hash_password
from app.models.elastic_ip import ElasticIp
from app.models.host import Host
from app.models.provider import Provider
from app.models.user import User
from app.services import eip_service
from tests.conftest import TestSession

# Test data setup
_db = TestSession()
_user = User(
    email="eip-test@example.com",
    display_name="EIP Tester",
    role="admin",
    auth_source="local",
    password_hash=hash_password("pass"),
)
_db.add(_user)
_db.commit()
_db.refresh(_user)

_provider = Provider(
    name="test-provider-eip",
    type="ec2",
    default_region="us-east-1",
)
_provider.set_credentials(
    {"access_key_id": "fake-key", "secret_access_key": "fake-secret"}
)
_db.add(_provider)
_db.commit()
_db.refresh(_provider)

_ocpvirt_provider = Provider(
    name="test-provider-ocpvirt",
    type="ocpvirt",
)
_ocpvirt_provider.set_credentials(
    {"api_url": "https://api.test:6443", "token": "fake", "namespace": "troshka"}
)
_db.add(_ocpvirt_provider)
_db.commit()
_db.refresh(_ocpvirt_provider)

_host = Host(
    provider_id=_provider.id,
    instance_id="i-test123",
    ip_address="10.0.1.100",
    private_key="test-key-not-real",
    state="running",
    max_eips=14,
)
_db.add(_host)
_db.commit()
_db.refresh(_host)

_provider_id = _provider.id
_ocpvirt_provider_id = _ocpvirt_provider.id
_host_id = _host.id
_db.close()


@patch("app.services.eip_service.get_provider_driver")
def test_allocate_eip(mock_get_driver):
    mock_driver = MagicMock()
    mock_driver.allocate_eip.return_value = {
        "allocation_id": "eipalloc-abc123",
        "public_ip": "54.123.45.67",
    }
    mock_get_driver.return_value = mock_driver

    db = TestSession()
    provider = db.query(Provider).filter_by(id=_provider_id).first()
    host = db.query(Host).filter_by(id=_host_id).first()
    project_id = str(uuid.uuid4())
    canvas_eip_id = f"eip-{uuid.uuid4()}"

    eip = eip_service.allocate_eip(db, provider, project_id, canvas_eip_id, host)

    mock_driver.allocate_eip.assert_called_once()
    assert eip.allocation_id == "eipalloc-abc123"
    assert eip.public_ip == "54.123.45.67"
    assert eip.state == "allocated"
    assert eip.host_id is None
    db.close()


@patch("app.services.eip_service.get_provider_driver")
def test_associate_eip(mock_get_driver):
    mock_driver = MagicMock()
    mock_driver.associate_eip.return_value = {
        "private_ip": "10.0.1.50",
        "association_id": "eipassoc-abc456",
    }
    mock_get_driver.return_value = mock_driver

    db = TestSession()
    host = db.query(Host).filter_by(id=_host_id).first()
    eip = ElasticIp(
        provider_id=_provider_id,
        project_id=str(uuid.uuid4()),
        canvas_eip_id=f"eip-{uuid.uuid4()}",
        allocation_id="eipalloc-assoc123",
        public_ip="54.11.22.33",
        state="allocated",
    )
    db.add(eip)
    db.commit()
    db.refresh(eip)

    eip_service.associate_eip(db, eip, host)

    mock_driver.associate_eip.assert_called_once()
    db.refresh(eip)
    assert eip.state == "associated"
    assert eip.private_ip == "10.0.1.50"
    assert eip.host_id == _host_id
    assert eip.association_id == "eipassoc-abc456"
    db.close()


@patch("app.services.eip_service.get_provider_driver")
def test_release_eip(mock_get_driver):
    mock_driver = MagicMock()
    mock_get_driver.return_value = mock_driver

    db = TestSession()
    eip = ElasticIp(
        provider_id=_provider_id,
        project_id=str(uuid.uuid4()),
        canvas_eip_id=f"eip-{uuid.uuid4()}",
        allocation_id="eipalloc-release123",
        public_ip="54.99.88.77",
        state="allocated",
    )
    db.add(eip)
    db.commit()
    db.refresh(eip)
    eip_id = eip.id

    eip_service.release_eip(db, eip)

    mock_driver.release_eip.assert_called_once()
    assert db.query(ElasticIp).filter_by(id=eip_id).first() is None
    db.close()


def test_allocate_transit_ports():
    db = TestSession()
    host = db.query(Host).filter_by(id=_host_id).first()

    eip = ElasticIp(
        provider_id=_ocpvirt_provider_id,
        project_id=str(uuid.uuid4()),
        canvas_eip_id=f"eip-{uuid.uuid4()}",
        allocation_id="troshka-eip-test1234",
        public_ip="67.228.103.10",
        state="associated",
        host_id=_host_id,
    )
    db.add(eip)
    db.commit()
    db.refresh(eip)

    port_forwards = [
        {"extPort": 443},
        {"extPort": 8080},
    ]
    port_map = eip_service.allocate_transit_ports(db, eip, host, port_forwards)

    assert port_map["443"] == 40000
    assert port_map["8080"] == 40001

    db.refresh(eip)
    assert eip.port_map == port_map

    # Second EIP on same host should get non-overlapping ports
    eip2 = ElasticIp(
        provider_id=_ocpvirt_provider_id,
        project_id=str(uuid.uuid4()),
        canvas_eip_id=f"eip-{uuid.uuid4()}",
        allocation_id="troshka-eip-test5678",
        public_ip="67.228.103.11",
        state="associated",
        host_id=_host_id,
    )
    db.add(eip2)
    db.commit()
    db.refresh(eip2)

    port_map2 = eip_service.allocate_transit_ports(db, eip2, host, [{"extPort": 443}])
    assert port_map2["443"] == 40002  # Skips 40000, 40001

    db.close()


@patch("app.services.eip_service.get_provider_driver")
def test_sync_sg_rules_noop_for_ocpvirt(mock_get_driver):
    db = TestSession()
    provider = db.query(Provider).filter_by(id=_ocpvirt_provider_id).first()
    result = eip_service.sync_security_group_rules(db, provider, [])
    assert result == {"added": 0, "removed": 0}
    mock_get_driver.assert_not_called()
    db.close()
