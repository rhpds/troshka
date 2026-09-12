"""Tests for KubeVirt uplink MTU discovery via clusterNetworkMTU."""

from unittest.mock import MagicMock, patch

from app.services.providers import get_provider_driver
from app.services.providers.kubevirt import KubeVirtDriver


def _make_provider(provider_type="kubevirt"):
    p = MagicMock()
    p.type = provider_type
    p.id = "test-provider-id"
    p.get_credentials.return_value = {
        "api_url": "https://api.cluster.example.com:6443",
        "token": "test-token",
        "namespace": "troshka",
        "verify_ssl": False,
    }
    return p


def test_read_cluster_network_mtu_returns_value():
    """_read_cluster_network_mtu returns int when clusterNetworkMTU present."""
    provider = _make_provider()
    driver: KubeVirtDriver = get_provider_driver(provider)  # type: ignore[assignment]

    with patch("app.services.providers.kubevirt._get_k8s_clients") as mock_clients:
        mock_custom = MagicMock()
        mock_custom.get_cluster_custom_object.return_value = {
            "status": {"clusterNetworkMTU": 8900}
        }
        mock_core = MagicMock()
        mock_clients.return_value = (mock_custom, mock_core, MagicMock())

        result = driver._read_cluster_network_mtu(provider)

    assert result == 8900


def test_read_cluster_network_mtu_returns_none_when_field_absent():
    """_read_cluster_network_mtu returns None when status or field missing."""
    provider = _make_provider()
    driver: KubeVirtDriver = get_provider_driver(provider)  # type: ignore[assignment]

    with patch("app.services.providers.kubevirt._get_k8s_clients") as mock_clients:
        mock_custom = MagicMock()
        mock_custom.get_cluster_custom_object.return_value = {"status": {}}
        mock_core = MagicMock()
        mock_clients.return_value = (mock_custom, mock_core, MagicMock())

        result = driver._read_cluster_network_mtu(provider)

    assert result is None


def test_read_cluster_network_mtu_returns_none_on_exception():
    """_read_cluster_network_mtu returns None when API call raises."""
    provider = _make_provider()
    driver: KubeVirtDriver = get_provider_driver(provider)  # type: ignore[assignment]

    with patch("app.services.providers.kubevirt._get_k8s_clients") as mock_clients:
        mock_custom = MagicMock()
        mock_custom.get_cluster_custom_object.side_effect = Exception("API error")
        mock_core = MagicMock()
        mock_clients.return_value = (mock_custom, mock_core, MagicMock())

        result = driver._read_cluster_network_mtu(provider)

    assert result is None


def test_get_host_status_includes_uplink_mtu():
    """get_host_status includes uplink_mtu key in returned dict."""
    provider = _make_provider()
    driver: KubeVirtDriver = get_provider_driver(provider)  # type: ignore[assignment]

    with patch("app.services.providers.kubevirt._get_k8s_clients") as mock_clients:
        mock_custom = MagicMock()
        mock_core = MagicMock()
        mock_clients.return_value = (mock_custom, mock_core, MagicMock())

        # Mock _read_cluster_network_mtu to return 8900
        with patch.object(driver, "_read_cluster_network_mtu", return_value=8900):
            result = driver.get_host_status(
                provider, "https://api.cluster.example.com:6443"
            )

    assert result is not None
    assert "uplink_mtu" in result
    assert result["uplink_mtu"] == 8900


def test_poll_kubevirt_host_sets_uplink_mtu():
    """_poll_kubevirt_host sets host.uplink_mtu from driver status."""
    from app.services.health_poller import _poll_kubevirt_host

    provider = _make_provider()

    # Fake host object
    host = MagicMock()
    host.id = "test-host-id"
    host.provider_id = provider.id
    host.instance_id = "https://api.cluster.example.com:6443"
    host.uplink_mtu = None
    host.agent_status = None

    # Fake db with query that returns provider
    mock_db = MagicMock()
    mock_db.query.return_value.filter_by.return_value.first.return_value = provider

    # Mock get_provider_driver to return a driver with get_host_status
    with patch("app.services.providers.get_provider_driver") as mock_get_driver:
        mock_driver = MagicMock()
        mock_driver.get_host_status.return_value = {
            "instance_id": "https://api.cluster.example.com:6443",
            "state": "running",
            "public_ip": "10.0.0.1",
            "private_ip": "10.0.0.1",
            "uplink_mtu": 8900,
        }
        # Mock get_external_ip_capacity to avoid errors in _refresh_kubevirt_eip_capacity
        mock_driver.get_external_ip_capacity.return_value = None
        mock_get_driver.return_value = mock_driver

        _poll_kubevirt_host(host, mock_db)

    # Verify host.uplink_mtu was set
    assert host.uplink_mtu == 8900
    assert host.agent_status == "connected"
    mock_db.commit.assert_called()
