import pytest
from unittest.mock import MagicMock, patch
from app.services.providers import get_provider_driver


def _make_provider(provider_type="kubevirt"):
    p = MagicMock()
    p.type = provider_type
    p.get_credentials.return_value = {
        "api_url": "https://api.cluster.example.com:6443",
        "token": "test-token",
        "namespace": "troshka",
        "verify_ssl": False,
    }
    return p


def test_get_provider_driver_returns_kubevirt():
    provider = _make_provider()
    driver = get_provider_driver(provider)
    from app.services.providers.kubevirt import KubeVirtDriver

    assert isinstance(driver, KubeVirtDriver)


def test_provision_host_returns_cluster_info():
    provider = _make_provider()
    driver = get_provider_driver(provider)

    mock_node = MagicMock()
    mock_node.status.allocatable = {"cpu": "64", "memory": "262144Mi"}
    mock_nodes = MagicMock()
    mock_nodes.items = [mock_node]

    with patch("app.services.providers.kubevirt._get_k8s_clients") as mock_clients:
        mock_custom = MagicMock()
        mock_core = MagicMock()
        mock_core.list_node.return_value = mock_nodes
        mock_clients.return_value = (mock_custom, mock_core, MagicMock())

        result = driver.provision_host(
            provider, "test-host-id", "kubevirt-cluster", 1000
        )

    assert result["host_id"] == "test-host-id"
    assert result["instance_type"] == "kubevirt-cluster"
    assert result["total_vcpus"] == 64
    assert result["total_ram_mb"] == 262144


def test_get_host_status_returns_running():
    provider = _make_provider()
    driver = get_provider_driver(provider)

    with patch("app.services.providers.kubevirt._get_k8s_clients") as mock_clients:
        mock_custom = MagicMock()
        mock_core = MagicMock()
        mock_clients.return_value = (mock_custom, mock_core, MagicMock())

        result = driver.get_host_status(
            provider, "https://api.cluster.example.com:6443"
        )

    assert result is not None
    assert result["state"] == "running"


def test_get_host_powerstate_always_running():
    provider = _make_provider()
    driver = get_provider_driver(provider)
    assert driver.get_host_powerstate(provider, "any") == "running"


def test_deploy_project_creates_namespace_and_cr():
    provider = _make_provider()
    driver = get_provider_driver(provider)

    with patch("app.services.providers.kubevirt._get_k8s_clients") as mock_clients:
        mock_custom = MagicMock()
        mock_core = MagicMock()
        mock_clients.return_value = (mock_custom, mock_core, MagicMock())

        result = driver.deploy_project(
            provider,
            "12345678-1234-1234-1234-123456789abc",
            {"nodes": [], "edges": []},
            {"bucket": "test", "endpoint": "s3.amazonaws.com", "region": "us-east-1"},
        )

    assert result == "project-12345678"
    mock_core.create_namespace.assert_called_once()
    mock_custom.create_namespaced_custom_object.assert_called_once()
    call_args = mock_custom.create_namespaced_custom_object.call_args
    assert call_args.kwargs["namespace"] == "troshka-12345678"
    assert call_args.kwargs["body"]["spec"]["action"] == "deploy"


def test_destroy_project_deletes_cr_and_namespace():
    provider = _make_provider()
    driver = get_provider_driver(provider)

    with patch("app.services.providers.kubevirt._get_k8s_clients") as mock_clients:
        mock_custom = MagicMock()
        mock_core = MagicMock()
        mock_clients.return_value = (mock_custom, mock_core, MagicMock())

        driver.destroy_project(provider, "12345678-1234-1234-1234-123456789abc")

    mock_custom.delete_namespaced_custom_object.assert_called_once()
    mock_core.delete_namespace.assert_called_once_with(name="troshka-12345678")


def test_setup_console_returns_config():
    provider = _make_provider()
    driver = get_provider_driver(provider)
    result = driver.setup_console(provider, "console.example.com")
    assert result["console_base_domain"] == "console.example.com"


def test_create_console_record_creates_service_and_route():
    provider = _make_provider()
    driver = get_provider_driver(provider)

    with patch("app.services.providers.kubevirt._get_k8s_clients") as mock_clients:
        mock_custom = MagicMock()
        mock_core = MagicMock()
        mock_clients.return_value = (mock_custom, mock_core, MagicMock())

        host = MagicMock()
        driver.create_console_record(
            provider, host, "vm1.console.example.com", "10.0.0.1"
        )

    mock_core.create_namespaced_service.assert_called_once()
    mock_custom.create_namespaced_custom_object.assert_called_once()
    route_call = mock_custom.create_namespaced_custom_object.call_args
    assert route_call.kwargs["body"]["spec"]["host"] == "vm1.console.example.com"


def test_delete_console_record_cleans_up():
    provider = _make_provider()
    driver = get_provider_driver(provider)

    with patch("app.services.providers.kubevirt._get_k8s_clients") as mock_clients:
        mock_custom = MagicMock()
        mock_core = MagicMock()
        mock_clients.return_value = (mock_custom, mock_core, MagicMock())

        host = MagicMock()
        driver.delete_console_record(
            provider, host, "vm1.console.example.com", "10.0.0.1"
        )

    mock_core.delete_namespaced_service.assert_called_once()
    mock_custom.delete_namespaced_custom_object.assert_called_once()


def test_create_route_access_creates_service_and_route():
    provider = _make_provider()
    driver = get_provider_driver(provider)

    with patch("app.services.providers.kubevirt._get_k8s_clients") as mock_clients:
        mock_custom = MagicMock()
        mock_core = MagicMock()
        mock_custom.create_namespaced_custom_object.return_value = {
            "spec": {"host": "bastion-443.apps.cluster.example.com"}
        }
        mock_clients.return_value = (mock_custom, mock_core, MagicMock())

        host = MagicMock()
        result = driver.create_route_access(
            provider, host, "proj-1234-5678", "bastion", "10.0.0.10", 443
        )

    assert result["hostname"] == "bastion-443.apps.cluster.example.com"
    mock_core.create_namespaced_service.assert_called_once()


def test_delete_route_access_cleans_up_by_label():
    provider = _make_provider()
    driver = get_provider_driver(provider)

    mock_svc = MagicMock()
    mock_svc.metadata.name = "rt-bastion-443"
    mock_route = {"metadata": {"name": "rt-bastion-443"}}

    with patch("app.services.providers.kubevirt._get_k8s_clients") as mock_clients:
        mock_custom = MagicMock()
        mock_core = MagicMock()
        mock_core.list_namespaced_service.return_value.items = [mock_svc]
        mock_custom.list_namespaced_custom_object.return_value = {"items": [mock_route]}
        mock_clients.return_value = (mock_custom, mock_core, MagicMock())

        driver.delete_route_access(provider, "proj-1234-5678")

    mock_core.delete_namespaced_service.assert_called_once()
    mock_custom.delete_namespaced_custom_object.assert_called_once()


def test_resize_and_extend_are_noops():
    provider = _make_provider()
    driver = get_provider_driver(provider)
    assert driver.resize_host(provider, "any", "any") == {}
    assert driver.extend_host_storage(provider, MagicMock(), MagicMock()) == {}


def test_start_stop_host_are_noops():
    provider = _make_provider()
    driver = get_provider_driver(provider)
    driver.start_host(provider, "any")
    driver.stop_host(provider, "any")
