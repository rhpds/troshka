from unittest.mock import MagicMock

import pytest

from app.services.providers.libvirt import DEFAULT_MAX_EIPS, LibvirtDriver


def _make_provider():
    provider = MagicMock()
    provider.type = "libvirt"
    provider.get_credentials.return_value = {
        "ssh_private_key": "-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n-----END OPENSSH PRIVATE KEY-----\n"
    }
    return provider


def _make_host(ip_address="192.168.124.198", host_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"):
    host = MagicMock()
    host.id = host_id
    host.ip_address = ip_address
    return host


def test_provision_host_returns_ip_and_nonzero_max_eips():
    driver = LibvirtDriver()
    provider = _make_provider()
    result = driver.provision_host(
        provider=provider,
        host_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        instance_type=None,
        storage_size_gb=50,
        ip_address="192.168.124.198",
    )
    assert result["public_ip"] == "192.168.124.198"
    assert result["private_ip"] == "192.168.124.198"
    assert result["max_eips"] == DEFAULT_MAX_EIPS
    assert result["max_eips"] > 0


def test_provision_host_requires_ip_address():
    driver = LibvirtDriver()
    provider = _make_provider()
    with pytest.raises(ValueError, match="ip_address"):
        driver.provision_host(
            provider=provider,
            host_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            instance_type=None,
            storage_size_gb=50,
        )


def test_provision_host_requires_ssh_private_key():
    driver = LibvirtDriver()
    provider = MagicMock()
    provider.get_credentials.return_value = {}
    with pytest.raises(ValueError, match="ssh_private_key"):
        driver.provision_host(
            provider=provider,
            host_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            instance_type=None,
            storage_size_gb=50,
            ip_address="192.168.124.198",
        )


def test_terminate_host_is_noop():
    driver = LibvirtDriver()
    # Should not raise for a provider/instance Troshka never created.
    assert driver.terminate_host(_make_provider(), "libvirt-aaaaaaaa") is None


def test_get_host_status_reports_terminated():
    driver = LibvirtDriver()
    result = driver.get_host_status(_make_provider(), "libvirt-aaaaaaaa")
    assert result["state"] == "terminated"
    assert result["instance_id"] == "libvirt-aaaaaaaa"


def test_allocate_eip_uses_host_ip_address():
    driver = LibvirtDriver()
    provider = _make_provider()
    host = _make_host(ip_address="192.168.124.198")

    result = driver.allocate_eip(provider, host, "11111111-2222-3333-4444-555555555555")

    assert result["public_ip"] == "192.168.124.198"
    assert result["allocation_id"] == "libvirt-eip-11111111"


def test_allocate_eip_requires_host_ip_address():
    driver = LibvirtDriver()
    provider = _make_provider()
    host = _make_host(ip_address=None)

    with pytest.raises(ValueError, match="ip_address"):
        driver.allocate_eip(provider, host, "11111111-2222-3333-4444-555555555555")


def test_associate_eip_returns_host_ip_as_private_ip():
    driver = LibvirtDriver()
    provider = _make_provider()
    host = _make_host(ip_address="192.168.124.198")

    result = driver.associate_eip(provider, host, "libvirt-eip-11111111")

    assert result == {"private_ip": "192.168.124.198"}


def test_release_eip_is_noop_and_does_not_raise():
    driver = LibvirtDriver()
    provider = _make_provider()
    # No allocation was ever made against a real API — must not raise.
    assert driver.release_eip(provider, "libvirt-eip-11111111") is None
    assert driver.release_eip(provider, "libvirt-eip-11111111", namespace="ignored") is None
