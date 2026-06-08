# src/backend/tests/test_troshkad_client.py
"""Tests for troshkad_client -- mocks HTTP to test client logic."""
import json
import unittest
from unittest.mock import patch, MagicMock

from app.services.troshkad_client import troshkad_request, start_job, poll_job


class FakeHost:
    ip_address = "10.0.0.1"
    agent_token = "a" * 64
    agent_cert_fingerprint = "sha256:" + "ab" * 32


class TestTroshkadClient(unittest.TestCase):

    @patch("app.services.troshkad_client.urllib.request.urlopen")
    def test_troshkad_request_sends_auth(self, mock_urlopen):
        """Request includes bearer token header."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"status": "ok"}'
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = troshkad_request(FakeHost(), "GET", "/health")
        self.assertEqual(result["status"], "ok")
        # Check the request had the auth header
        req = mock_urlopen.call_args[0][0]
        self.assertTrue(req.get_header("Authorization").startswith("Bearer "))

    @patch("app.services.troshkad_client.urllib.request.urlopen")
    def test_start_job_returns_job_id(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"job_id": "test-123", "status": "running"}).encode()
        mock_resp.status = 202
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        job_id = start_job(FakeHost(), "/vms/create", {"domain_name": "test"})
        self.assertEqual(job_id, "test-123")

    @patch("app.services.troshkad_client.urllib.request.urlopen")
    def test_poll_job_returns_status(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "job_id": "test-123", "status": "completed",
            "result": {"domain": "test"}, "output": [],
        }).encode()
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        job = poll_job(FakeHost(), "test-123")
        self.assertEqual(job["status"], "completed")


if __name__ == "__main__":
    unittest.main()
