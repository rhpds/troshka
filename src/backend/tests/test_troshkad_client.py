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


if __name__ == "__main__":
    unittest.main()
