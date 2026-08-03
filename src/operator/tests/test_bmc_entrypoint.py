"""Tests for BMC entrypoint (Redfish emulator)."""

import base64
import json
import os
import sys
from io import BytesIO
from unittest.mock import MagicMock, patch, call

# Add entrypoint source to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "images", "bmc"))

# Must mock KubeVirtDriver before importing entrypoint
_mock_driver = MagicMock()
with patch.dict(
    sys.modules, {"kubevirt_driver": MagicMock(KubeVirtDriver=lambda: _mock_driver)}
):
    os.environ.setdefault("SUSHY_PASSWORD", "test-pass")
    import entrypoint

    entrypoint.driver = _mock_driver


def _make_handler(method, path, body=None, auth=True):
    """Create a mock HTTP handler for testing."""
    handler = MagicMock()
    handler.path = path
    handler.headers = {}
    if auth:
        cred = base64.b64encode(
            f"{entrypoint.USERNAME}:{entrypoint.PASSWORD}".encode()
        ).decode()
        handler.headers["Authorization"] = f"Basic {cred}"
    if body:
        raw = json.dumps(body).encode()
        handler.headers["Content-Length"] = str(len(raw))
        handler.rfile = BytesIO(raw)
    else:
        handler.headers["Content-Length"] = "0"
        handler.rfile = BytesIO(b"")
    return handler


# ── _check_auth tests ──


class TestCheckAuth:
    def test_valid_credentials(self):
        handler = _make_handler("GET", "/", auth=True)
        assert entrypoint._check_auth(handler) is True

    def test_missing_auth_header(self):
        handler = _make_handler("GET", "/", auth=False)
        handler.headers["Authorization"] = ""
        assert entrypoint._check_auth(handler) is False

    def test_wrong_credentials(self):
        handler = _make_handler("GET", "/", auth=False)
        cred = base64.b64encode(b"wrong:creds").decode()
        handler.headers["Authorization"] = f"Basic {cred}"
        assert entrypoint._check_auth(handler) is False

    def test_no_basic_prefix(self):
        handler = _make_handler("GET", "/", auth=False)
        handler.headers["Authorization"] = "Bearer token123"
        assert entrypoint._check_auth(handler) is False


# ── _send_json tests ──


class TestSendJson:
    def test_sends_200_json(self):
        handler = MagicMock()
        entrypoint._send_json(handler, {"ok": True})
        handler.send_response.assert_called_with(200)
        handler.send_header.assert_any_call("Content-Type", "application/json")

    def test_sends_custom_status(self):
        handler = MagicMock()
        entrypoint._send_json(handler, {"error": "nope"}, status=404)
        handler.send_response.assert_called_with(404)


# ── RedfishHandler.do_GET tests ──


class TestRedfishDoGet:
    def test_service_root(self):
        handler = _make_handler("GET", "/redfish/v1")
        entrypoint.RedfishHandler.do_GET(handler)
        written = handler.wfile.write.call_args[0][0]
        data = json.loads(written)
        assert data["Id"] == "RootService"

    def test_systems_list(self):
        _mock_driver.get_systems.return_value = ["vm-1", "vm-2"]
        handler = _make_handler("GET", "/redfish/v1/Systems")
        entrypoint.RedfishHandler.do_GET(handler)
        written = handler.wfile.write.call_args[0][0]
        data = json.loads(written)
        assert data["Members@odata.count"] == 2

    def test_system_detail(self):
        _mock_driver.get_power_state.return_value = "On"
        _mock_driver.get_boot_device.return_value = "Hdd"
        _mock_driver.get_boot_mode.return_value = "UEFI"
        _mock_driver.get_total_memory.return_value = 4096
        _mock_driver.get_total_cpus.return_value = 4
        _mock_driver.get_boot_override_enabled.return_value = "Continuous"
        _mock_driver.get_uuid.return_value = "550e8400-e29b-41d4-a716-446655440000"

        handler = _make_handler("GET", "/redfish/v1/Systems/vm-1")
        entrypoint.RedfishHandler.do_GET(handler)
        written = handler.wfile.write.call_args[0][0]
        data = json.loads(written)
        assert data["PowerState"] == "On"
        assert data["ProcessorSummary"]["Count"] == 4
        assert data["MemorySummary"]["TotalSystemMemoryGiB"] == 4.0

    def test_not_found(self):
        handler = _make_handler("GET", "/redfish/v1/Chassis")
        entrypoint.RedfishHandler.do_GET(handler)
        handler.send_response.assert_called_with(404)

    def test_service_root_no_auth_required(self):
        handler = _make_handler("GET", "/redfish/v1", auth=False)
        handler.headers["Authorization"] = ""
        entrypoint.RedfishHandler.do_GET(handler)
        written = handler.wfile.write.call_args[0][0]
        data = json.loads(written)
        assert data["Id"] == "RootService"

    def test_systems_list_no_auth_required(self):
        _mock_driver.get_systems.return_value = ["vm-1"]
        handler = _make_handler("GET", "/redfish/v1/Systems", auth=False)
        handler.headers["Authorization"] = ""
        entrypoint.RedfishHandler.do_GET(handler)
        written = handler.wfile.write.call_args[0][0]
        data = json.loads(written)
        assert data["Members@odata.count"] == 1

    def test_system_detail_unauthorized(self):
        handler = _make_handler("GET", "/redfish/v1/Systems/vm-1", auth=False)
        handler.headers["Authorization"] = ""
        entrypoint.RedfishHandler.do_GET(handler)
        handler.send_response.assert_called_with(401)
        written = handler.wfile.write.call_args[0][0]
        data = json.loads(written)
        assert "Authentication required" in data["error"]["message"]


# ── RedfishHandler.do_PATCH tests ──


class TestRedfishDoPatch:
    def test_set_boot_device(self):
        body = {
            "Boot": {
                "BootSourceOverrideTarget": "Pxe",
                "BootSourceOverrideEnabled": "Once",
            }
        }
        handler = _make_handler("PATCH", "/redfish/v1/Systems/vm-1", body)
        entrypoint.RedfishHandler.do_PATCH(handler)
        _mock_driver.set_boot_device.assert_called_with(
            "vm-1", "Pxe", boot_enabled="Once"
        )
        handler.send_response.assert_called_with(204)

    def test_patch_not_found(self):
        handler = _make_handler("PATCH", "/unknown/path")
        entrypoint.RedfishHandler.do_PATCH(handler)
        handler.send_response.assert_called_with(404)

    def test_patch_unauthorized(self):
        handler = _make_handler("PATCH", "/redfish/v1/Systems/vm-1", auth=False)
        handler.headers["Authorization"] = ""
        entrypoint.RedfishHandler.do_PATCH(handler)
        handler.send_response.assert_called_with(401)


# ── RedfishHandler.do_POST tests ──


class TestRedfishDoPost:
    def test_reset_action(self):
        body = {"ResetType": "ForceOff"}
        handler = _make_handler(
            "POST",
            "/redfish/v1/Systems/vm-1/Actions/ComputerSystem.Reset",
            body,
        )
        entrypoint.RedfishHandler.do_POST(handler)
        _mock_driver.set_power_state.assert_called_with("vm-1", "ForceOff")
        _mock_driver.revert_boot_once.assert_called_with("vm-1")
        handler.send_response.assert_called_with(204)

    def test_post_not_found(self):
        handler = _make_handler("POST", "/redfish/v1/Unknown")
        entrypoint.RedfishHandler.do_POST(handler)
        handler.send_response.assert_called_with(404)

    def test_post_unauthorized(self):
        handler = _make_handler(
            "POST",
            "/redfish/v1/Systems/vm-1/Actions/ComputerSystem.Reset",
            auth=False,
        )
        handler.headers["Authorization"] = ""
        entrypoint.RedfishHandler.do_POST(handler)
        handler.send_response.assert_called_with(401)


# ── _generate_self_signed_cert tests ──


class TestGenerateSelfSignedCert:
    @patch("subprocess.run")
    def test_calls_openssl(self, mock_run):
        entrypoint._generate_self_signed_cert("/tmp/cert.pem", "/tmp/key.pem")
        mock_run.assert_called_once()
        args = mock_run.call_args
        cmd = args[0][0]
        assert cmd[0] == "openssl"
        assert "/tmp/cert.pem" in cmd
        assert "/tmp/key.pem" in cmd


# ── _configure_network tests ──


class TestConfigureNetwork:
    @patch.dict(os.environ, {"SUSHY_BMC_IPS": ""})
    @patch("subprocess.run")
    def test_no_ips_noop(self, mock_run):
        entrypoint._configure_network()
        mock_run.assert_not_called()

    @patch.dict(os.environ, {"SUSHY_BMC_IPS": "10.0.0.5,10.0.0.6"})
    @patch("subprocess.run")
    def test_assigns_multiple_ips(self, mock_run):
        entrypoint._configure_network()
        assert mock_run.call_count == 2
        first_call = mock_run.call_args_list[0]
        assert "10.0.0.5/24" in first_call[0][0]

    @patch.dict(os.environ, {"SUSHY_BMC_IPS": "10.0.0.5/16"})
    @patch("subprocess.run")
    def test_preserves_cidr(self, mock_run):
        entrypoint._configure_network()
        first_call = mock_run.call_args_list[0]
        assert "10.0.0.5/16" in first_call[0][0]


# ── log_message suppression test ──


class TestLogSuppression:
    def test_log_message_suppressed(self):
        handler = MagicMock()
        entrypoint.RedfishHandler.log_message(handler, "test %s", "arg")
        # Should not raise; log_message is a no-op


# ── Additional coverage: _configure_network exception & skip paths ──


class TestConfigureNetworkEdgeCases:
    """Cover the except branch (line 216-217) and empty-IP skip (line 207)."""

    @patch.dict(os.environ, {"SUSHY_BMC_IPS": "10.0.0.5"})
    @patch("subprocess.run", side_effect=OSError("Permission denied"))
    @patch("builtins.print")
    def test_subprocess_exception_caught_and_logged(self, mock_print, mock_run):
        """Exception from subprocess.run is caught, not raised."""
        entrypoint._configure_network()
        mock_run.assert_called_once()
        msg = mock_print.call_args[0][0]
        assert "Failed to assign" in msg
        assert "10.0.0.5/24" in msg

    @patch.dict(os.environ, {"SUSHY_BMC_IPS": "10.0.0.5,,10.0.0.6"})
    @patch("subprocess.run")
    def test_empty_entries_between_commas_skipped(self, mock_run):
        """Empty strings between commas are skipped (continue branch)."""
        entrypoint._configure_network()
        assert mock_run.call_count == 2

    @patch.dict(os.environ, {"SUSHY_BMC_IPS": ","})
    @patch("subprocess.run")
    def test_only_commas_is_noop(self, mock_run):
        """A value of just commas produces no subprocess calls."""
        entrypoint._configure_network()
        mock_run.assert_not_called()

    @patch.dict(os.environ, {"SUSHY_BMC_IPS": "10.0.0.5,10.0.0.6"})
    @patch("subprocess.run", side_effect=[OSError("fail"), MagicMock()])
    @patch("builtins.print")
    def test_continues_processing_after_failure(self, mock_print, mock_run):
        """Second IP is still attempted after the first raises."""
        entrypoint._configure_network()
        assert mock_run.call_count == 2


# ── Additional coverage: do_PATCH edge cases ──


class TestRedfishDoPatchEdgeCases:
    """Cover boot-target-only and empty-body PATCH paths."""

    def test_target_without_enabled_defaults_to_continuous(self):
        """BootSourceOverrideTarget alone uses Continuous as default."""
        _mock_driver.reset_mock()
        body = {"Boot": {"BootSourceOverrideTarget": "Cd"}}
        handler = _make_handler("PATCH", "/redfish/v1/Systems/vm-1", body)
        entrypoint.RedfishHandler.do_PATCH(handler)
        _mock_driver.set_boot_device.assert_called_with(
            "vm-1", "Cd", boot_enabled="Continuous"
        )
        handler.send_response.assert_called_with(204)

    def test_boot_without_target_skips_driver_call(self):
        """Boot with BootSourceOverrideEnabled but no target => no driver call."""
        _mock_driver.reset_mock()
        body = {"Boot": {"BootSourceOverrideEnabled": "Once"}}
        handler = _make_handler("PATCH", "/redfish/v1/Systems/vm-1", body)
        entrypoint.RedfishHandler.do_PATCH(handler)
        _mock_driver.set_boot_device.assert_not_called()
        handler.send_response.assert_called_with(204)

    def test_empty_body_patch_accepted(self):
        """PATCH with no request body on system path returns 204."""
        _mock_driver.reset_mock()
        handler = _make_handler("PATCH", "/redfish/v1/Systems/vm-1")
        entrypoint.RedfishHandler.do_PATCH(handler)
        _mock_driver.set_boot_device.assert_not_called()
        handler.send_response.assert_called_with(204)


# ── Additional coverage: do_GET edge cases ──


class TestRedfishDoGetEdgeCases:
    """Cover driver errors and sub-resource paths in do_GET."""

    def test_system_subpath_returns_404(self):
        """Sub-resource paths like /Systems/vm-1/Bios return 404."""
        handler = _make_handler("GET", "/redfish/v1/Systems/vm-1/Bios")
        entrypoint.RedfishHandler.do_GET(handler)
        handler.send_response.assert_called_with(404)

    def test_trailing_slash_on_service_root(self):
        """Trailing slash on /redfish/v1/ is stripped and matched."""
        handler = _make_handler("GET", "/redfish/v1/")
        entrypoint.RedfishHandler.do_GET(handler)
        written = handler.wfile.write.call_args[0][0]
        data = json.loads(written)
        assert data["Id"] == "RootService"

    def test_driver_exception_on_get_systems(self):
        """Driver exception from get_systems propagates."""
        _mock_driver.get_systems.side_effect = RuntimeError("driver broke")
        handler = _make_handler("GET", "/redfish/v1/Systems")
        raised = False
        try:
            entrypoint.RedfishHandler.do_GET(handler)
        except RuntimeError as exc:
            raised = True
            assert "driver broke" in str(exc)
        finally:
            _mock_driver.get_systems.side_effect = None
        assert raised

    def test_driver_exception_on_system_detail(self):
        """Driver exception from get_power_state propagates."""
        _mock_driver.get_power_state.side_effect = RuntimeError("vm gone")
        handler = _make_handler("GET", "/redfish/v1/Systems/gone-vm")
        raised = False
        try:
            entrypoint.RedfishHandler.do_GET(handler)
        except RuntimeError as exc:
            raised = True
            assert "vm gone" in str(exc)
        finally:
            _mock_driver.get_power_state.side_effect = None
        assert raised


# ── Additional coverage: _generate_self_signed_cert error paths ──


class TestGenerateSelfSignedCertErrors:
    """Cover error paths in _generate_self_signed_cert."""

    @patch("subprocess.run", side_effect=FileNotFoundError("openssl not found"))
    def test_missing_openssl_raises(self, mock_run):
        """FileNotFoundError propagates when openssl is absent."""
        raised = False
        try:
            entrypoint._generate_self_signed_cert("/tmp/c.pem", "/tmp/k.pem")
        except FileNotFoundError:
            raised = True
        assert raised

    @patch("subprocess.run")
    def test_nonzero_exit_raises(self, mock_run):
        """CalledProcessError propagates (check=True in implementation)."""
        import subprocess as sp

        mock_run.side_effect = sp.CalledProcessError(1, "openssl")
        raised = False
        try:
            entrypoint._generate_self_signed_cert("/tmp/c.pem", "/tmp/k.pem")
        except sp.CalledProcessError:
            raised = True
        assert raised

    @patch("subprocess.run")
    def test_timeout_raises(self, mock_run):
        """TimeoutExpired propagates (timeout=10 in implementation)."""
        import subprocess as sp

        mock_run.side_effect = sp.TimeoutExpired("openssl", 10)
        raised = False
        try:
            entrypoint._generate_self_signed_cert("/tmp/c.pem", "/tmp/k.pem")
        except sp.TimeoutExpired:
            raised = True
        assert raised


# ── Coverage for __main__ block (lines 220-245) via runpy ──


class TestMainBlock:
    """Exercise the if __name__ == '__main__' block via runpy.run_path."""

    @patch.dict(os.environ, {"SUSHY_SSL_PORT": "9443", "SUSHY_BMC_IPS": ""})
    def test_main_starts_http_and_https_servers(self):
        """The __main__ block creates HTTP + HTTPS servers and starts SSL thread."""
        import runpy

        http_srv = MagicMock()
        https_srv = MagicMock()
        http_srv.serve_forever.side_effect = KeyboardInterrupt

        mock_kv = MagicMock(KubeVirtDriver=lambda: MagicMock())

        with patch.dict(sys.modules, {"kubevirt_driver": mock_kv}), patch(
            "http.server.HTTPServer", side_effect=[http_srv, https_srv]
        ) as mock_httpd, patch("ssl.SSLContext") as mock_ctx_cls, patch(
            "threading.Thread"
        ) as mock_thread_cls, patch(
            "subprocess.run"
        ), patch(
            "builtins.print"
        ):

            ctx_instance = MagicMock()
            mock_ctx_cls.return_value = ctx_instance
            thread_instance = MagicMock()
            mock_thread_cls.return_value = thread_instance

            entrypoint_path = os.path.join(
                os.path.dirname(__file__), "..", "images", "bmc", "entrypoint.py"
            )

            try:
                runpy.run_path(entrypoint_path, run_name="__main__")
            except KeyboardInterrupt:
                pass

            # Two HTTPServer instances: HTTP on default port, HTTPS on 9443
            assert mock_httpd.call_count == 2
            http_bind = mock_httpd.call_args_list[0][0][0]
            https_bind = mock_httpd.call_args_list[1][0][0]
            assert http_bind == ("0.0.0.0", 8000)
            assert https_bind == ("0.0.0.0", 9443)

            # SSL context wraps the HTTPS socket
            ctx_instance.load_cert_chain.assert_called_once_with(
                "/tmp/sushy.crt", "/tmp/sushy.key"
            )
            ctx_instance.wrap_socket.assert_called_once()

            # SSL thread created and started
            mock_thread_cls.assert_called_once()
            thread_instance.start.assert_called_once()

            # HTTP serve_forever called (interrupted by KeyboardInterrupt)
            http_srv.serve_forever.assert_called_once()
