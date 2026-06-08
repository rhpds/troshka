# src/troshkad/tests/test_troshkad.py
"""Tests for troshkad daemon — uses a real HTTPS server on localhost."""
import json
import os
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

# Generate test TLS cert + key in a temp dir
TEST_DIR = tempfile.mkdtemp(prefix="troshkad-test-")
CERT_PATH = os.path.join(TEST_DIR, "server.crt")
KEY_PATH = os.path.join(TEST_DIR, "server.key")
CONF_PATH = os.path.join(TEST_DIR, "troshkad.conf")
TEST_TOKEN = "a" * 64
TEST_PORT = 31338  # avoid clashing with a real troshkad

# Generate self-signed cert for tests
subprocess.run([
    "openssl", "req", "-x509", "-newkey", "ec",
    "-pkeyopt", "ec_paramgen_curve:prime256v1",
    "-nodes", "-days", "1", "-subj", "/CN=localhost",
    "-keyout", KEY_PATH, "-out", CERT_PATH,
], capture_output=True, check=True)

# Write test config
with open(CONF_PATH, "w") as f:
    json.dump({
        "port": TEST_PORT,
        "token": TEST_TOKEN,
        "tls_cert": CERT_PATH,
        "tls_key": KEY_PATH,
        "host_id": "test-host-id",
        "max_concurrent_jobs": 2,
        "drain_timeout_seconds": 5,
    }, f)

# Import troshkad — add its directory to sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import troshkad


def _make_request(path, method="GET", body=None, token=TEST_TOKEN, expect_status=None):
    """Helper: make HTTPS request to test server, skip cert verification."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    url = f"https://localhost:{TEST_PORT}{path}"
    data = json.dumps(body).encode() if body else None
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        resp = urllib.request.urlopen(req, context=ctx, timeout=10)
        result = json.loads(resp.read().decode())
        if expect_status:
            assert resp.status == expect_status, f"Expected {expect_status}, got {resp.status}"
        return resp.status, result
    except urllib.error.HTTPError as e:
        result = json.loads(e.read().decode()) if e.fp else {}
        if expect_status:
            assert e.code == expect_status, f"Expected {expect_status}, got {e.code}"
        return e.code, result


class TestTroshkadServer(unittest.TestCase):
    """Integration tests against a real running troshkad server."""

    server = None
    server_thread = None

    @classmethod
    def setUpClass(cls):
        troshkad._config = troshkad.load_config(CONF_PATH)
        cls.server = troshkad.create_server(troshkad._config)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        time.sleep(0.3)  # let server start

    @classmethod
    def tearDownClass(cls):
        if cls.server:
            cls.server.shutdown()

    def test_health_returns_ok(self):
        status, body = _make_request("/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["host_id"], "test-host-id")
        self.assertIn("version", body)
        self.assertIn("capacity", body)

    def test_auth_missing_token_returns_401(self):
        status, _ = _make_request("/health", token=None)
        self.assertEqual(status, 401)

    def test_auth_wrong_token_returns_401(self):
        status, _ = _make_request("/health", token="wrong-token")
        self.assertEqual(status, 401)

    def test_unknown_path_returns_404(self):
        status, _ = _make_request("/nonexistent")
        self.assertEqual(status, 404)

    def test_wrong_method_returns_405(self):
        status, _ = _make_request("/health", method="POST")
        self.assertEqual(status, 405)


if __name__ == "__main__":
    unittest.main()
