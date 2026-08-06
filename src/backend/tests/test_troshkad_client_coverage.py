# src/backend/tests/test_troshkad_client_coverage.py
"""Extended coverage tests for troshkad_client — covers functions and branches
not exercised by test_troshkad_client.py."""
import hashlib
import json
import unittest
from unittest.mock import MagicMock, patch

from urllib3.exceptions import MaxRetryError, NewConnectionError, SSLError

from app.services.troshkad_client import (
    TroshkadError,
    _execute_single_request,
    _find_primary_partition,
    _get_drain_retry_reason,
    _get_pool,
    _is_drain_retryable,
    cancel_job,
    check_disk_usage,
    check_health,
    get_all_container_states,
    get_all_vm_states,
    get_vm_config,
    get_vm_state,
    get_vnc_port,
    push_update,
    push_vncd_update,
    reconfigure_vm,
    troshkad_download_from_vm,
    troshkad_request,
    troshkad_request_raw,
    troshkad_upload_to_vm,
    undefine_vm,
    wait_for_job,
)

FAKE_CERT_DER = b"fake-cert-der-bytes-for-testing"
FAKE_FINGERPRINT = hashlib.sha256(FAKE_CERT_DER).hexdigest().upper()


class FakeHost:
    ip_address = "10.0.0.1"
    agent_token = "a" * 64
    agent_cert_fingerprint = FAKE_FINGERPRINT
    agent_status = "connected"


class DisconnectedHost:
    ip_address = "10.0.0.2"
    agent_token = "b" * 64
    agent_cert_fingerprint = FAKE_FINGERPRINT
    agent_status = "disconnected"


def _mock_response(body, status=200):
    """Create a mock urllib3 HTTPResponse."""
    resp = MagicMock()
    resp.status = status
    if isinstance(body, dict):
        resp.data = json.dumps(body).encode()
    elif isinstance(body, bytes):
        resp.data = body
    else:
        resp.data = str(body).encode()
    return resp


# ---------------------------------------------------------------------------
# troshkad_request_raw
# ---------------------------------------------------------------------------
class TestTroshkadRequestRaw(unittest.TestCase):
    @patch("app.services.troshkad_client._get_pool")
    def test_success_returns_response(self, mock_get_pool):
        pool = MagicMock()
        resp = _mock_response(b"binary-data")
        pool.urlopen.return_value = resp
        mock_get_pool.return_value = pool

        result = troshkad_request_raw(FakeHost(), "GET", "/some/path")
        self.assertEqual(result.data, b"binary-data")
        self.assertEqual(result.status, 200)

    @patch("app.services.troshkad_client._get_pool")
    def test_custom_headers_merged(self, mock_get_pool):
        pool = MagicMock()
        pool.urlopen.return_value = _mock_response(b"ok")
        mock_get_pool.return_value = pool

        troshkad_request_raw(
            FakeHost(),
            "POST",
            "/path",
            body=b"data",
            headers={"Content-Type": "application/octet-stream"},
        )
        call_kwargs = pool.urlopen.call_args
        headers = call_kwargs[1].get("headers") or call_kwargs.kwargs.get("headers")
        self.assertIn("Authorization", headers)
        self.assertEqual(headers["Content-Type"], "application/octet-stream")

    @patch("app.services.troshkad_client._get_pool")
    def test_error_400_json_body(self, mock_get_pool):
        pool = MagicMock()
        pool.urlopen.return_value = _mock_response({"error": "bad request"}, status=400)
        mock_get_pool.return_value = pool

        with self.assertRaises(TroshkadError) as ctx:
            troshkad_request_raw(FakeHost(), "POST", "/fail")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.response["error"], "bad request")

    @patch("app.services.troshkad_client._get_pool")
    def test_error_500_non_json_body(self, mock_get_pool):
        pool = MagicMock()
        resp = MagicMock()
        resp.status = 500
        resp.data = b"Internal Server Error"
        pool.urlopen.return_value = resp
        mock_get_pool.return_value = pool

        with self.assertRaises(TroshkadError) as ctx:
            troshkad_request_raw(FakeHost(), "GET", "/fail")
        self.assertEqual(ctx.exception.status_code, 500)
        self.assertEqual(ctx.exception.response["error"], "Internal Server Error")

    @patch("app.services.troshkad_client._get_pool")
    def test_ssl_error(self, mock_get_pool):
        pool = MagicMock()
        pool.urlopen.side_effect = SSLError("fingerprint mismatch")
        mock_get_pool.return_value = pool

        with self.assertRaises(TroshkadError) as ctx:
            troshkad_request_raw(FakeHost(), "GET", "/path")
        self.assertIn("Certificate verification failed", str(ctx.exception))

    @patch("app.services.troshkad_client._get_pool")
    def test_max_retry_error(self, mock_get_pool):
        pool = MagicMock()
        pool.urlopen.side_effect = MaxRetryError(
            pool, "/path", reason=NewConnectionError(pool, "refused")
        )
        mock_get_pool.return_value = pool

        with self.assertRaises(TroshkadError) as ctx:
            troshkad_request_raw(FakeHost(), "GET", "/path")
        self.assertIn("Cannot connect", str(ctx.exception))

    @patch("app.services.troshkad_client._get_pool")
    def test_generic_exception(self, mock_get_pool):
        pool = MagicMock()
        pool.urlopen.side_effect = RuntimeError("unexpected")
        mock_get_pool.return_value = pool

        with self.assertRaises(TroshkadError) as ctx:
            troshkad_request_raw(FakeHost(), "GET", "/path")
        self.assertIn("troshkad request failed", str(ctx.exception))


# ---------------------------------------------------------------------------
# troshkad_upload_to_vm
# ---------------------------------------------------------------------------
class TestTroshkadUploadToVm(unittest.TestCase):
    @patch("app.services.troshkad_client.troshkad_request_raw")
    def test_sync_200(self, mock_raw):
        mock_raw.return_value = _mock_response({"ok": True}, status=200)
        result = troshkad_upload_to_vm(
            FakeHost(),
            b"file-content",
            "proj-1",
            "192.168.1.10",
            "user",
            "pass",
            "/tmp/file.txt",
        )
        self.assertTrue(result["ok"])

    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.troshkad_request_raw")
    def test_async_202_success(self, mock_raw, mock_wait):
        mock_raw.return_value = _mock_response({"job_id": "upload-job-1"}, status=202)
        mock_wait.return_value = {
            "status": "completed",
            "result": {"bytes_written": 1024},
        }
        result = troshkad_upload_to_vm(
            FakeHost(),
            b"x" * 1024,
            "proj-1",
            "192.168.1.10",
            "user",
            "pass",
            "/tmp/big.bin",
        )
        self.assertEqual(result["bytes_written"], 1024)
        mock_wait.assert_called_once()

    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.troshkad_request_raw")
    def test_async_202_failed_job(self, mock_raw, mock_wait):
        mock_raw.return_value = _mock_response({"job_id": "upload-job-2"}, status=202)
        mock_wait.return_value = {
            "status": "failed",
            "result": {"error": "disk full"},
        }
        with self.assertRaises(TroshkadError) as ctx:
            troshkad_upload_to_vm(
                FakeHost(),
                b"data",
                "proj-1",
                "192.168.1.10",
                "user",
                "pass",
                "/tmp/file.txt",
            )
        self.assertIn("disk full", str(ctx.exception))

    @patch("app.services.troshkad_client.troshkad_request_raw")
    def test_with_private_key(self, mock_raw):
        mock_raw.return_value = _mock_response({"ok": True}, status=200)
        troshkad_upload_to_vm(
            FakeHost(),
            b"data",
            "proj-1",
            "192.168.1.10",
            "",
            "",
            "/tmp/file.txt",
            mode="0755",
            private_key="ssh-rsa AAAA",
        )
        call_args = mock_raw.call_args
        path = call_args[0][2]  # third positional arg is the URL path
        self.assertIn("private_key=", path)
        self.assertIn("mode=0755", path)


# ---------------------------------------------------------------------------
# troshkad_download_from_vm
# ---------------------------------------------------------------------------
class TestTroshkadDownloadFromVm(unittest.TestCase):
    @patch("app.services.troshkad_client.troshkad_request_raw")
    def test_returns_bytes(self, mock_raw):
        resp = MagicMock()
        resp.status = 200
        resp.data = b"file-content-bytes"
        mock_raw.return_value = resp

        result = troshkad_download_from_vm(
            FakeHost(),
            "proj-1",
            "192.168.1.10",
            "user",
            "pass",
            "/etc/hosts",
        )
        self.assertEqual(result, b"file-content-bytes")


# ---------------------------------------------------------------------------
# _is_drain_retryable
# ---------------------------------------------------------------------------
class TestIsDrainRetryable(unittest.TestCase):
    def test_503_is_retryable(self):
        e = TroshkadError("draining", status_code=503)
        self.assertTrue(_is_drain_retryable(e))

    def test_cannot_connect_is_retryable(self):
        e = TroshkadError("Cannot connect to troshkad on 10.0.0.1")
        self.assertTrue(_is_drain_retryable(e))

    def test_timed_out_is_retryable(self):
        e = TroshkadError("Request timed out waiting for response")
        self.assertTrue(_is_drain_retryable(e))

    def test_is_disconnected_is_retryable(self):
        e = TroshkadError("Host 10.0.0.1 is disconnected — skipping request")
        self.assertTrue(_is_drain_retryable(e))

    def test_auth_error_not_retryable(self):
        e = TroshkadError("Unauthorized", status_code=401)
        self.assertFalse(_is_drain_retryable(e))

    def test_generic_error_not_retryable(self):
        e = TroshkadError("some other error", status_code=500)
        self.assertFalse(_is_drain_retryable(e))


# ---------------------------------------------------------------------------
# _get_drain_retry_reason
# ---------------------------------------------------------------------------
class TestGetDrainRetryReason(unittest.TestCase):
    def test_no_status_code_returns_unreachable(self):
        e = TroshkadError("Cannot connect")
        self.assertEqual(_get_drain_retry_reason(e), "unreachable")

    def test_draining_status(self):
        e = TroshkadError("draining", status_code=503, response={"status": "draining"})
        self.assertEqual(_get_drain_retry_reason(e), "draining")

    def test_fallback_busy(self):
        e = TroshkadError("busy", status_code=503, response={"status": "busy"})
        self.assertEqual(_get_drain_retry_reason(e), "busy (job queue full)")

    def test_status_code_no_response(self):
        e = TroshkadError("error", status_code=503, response=None)
        self.assertEqual(_get_drain_retry_reason(e), "busy (job queue full)")


# ---------------------------------------------------------------------------
# wait_for_job
# ---------------------------------------------------------------------------
class TestWaitForJob(unittest.TestCase):
    @patch("app.services.troshkad_client.time.sleep")
    @patch("app.services.troshkad_client.poll_job")
    def test_completed(self, mock_poll, mock_sleep):
        mock_poll.return_value = {
            "job_id": "j1",
            "status": "completed",
            "result": {"ok": True},
            "output": ["done"],
        }
        job = wait_for_job(FakeHost(), "j1", timeout=30)
        self.assertEqual(job["status"], "completed")

    @patch("app.services.troshkad_client.time.sleep")
    @patch("app.services.troshkad_client.poll_job")
    def test_failed(self, mock_poll, mock_sleep):
        mock_poll.return_value = {
            "job_id": "j1",
            "status": "failed",
            "result": {"error": "boom"},
            "output": [],
        }
        job = wait_for_job(FakeHost(), "j1", timeout=30)
        self.assertEqual(job["status"], "failed")

    @patch("app.services.troshkad_client.time.time")
    @patch("app.services.troshkad_client.time.sleep")
    @patch("app.services.troshkad_client.poll_job")
    def test_timeout(self, mock_poll, mock_sleep, mock_time):
        # Simulate time passing beyond deadline
        mock_time.side_effect = [0, 100, 200]  # start, first check > deadline
        mock_poll.return_value = {
            "job_id": "j1",
            "status": "running",
            "output": [],
        }
        with self.assertRaises(TroshkadError) as ctx:
            wait_for_job(FakeHost(), "j1", timeout=50)
        self.assertIn("timed out", str(ctx.exception))

    @patch("app.services.troshkad_client.time.sleep")
    @patch("app.services.troshkad_client.poll_job")
    def test_404_job_lost(self, mock_poll, mock_sleep):
        mock_poll.side_effect = TroshkadError("Not found", status_code=404)
        job = wait_for_job(FakeHost(), "j1-lost-job", timeout=30)
        self.assertEqual(job["status"], "failed")
        self.assertIn("Job lost", job["result"]["error"])

    @patch("app.services.troshkad_client.time.sleep")
    @patch("app.services.troshkad_client.poll_job")
    def test_consecutive_failures_exceed_max(self, mock_poll, mock_sleep):
        mock_poll.side_effect = TroshkadError("Cannot connect", status_code=None)
        with self.assertRaises(TroshkadError):
            wait_for_job(FakeHost(), "j1", timeout=9999, poll_interval=0)

    @patch("app.services.troshkad_client.time.sleep")
    @patch("app.services.troshkad_client.poll_job")
    def test_transient_failures_then_success(self, mock_poll, mock_sleep):
        """A few transient errors followed by completion should succeed."""
        fail = TroshkadError("Cannot connect", status_code=None)
        success = {
            "job_id": "j1",
            "status": "completed",
            "result": {"ok": True},
            "output": ["line1"],
        }
        mock_poll.side_effect = [fail, fail, success]
        job = wait_for_job(FakeHost(), "j1", timeout=9999, poll_interval=0)
        self.assertEqual(job["status"], "completed")

    @patch("app.services.troshkad_client.time.sleep")
    @patch("app.services.troshkad_client.poll_job")
    def test_fast_polling_delays(self, mock_poll, mock_sleep):
        """First few polls use fast delays, then fall back to poll_interval."""
        calls = []
        mock_poll.side_effect = [
            {"job_id": "j1", "status": "running", "output": []},
            {"job_id": "j1", "status": "running", "output": []},
            {"job_id": "j1", "status": "running", "output": []},
            {"job_id": "j1", "status": "running", "output": []},
            {"job_id": "j1", "status": "running", "output": []},
            {"job_id": "j1", "status": "completed", "result": {}, "output": []},
        ]

        def record_sleep(secs):
            calls.append(secs)

        mock_sleep.side_effect = record_sleep
        wait_for_job(FakeHost(), "j1", timeout=9999, poll_interval=5)
        # First 4 should be fast delays: 0.1, 0.3, 0.5, 1.0, then 5
        self.assertEqual(calls[:4], [0.1, 0.3, 0.5, 1.0])
        self.assertEqual(calls[4], 5)


# ---------------------------------------------------------------------------
# check_health
# ---------------------------------------------------------------------------
class TestCheckHealth(unittest.TestCase):
    @patch("app.services.troshkad_client._get_pool")
    def test_success(self, mock_get_pool):
        pool = MagicMock()
        pool.urlopen.return_value = _mock_response(
            {"status": "healthy", "version": "1.0"}
        )
        mock_get_pool.return_value = pool

        result = check_health(FakeHost())
        self.assertEqual(result["status"], "healthy")

    @patch("app.services.troshkad_client._get_pool")
    def test_error_returns_none(self, mock_get_pool):
        pool = MagicMock()
        pool.urlopen.side_effect = SSLError("cert mismatch")
        mock_get_pool.return_value = pool

        result = check_health(FakeHost())
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# push_update
# ---------------------------------------------------------------------------
class TestPushUpdate(unittest.TestCase):
    @patch("app.services.troshkad_client._get_pool")
    def test_push_without_force(self, mock_get_pool):
        pool = MagicMock()
        pool.urlopen.return_value = _mock_response({"status": "restarting"})
        mock_get_pool.return_value = pool

        result = push_update(FakeHost(), b"#!/usr/bin/env python3\n", "v1.0")
        self.assertEqual(result["status"], "restarting")
        call_args = pool.urlopen.call_args
        path = call_args[0][1]
        self.assertEqual(path, "/admin/update")

    @patch("app.services.troshkad_client._get_pool")
    def test_push_with_force(self, mock_get_pool):
        pool = MagicMock()
        pool.urlopen.return_value = _mock_response({"status": "restarting"})
        mock_get_pool.return_value = pool

        push_update(FakeHost(), b"script", "v2.0", force=True)
        call_args = pool.urlopen.call_args
        path = call_args[0][1]
        self.assertEqual(path, "/admin/update?force=true")


# ---------------------------------------------------------------------------
# push_vncd_update
# ---------------------------------------------------------------------------
class TestPushVncdUpdate(unittest.TestCase):
    @patch("app.services.troshkad_client._get_pool")
    def test_push_vncd(self, mock_get_pool):
        pool = MagicMock()
        pool.urlopen.return_value = _mock_response({"status": "ok"})
        mock_get_pool.return_value = pool

        push_vncd_update(FakeHost(), b"vncd-script-bytes")
        pool.urlopen.assert_called_once()
        call_args = pool.urlopen.call_args
        path = call_args[0][1]
        self.assertEqual(path, "/admin/update-vncd")


# ---------------------------------------------------------------------------
# get_vm_state
# ---------------------------------------------------------------------------
class TestGetVmState(unittest.TestCase):
    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.troshkad_request")
    def test_completed(self, mock_request, mock_wait):
        mock_request.return_value = {"job_id": "j1"}
        mock_wait.return_value = {
            "status": "completed",
            "result": {"state": "running", "boot_devs": ["hd"]},
        }
        result = get_vm_state(FakeHost(), "test-domain")
        self.assertEqual(result["state"], "running")
        self.assertEqual(result["boot_devs"], ["hd"])

    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.troshkad_request")
    def test_failed_returns_unknown(self, mock_request, mock_wait):
        mock_request.return_value = {"job_id": "j1"}
        mock_wait.return_value = {
            "status": "failed",
            "result": {"error": "domain not found"},
        }
        result = get_vm_state(FakeHost(), "missing-domain")
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(result["boot_devs"], [])

    @patch("app.services.troshkad_client.troshkad_request")
    def test_troshkad_error_returns_not_found(self, mock_request):
        mock_request.side_effect = TroshkadError("Cannot connect")
        result = get_vm_state(FakeHost(), "unreachable-domain")
        self.assertEqual(result["state"], "not_found")
        self.assertEqual(result["boot_devs"], [])


# ---------------------------------------------------------------------------
# get_all_vm_states
# ---------------------------------------------------------------------------
class TestGetAllVmStates(unittest.TestCase):
    @patch("app.services.troshkad_client._get_pool")
    def test_success(self, mock_get_pool):
        pool = MagicMock()
        pool.urlopen.return_value = _mock_response(
            {
                "domains": {
                    "troshka-aaa": {"state": "running"},
                    "troshka-bbb": {"state": "shutoff"},
                }
            }
        )
        mock_get_pool.return_value = pool

        result = get_all_vm_states(FakeHost())
        self.assertEqual(result, {"troshka-aaa": "running", "troshka-bbb": "shutoff"})

    @patch("app.services.troshkad_client._get_pool")
    def test_error_returns_none(self, mock_get_pool):
        pool = MagicMock()
        pool.urlopen.side_effect = SSLError("cert issue")
        mock_get_pool.return_value = pool

        result = get_all_vm_states(FakeHost())
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# get_all_container_states
# ---------------------------------------------------------------------------
class TestGetAllContainerStates(unittest.TestCase):
    @patch("app.services.troshkad_client._get_pool")
    def test_success(self, mock_get_pool):
        pool = MagicMock()
        pool.urlopen.return_value = _mock_response(
            {
                "containers": {
                    "troshka-web": {"state": "running", "ips": ["10.0.0.5"]},
                }
            }
        )
        mock_get_pool.return_value = pool

        result = get_all_container_states(FakeHost())
        self.assertEqual(result["troshka-web"]["state"], "running")

    @patch("app.services.troshkad_client._get_pool")
    def test_error_returns_none(self, mock_get_pool):
        pool = MagicMock()
        pool.urlopen.side_effect = SSLError("cert issue")
        mock_get_pool.return_value = pool

        result = get_all_container_states(FakeHost())
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# get_vnc_port
# ---------------------------------------------------------------------------
class TestGetVncPort(unittest.TestCase):
    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job")
    def test_success(self, mock_start, mock_wait):
        mock_start.return_value = "j1"
        mock_wait.return_value = {
            "status": "completed",
            "result": {"vnc_port": 5901},
        }
        port = get_vnc_port(FakeHost(), "test-domain")
        self.assertEqual(port, 5901)

    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job")
    def test_failed_returns_none(self, mock_start, mock_wait):
        mock_start.return_value = "j1"
        mock_wait.return_value = {
            "status": "failed",
            "result": {"error": "no VNC"},
        }
        port = get_vnc_port(FakeHost(), "no-vnc-domain")
        self.assertIsNone(port)

    @patch("app.services.troshkad_client.start_job")
    def test_error_returns_none(self, mock_start):
        mock_start.side_effect = TroshkadError("Cannot connect")
        port = get_vnc_port(FakeHost(), "unreachable-domain")
        self.assertIsNone(port)


# ---------------------------------------------------------------------------
# get_vm_config
# ---------------------------------------------------------------------------
class TestGetVmConfig(unittest.TestCase):
    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job")
    def test_success(self, mock_start, mock_wait):
        mock_start.return_value = "j1"
        mock_wait.return_value = {
            "status": "completed",
            "result": {"vcpus": 4, "ram_mb": 8192},
        }
        config = get_vm_config(FakeHost(), "test-domain")
        self.assertEqual(config["vcpus"], 4)

    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job")
    def test_failed_returns_none(self, mock_start, mock_wait):
        mock_start.return_value = "j1"
        mock_wait.return_value = {
            "status": "failed",
            "result": {"error": "not found"},
        }
        config = get_vm_config(FakeHost(), "missing-domain")
        self.assertIsNone(config)

    @patch("app.services.troshkad_client.start_job")
    def test_error_returns_none(self, mock_start):
        mock_start.side_effect = TroshkadError("Cannot connect")
        config = get_vm_config(FakeHost(), "unreachable-domain")
        self.assertIsNone(config)


# ---------------------------------------------------------------------------
# reconfigure_vm
# ---------------------------------------------------------------------------
class TestReconfigureVm(unittest.TestCase):
    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job")
    def test_success(self, mock_start, mock_wait):
        mock_start.return_value = "j1"
        mock_wait.return_value = {
            "status": "completed",
            "result": {"vcpus": 8},
        }
        result = reconfigure_vm(FakeHost(), "test-domain", vcpus=8)
        self.assertEqual(result["vcpus"], 8)

    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job")
    def test_failed_raises(self, mock_start, mock_wait):
        mock_start.return_value = "j1"
        mock_wait.return_value = {
            "status": "failed",
            "result": {"error": "cannot hotplug"},
        }
        with self.assertRaises(TroshkadError) as ctx:
            reconfigure_vm(FakeHost(), "test-domain", vcpus=128)
        self.assertIn("Reconfigure failed", str(ctx.exception))


# ---------------------------------------------------------------------------
# undefine_vm
# ---------------------------------------------------------------------------
class TestUndefineVm(unittest.TestCase):
    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job")
    def test_completed(self, mock_start, mock_wait):
        mock_start.return_value = "j1"
        mock_wait.return_value = {"status": "completed", "result": {}}
        result = undefine_vm(FakeHost(), "test-domain")
        self.assertTrue(result)

    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job")
    def test_failed(self, mock_start, mock_wait):
        mock_start.return_value = "j1"
        mock_wait.return_value = {
            "status": "failed",
            "result": {"error": "domain not found"},
        }
        result = undefine_vm(FakeHost(), "ghost-domain")
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# _find_primary_partition
# ---------------------------------------------------------------------------
class TestFindPrimaryPartition(unittest.TestCase):
    def test_var_lib_troshka_preferred(self):
        partitions = [
            {"mount": "/", "used_pct": 20},
            {"mount": "/var/lib/troshka", "used_pct": 80},
            {"mount": "/boot", "used_pct": 10},
        ]
        result = _find_primary_partition(partitions)
        self.assertEqual(result["mount"], "/var/lib/troshka")

    def test_root_fallback(self):
        partitions = [
            {"mount": "/boot", "used_pct": 10},
            {"mount": "/", "used_pct": 30},
        ]
        result = _find_primary_partition(partitions)
        self.assertEqual(result["mount"], "/")

    def test_first_partition_fallback(self):
        partitions = [
            {"mount": "/data", "used_pct": 50},
            {"mount": "/opt", "used_pct": 60},
        ]
        result = _find_primary_partition(partitions)
        self.assertEqual(result["mount"], "/data")

    def test_empty_list(self):
        result = _find_primary_partition([])
        self.assertEqual(result, {})


# ---------------------------------------------------------------------------
# check_disk_usage with partitions key
# ---------------------------------------------------------------------------
class TestCheckDiskUsagePartitions(unittest.TestCase):
    @patch("app.services.troshkad_client._get_pool")
    def test_with_partitions(self, mock_get_pool):
        pool = MagicMock()
        partitions = [
            {"mount": "/", "free_bytes": 100, "total_bytes": 200, "used_pct": 50},
            {
                "mount": "/var/lib/troshka",
                "free_bytes": 300,
                "total_bytes": 500,
                "used_pct": 40,
            },
        ]
        pool.urlopen.return_value = _mock_response({"partitions": partitions})
        mock_get_pool.return_value = pool

        result = check_disk_usage(FakeHost())
        self.assertEqual(result["mount"], "/var/lib/troshka")
        self.assertEqual(result["used_pct"], 40)
        self.assertEqual(len(result["partitions"]), 2)

    @patch("app.services.troshkad_client.time.sleep")
    @patch("app.services.troshkad_client._get_pool")
    def test_retry_on_error(self, mock_get_pool, mock_sleep):
        pool = MagicMock()
        pool.urlopen.side_effect = [
            SSLError("transient"),
            _mock_response({"free_bytes": 100, "total_bytes": 200, "used_pct": 50}),
        ]
        mock_get_pool.return_value = pool

        result = check_disk_usage(FakeHost(), retries=2)
        self.assertEqual(result["used_pct"], 50)
        self.assertEqual(pool.urlopen.call_count, 2)

    @patch("app.services.troshkad_client.time.sleep")
    @patch("app.services.troshkad_client._get_pool")
    def test_all_retries_exhausted(self, mock_get_pool, mock_sleep):
        pool = MagicMock()
        pool.urlopen.side_effect = SSLError("persistent")
        mock_get_pool.return_value = pool

        with self.assertRaises(TroshkadError):
            check_disk_usage(FakeHost(), retries=3)
        self.assertEqual(pool.urlopen.call_count, 3)


# ---------------------------------------------------------------------------
# cancel_job
# ---------------------------------------------------------------------------
class TestCancelJob(unittest.TestCase):
    @patch("app.services.troshkad_client._get_pool")
    def test_cancel(self, mock_get_pool):
        pool = MagicMock()
        pool.urlopen.return_value = _mock_response(
            {"job_id": "j1", "status": "cancelled"}
        )
        mock_get_pool.return_value = pool

        result = cancel_job(FakeHost(), "j1")
        self.assertEqual(result["status"], "cancelled")
        call_args = pool.urlopen.call_args
        self.assertEqual(call_args[0][0], "DELETE")
        self.assertIn("/jobs/j1", call_args[0][1])


# ---------------------------------------------------------------------------
# troshkad_request with allow_disconnected
# ---------------------------------------------------------------------------
class TestAllowDisconnected(unittest.TestCase):
    def test_disconnected_host_raises_without_flag(self):
        with self.assertRaises(TroshkadError) as ctx:
            troshkad_request(DisconnectedHost(), "GET", "/health")
        self.assertIn("disconnected", str(ctx.exception))

    @patch("app.services.troshkad_client._get_pool")
    def test_disconnected_host_allowed_with_flag(self, mock_get_pool):
        pool = MagicMock()
        pool.urlopen.return_value = _mock_response({"status": "healthy"})
        mock_get_pool.return_value = pool

        result = troshkad_request(
            DisconnectedHost(), "GET", "/health", allow_disconnected=True
        )
        self.assertEqual(result["status"], "healthy")


# ---------------------------------------------------------------------------
# _get_pool caching
# ---------------------------------------------------------------------------
class TestGetPoolCaching(unittest.TestCase):
    def setUp(self):
        # Clear the pool cache before each test
        from app.services.troshkad_client import _pools

        self._original_pools = _pools.copy()
        _pools.clear()

    def tearDown(self):
        from app.services.troshkad_client import _pools

        _pools.clear()
        _pools.update(self._original_pools)

    @patch("app.services.troshkad_client.urllib3.HTTPSConnectionPool")
    def test_same_host_returns_cached_pool(self, mock_pool_cls):
        mock_pool_cls.return_value = MagicMock()
        host = FakeHost()

        pool1 = _get_pool(host)
        pool2 = _get_pool(host)
        self.assertIs(pool1, pool2)
        # Constructor called only once
        self.assertEqual(mock_pool_cls.call_count, 1)


# ---------------------------------------------------------------------------
# _execute_single_request — non-JSON error body
# ---------------------------------------------------------------------------
class TestExecuteSingleRequest(unittest.TestCase):
    def test_non_json_error_body(self):
        pool = MagicMock()
        resp = MagicMock()
        resp.status = 502
        resp.data = b"Bad Gateway"
        pool.urlopen.return_value = resp

        with self.assertRaises(TroshkadError) as ctx:
            _execute_single_request(pool, FakeHost(), "GET", "/path", None, 30)
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertEqual(ctx.exception.response["error"], "Bad Gateway")

    def test_json_error_body(self):
        pool = MagicMock()
        resp = MagicMock()
        resp.status = 422
        resp.data = json.dumps({"detail": "invalid params"}).encode()
        pool.urlopen.return_value = resp

        with self.assertRaises(TroshkadError) as ctx:
            _execute_single_request(
                pool, FakeHost(), "POST", "/path", {"key": "val"}, 30
            )
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(ctx.exception.response["detail"], "invalid params")

    def test_body_is_json_encoded(self):
        pool = MagicMock()
        pool.urlopen.return_value = _mock_response({"ok": True})
        _execute_single_request(
            pool, FakeHost(), "POST", "/path", {"param": "value"}, 30
        )
        call_kwargs = pool.urlopen.call_args
        body = call_kwargs[1].get("body") or call_kwargs.kwargs.get("body")
        self.assertEqual(json.loads(body), {"param": "value"})

    def test_no_body(self):
        pool = MagicMock()
        pool.urlopen.return_value = _mock_response({"ok": True})
        _execute_single_request(pool, FakeHost(), "GET", "/path", None, 30)
        call_kwargs = pool.urlopen.call_args
        body = call_kwargs[1].get("body") or call_kwargs.kwargs.get("body")
        self.assertIsNone(body)


if __name__ == "__main__":
    unittest.main()
