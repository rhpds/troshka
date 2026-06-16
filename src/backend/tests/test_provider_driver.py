from unittest.mock import MagicMock

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
