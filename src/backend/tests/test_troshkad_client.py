# src/backend/tests/test_troshkad_client.py
"""Tests for troshkad_client -- mocks urllib3 to test client logic."""
import hashlib
import json
import unittest
from unittest.mock import patch, MagicMock, PropertyMock

from app.services.troshkad_client import (
    troshkad_request, start_job, poll_job, check_disk_usage,
    TroshkadError, _get_pool,
)


FAKE_CERT_DER = b"fake-cert-der-bytes-for-testing"
FAKE_FINGERPRINT = hashlib.sha256(FAKE_CERT_DER).hexdigest().upper()


class FakeHost:
    ip_address = "10.0.0.1"
    agent_token = "a" * 64
    agent_cert_fingerprint = FAKE_FINGERPRINT


class NoFingerprintHost:
    ip_address = "10.0.0.1"
    agent_token = "a" * 64
    agent_cert_fingerprint = None


def _mock_response(body, status=200):
    """Create a mock urllib3 HTTPResponse."""
    resp = MagicMock()
    resp.status = status
    resp.data = json.dumps(body).encode() if isinstance(body, dict) else body
    return resp


class TestTroshkadClient(unittest.TestCase):

    @patch("app.services.troshkad_client._get_pool")
    def test_troshkad_request_sends_auth(self, mock_get_pool):
        pool = MagicMock()
        pool.urlopen.return_value = _mock_response({"status": "ok"})
        mock_get_pool.return_value = pool

        result = troshkad_request(FakeHost(), "GET", "/health")
        self.assertEqual(result["status"], "ok")
        call_kwargs = pool.urlopen.call_args
        headers = call_kwargs[1].get("headers", {}) if call_kwargs[1] else call_kwargs[0][2] if len(call_kwargs[0]) > 2 else {}
        self.assertIn("Authorization", headers)

    @patch("app.services.troshkad_client._get_pool")
    def test_start_job_returns_job_id(self, mock_get_pool):
        pool = MagicMock()
        pool.urlopen.return_value = _mock_response({"job_id": "test-123", "status": "running"})
        mock_get_pool.return_value = pool

        job_id = start_job(FakeHost(), "/vms/create", {"domain_name": "test"})
        self.assertEqual(job_id, "test-123")

    @patch("app.services.troshkad_client._get_pool")
    def test_poll_job_returns_status(self, mock_get_pool):
        pool = MagicMock()
        pool.urlopen.return_value = _mock_response({
            "job_id": "test-123", "status": "completed",
            "result": {"domain": "test"}, "output": [],
        })
        mock_get_pool.return_value = pool

        job = poll_job(FakeHost(), "test-123")
        self.assertEqual(job["status"], "completed")

    def test_missing_fingerprint_raises(self):
        with self.assertRaises(TroshkadError) as ctx:
            _get_pool(NoFingerprintHost())
        self.assertIn("No cert fingerprint", str(ctx.exception))

    @patch("app.services.troshkad_client._get_pool")
    def test_503_retries(self, mock_get_pool):
        pool = MagicMock()
        pool.urlopen.side_effect = [
            _mock_response({"error": "draining"}, status=503),
            _mock_response({"status": "ok"}),
        ]
        mock_get_pool.return_value = pool

        with patch("app.services.troshkad_client.time.sleep"):
            result = troshkad_request(FakeHost(), "GET", "/health")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(pool.urlopen.call_count, 2)

    @patch("app.services.troshkad_client._get_pool")
    def test_connection_error_retries(self, mock_get_pool):
        from urllib3.exceptions import MaxRetryError, NewConnectionError
        pool = MagicMock()
        pool.urlopen.side_effect = [
            MaxRetryError(pool, "/health", reason=NewConnectionError(pool, "Connection refused")),
            _mock_response({"status": "ok"}),
        ]
        mock_get_pool.return_value = pool

        with patch("app.services.troshkad_client.time.sleep"):
            result = troshkad_request(FakeHost(), "GET", "/health")
        self.assertEqual(result["status"], "ok")

    @patch("app.services.troshkad_client._get_pool")
    def test_ssl_error_on_fingerprint_mismatch(self, mock_get_pool):
        from urllib3.exceptions import SSLError
        pool = MagicMock()
        pool.urlopen.side_effect = SSLError("fingerprint mismatch")
        mock_get_pool.return_value = pool

        with self.assertRaises(TroshkadError) as ctx:
            troshkad_request(FakeHost(), "GET", "/health")
        self.assertIn("Certificate", str(ctx.exception))

    @patch("app.services.troshkad_client._get_pool")
    def test_check_disk_usage(self, mock_get_pool):
        pool = MagicMock()
        pool.urlopen.return_value = _mock_response({"free_bytes": 380*1024**3, "total_bytes": 500*1024**3, "used_pct": 24})
        mock_get_pool.return_value = pool
        result = check_disk_usage(FakeHost())
        self.assertEqual(result["used_pct"], 24)

    @patch("app.services.troshkad_client._get_pool")
    def test_start_job_retries_during_drain(self, mock_get_pool):
        pool = MagicMock()
        call_count = 0
        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return _mock_response({"status": "draining", "error": "draining for update"}, status=503)
            return _mock_response({"job_id": "new-job-123", "status": "running"})
        pool.urlopen.side_effect = side_effect
        mock_get_pool.return_value = pool

        with patch("app.services.troshkad_client.time.sleep"):
            job_id = start_job(FakeHost(), "/vms/create", {"domain_name": "test"})
        self.assertEqual(job_id, "new-job-123")
