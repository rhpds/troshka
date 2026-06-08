# src/backend/tests/test_troshkad_client.py
"""Tests for troshkad_client -- mocks http.client to test client logic."""
import hashlib
import json
import unittest
from unittest.mock import patch, MagicMock

from app.services.troshkad_client import troshkad_request, start_job, poll_job


# Generate a fake cert fingerprint for tests
FAKE_CERT_DER = b"fake-cert-der-bytes-for-testing"
FAKE_FINGERPRINT = hashlib.sha256(FAKE_CERT_DER).hexdigest().upper()


class FakeHost:
    ip_address = "10.0.0.1"
    agent_token = "a" * 64
    agent_cert_fingerprint = FAKE_FINGERPRINT


def _mock_conn(response_body, status=200):
    """Create a mock HTTPSConnection that returns the given response."""
    mock_conn = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status = status
    mock_resp.read.return_value = json.dumps(response_body).encode() if isinstance(response_body, dict) else response_body
    mock_conn.getresponse.return_value = mock_resp
    mock_conn.sock.getpeercert.return_value = FAKE_CERT_DER
    return mock_conn


class TestTroshkadClient(unittest.TestCase):

    @patch("app.services.troshkad_client.http.client.HTTPSConnection")
    def test_troshkad_request_sends_auth(self, mock_https_cls):
        """Request includes bearer token header."""
        mock_conn = _mock_conn({"status": "ok"})
        mock_https_cls.return_value = mock_conn

        result = troshkad_request(FakeHost(), "GET", "/health")
        self.assertEqual(result["status"], "ok")
        # Check the request had the auth header
        call_args = mock_conn.request.call_args
        headers = call_args[1].get("headers", call_args[0][3] if len(call_args[0]) > 3 else {})
        self.assertTrue(headers["Authorization"].startswith("Bearer "))

    @patch("app.services.troshkad_client.http.client.HTTPSConnection")
    def test_start_job_returns_job_id(self, mock_https_cls):
        mock_conn = _mock_conn({"job_id": "test-123", "status": "running"}, status=202)
        mock_https_cls.return_value = mock_conn

        job_id = start_job(FakeHost(), "/vms/create", {"domain_name": "test"})
        self.assertEqual(job_id, "test-123")

    @patch("app.services.troshkad_client.http.client.HTTPSConnection")
    def test_poll_job_returns_status(self, mock_https_cls):
        mock_conn = _mock_conn({
            "job_id": "test-123", "status": "completed",
            "result": {"domain": "test"}, "output": [],
        })
        mock_https_cls.return_value = mock_conn

        job = poll_job(FakeHost(), "test-123")
        self.assertEqual(job["status"], "completed")

    @patch("app.services.troshkad_client.http.client.HTTPSConnection")
    def test_cert_fingerprint_mismatch_raises(self, mock_https_cls):
        """Wrong cert fingerprint raises TroshkadError."""
        from app.services.troshkad_client import TroshkadError
        mock_conn = _mock_conn({"status": "ok"})
        mock_conn.sock.getpeercert.return_value = b"wrong-cert-bytes"
        mock_https_cls.return_value = mock_conn

        with self.assertRaises(TroshkadError) as ctx:
            troshkad_request(FakeHost(), "GET", "/health")
        self.assertIn("fingerprint mismatch", str(ctx.exception))

    @patch("app.services.troshkad_client.http.client.HTTPSConnection")
    def test_missing_fingerprint_raises(self, mock_https_cls):
        """Missing cert fingerprint raises TroshkadError (fail-closed)."""
        from app.services.troshkad_client import TroshkadError

        class NoFingerprintHost:
            ip_address = "10.0.0.1"
            agent_token = "a" * 64
            agent_cert_fingerprint = None

        mock_conn = _mock_conn({"status": "ok"})
        mock_https_cls.return_value = mock_conn

        with self.assertRaises(TroshkadError) as ctx:
            troshkad_request(NoFingerprintHost(), "GET", "/health")
        self.assertIn("No cert fingerprint", str(ctx.exception))

    @patch("app.services.troshkad_client.http.client.HTTPSConnection")
    def test_check_disk_usage(self, mock_https_cls):
        from app.services.troshkad_client import check_disk_usage
        mock_conn = _mock_conn({"free_bytes": 380*1024**3, "total_bytes": 500*1024**3, "used_pct": 24})
        mock_https_cls.return_value = mock_conn
        result = check_disk_usage(FakeHost())
        self.assertEqual(result["used_pct"], 24)

    @patch("app.services.troshkad_client.http.client.HTTPSConnection")
    def test_start_job_retries_during_drain(self, mock_https_cls):
        """start_job retries when troshkad is draining, succeeds when it comes back."""
        from app.services.troshkad_client import TroshkadError

        call_count = 0
        def mock_conn_factory(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                # First 2 calls: draining
                return _mock_conn({"status": "draining", "error": "draining for update"}, status=503)
            else:
                # 3rd call: back up
                return _mock_conn({"job_id": "new-job-123", "status": "running"}, status=202)

        mock_https_cls.side_effect = mock_conn_factory

        # Patch sleep to avoid waiting
        with patch("app.services.troshkad_client.time.sleep"):
            job_id = start_job(FakeHost(), "/vms/create", {"domain_name": "test"})

        self.assertEqual(job_id, "new-job-123")
        self.assertEqual(call_count, 3)

    @patch("app.services.troshkad_client.http.client.HTTPSConnection")
    def test_start_job_does_not_retry_max_concurrent(self, mock_https_cls):
        """start_job does NOT retry on max_concurrent_jobs 503."""
        from app.services.troshkad_client import TroshkadError

        mock_conn = _mock_conn({"error": "max_concurrent_jobs reached"}, status=503)
        mock_https_cls.return_value = mock_conn

        with self.assertRaises(TroshkadError):
            start_job(FakeHost(), "/vms/create", {"domain_name": "test"})


class TestVmHelpers(unittest.TestCase):
    """Tests for VM management convenience functions."""

    @patch("app.services.troshkad_client.http.client.HTTPSConnection")
    def test_get_vm_state_running(self, mock_https_cls):
        from app.services.troshkad_client import get_vm_state

        call_count = 0
        def conn_factory(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # start_job response
                return _mock_conn({"job_id": "j1", "status": "running"}, status=202)
            else:
                # poll_job response
                return _mock_conn({"job_id": "j1", "status": "completed",
                                   "result": {"domain": "test", "state": "running"}, "output": []})
        mock_https_cls.side_effect = conn_factory
        state = get_vm_state(FakeHost(), "troshka-aabbccdd-11223344")
        self.assertEqual(state, "running")

    @patch("app.services.troshkad_client.http.client.HTTPSConnection")
    def test_get_vm_state_error_returns_not_found(self, mock_https_cls):
        from app.services.troshkad_client import get_vm_state, TroshkadError

        mock_conn = _mock_conn({"error": "connection refused"}, status=500)
        mock_https_cls.return_value = mock_conn
        state = get_vm_state(FakeHost(), "troshka-aabbccdd-11223344")
        self.assertEqual(state, "not_found")

    @patch("app.services.troshkad_client.http.client.HTTPSConnection")
    def test_get_vnc_port(self, mock_https_cls):
        from app.services.troshkad_client import get_vnc_port

        call_count = 0
        def conn_factory(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _mock_conn({"job_id": "j1", "status": "running"}, status=202)
            else:
                return _mock_conn({"job_id": "j1", "status": "completed",
                                   "result": {"domain": "test", "vnc_port": 5901}, "output": []})
        mock_https_cls.side_effect = conn_factory
        port = get_vnc_port(FakeHost(), "troshka-aabbccdd-11223344")
        self.assertEqual(port, 5901)

    @patch("app.services.troshkad_client.http.client.HTTPSConnection")
    def test_get_vnc_port_none(self, mock_https_cls):
        from app.services.troshkad_client import get_vnc_port

        call_count = 0
        def conn_factory(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _mock_conn({"job_id": "j1", "status": "running"}, status=202)
            else:
                return _mock_conn({"job_id": "j1", "status": "completed",
                                   "result": {"domain": "test", "vnc_port": None}, "output": []})
        mock_https_cls.side_effect = conn_factory
        port = get_vnc_port(FakeHost(), "troshka-aabbccdd-11223344")
        self.assertIsNone(port)

    @patch("app.services.troshkad_client.http.client.HTTPSConnection")
    def test_get_vm_config(self, mock_https_cls):
        from app.services.troshkad_client import get_vm_config

        config = {"vcpus": 4, "ram_mb": 8192, "boot_devs": ["hd"], "nics": [], "disks": [], "cdroms": []}
        call_count = 0
        def conn_factory(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _mock_conn({"job_id": "j1", "status": "running"}, status=202)
            else:
                return _mock_conn({"job_id": "j1", "status": "completed",
                                   "result": config, "output": []})
        mock_https_cls.side_effect = conn_factory
        result = get_vm_config(FakeHost(), "troshka-aabbccdd-11223344")
        self.assertEqual(result["vcpus"], 4)
        self.assertEqual(result["ram_mb"], 8192)

    @patch("app.services.troshkad_client.http.client.HTTPSConnection")
    def test_reconfigure_vm(self, mock_https_cls):
        from app.services.troshkad_client import reconfigure_vm

        call_count = 0
        def conn_factory(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _mock_conn({"job_id": "j1", "status": "running"}, status=202)
            else:
                return _mock_conn({"job_id": "j1", "status": "completed",
                                   "result": {"domain": "test", "status": "reconfigured", "restarted": True}, "output": []})
        mock_https_cls.side_effect = conn_factory
        result = reconfigure_vm(FakeHost(), "troshka-aabbccdd-11223344", vcpus=8, ram_mb=16384)
        self.assertEqual(result["status"], "reconfigured")

    @patch("app.services.troshkad_client.http.client.HTTPSConnection")
    def test_reconfigure_vm_failure_raises(self, mock_https_cls):
        from app.services.troshkad_client import reconfigure_vm, TroshkadError

        call_count = 0
        def conn_factory(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _mock_conn({"job_id": "j1", "status": "running"}, status=202)
            else:
                return _mock_conn({"job_id": "j1", "status": "failed",
                                   "result": {"error": "virsh define failed"}, "output": []})
        mock_https_cls.side_effect = conn_factory
        with self.assertRaises(TroshkadError):
            reconfigure_vm(FakeHost(), "troshka-aabbccdd-11223344", vcpus=8)

    @patch("app.services.troshkad_client.http.client.HTTPSConnection")
    def test_undefine_vm(self, mock_https_cls):
        from app.services.troshkad_client import undefine_vm

        call_count = 0
        def conn_factory(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _mock_conn({"job_id": "j1", "status": "running"}, status=202)
            else:
                return _mock_conn({"job_id": "j1", "status": "completed",
                                   "result": {"domain": "test", "status": "undefined"}, "output": []})
        mock_https_cls.side_effect = conn_factory
        result = undefine_vm(FakeHost(), "troshka-aabbccdd-11223344")
        self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()
