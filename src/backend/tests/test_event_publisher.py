"""Tests for the platform integration event publisher."""

from unittest.mock import MagicMock, patch

import pytest


class TestPublishEvent:

    @patch("app.services.event_publisher.config")
    def test_disabled_does_nothing(self, mock_config):
        mock_config.get.side_effect = lambda k, d=None: {"integration.enabled": False}.get(k, d)
        from app.services.event_publisher import publish_event
        publish_event("proj-1", "deploy.completed", {"vm_count": 3})

    @patch("app.services.event_publisher._post")
    @patch("app.services.event_publisher.config")
    def test_deploy_failed_pushes_to_deepfield(self, mock_config, mock_post):
        mock_config.get.side_effect = lambda k, d=None: {
            "integration.enabled": True,
            "integration.deepfield_url": "https://deepfield.test",
            "integration.api_key": "key-1",
            "integration.ssl_verify": True,
        }.get(k, d)
        from app.services.event_publisher import publish_event
        publish_event("proj-1", "deploy.failed", {"error": "disk not found", "step": "disks"})
        mock_post.assert_called_once()
        args = mock_post.call_args
        assert "deepfield.test/integration/events" in args[0][0]
        payload = args[0][1]
        assert payload["source"] == "troshka"
        assert payload["payload"]["signal_type"] == "vm_failed"
        assert payload["payload"]["severity"] == "high"

    @patch("app.services.event_publisher._post")
    @patch("app.services.event_publisher.config")
    def test_deploy_completed_maps_to_vm_running(self, mock_config, mock_post):
        mock_config.get.side_effect = lambda k, d=None: {
            "integration.enabled": True,
            "integration.deepfield_url": "https://deepfield.test",
            "integration.api_key": "",
            "integration.ssl_verify": True,
        }.get(k, d)
        from app.services.event_publisher import publish_event
        publish_event("proj-2", "deploy.completed", {"vm_count": 5})
        mock_post.assert_called_once()
        payload = mock_post.call_args[0][1]
        assert payload["payload"]["signal_type"] == "vm_running"
        assert payload["payload"]["severity"] == "info"

    @patch("app.services.event_publisher._post")
    @patch("app.services.event_publisher.config")
    def test_unmapped_event_type_skipped(self, mock_config, mock_post):
        mock_config.get.side_effect = lambda k, d=None: {
            "integration.enabled": True,
            "integration.deepfield_url": "https://deepfield.test",
            "integration.api_key": "",
            "integration.ssl_verify": True,
        }.get(k, d)
        from app.services.event_publisher import publish_event
        publish_event("proj-3", "deploy.step_completed", {"step": "networks"})
        mock_post.assert_not_called()

    @patch("app.services.event_publisher._post")
    @patch("app.services.event_publisher.config")
    def test_no_deepfield_url_skips(self, mock_config, mock_post):
        mock_config.get.side_effect = lambda k, d=None: {
            "integration.enabled": True,
            "integration.deepfield_url": "",
            "integration.api_key": "",
            "integration.ssl_verify": True,
        }.get(k, d)
        from app.services.event_publisher import publish_event
        publish_event("proj-4", "deploy.failed", {"error": "test"})
        mock_post.assert_not_called()

    @patch("app.services.event_publisher._post")
    @patch("app.services.event_publisher.config")
    def test_vm_state_changed_stopped(self, mock_config, mock_post):
        mock_config.get.side_effect = lambda k, d=None: {
            "integration.enabled": True,
            "integration.deepfield_url": "https://deepfield.test",
            "integration.api_key": "",
            "integration.ssl_verify": True,
        }.get(k, d)
        from app.services.event_publisher import publish_event
        publish_event("proj-5", "vm.state_changed", {"vm_id": "vm-1", "new_state": "stopped"})
        mock_post.assert_called_once()
        payload = mock_post.call_args[0][1]
        assert payload["payload"]["signal_type"] == "vm_failed"

    @patch("app.services.event_publisher._post")
    @patch("app.services.event_publisher.config")
    def test_host_health_warning_critical(self, mock_config, mock_post):
        mock_config.get.side_effect = lambda k, d=None: {
            "integration.enabled": True,
            "integration.deepfield_url": "https://deepfield.test",
            "integration.api_key": "",
            "integration.ssl_verify": True,
        }.get(k, d)
        from app.services.event_publisher import publish_event
        publish_event("host-1", "host.health_warning", {"mount": "/var", "used_pct": 97, "severity": "critical"})
        mock_post.assert_called_once()
        payload = mock_post.call_args[0][1]
        assert payload["payload"]["signal_type"] == "node_pressure"
        assert payload["payload"]["severity"] == "critical"


class TestStarGateClient:

    @patch("app.services.stargate_client.config")
    def test_disabled_returns_none(self, mock_config):
        mock_config.get.side_effect = lambda k, d=None: {"integration.enabled": False}.get(k, d)
        from app.services.stargate_client import create_run
        assert create_run("proj-1", "test-project") is None

    @patch("app.services.stargate_client._get_session")
    @patch("app.services.stargate_client.config")
    def test_create_run_success(self, mock_config, mock_get_session):
        mock_config.get.side_effect = lambda k, d=None: {
            "integration.enabled": True,
            "integration.stargate_url": "https://stargate.test",
            "integration.api_key": "key",
            "integration.ssl_verify": True,
        }.get(k, d)
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_session = MagicMock()
        mock_session.post.return_value = mock_resp
        mock_get_session.return_value = mock_session

        from app.services.stargate_client import create_run
        run_id = create_run("proj-1", "ocp-lab", host_name="10.0.0.1")
        assert run_id is not None
        assert run_id.startswith("troshka-")
        mock_session.post.assert_called_once()

    @patch("app.services.stargate_client._get_session")
    @patch("app.services.stargate_client.config")
    def test_create_run_failure_returns_none(self, mock_config, mock_get_session):
        mock_config.get.side_effect = lambda k, d=None: {
            "integration.enabled": True,
            "integration.stargate_url": "https://stargate.test",
            "integration.api_key": "",
            "integration.ssl_verify": True,
        }.get(k, d)
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        mock_session = MagicMock()
        mock_session.post.return_value = mock_resp
        mock_get_session.return_value = mock_session

        from app.services.stargate_client import create_run
        assert create_run("proj-1", "ocp-lab") is None

    @patch("app.services.stargate_client._get_session")
    @patch("app.services.stargate_client.config")
    def test_submit_evidence_graceful_failure(self, mock_config, mock_get_session):
        mock_config.get.side_effect = lambda k, d=None: {
            "integration.enabled": True,
            "integration.stargate_url": "https://stargate.test",
            "integration.api_key": "",
            "integration.ssl_verify": True,
        }.get(k, d)
        mock_session = MagicMock()
        mock_session.post.side_effect = ConnectionError("refused")
        mock_get_session.return_value = mock_session

        from app.services.stargate_client import _run_ids, submit_evidence
        _run_ids["proj-1"] = "run-123"
        submit_evidence("proj-1", "networks", {"status": "completed"})
