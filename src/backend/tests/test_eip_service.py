"""Tests for EIP service lifecycle operations."""
import uuid
from unittest.mock import MagicMock, patch

from tests.conftest import TestSession
from app.core.auth import hash_password
from app.models.user import User
from app.models.provider import Provider
from app.models.host import Host
from app.models.elastic_ip import ElasticIp
from app.services import eip_service

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
    type="aws",
    default_region="us-east-1",
)
_provider.set_credentials({"access_key_id": "fake-key", "secret_access_key": "fake-secret"})
_db.add(_provider)
_db.commit()
_db.refresh(_provider)

_host = Host(
    provider_id=_provider.id,
    instance_id="i-test123",
    ip_address="10.0.1.100",
    private_key="-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----",
    state="running",
    max_eips=14,
)
_db.add(_host)
_db.commit()
_db.refresh(_host)

# Store IDs for later use
_provider_id = _provider.id
_host_id = _host.id
_db.close()


@patch("app.services.eip_service._get_ec2_client")
def test_allocate_eip(mock_ec2_client):
    """Test EIP allocation — mocks ec2.allocate_address, verifies DB row created."""
    mock_ec2 = MagicMock()
    mock_ec2.allocate_address.return_value = {
        "AllocationId": "eipalloc-abc123",
        "PublicIp": "54.123.45.67",
    }
    mock_ec2_client.return_value = mock_ec2

    db = TestSession()
    provider = db.query(Provider).filter_by(id=_provider_id).first()
    project_id = str(uuid.uuid4())
    canvas_eip_id = f"eip-{uuid.uuid4()}"

    eip = eip_service.allocate_eip(db, provider, project_id, canvas_eip_id)

    # Verify EC2 call
    mock_ec2.allocate_address.assert_called_once_with(Domain="vpc")
    mock_ec2.create_tags.assert_called_once()
    tags_call_args = mock_ec2.create_tags.call_args
    assert tags_call_args[1]["Resources"] == ["eipalloc-abc123"]
    tags = {t["Key"]: t["Value"] for t in tags_call_args[1]["Tags"]}
    assert tags["ManagedBy"] == "troshka"
    assert tags["troshka-provider-id"] == _provider_id
    assert tags["troshka-project-id"] == project_id
    assert tags["troshka-canvas-eip-id"] == canvas_eip_id

    # Verify DB row
    assert eip.provider_id == _provider_id
    assert eip.project_id == project_id
    assert eip.canvas_eip_id == canvas_eip_id
    assert eip.allocation_id == "eipalloc-abc123"
    assert eip.public_ip == "54.123.45.67"
    assert eip.state == "allocated"
    assert eip.host_id is None
    assert eip.private_ip is None

    db.close()


@patch("app.services.eip_service.run_ssh_script")
@patch("app.services.eip_service._get_ec2_client")
def test_release_eip(mock_ec2_client, mock_ssh):
    """Test EIP release — calls ec2.release_address and deletes DB row."""
    mock_ec2 = MagicMock()
    mock_ec2_client.return_value = mock_ec2

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

    # Verify EC2 call
    mock_ec2.release_address.assert_called_once_with(AllocationId="eipalloc-release123")

    # Verify DB row deleted
    assert db.query(ElasticIp).filter_by(id=eip_id).first() is None

    db.close()


@patch("app.services.eip_service.run_ssh_script")
@patch("app.services.eip_service._get_ec2_client")
def test_associate_eip(mock_ec2_client, mock_ssh):
    """Test EIP association — mocks ENI lookup, assign, associate, SSH config."""
    mock_ec2 = MagicMock()
    mock_ec2.describe_instances.return_value = {
        "Reservations": [
            {
                "Instances": [
                    {
                        "NetworkInterfaces": [
                            {
                                "NetworkInterfaceId": "eni-primary123",
                                "Attachment": {"DeviceIndex": 0},
                            }
                        ]
                    }
                ]
            }
        ]
    }
    mock_ec2.assign_private_ip_addresses.return_value = {
        "AssignedPrivateIpAddresses": [{"PrivateIpAddress": "10.0.1.50"}]
    }
    mock_ec2.associate_address.return_value = {"AssociationId": "eipassoc-abc456"}
    mock_ec2_client.return_value = mock_ec2
    mock_ssh.return_value = {"success": True, "output": "eth0\n"}

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

    # Verify EC2 calls
    mock_ec2.describe_instances.assert_called_once_with(InstanceIds=["i-test123"])
    mock_ec2.assign_private_ip_addresses.assert_called_once()
    assign_call = mock_ec2.assign_private_ip_addresses.call_args
    assert assign_call[1]["NetworkInterfaceId"] == "eni-primary123"
    assert assign_call[1]["SecondaryPrivateIpAddressCount"] == 1

    mock_ec2.associate_address.assert_called_once()
    assoc_call = mock_ec2.associate_address.call_args
    assert assoc_call[1]["AllocationId"] == "eipalloc-assoc123"
    assert assoc_call[1]["NetworkInterfaceId"] == "eni-primary123"
    assert assoc_call[1]["PrivateIpAddress"] == "10.0.1.50"

    # Verify SSH call
    assert mock_ssh.call_count == 2  # detect iface + ip addr add
    ssh_calls = [call[0] for call in mock_ssh.call_args_list]
    assert "ip route show default" in ssh_calls[0][2]
    assert "ip addr add 10.0.1.50/32 dev eth0" in ssh_calls[1][2]

    # Verify DB state
    db.refresh(eip)
    assert eip.state == "associated"
    assert eip.private_ip == "10.0.1.50"
    assert eip.host_id == _host_id
    assert eip.association_id == "eipassoc-abc456"

    db.close()


@patch("app.services.eip_service.run_ssh_script")
@patch("app.services.eip_service._get_ec2_client")
def test_disassociate_eip(mock_ec2_client, mock_ssh):
    """Test EIP disassociation — calls disassociate, unassign, SSH cleanup."""
    mock_ec2 = MagicMock()
    mock_ec2.describe_instances.return_value = {
        "Reservations": [
            {
                "Instances": [
                    {
                        "NetworkInterfaces": [
                            {
                                "NetworkInterfaceId": "eni-primary123",
                                "Attachment": {"DeviceIndex": 0},
                            }
                        ]
                    }
                ]
            }
        ]
    }
    mock_ec2_client.return_value = mock_ec2
    mock_ssh.return_value = {"success": True, "output": "eth0\n"}

    db = TestSession()
    host = db.query(Host).filter_by(id=_host_id).first()
    eip = ElasticIp(
        provider_id=_provider_id,
        project_id=str(uuid.uuid4()),
        canvas_eip_id=f"eip-{uuid.uuid4()}",
        allocation_id="eipalloc-disassoc123",
        public_ip="54.44.55.66",
        private_ip="10.0.1.60",
        host_id=_host_id,
        association_id="eipassoc-xyz789",
        state="associated",
    )
    db.add(eip)
    db.commit()
    db.refresh(eip)

    eip_service.disassociate_eip(db, eip, host)

    # Verify EC2 calls
    mock_ec2.disassociate_address.assert_called_once_with(AssociationId="eipassoc-xyz789")
    mock_ec2.unassign_private_ip_addresses.assert_called_once()
    unassign_call = mock_ec2.unassign_private_ip_addresses.call_args
    assert unassign_call[1]["NetworkInterfaceId"] == "eni-primary123"
    assert unassign_call[1]["PrivateIpAddresses"] == ["10.0.1.60"]

    # Verify SSH cleanup
    assert mock_ssh.call_count == 2  # detect iface + ip addr del
    ssh_calls = [call[0] for call in mock_ssh.call_args_list]
    assert "ip route show default" in ssh_calls[0][2]
    assert "ip addr del 10.0.1.60/32 dev eth0" in ssh_calls[1][2]

    # Verify DB state
    db.refresh(eip)
    assert eip.state == "allocated"
    assert eip.private_ip is None
    assert eip.host_id is None
    assert eip.association_id is None

    db.close()
