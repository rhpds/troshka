"""Tests for health_poller — mocks troshkad client and DB."""
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

# Need to set up test DB env before importing
import os
os.environ["TROSHKA_DATABASE__URL"] = "sqlite:///./test.db"


class TestPollHosts(unittest.TestCase):

    @patch("app.core.database.SessionLocal")
    @patch("app.services.troshkad_client.check_health")
    def test_successful_health_updates_fields(self, mock_check, mock_session_cls):
        from app.services.health_poller import _poll_hosts

        host = MagicMock()
        host.id = "test-host-uuid-1234"
        host.agent_status = "connected"
        host.last_health_at = datetime.now(timezone.utc)
        host.agent_token = "token123"

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = [host]
        mock_session_cls.return_value = mock_db

        mock_check.return_value = {
            "status": "ok",
            "version": "2026.06.08.1",
            "capacity": {
                "vcpus_total": 16, "vcpus_used": 4,
                "ram_total_mb": 65536, "ram_used_mb": 8192,
            },
        }

        _poll_hosts()

        self.assertIsNotNone(host.last_health_at)
        self.assertEqual(host.agent_version, "2026.06.08.1")
        self.assertEqual(host.total_vcpus, 16)
        self.assertEqual(host.used_vcpus, 4)
        mock_db.commit.assert_called_once()

    @patch("app.core.database.SessionLocal")
    @patch("app.services.troshkad_client.check_health")
    def test_failed_health_disconnects_after_timeout(self, mock_check, mock_session_cls):
        from app.services.health_poller import _poll_hosts

        host = MagicMock()
        host.id = "test-host-uuid-1234"
        host.agent_status = "connected"
        host.last_health_at = datetime.now(timezone.utc) - timedelta(seconds=120)
        host.agent_token = "token123"

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = [host]
        mock_session_cls.return_value = mock_db

        mock_check.return_value = None  # health check failed

        _poll_hosts()

        self.assertEqual(host.agent_status, "disconnected")

    @patch("app.core.database.SessionLocal")
    @patch("app.services.troshkad_client.check_health")
    def test_reconnects_disconnected_host(self, mock_check, mock_session_cls):
        from app.services.health_poller import _poll_hosts

        host = MagicMock()
        host.id = "test-host-uuid-1234"
        host.agent_status = "disconnected"
        host.agent_token = "token123"

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = [host]
        mock_session_cls.return_value = mock_db

        mock_check.return_value = {"status": "ok", "version": "1.0", "capacity": {}}

        _poll_hosts()

        self.assertEqual(host.agent_status, "connected")

    @patch("app.core.database.SessionLocal")
    @patch("app.services.troshkad_client.check_health")
    def test_partition_warning_stored_on_host(self, mock_check, mock_session_cls):
        from app.services.health_poller import _poll_hosts

        host = MagicMock()
        host.id = "test-host-uuid-1234"
        host.agent_status = "connected"
        host.last_health_at = datetime.now(timezone.utc)
        host.agent_token = "token123"
        host.storage_warnings = None

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = [host]
        mock_session_cls.return_value = mock_db

        mock_check.return_value = {
            "status": "ok", "version": "1.0",
            "capacity": {},
            "partitions": [
                {"mount": "/", "used_pct": 96.5, "total_bytes": 100*1024**3,
                 "used_bytes": 96*1024**3, "free_bytes": 4*1024**3,
                 "device": "/dev/nvme0n1p1", "fstype": "xfs"},
                {"mount": "/var/lib/troshka", "used_pct": 45.0, "total_bytes": 500*1024**3,
                 "used_bytes": 225*1024**3, "free_bytes": 275*1024**3,
                 "device": "/dev/nvme1n1", "fstype": "xfs"},
            ],
        }

        _poll_hosts()

        self.assertIsNotNone(host.storage_warnings)
        self.assertEqual(len(host.storage_warnings), 1)
        self.assertEqual(host.storage_warnings[0]["mount"], "/")
        self.assertEqual(host.storage_warnings[0]["level"], "critical")

    @patch("app.core.database.SessionLocal")
    @patch("app.services.troshkad_client.check_health")
    def test_partition_warnings_cleared_when_healthy(self, mock_check, mock_session_cls):
        from app.services.health_poller import _poll_hosts

        host = MagicMock()
        host.id = "test-host-uuid-1234"
        host.agent_status = "connected"
        host.last_health_at = datetime.now(timezone.utc)
        host.agent_token = "token123"
        host.storage_warnings = [{"mount": "/", "used_pct": 92.1, "level": "critical"}]

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = [host]
        mock_session_cls.return_value = mock_db

        mock_check.return_value = {
            "status": "ok", "version": "1.0",
            "capacity": {},
            "partitions": [
                {"mount": "/", "used_pct": 60.0, "total_bytes": 100*1024**3,
                 "used_bytes": 60*1024**3, "free_bytes": 40*1024**3,
                 "device": "/dev/nvme0n1p1", "fstype": "xfs"},
            ],
        }

        _poll_hosts()

        self.assertIsNone(host.storage_warnings)


if __name__ == '__main__':
    unittest.main()
