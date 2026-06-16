from unittest.mock import MagicMock, patch

import pytest

from app.services.providers import get_provider_driver
from app.services.providers.base import ProviderDriver
from app.services.providers.ec2 import EC2Driver


def test_get_ec2_driver():
    provider = MagicMock()
    provider.type = "ec2"
    driver = get_provider_driver(provider)
    assert isinstance(driver, ProviderDriver)
    assert isinstance(driver, EC2Driver)


def test_get_ocpvirt_driver():
    provider = MagicMock()
    provider.type = "ocpvirt"
    driver = get_provider_driver(provider)
    assert isinstance(driver, ProviderDriver)


def test_get_unknown_driver_raises():
    provider = MagicMock()
    provider.type = "unknown_xyz"
    with pytest.raises(ValueError, match="unknown_xyz"):
        get_provider_driver(provider)


@patch("app.services.provisioner._get_ec2_client")
def test_ec2_allocate_eip(mock_get_client):
    mock_ec2 = MagicMock()
    mock_ec2.allocate_address.return_value = {
        "AllocationId": "eipalloc-test123",
        "PublicIp": "54.1.2.3",
    }
    mock_get_client.return_value = mock_ec2

    provider = MagicMock()
    provider.type = "ec2"
    provider.id = "prov-123"
    provider.default_region = "us-east-1"
    provider.get_credentials.return_value = {
        "access_key_id": "fake",
        "secret_access_key": "fake",
    }

    host = MagicMock()
    host.instance_id = "i-abc123"

    driver = EC2Driver()
    result = driver.allocate_eip(provider, host, "eip-uuid-1234")

    assert result["public_ip"] == "54.1.2.3"
    assert result["allocation_id"] == "eipalloc-test123"
    mock_ec2.allocate_address.assert_called_once_with(Domain="vpc")
    mock_ec2.create_tags.assert_called_once()


@patch("app.services.provisioner._get_ec2_client")
def test_ec2_associate_eip(mock_get_client):
    mock_ec2 = MagicMock()
    mock_ec2.describe_instances.return_value = {
        "Reservations": [
            {
                "Instances": [
                    {
                        "NetworkInterfaces": [
                            {
                                "NetworkInterfaceId": "eni-primary",
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
    mock_ec2.associate_address.return_value = {"AssociationId": "eipassoc-xyz"}
    mock_get_client.return_value = mock_ec2

    provider = MagicMock()
    provider.type = "ec2"
    provider.get_credentials.return_value = {
        "access_key_id": "fake",
        "secret_access_key": "fake",
    }

    host = MagicMock()
    host.instance_id = "i-abc123"

    driver = EC2Driver()
    result = driver.associate_eip(provider, host, "eipalloc-test123")

    assert result["private_ip"] == "10.0.1.50"
    assert result["association_id"] == "eipassoc-xyz"


@patch("app.services.provisioner._get_ec2_client")
def test_ec2_release_eip(mock_get_client):
    mock_ec2 = MagicMock()
    mock_get_client.return_value = mock_ec2

    provider = MagicMock()
    provider.type = "ec2"
    provider.get_credentials.return_value = {
        "access_key_id": "fake",
        "secret_access_key": "fake",
    }

    driver = EC2Driver()
    driver.release_eip(provider, "eipalloc-test123")

    mock_ec2.release_address.assert_called_once_with(AllocationId="eipalloc-test123")
