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
        mock_clients.return_value = (mock_custom, mock_core)

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
        mock_clients.return_value = (mock_custom, mock_core)

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
        mock_clients.return_value = (mock_custom, mock_core)

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
        mock_clients.return_value = (mock_custom, mock_core)

        driver.destroy_project(provider, "12345678-1234-1234-1234-123456789abc")

    mock_custom.delete_namespaced_custom_object.assert_called_once()
    mock_core.delete_namespace.assert_called_once_with(name="troshka-12345678")
