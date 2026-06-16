from unittest.mock import MagicMock, patch

import pytest

from app.services.providers.ocpvirt import OCPVirtDriver, _parse_instance_type


def _make_provider():
    provider = MagicMock()
    provider.type = "ocpvirt"
    provider.get_credentials.return_value = {
        "api_url": "https://api.test.example.com:6443",
        "token": "sha256~testtoken",
        "namespace": "troshka",
        "verify_ssl": False,
    }
    return provider


def test_parse_instance_type_standard():
    assert _parse_instance_type("64c-256g") == (64, 256)
    assert _parse_instance_type("128c-512g") == (128, 512)
    assert _parse_instance_type("8c-16g") == (8, 16)


def test_parse_instance_type_defaults():
    assert _parse_instance_type(None) == (64, 256)
    assert _parse_instance_type("invalid") == (64, 256)
    assert _parse_instance_type("") == (64, 256)


@patch("app.services.providers.ocpvirt._get_k8s_clients")
@patch("app.services.providers.ocpvirt._generate_ssh_keypair")
@patch("app.services.providers.ocpvirt.time")
def test_provision_host_creates_vm(mock_time, mock_keygen, mock_clients):
    mock_custom = MagicMock()
    mock_core = MagicMock()
    mock_clients.return_value = (mock_custom, mock_core)
    mock_keygen.return_value = ("PRIVATE_KEY", "ssh-rsa PUBKEY")

    # VMI reaches Running with pod IP
    mock_custom.get_namespaced_custom_object.return_value = {
        "status": {
            "phase": "Running",
            "interfaces": [{"ipAddress": "10.128.2.50"}],
        }
    }
    # LoadBalancer service with external IP
    lb_svc = MagicMock()
    lb_ingress = MagicMock()
    lb_ingress.ip = "67.228.103.5"
    lb_svc.status.load_balancer.ingress = [lb_ingress]
    mock_core.read_namespaced_service.return_value = lb_svc

    driver = OCPVirtDriver()
    provider = _make_provider()
    result = driver.provision_host(
        provider=provider,
        host_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        instance_type="64c-256g",
        storage_size_gb=500,
    )

    assert result["host_id"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert result["private_ip"] == "10.128.2.50"
    assert result["private_key"] == "PRIVATE_KEY"
    assert result["total_vcpus"] == 64
    assert result["total_ram_mb"] == 256 * 1024
    assert result["max_eips"] == 0
    assert result["instance_id"] == "troshka-host-aaaaaaaa"
    assert result["_ssh_host"] == "67.228.103.5"
    assert result["_ssh_port"] == 22000
    assert result["public_ip"] == "67.228.103.5"

    # Should have created the VM
    mock_custom.create_namespaced_custom_object.assert_called_once()
    call_kwargs = mock_custom.create_namespaced_custom_object.call_args
    assert call_kwargs[1]["group"] == "kubevirt.io"
    assert call_kwargs[1]["plural"] == "virtualmachines"

    # Should have created 1 LoadBalancer service (SSH + agent ports)
    assert mock_core.create_namespaced_service.call_count == 1


@patch("app.services.providers.ocpvirt._get_k8s_clients")
def test_terminate_host_deletes_vm(mock_clients):
    mock_custom = MagicMock()
    mock_core = MagicMock()
    mock_clients.return_value = (mock_custom, mock_core)

    driver = OCPVirtDriver()
    provider = _make_provider()
    driver.terminate_host(provider, "troshka-host-aaaaaaaa")

    assert (
        mock_custom.delete_namespaced_custom_object.call_count == 3
    )  # VMI + VM + Route
    assert mock_core.delete_namespaced_service.call_count == 2  # lb, vncd


@patch("app.services.providers.ocpvirt._get_k8s_clients")
def test_get_host_status_running(mock_clients):
    mock_custom = MagicMock()
    mock_core = MagicMock()
    mock_clients.return_value = (mock_custom, mock_core)

    mock_custom.get_namespaced_custom_object.return_value = {
        "status": {
            "phase": "Running",
            "interfaces": [{"ipAddress": "10.128.2.50"}],
        }
    }

    driver = OCPVirtDriver()
    provider = _make_provider()
    status = driver.get_host_status(provider, "troshka-host-test")

    assert status["state"] == "running"
    assert status["private_ip"] == "10.128.2.50"


@patch("app.services.providers.ocpvirt._get_k8s_clients")
def test_stop_host_patches_vm(mock_clients):
    mock_custom = MagicMock()
    mock_core = MagicMock()
    mock_clients.return_value = (mock_custom, mock_core)

    driver = OCPVirtDriver()
    provider = _make_provider()
    driver.stop_host(provider, "troshka-host-test")

    mock_custom.patch_namespaced_custom_object.assert_called_once()
    call_kwargs = mock_custom.patch_namespaced_custom_object.call_args
    assert call_kwargs[1]["body"] == {"spec": {"running": False}}


@patch("app.services.providers.ocpvirt._get_k8s_clients")
def test_start_host_patches_vm(mock_clients):
    mock_custom = MagicMock()
    mock_core = MagicMock()
    mock_clients.return_value = (mock_custom, mock_core)

    driver = OCPVirtDriver()
    provider = _make_provider()
    driver.start_host(provider, "troshka-host-test")

    mock_custom.patch_namespaced_custom_object.assert_called_once()
    call_kwargs = mock_custom.patch_namespaced_custom_object.call_args
    assert call_kwargs[1]["body"] == {"spec": {"running": True}}


def test_resize_raises():
    driver = OCPVirtDriver()
    provider = _make_provider()
    with pytest.raises(NotImplementedError):
        driver.resize_host(provider, "test", "128c-512g")


@patch("app.services.providers.ocpvirt._get_k8s_clients")
@patch("app.services.providers.ocpvirt.time")
def test_allocate_eip_creates_lb_service(mock_time, mock_clients):
    mock_custom = MagicMock()
    mock_core = MagicMock()
    mock_clients.return_value = (mock_custom, mock_core)

    lb_svc = MagicMock()
    lb_ingress = MagicMock()
    lb_ingress.ip = "67.228.103.10"
    lb_svc.status.load_balancer.ingress = [lb_ingress]
    mock_core.read_namespaced_service.return_value = lb_svc

    driver = OCPVirtDriver()
    provider = _make_provider()
    host = MagicMock()
    host.instance_id = "troshka-host-aaaaaaaa"

    result = driver.allocate_eip(provider, host, "eip-uuid-1234")

    assert result["public_ip"] == "67.228.103.10"
    assert result["allocation_id"] == "troshka-eip-eip-uuid"
    mock_core.create_namespaced_service.assert_called_once()

    svc_call = mock_core.create_namespaced_service.call_args
    svc_body = svc_call[1]["body"]
    assert svc_body.spec.type == "LoadBalancer"
    assert svc_body.spec.selector == {"kubevirt.io/domain": "troshka-host-aaaaaaaa"}


@patch("app.services.providers.ocpvirt._get_k8s_clients")
def test_release_eip_deletes_lb_service(mock_clients):
    mock_custom = MagicMock()
    mock_core = MagicMock()
    mock_clients.return_value = (mock_custom, mock_core)

    driver = OCPVirtDriver()
    provider = _make_provider()
    driver.release_eip(provider, "troshka-eip-abcdefgh", namespace="troshka")

    mock_core.delete_namespaced_service.assert_called_once_with(
        "troshka-eip-abcdefgh", "troshka"
    )


@patch("app.services.providers.ocpvirt._get_k8s_clients")
def test_update_eip_ports_patches_service(mock_clients):
    mock_custom = MagicMock()
    mock_core = MagicMock()
    mock_clients.return_value = (mock_custom, mock_core)

    driver = OCPVirtDriver()
    provider = _make_provider()
    host = MagicMock()
    host.instance_id = "troshka-host-aaaaaaaa"

    ports = [
        {"port": 443, "targetPort": 40001, "name": "pf-0"},
        {"port": 8080, "targetPort": 40002, "name": "pf-1"},
    ]
    driver.update_eip_ports(provider, host, "troshka-eip-abcdefgh", ports)

    mock_core.patch_namespaced_service.assert_called_once()
    patch_call = mock_core.patch_namespaced_service.call_args
    assert patch_call[0][0] == "troshka-eip-abcdefgh"


def test_associate_eip_is_noop():
    driver = OCPVirtDriver()
    provider = _make_provider()
    host = MagicMock()
    result = driver.associate_eip(provider, host, "troshka-eip-abcdefgh")
    assert result == {}
