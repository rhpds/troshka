"""Tests for deploy uplink MTU synchronous fetch."""

from unittest.mock import MagicMock, patch


def test_ensure_host_uplink_mtu_already_set():
    """When uplink_mtu is already set, return it without calling check_health."""
    from app.services.deploy_service import _ensure_host_uplink_mtu

    host = MagicMock()
    host.host_type = "standard"
    host.uplink_mtu = 9000
    s = MagicMock()

    with patch("app.services.troshkad_client.check_health") as mock_check_health:
        result = _ensure_host_uplink_mtu(host, s)

    assert result == 9000
    mock_check_health.assert_not_called()
    s.commit.assert_not_called()


def test_ensure_host_uplink_mtu_fetches_and_sets():
    """When uplink_mtu is None, fetch from health and set it."""
    from app.services.deploy_service import _ensure_host_uplink_mtu

    host = MagicMock()
    host.host_type = "standard"
    host.uplink_mtu = None
    s = MagicMock()

    with patch("app.services.troshkad_client.check_health") as mock_check_health:
        with patch("app.services.health_poller._apply_uplink_mtu") as mock_apply:
            mock_check_health.return_value = {"uplink_mtu": 8900}

            # Simulate _apply_uplink_mtu setting the value
            def apply_side_effect(h, health):
                h.uplink_mtu = 8900

            mock_apply.side_effect = apply_side_effect

            result = _ensure_host_uplink_mtu(host, s)

    assert result == 8900
    mock_check_health.assert_called_once_with(host)
    mock_apply.assert_called_once_with(host, {"uplink_mtu": 8900})
    s.commit.assert_called_once()


def test_ensure_host_uplink_mtu_health_returns_none():
    """When check_health returns None, return None without crashing."""
    from app.services.deploy_service import _ensure_host_uplink_mtu

    host = MagicMock()
    host.host_type = "standard"
    host.uplink_mtu = None
    s = MagicMock()

    with patch("app.services.troshkad_client.check_health") as mock_check_health:
        with patch("app.services.health_poller._apply_uplink_mtu") as mock_apply:
            mock_check_health.return_value = None

            result = _ensure_host_uplink_mtu(host, s)

    assert result is None
    mock_check_health.assert_called_once_with(host)
    mock_apply.assert_not_called()  # not called when health is None (falsy)
    s.commit.assert_not_called()


def test_ensure_host_uplink_mtu_health_returns_empty():
    """When check_health returns empty dict, return None."""
    from app.services.deploy_service import _ensure_host_uplink_mtu

    host = MagicMock()
    host.host_type = "standard"
    host.uplink_mtu = None
    s = MagicMock()

    with patch("app.services.troshkad_client.check_health") as mock_check_health:
        with patch("app.services.health_poller._apply_uplink_mtu") as mock_apply:
            mock_check_health.return_value = {}

            result = _ensure_host_uplink_mtu(host, s)

    assert result is None
    mock_check_health.assert_called_once_with(host)
    mock_apply.assert_not_called()  # not called when health is {} (falsy)
    s.commit.assert_not_called()


def test_ensure_host_uplink_mtu_health_raises():
    """When check_health raises, return None without crashing."""
    from app.services.deploy_service import _ensure_host_uplink_mtu

    host = MagicMock()
    host.uplink_mtu = None
    host.host_type = "standard"
    s = MagicMock()

    with patch("app.services.troshkad_client.check_health") as mock_check_health:
        mock_check_health.side_effect = Exception("Network error")

        result = _ensure_host_uplink_mtu(host, s)

    assert result is None
    mock_check_health.assert_called_once_with(host)
    s.commit.assert_not_called()


def test_ensure_host_uplink_mtu_kubevirt_cluster_skips_check():
    """KubeVirt-cluster hosts skip check_health and return uplink_mtu as-is."""
    from app.services.deploy_service import _ensure_host_uplink_mtu

    host = MagicMock()
    host.host_type = "kubevirt-cluster"
    host.uplink_mtu = None
    s = MagicMock()

    with patch("app.services.troshkad_client.check_health") as mock_check_health:
        result = _ensure_host_uplink_mtu(host, s)

    assert result is None
    mock_check_health.assert_not_called()  # kubevirt hosts skip check_health
    s.commit.assert_not_called()


def test_ensure_host_uplink_mtu_kubevirt_cluster_with_uplink_set():
    """KubeVirt-cluster hosts with uplink_mtu set return it immediately."""
    from app.services.deploy_service import _ensure_host_uplink_mtu

    host = MagicMock()
    host.host_type = "kubevirt-cluster"
    host.uplink_mtu = 8900
    s = MagicMock()

    with patch("app.services.troshkad_client.check_health") as mock_check_health:
        result = _ensure_host_uplink_mtu(host, s)

    assert result == 8900
    mock_check_health.assert_not_called()
    s.commit.assert_not_called()
