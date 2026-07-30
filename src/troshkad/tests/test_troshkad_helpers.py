# src/troshkad/tests/test_troshkad_helpers.py
"""Unit tests for troshkad helper functions.

Tests pure helpers, validators, rate limiting, job tracking, route matching,
and BMC helpers — all mocked, no server or libvirt needed.
"""
import os
import signal
import subprocess
import time
import unittest
from io import StringIO
from unittest.mock import MagicMock, mock_open, patch, call

import troshkad


# ── Constants ──


class TestConstants(unittest.TestCase):
    """Verify key constants haven't drifted."""

    def test_vms_dir(self):
        assert troshkad._VMS_DIR == "/var/lib/troshka/vms"

    def test_inet_prefix(self):
        assert troshkad._INET_PREFIX == "inet "

    def test_ban_window(self):
        assert troshkad._BAN_WINDOW == 60

    def test_ban_threshold(self):
        assert troshkad._BAN_THRESHOLD == 10

    def test_ban_duration(self):
        assert troshkad._BAN_DURATION == 300

    def test_permaban_threshold(self):
        assert troshkad._PERMABAN_THRESHOLD == 3

    def test_permaban_window(self):
        assert troshkad._PERMABAN_WINDOW == 3600

    def test_bus_types(self):
        assert troshkad._BUS_TYPES == {"virtio", "scsi", "sata", "ide", "usb"}

    def test_net_models(self):
        assert troshkad._NET_MODELS == {"virtio", "e1000", "e1000e", "igb", "rtl8139"}

    def test_skip_drain_set(self):
        expected = {
            "vms/state",
            "vms/states",
            "host/disk-usage",
            "gc/discover",
            "vm/ssh-exec",
            "vm/guest-exec",
            "containers/states",
        }
        assert troshkad._SKIP_DRAIN == expected

    def test_domain_re_matches_valid(self):
        assert troshkad._DOMAIN_RE.match("troshka-abcdef01-12345678")

    def test_domain_re_rejects_invalid(self):
        assert not troshkad._DOMAIN_RE.match("not-a-domain")
        assert not troshkad._DOMAIN_RE.match("troshka-UPPER-12345678")

    def test_uuid_re_matches_valid(self):
        assert troshkad._UUID_RE.match("12345678-1234-1234-1234-123456789abc")

    def test_uuid_re_rejects_invalid(self):
        assert not troshkad._UUID_RE.match("not-a-uuid")
        assert not troshkad._UUID_RE.match("12345678-1234-1234-1234-12345678")


# ── Validators ──


class TestValidateDomainName(unittest.TestCase):
    def test_valid_name(self):
        result = troshkad._validate_domain_name("troshka-abcdef01-12345678")
        assert result == "troshka-abcdef01-12345678"

    def test_invalid_name_raises(self):
        with self.assertRaises(ValueError):
            troshkad._validate_domain_name("bad-name")

    def test_uppercase_hex_rejected(self):
        with self.assertRaises(ValueError):
            troshkad._validate_domain_name("troshka-ABCDEF01-12345678")

    def test_empty_string_rejected(self):
        with self.assertRaises(ValueError):
            troshkad._validate_domain_name("")

    def test_too_short_hex_rejected(self):
        with self.assertRaises(ValueError):
            troshkad._validate_domain_name("troshka-abcde-12345678")


class TestValidateProjectId(unittest.TestCase):
    def test_valid_uuid(self):
        uid = "12345678-abcd-ef01-2345-6789abcdef01"
        assert troshkad._validate_project_id(uid) == uid

    def test_invalid_uuid_raises(self):
        with self.assertRaises(ValueError):
            troshkad._validate_project_id("not-a-uuid")

    def test_uppercase_uuid_rejected(self):
        with self.assertRaises(ValueError):
            troshkad._validate_project_id("12345678-ABCD-EF01-2345-6789ABCDEF01")


class TestValidateIP(unittest.TestCase):
    def test_valid_ipv4(self):
        assert troshkad._validate_ip("192.168.1.1") == "192.168.1.1"

    def test_valid_ipv6(self):
        assert troshkad._validate_ip("::1") == "::1"

    def test_invalid_ip_raises(self):
        with self.assertRaises(ValueError):
            troshkad._validate_ip("999.999.999.999")

    def test_empty_ip_raises(self):
        with self.assertRaises(ValueError):
            troshkad._validate_ip("")

    def test_hostname_rejected(self):
        with self.assertRaises(ValueError):
            troshkad._validate_ip("example.com")


class TestValidateCIDR(unittest.TestCase):
    def test_valid_cidr(self):
        assert troshkad._validate_cidr("10.0.0.0/24") == "10.0.0.0/24"

    def test_non_strict_cidr(self):
        # non-strict allows host bits set
        assert troshkad._validate_cidr("10.0.0.5/24") == "10.0.0.5/24"

    def test_invalid_cidr_raises(self):
        with self.assertRaises(ValueError):
            troshkad._validate_cidr("not-a-cidr")

    def test_bare_ip_treated_as_host_network(self):
        # ipaddress treats bare IPs as /32 host networks, so this is valid
        assert troshkad._validate_cidr("10.0.0.0") == "10.0.0.0"


class TestValidateMAC(unittest.TestCase):
    def test_valid_mac_lowercase(self):
        assert troshkad._validate_mac("aa:bb:cc:dd:ee:ff") == "aa:bb:cc:dd:ee:ff"

    def test_valid_mac_uppercase(self):
        assert troshkad._validate_mac("AA:BB:CC:DD:EE:FF") == "AA:BB:CC:DD:EE:FF"

    def test_valid_mac_mixed_case(self):
        assert troshkad._validate_mac("aA:bB:cC:dD:eE:fF") == "aA:bB:cC:dD:eE:fF"

    def test_invalid_mac_raises(self):
        with self.assertRaises(ValueError):
            troshkad._validate_mac("not-a-mac")

    def test_too_short_mac_raises(self):
        with self.assertRaises(ValueError):
            troshkad._validate_mac("aa:bb:cc:dd:ee")


class TestValidateBus(unittest.TestCase):
    def test_all_valid_bus_types(self):
        for bus in ("virtio", "scsi", "sata", "ide", "usb"):
            assert troshkad._validate_bus(bus) == bus

    def test_invalid_bus_raises(self):
        with self.assertRaises(ValueError):
            troshkad._validate_bus("nvme")


class TestValidateNetModel(unittest.TestCase):
    def test_all_valid_models(self):
        for model in ("virtio", "e1000", "e1000e", "igb", "rtl8139"):
            assert troshkad._validate_net_model(model) == model

    def test_invalid_model_raises(self):
        with self.assertRaises(ValueError):
            troshkad._validate_net_model("vmxnet3")


class TestValidateNetworkName(unittest.TestCase):
    def test_valid_name(self):
        assert troshkad._validate_network_name("troshka-net-abcdef0123") == "troshka-net-abcdef0123"

    def test_invalid_name_raises(self):
        with self.assertRaises(ValueError):
            troshkad._validate_network_name("bad-net")


class TestValidateBridgeName(unittest.TestCase):
    def test_valid_troshka_bridge(self):
        assert troshkad._validate_bridge_name("br-troshka-abcdef01") == "br-troshka-abcdef01"

    def test_valid_bmc_bridge(self):
        assert troshkad._validate_bridge_name("br-bmc-abcdef01") == "br-bmc-abcdef01"

    def test_valid_plain_hex_bridge(self):
        assert troshkad._validate_bridge_name("br-abcdef01") == "br-abcdef01"

    def test_invalid_bridge_raises(self):
        with self.assertRaises(ValueError):
            troshkad._validate_bridge_name("eth0")


class TestValidateURL(unittest.TestCase):
    def test_valid_https_url(self):
        assert troshkad._validate_url("https://example.com/path") == "https://example.com/path"

    def test_valid_http_url(self):
        assert troshkad._validate_url("http://10.0.0.1:8080/api") == "http://10.0.0.1:8080/api"

    def test_invalid_url_raises(self):
        with self.assertRaises(ValueError):
            troshkad._validate_url("ftp://example.com")


class TestValidatePath(unittest.TestCase):
    @patch("os.path.exists", return_value=False)
    def test_allowed_troshka_path(self, _exists):
        result = troshkad._validate_path("/var/lib/troshka/vms/test")
        assert result == "/var/lib/troshka/vms/test"

    @patch("os.path.exists", return_value=False)
    def test_allowed_opt_path(self, _exists):
        result = troshkad._validate_path("/opt/troshka/something")
        assert result == "/opt/troshka/something"

    @patch("os.path.exists", return_value=False)
    def test_disallowed_path_raises(self, _exists):
        with self.assertRaises(ValueError):
            troshkad._validate_path("/etc/passwd")

    @patch("os.path.exists", return_value=False)
    def test_traversal_attack_rejected(self, _exists):
        with self.assertRaises(ValueError):
            troshkad._validate_path("/var/lib/troshka/../../etc/passwd")

    @patch("os.path.exists", return_value=True)
    @patch("os.path.realpath", return_value="/etc/shadow")
    def test_symlink_escape_rejected(self, _realpath, _exists):
        with self.assertRaises(ValueError):
            troshkad._validate_path("/var/lib/troshka/vms/evil-link")

    @patch("os.path.exists", return_value=True)
    @patch("os.path.realpath", return_value="/var/lib/troshka/vms/real-path")
    def test_symlink_inside_allowed(self, _realpath, _exists):
        result = troshkad._validate_path("/var/lib/troshka/vms/a-link")
        assert result == "/var/lib/troshka/vms/real-path"


# ── Storage path resolution ──


class TestStoragePath(unittest.TestCase):
    def setUp(self):
        self._orig_config = troshkad._config.copy()

    def tearDown(self):
        troshkad._config.clear()
        troshkad._config.update(self._orig_config)

    def test_local_mode_vms(self):
        troshkad._config["storage_mode"] = "local"
        assert troshkad._storage_path("vms") == "/var/lib/troshka/vms"

    def test_local_mode_images(self):
        troshkad._config["storage_mode"] = "local"
        assert troshkad._storage_path("images") == "/var/lib/troshka/images"

    def test_local_mode_seeds(self):
        troshkad._config["storage_mode"] = "local"
        assert troshkad._storage_path("seeds") == "/var/lib/troshka/vms"

    def test_shared_mode_vms(self):
        troshkad._config["storage_mode"] = "shared"
        troshkad._config["shared_mount"] = "/mnt/shared"
        troshkad._config["local_mount"] = "/mnt/local"
        assert troshkad._storage_path("vms") == "/mnt/shared/vms"

    def test_shared_mode_images(self):
        troshkad._config["storage_mode"] = "shared"
        troshkad._config["shared_mount"] = "/mnt/shared"
        troshkad._config["local_mount"] = "/mnt/local"
        assert troshkad._storage_path("images") == "/mnt/shared/images"

    def test_shared_mode_pxe_goes_local(self):
        troshkad._config["storage_mode"] = "shared"
        troshkad._config["shared_mount"] = "/mnt/shared"
        troshkad._config["local_mount"] = "/mnt/local"
        assert troshkad._storage_path("pxe") == "/mnt/local/pxe"

    def test_shared_mode_cache_patterns_goes_local(self):
        troshkad._config["storage_mode"] = "shared"
        troshkad._config["shared_mount"] = "/mnt/shared"
        troshkad._config["local_mount"] = "/mnt/local"
        assert troshkad._storage_path("cache/patterns") == "/mnt/local/cache/patterns"

    def test_shared_mode_cache_snapshots_goes_shared(self):
        troshkad._config["storage_mode"] = "shared"
        troshkad._config["shared_mount"] = "/mnt/shared"
        troshkad._config["local_mount"] = "/mnt/local"
        assert troshkad._storage_path("cache/snapshots") == "/mnt/shared/cache/snapshots"

    def test_shared_mode_seeds(self):
        troshkad._config["storage_mode"] = "shared"
        troshkad._config["shared_mount"] = "/mnt/shared"
        troshkad._config["local_mount"] = "/mnt/local"
        assert troshkad._storage_path("seeds") == "/var/lib/troshka/seeds"

    def test_shared_mode_unknown_category_goes_shared(self):
        troshkad._config["storage_mode"] = "shared"
        troshkad._config["shared_mount"] = "/mnt/shared"
        troshkad._config["local_mount"] = "/mnt/local"
        assert troshkad._storage_path("something-else") == "/mnt/shared/something-else"

    def test_default_mode_is_local(self):
        troshkad._config.pop("storage_mode", None)
        assert troshkad._storage_path("vms") == "/var/lib/troshka/vms"


# ── Rate limiting / auto-ban ──


class TestRateLimiting(unittest.TestCase):
    def setUp(self):
        # Reset all rate limit state
        troshkad._fail_tracker.clear()
        troshkad._banned_ips.clear()
        troshkad._permabanned_ips.clear()
        troshkad._ban_history.clear()

    def test_not_banned_initially(self):
        assert troshkad._is_banned("10.0.0.1") is False

    def test_single_failure_not_banned(self):
        troshkad._record_auth_failure("10.0.0.1")
        assert troshkad._is_banned("10.0.0.1") is False

    def test_threshold_failures_causes_ban(self):
        for _ in range(troshkad._BAN_THRESHOLD):
            troshkad._record_auth_failure("10.0.0.2")
        assert troshkad._is_banned("10.0.0.2") is True

    def test_ban_expires(self):
        for _ in range(troshkad._BAN_THRESHOLD):
            troshkad._record_auth_failure("10.0.0.3")
        assert troshkad._is_banned("10.0.0.3") is True
        # Manually expire the ban
        troshkad._banned_ips["10.0.0.3"] = time.monotonic() - 1
        assert troshkad._is_banned("10.0.0.3") is False

    def test_permaban_after_repeated_bans(self):
        ip = "10.0.0.4"
        for _ in range(troshkad._PERMABAN_THRESHOLD):
            for _ in range(troshkad._BAN_THRESHOLD):
                troshkad._record_auth_failure(ip)
            # Expire the temp ban to allow next round
            troshkad._banned_ips.pop(ip, None)
        assert ip in troshkad._permabanned_ips
        assert troshkad._is_banned(ip) is True

    def test_permaban_does_not_expire(self):
        ip = "10.0.0.5"
        troshkad._permabanned_ips.add(ip)
        assert troshkad._is_banned(ip) is True
        # Unlike temp bans, permabans never expire
        troshkad._banned_ips.pop(ip, None)
        assert troshkad._is_banned(ip) is True

    def test_cleanup_removes_expired_entries(self):
        troshkad._fail_tracker["10.0.0.6"] = [time.monotonic() - 120]
        troshkad._banned_ips["10.0.0.7"] = time.monotonic() - 1
        troshkad._ban_history["10.0.0.8"] = [time.monotonic() - 7200]
        troshkad._cleanup_rate_limit()
        assert "10.0.0.6" not in troshkad._fail_tracker
        assert "10.0.0.7" not in troshkad._banned_ips
        assert "10.0.0.8" not in troshkad._ban_history

    def test_cleanup_preserves_active_entries(self):
        now = time.monotonic()
        troshkad._fail_tracker["10.0.0.9"] = [now]
        troshkad._banned_ips["10.0.0.10"] = now + 300
        troshkad._ban_history["10.0.0.11"] = [now]
        troshkad._cleanup_rate_limit()
        assert "10.0.0.9" in troshkad._fail_tracker
        assert "10.0.0.10" in troshkad._banned_ips
        assert "10.0.0.11" in troshkad._ban_history


# ── _safe_kill ──


class TestSafeKill(unittest.TestCase):
    def test_refuses_zero_pid(self):
        assert troshkad._safe_kill(0, signal.SIGTERM) is False

    def test_refuses_negative_pid(self):
        assert troshkad._safe_kill(-1, signal.SIGTERM) is False

    def test_refuses_own_pid(self):
        assert troshkad._safe_kill(os.getpid(), signal.SIGTERM) is False

    @patch("os.kill")
    def test_kills_valid_pid(self, mock_kill):
        # Use a PID we know isn't ours
        pid = os.getpid() + 99999
        result = troshkad._safe_kill(pid, signal.SIGTERM)
        assert result is True
        mock_kill.assert_called_once_with(pid, signal.SIGTERM)

    @patch("os.kill")
    @patch("builtins.open", mock_open(read_data=b"python3 troshkad.py\x00"))
    @patch("os.path.exists", return_value=True)
    def test_kills_with_matching_cmdline(self, _exists, mock_kill):
        pid = os.getpid() + 99999
        result = troshkad._safe_kill(pid, signal.SIGTERM, expected_cmdline_substring="troshkad")
        assert result is True

    @patch("os.kill")
    @patch("builtins.open", mock_open(read_data=b"nginx\x00-g\x00daemon off;\x00"))
    def test_skips_kill_on_cmdline_mismatch(self, mock_kill):
        pid = os.getpid() + 99999
        result = troshkad._safe_kill(pid, signal.SIGTERM, expected_cmdline_substring="troshkad")
        assert result is False
        mock_kill.assert_not_called()


# ── _kill_pid_file ──


class TestKillPidFile(unittest.TestCase):
    @patch("os.path.exists", return_value=False)
    def test_missing_pid_file_is_noop(self, _exists):
        troshkad._kill_pid_file("/run/fake.pid")
        # No exception, no kill

    @patch("troshkad._safe_kill")
    @patch("builtins.open", mock_open(read_data="12345\n"))
    @patch("os.path.exists", return_value=True)
    def test_reads_pid_and_kills(self, _exists, mock_safe_kill):
        troshkad._kill_pid_file("/run/test.pid")
        mock_safe_kill.assert_called_once_with(12345, signal.SIGTERM)

    @patch("troshkad._safe_kill", side_effect=ProcessLookupError)
    @patch("builtins.open", mock_open(read_data="99999\n"))
    @patch("os.path.exists", return_value=True)
    def test_handles_dead_process(self, _exists, _mock_kill):
        # Should not raise
        troshkad._kill_pid_file("/run/dead.pid")

    @patch("builtins.open", mock_open(read_data="not-a-number\n"))
    @patch("os.path.exists", return_value=True)
    def test_handles_corrupt_pid_file(self, _exists):
        # ValueError from int() should be caught
        troshkad._kill_pid_file("/run/corrupt.pid")


# ── _detect_namespace_gateway_ip ──


class TestDetectNamespaceGatewayIP(unittest.TestCase):
    @patch("subprocess.run")
    def test_detects_global_ip(self, mock_run):
        # Real `ip -4 addr show` has inet lines on their own indented lines
        mock_run.return_value = MagicMock(
            stdout=(
                "1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536\n"
                "    inet 127.0.0.1/8 scope host lo\n"
                "2: veth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500\n"
                "    inet 10.0.0.1/24 brd 10.0.0.255 scope global veth0\n"
            )
        )
        result = troshkad._detect_namespace_gateway_ip("troshka-abcdef01")
        assert result == "10.0.0.1"
        mock_run.assert_called_once()
        args = mock_run.call_args
        assert args[0][0] == ["ip", "netns", "exec", "troshka-abcdef01", "ip", "-4", "addr", "show"]

    @patch("subprocess.run")
    def test_skips_loopback(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="    inet 127.0.0.1/8 scope global lo\n"
        )
        result = troshkad._detect_namespace_gateway_ip("troshka-abcdef01")
        assert result == ""

    @patch("subprocess.run")
    def test_skips_link_local(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="    inet 169.254.1.1/16 scope global eth0\n"
        )
        result = troshkad._detect_namespace_gateway_ip("troshka-abcdef01")
        assert result == ""

    @patch("subprocess.run")
    def test_skips_bmc_subnet(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="    inet 192.168.100.1/24 scope global br-bmc\n"
        )
        result = troshkad._detect_namespace_gateway_ip("troshka-abcdef01")
        assert result == ""

    @patch("subprocess.run")
    def test_skips_secondary_addresses(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="    inet 10.0.0.2/24 brd 10.0.0.255 scope global secondary veth0\n"
        )
        result = troshkad._detect_namespace_gateway_ip("troshka-abcdef01")
        assert result == ""

    @patch("subprocess.run")
    def test_returns_first_usable_ip(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout=(
                "    inet 127.0.0.1/8 scope global lo\n"
                "    inet 169.254.1.1/16 scope global eth0\n"
                "    inet 10.1.0.1/24 brd 10.1.0.255 scope global br0\n"
                "    inet 10.2.0.1/24 brd 10.2.0.255 scope global br1\n"
            )
        )
        result = troshkad._detect_namespace_gateway_ip("ns1")
        assert result == "10.1.0.1"

    @patch("subprocess.run", side_effect=Exception("command not found"))
    def test_returns_empty_on_error(self, _mock_run):
        result = troshkad._detect_namespace_gateway_ip("ns-fail")
        assert result == ""

    @patch("subprocess.run")
    def test_returns_empty_with_no_output(self, mock_run):
        mock_run.return_value = MagicMock(stdout="")
        result = troshkad._detect_namespace_gateway_ip("ns-empty")
        assert result == ""


# ── _bmc_write_sushy_conf ──


class TestBmcWriteSushyConf(unittest.TestCase):
    def test_basic_config(self):
        m = mock_open()
        with patch("builtins.open", m):
            troshkad._bmc_write_sushy_conf(
                conf_path="/tmp/sushy.conf",
                bmc_ip="10.0.0.50",
                port=8000,
                pool_name="default",
                htpasswd_path="/tmp/htpasswd",
                dom_uuid="test-uuid-1234",
            )
        handle = m()
        written = "".join(c.args[0] for c in handle.write.call_args_list)
        assert "SUSHY_EMULATOR_LISTEN_IP = '10.0.0.50'" in written
        assert "SUSHY_EMULATOR_LISTEN_PORT = 8000" in written
        assert "SUSHY_EMULATOR_LIBVIRT_URI = 'qemu:///system'" in written
        assert "SUSHY_EMULATOR_FEATURE_SET = 'vmedia'" in written
        assert "SUSHY_EMULATOR_IGNORE_BOOT_DEVICE = False" in written
        assert "SUSHY_EMULATOR_VMEDIA_VERIFY_SSL = False" in written
        assert "SUSHY_EMULATOR_STORAGE_POOL = 'default'" in written
        assert "SUSHY_EMULATOR_AUTH_FILE = '/tmp/htpasswd'" in written
        assert "SUSHY_EMULATOR_ALLOWED_INSTANCES = ['test-uuid-1234']" in written

    def test_ssl_config(self):
        m = mock_open()
        with patch("builtins.open", m):
            troshkad._bmc_write_sushy_conf(
                conf_path="/tmp/sushy-ssl.conf",
                bmc_ip="10.0.0.50",
                port=8443,
                pool_name="default",
                htpasswd_path="/tmp/htpasswd",
                dom_uuid="uuid-2",
                ssl_cert="/tmp/cert.pem",
                ssl_key="/tmp/key.pem",
            )
        handle = m()
        written = "".join(c.args[0] for c in handle.write.call_args_list)
        assert "SUSHY_EMULATOR_SSL_CERT = '/tmp/cert.pem'" in written
        assert "SUSHY_EMULATOR_SSL_KEY = '/tmp/key.pem'" in written
        assert "SUSHY_EMULATOR_LISTEN_PORT = 8443" in written

    def test_no_ssl_when_not_provided(self):
        m = mock_open()
        with patch("builtins.open", m):
            troshkad._bmc_write_sushy_conf(
                conf_path="/tmp/sushy.conf",
                bmc_ip="10.0.0.50",
                port=8000,
                pool_name="default",
                htpasswd_path="/tmp/htpasswd",
                dom_uuid="uuid-3",
            )
        handle = m()
        written = "".join(c.args[0] for c in handle.write.call_args_list)
        assert "SUSHY_EMULATOR_SSL_CERT" not in written
        assert "SUSHY_EMULATOR_SSL_KEY" not in written

    def test_empty_dom_uuid_omits_allowed_instances(self):
        m = mock_open()
        with patch("builtins.open", m):
            troshkad._bmc_write_sushy_conf(
                conf_path="/tmp/sushy.conf",
                bmc_ip="10.0.0.50",
                port=8000,
                pool_name="default",
                htpasswd_path="/tmp/htpasswd",
                dom_uuid="",
            )
        handle = m()
        written = "".join(c.args[0] for c in handle.write.call_args_list)
        assert "SUSHY_EMULATOR_ALLOWED_INSTANCES" not in written


# ── _bmc_start_sushy_instance ──


class TestBmcStartSushyInstance(unittest.TestCase):
    @patch("troshkad._job_log")
    @patch("builtins.open", mock_open())
    @patch("subprocess.Popen")
    @patch("troshkad._kill_pid_file")
    def test_starts_sushy_process(self, mock_kill_pid, mock_popen, _mock_log):
        mock_proc = MagicMock()
        mock_proc.pid = 42
        mock_popen.return_value = mock_proc

        job = {"job_id": "test-job", "output": []}
        troshkad._bmc_start_sushy_instance(
            job=job,
            ns="test-ns",
            venv_bin="/opt/troshka/venv/bin",
            conf_path="/tmp/sushy.conf",
            pid_path="/tmp/sushy.pid",
            bmc_ip="10.0.0.50",
            port=8000,
            domain_name="troshka-abcdef01-12345678",
        )

        mock_kill_pid.assert_called_once_with("/tmp/sushy.pid")
        mock_popen.assert_called_once()
        popen_args = mock_popen.call_args[0][0]
        assert popen_args[0] == "ip"
        assert popen_args[1] == "netns"
        assert popen_args[2] == "exec"
        assert popen_args[3] == "test-ns"
        assert "/opt/troshka/venv/bin/sushy-emulator" in popen_args[4]

    @patch("troshkad._job_log")
    @patch("builtins.open", mock_open())
    @patch("subprocess.Popen")
    @patch("troshkad._kill_pid_file")
    def test_writes_pid_file(self, _mock_kill, mock_popen, _mock_log):
        mock_proc = MagicMock()
        mock_proc.pid = 123
        mock_popen.return_value = mock_proc

        m = mock_open()
        with patch("builtins.open", m):
            troshkad._bmc_start_sushy_instance(
                job={"job_id": "test", "output": []},
                ns="ns1",
                venv_bin="/opt/troshka/venv/bin",
                conf_path="/tmp/conf",
                pid_path="/tmp/test.pid",
                bmc_ip="10.0.0.1",
                port=8000,
                domain_name="troshka-abcdef01-12345678",
            )
        # Check PID was written
        handle = m()
        handle.write.assert_called_with("123")


# ── _bmc_stop_vbmcd ──


class TestBmcStopVbmcd(unittest.TestCase):
    @patch("os.path.exists", return_value=False)
    def test_missing_pid_file_is_noop(self, _exists):
        troshkad._bmc_stop_vbmcd("/tmp/no-such-file.pid")

    @patch("time.sleep")
    @patch("os.kill")
    @patch("troshkad._safe_kill")
    @patch("builtins.open", mock_open(read_data="5678\n"))
    @patch("os.path.exists", return_value=True)
    def test_kills_and_waits(self, _exists, mock_safe_kill, mock_os_kill, _sleep):
        # Process dies on first kill check
        mock_os_kill.side_effect = ProcessLookupError
        troshkad._bmc_stop_vbmcd("/tmp/vbmcd.pid")
        mock_safe_kill.assert_called_once_with(5678, signal.SIGTERM)

    @patch("time.sleep")
    @patch("os.kill")
    @patch("troshkad._safe_kill")
    @patch("builtins.open", mock_open(read_data="bad\n"))
    @patch("os.path.exists", return_value=True)
    def test_handles_corrupt_pid(self, _exists, _mock_safe_kill, _mock_kill, _sleep):
        # ValueError from int() should be caught
        troshkad._bmc_stop_vbmcd("/tmp/bad.pid")


# ── Route matching ──


class TestMatchRoute(unittest.TestCase):
    def test_exact_match(self):
        handler, params = troshkad._match_route("GET", "/health")
        assert handler is not None
        assert params == {}

    def test_parameterized_match(self):
        handler, params = troshkad._match_route("GET", "/jobs/some-job-id")
        assert handler is not None
        assert params.get("job_id") == "some-job-id"

    def test_commands_prefix(self):
        handler, params = troshkad._match_route("POST", "/commands/vms/create")
        assert handler is not None
        assert params.get("command_path") == "vms/create"

    def test_no_match_returns_none(self):
        handler, params = troshkad._match_route("GET", "/nonexistent/path/here")
        assert handler is None

    def test_wrong_method(self):
        # /health is GET, try DELETE
        handler, params = troshkad._match_route("DELETE", "/health")
        assert handler is None


# ── Job tracking ──


class TestJobTracking(unittest.TestCase):
    def test_create_job(self):
        job = troshkad._create_job("test-cmd", {"key": "val"})
        assert job["command"] == "test-cmd"
        assert job["params"] == {"key": "val"}
        assert job["status"] == "running"
        assert job["result"] is None
        assert job["output"] == []
        assert job["_cancelled"] is False
        # Cleanup
        with troshkad._jobs_lock:
            troshkad._jobs.pop(job["job_id"], None)

    def test_complete_job(self):
        job = troshkad._create_job("test-complete", {})
        troshkad._complete_job(job, "completed", {"data": "ok"})
        assert job["status"] == "completed"
        assert job["result"] == {"data": "ok"}
        assert job["completed_at"] is not None
        with troshkad._jobs_lock:
            troshkad._jobs.pop(job["job_id"], None)

    def test_get_job(self):
        job = troshkad._create_job("test-get", {})
        fetched = troshkad._get_job(job["job_id"])
        assert fetched is job
        with troshkad._jobs_lock:
            troshkad._jobs.pop(job["job_id"], None)

    def test_get_missing_job(self):
        assert troshkad._get_job("nonexistent-id") is None

    def test_running_job_count(self):
        initial = troshkad._running_job_count()
        job1 = troshkad._create_job("count1", {})
        job2 = troshkad._create_job("count2", {})
        assert troshkad._running_job_count() == initial + 2
        troshkad._complete_job(job1, "completed")
        assert troshkad._running_job_count() == initial + 1
        troshkad._complete_job(job2, "failed")
        assert troshkad._running_job_count() == initial
        with troshkad._jobs_lock:
            troshkad._jobs.pop(job1["job_id"], None)
            troshkad._jobs.pop(job2["job_id"], None)

    def test_cancel_running_job(self):
        job = troshkad._create_job("test-cancel", {})
        result = troshkad._cancel_job(job["job_id"])
        assert result["status"] == "cancelled"
        assert result["_cancelled"] is True
        with troshkad._jobs_lock:
            troshkad._jobs.pop(job["job_id"], None)

    def test_cancel_nonexistent_job(self):
        assert troshkad._cancel_job("no-such-id") is None

    def test_cancel_already_completed_job(self):
        job = troshkad._create_job("test-cancel-done", {})
        troshkad._complete_job(job, "completed")
        result = troshkad._cancel_job(job["job_id"])
        assert result["status"] == "completed"  # unchanged
        with troshkad._jobs_lock:
            troshkad._jobs.pop(job["job_id"], None)

    def test_cancel_kills_subprocess(self):
        job = troshkad._create_job("test-cancel-proc", {})
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        job["_process"] = mock_proc
        troshkad._cancel_job(job["job_id"])
        mock_proc.kill.assert_called_once()
        with troshkad._jobs_lock:
            troshkad._jobs.pop(job["job_id"], None)


# ── Job cleanup ──


class TestJobCleanup(unittest.TestCase):
    def test_removes_old_completed_jobs(self):
        job = troshkad._create_job("test-old", {})
        troshkad._complete_job(job, "completed")
        # _cleanup_old_jobs uses time.mktime(time.strptime(...)) which treats
        # the UTC timestamp as local time.  Use a date far enough in the past
        # (48 hours) that it's outside the 1-hour window regardless of TZ.
        old_time = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(time.time() - 172800),
        )
        job["completed_at"] = old_time
        troshkad._cleanup_old_jobs()
        assert troshkad._get_job(job["job_id"]) is None

    def test_preserves_recent_completed_jobs(self):
        job = troshkad._create_job("test-recent", {})
        troshkad._complete_job(job, "completed")
        troshkad._cleanup_old_jobs()
        assert troshkad._get_job(job["job_id"]) is not None
        with troshkad._jobs_lock:
            troshkad._jobs.pop(job["job_id"], None)

    def test_preserves_running_jobs(self):
        job = troshkad._create_job("test-running", {})
        troshkad._cleanup_old_jobs()
        assert troshkad._get_job(job["job_id"]) is not None
        with troshkad._jobs_lock:
            troshkad._jobs.pop(job["job_id"], None)


# ── _job_log ──


class TestJobLog(unittest.TestCase):
    def test_appends_message(self):
        job = {"job_id": "aaaa1111-0000-0000-0000-000000000000", "output": []}
        troshkad._job_log(job, "test message")
        assert job["output"] == ["test message"]

    def test_appends_multiple_messages(self):
        job = {"job_id": "aaaa2222-0000-0000-0000-000000000000", "output": []}
        troshkad._job_log(job, "line 1")
        troshkad._job_log(job, "line 2")
        assert job["output"] == ["line 1", "line 2"]


# ── _compute_version ──


class TestComputeVersion(unittest.TestCase):
    def test_returns_string(self):
        result = troshkad._compute_version()
        assert isinstance(result, str)

    def test_returns_12_char_hex(self):
        result = troshkad._compute_version()
        # Should be a 12-char hex substring of SHA-256
        assert len(result) == 12
        int(result, 16)  # Should not raise


# ── load_config ──


class TestLoadConfig(unittest.TestCase):
    def test_loads_json_config(self):
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write('{"port": 31337, "token": "abc"}')
            f.flush()
            config = troshkad.load_config(f.name)
        assert config == {"port": 31337, "token": "abc"}
        os.unlink(f.name)

    def test_raises_on_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            troshkad.load_config("/nonexistent/config.json")

    def test_raises_on_invalid_json(self):
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("not json")
            f.flush()
        with self.assertRaises(Exception):
            troshkad.load_config(f.name)
        os.unlink(f.name)


# ── route decorator ──


class TestRouteDecorator(unittest.TestCase):
    def test_registers_route(self):
        @troshkad.route("GET", "/test-route-decorator")
        def _handler(handler, params):
            pass

        assert ("GET", "/test-route-decorator") in troshkad.ROUTES
        assert troshkad.ROUTES[("GET", "/test-route-decorator")] is _handler
        # Cleanup
        del troshkad.ROUTES[("GET", "/test-route-decorator")]


# ── _handle_oc_exec ──


class TestHandleOcExec(unittest.TestCase):
    def _make_job(self):
        return {"id": "test-job", "output": [], "status": "running"}

    @patch("subprocess.run")
    @patch("os.path.isfile", return_value=True)
    @patch("troshkad._detect_namespace_gateway_ip", return_value="10.0.0.1")
    @patch("builtins.open", mock_open())
    def test_with_gateway_ip_uses_unshare(self, _mock_gw, _mock_isfile, mock_run):
        mock_run.return_value = MagicMock(stdout="output", stderr="", returncode=0)
        job = self._make_job()
        result = troshkad._handle_oc_exec(job, {
            "project_id": "12345678-abcd-ef01-2345-6789abcdef01",
            "command": "get nodes",
            "timeout": "30",
            "gateway_ip": "10.0.0.1",
        })
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["output"], "output")
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[0], "ip")
        self.assertEqual(cmd[1], "netns")
        self.assertEqual(cmd[2], "exec")
        self.assertIn("unshare", cmd)
        self.assertIn("--mount", cmd)

    @patch("subprocess.run")
    @patch("os.path.isfile", return_value=True)
    def test_without_gateway_ip_no_unshare(self, _mock_isfile, mock_run):
        mock_run.return_value = MagicMock(stdout="nodes", stderr="", returncode=0)
        job = self._make_job()
        result = troshkad._handle_oc_exec(job, {
            "project_id": "12345678-abcd-ef01-2345-6789abcdef01",
            "command": "get nodes",
            "gateway_ip": "",
        })
        self.assertEqual(result["exit_code"], 0)
        cmd = mock_run.call_args[0][0]
        self.assertIn("/usr/local/bin/oc", cmd)
        self.assertNotIn("unshare", cmd)

    @patch("os.path.isfile", return_value=False)
    def test_missing_kubeconfig_raises(self, _mock_isfile):
        job = self._make_job()
        with self.assertRaises(RuntimeError):
            troshkad._handle_oc_exec(job, {
                "project_id": "12345678-abcd-ef01-2345-6789abcdef01",
                "command": "get nodes",
            })

    def test_empty_command_raises(self):
        job = self._make_job()
        with self.assertRaises(RuntimeError):
            troshkad._handle_oc_exec(job, {
                "project_id": "12345678-abcd-ef01-2345-6789abcdef01",
                "command": "",
            })

    @patch("subprocess.run")
    @patch("os.path.isfile", return_value=True)
    def test_timeout_capped_at_300(self, _mock_isfile, mock_run):
        mock_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
        job = self._make_job()
        troshkad._handle_oc_exec(job, {
            "project_id": "12345678-abcd-ef01-2345-6789abcdef01",
            "command": "get pods",
            "timeout": "9999",
            "gateway_ip": "",
        })
        _, kwargs = mock_run.call_args
        self.assertEqual(kwargs["timeout"], 300)


# ── _bmc_start_dnsmasq ──


class TestBmcStartDnsmasq(unittest.TestCase):
    def _make_job(self):
        return {"id": "test-job", "output": [], "status": "running"}

    @patch("troshkad._job_log")
    @patch("subprocess.Popen")
    @patch("troshkad._kill_pid_file")
    @patch("builtins.open", mock_open())
    def test_starts_dnsmasq_process(self, _mock_kill, mock_popen, _mock_log):
        job = self._make_job()
        troshkad._bmc_start_dnsmasq(
            job=job,
            ns="troshka-abcdef01",
            pid="abcdef01",
            bridge="br-bmc-abcdef01",
            bmc_cidr="192.168.100.0/24",
            dhcp_hosts=[{"mac": "aa:bb:cc:dd:ee:ff", "ip": "192.168.100.10", "name": "vm1"}],
        )
        mock_popen.assert_called_once()
        cmd = mock_popen.call_args[0][0]
        self.assertEqual(cmd[:4], ["ip", "netns", "exec", "troshka-abcdef01"])
        self.assertEqual(cmd[4], "dnsmasq")

    @patch("troshkad._job_log")
    @patch("subprocess.Popen")
    @patch("troshkad._kill_pid_file")
    def test_writes_config_with_dhcp_hosts(self, _mock_kill, _mock_popen, _mock_log):
        m = mock_open()
        with patch("builtins.open", m):
            troshkad._bmc_start_dnsmasq(
                job=self._make_job(),
                ns="troshka-abcdef01",
                pid="abcdef01",
                bridge="br-bmc-abcdef01",
                bmc_cidr="192.168.100.0/24",
                dhcp_hosts=[
                    {"mac": "aa:bb:cc:dd:ee:01", "ip": "192.168.100.10", "name": "node-1"},
                    {"mac": "aa:bb:cc:dd:ee:02", "ip": "192.168.100.11", "name": "node-2"},
                ],
            )
        handle = m()
        written = "".join(c.args[0] for c in handle.write.call_args_list)
        self.assertIn("dhcp-host=aa:bb:cc:dd:ee:01,192.168.100.10,node-1", written)
        self.assertIn("dhcp-host=aa:bb:cc:dd:ee:02,192.168.100.11,node-2", written)
        self.assertIn("interface=br-bmc-abcdef01", written)

    @patch("troshkad._job_log")
    @patch("subprocess.Popen")
    @patch("troshkad._kill_pid_file")
    def test_kills_old_pid_before_start(self, mock_kill, _mock_popen, _mock_log):
        m = mock_open()
        with patch("builtins.open", m):
            troshkad._bmc_start_dnsmasq(
                job=self._make_job(),
                ns="troshka-abcdef01",
                pid="abcdef01",
                bridge="br-bmc-abcdef01",
                bmc_cidr="192.168.100.0/24",
                dhcp_hosts=[],
            )
        mock_kill.assert_called_once_with("/run/troshka-dnsmasq-bmc-abcdef01.pid")

    @patch("troshkad._job_log")
    @patch("subprocess.Popen")
    @patch("troshkad._kill_pid_file")
    def test_dhcp_host_without_name(self, _mock_kill, _mock_popen, _mock_log):
        m = mock_open()
        with patch("builtins.open", m):
            troshkad._bmc_start_dnsmasq(
                job=self._make_job(),
                ns="troshka-abcdef01",
                pid="abcdef01",
                bridge="br-bmc-abcdef01",
                bmc_cidr="192.168.100.0/24",
                dhcp_hosts=[{"mac": "aa:bb:cc:dd:ee:ff", "ip": "192.168.100.10"}],
            )
        handle = m()
        written = "".join(c.args[0] for c in handle.write.call_args_list)
        # No trailing hostname
        self.assertIn("dhcp-host=aa:bb:cc:dd:ee:ff,192.168.100.10\n", written)

    @patch("troshkad._job_log")
    @patch("subprocess.Popen")
    @patch("troshkad._kill_pid_file")
    def test_dhcp_range_uses_cidr_base(self, _mock_kill, _mock_popen, _mock_log):
        m = mock_open()
        with patch("builtins.open", m):
            troshkad._bmc_start_dnsmasq(
                job=self._make_job(),
                ns="ns1",
                pid="aabbccdd",
                bridge="br-bmc-aabbccdd",
                bmc_cidr="10.20.30.0/24",
                dhcp_hosts=[],
            )
        handle = m()
        written = "".join(c.args[0] for c in handle.write.call_args_list)
        self.assertIn("dhcp-range=10.20.30.100,10.20.30.199,24h", written)


# ── _bmc_start_vbmcd ──


class TestBmcStartVbmcd(unittest.TestCase):
    def _make_job(self):
        return {"id": "test-job", "output": [], "status": "running"}

    @patch("troshkad._job_log")
    @patch("troshkad._run_cmd")
    @patch("time.sleep")
    @patch("os.path.exists", return_value=True)
    @patch("subprocess.Popen")
    @patch("troshkad._bmc_stop_vbmcd")
    @patch("os.makedirs")
    @patch("os.path.isdir", return_value=False)
    @patch("builtins.open", mock_open())
    def test_starts_vbmcd_and_registers_vms(
        self, _mock_isdir, _mock_makedirs, _mock_stop, mock_popen,
        _mock_exists, _mock_sleep, mock_run_cmd, _mock_log,
    ):
        mock_proc = MagicMock()
        mock_proc.pid = 999
        mock_popen.return_value = mock_proc
        job = self._make_job()
        troshkad._bmc_start_vbmcd(
            job=job,
            ns="troshka-abcdef01",
            bmc_dir="/var/lib/troshka/bmc/proj-id",
            venv_bin="/opt/troshka/venv/bin",
            vms=[{"domain_name": "troshka-abcdef01-12345678", "bmc_ip": "192.168.100.10"}],
            bmc_username="admin",
            bmc_password="password", # pragma: allowlist secret
        )
        mock_popen.assert_called_once()
        # vbmc add + vbmc start = 2 calls per VM
        self.assertEqual(mock_run_cmd.call_count, 2)

    @patch("troshkad._job_log")
    @patch("troshkad._run_cmd")
    @patch("time.sleep")
    @patch("os.path.exists", return_value=True)
    @patch("subprocess.Popen")
    @patch("troshkad._bmc_stop_vbmcd")
    @patch("os.makedirs")
    @patch("os.path.isdir", return_value=True)
    @patch("shutil.rmtree")
    @patch("builtins.open", mock_open())
    def test_cleans_old_conf_dir(
        self, mock_rmtree, _mock_isdir, _mock_makedirs, _mock_stop,
        mock_popen, _mock_exists, _mock_sleep, _mock_run_cmd, _mock_log,
    ):
        mock_popen.return_value = MagicMock(pid=100)
        troshkad._bmc_start_vbmcd(
            job=self._make_job(),
            ns="troshka-abcdef01",
            bmc_dir="/var/lib/troshka/bmc/proj-id",
            venv_bin="/opt/troshka/venv/bin",
            vms=[],
            bmc_username="admin",
            bmc_password="pass", # pragma: allowlist secret
        )
        mock_rmtree.assert_called_once_with("/var/lib/troshka/bmc/proj-id/vbmcd")

    @patch("troshkad._job_log")
    @patch("troshkad._run_cmd")
    @patch("time.sleep")
    @patch("os.path.exists", return_value=True)
    @patch("subprocess.Popen")
    @patch("troshkad._bmc_stop_vbmcd")
    @patch("os.makedirs")
    @patch("os.path.isdir", return_value=False)
    def test_writes_vbmcd_config(
        self, _mock_isdir, _mock_makedirs, _mock_stop, mock_popen,
        _mock_exists, _mock_sleep, _mock_run_cmd, _mock_log,
    ):
        mock_popen.return_value = MagicMock(pid=100)
        m = mock_open()
        with patch("builtins.open", m):
            troshkad._bmc_start_vbmcd(
                job=self._make_job(),
                ns="troshka-abcdef01",
                bmc_dir="/var/lib/troshka/bmc/proj-id",
                venv_bin="/opt/troshka/venv/bin",
                vms=[],
                bmc_username="admin",
                bmc_password="pass", # pragma: allowlist secret
            )
        handle = m()
        written = "".join(c.args[0] for c in handle.write.call_args_list)
        self.assertIn("[default]", written)
        self.assertIn("config_dir = /var/lib/troshka/bmc/proj-id/vbmcd", written)
        self.assertIn("pid_file = /var/lib/troshka/bmc/proj-id/vbmcd.pid", written)

    @patch("troshkad._job_log")
    @patch("troshkad._run_cmd")
    @patch("time.sleep")
    @patch("os.path.exists", return_value=True)
    @patch("subprocess.Popen")
    @patch("troshkad._bmc_stop_vbmcd")
    @patch("os.makedirs")
    @patch("os.path.isdir", return_value=False)
    @patch("builtins.open", mock_open())
    def test_stops_old_vbmcd_before_start(
        self, _mock_isdir, _mock_makedirs, mock_stop, mock_popen,
        _mock_exists, _mock_sleep, _mock_run_cmd, _mock_log,
    ):
        mock_popen.return_value = MagicMock(pid=100)
        troshkad._bmc_start_vbmcd(
            job=self._make_job(),
            ns="troshka-abcdef01",
            bmc_dir="/var/lib/troshka/bmc/proj-id",
            venv_bin="/opt/troshka/venv/bin",
            vms=[],
            bmc_username="admin",
            bmc_password="pass", # pragma: allowlist secret
        )
        mock_stop.assert_called_once_with("/var/lib/troshka/bmc/proj-id/vbmcd.pid")


# ── _bmc_stop_vbmcd (extended) ──


class TestBmcStopVbmcdExtended(unittest.TestCase):
    @patch("os.remove")
    @patch("os.kill", side_effect=ProcessLookupError)
    @patch("time.sleep")
    @patch("troshkad._safe_kill")
    @patch("builtins.open", mock_open(read_data="9876\n"))
    @patch("os.path.exists", return_value=True)
    def test_removes_pid_file_after_process_dies(
        self, _mock_exists, _mock_safe_kill, _mock_sleep, _mock_os_kill, mock_remove,
    ):
        troshkad._bmc_stop_vbmcd("/tmp/vbmcd.pid")
        mock_remove.assert_called_once_with("/tmp/vbmcd.pid")

    @patch("os.remove")
    @patch("os.kill")
    @patch("time.sleep")
    @patch("troshkad._safe_kill")
    @patch("builtins.open", mock_open(read_data="1111\n"))
    @patch("os.path.exists", return_value=True)
    def test_does_not_remove_pid_if_process_alive(
        self, _mock_exists, _mock_safe_kill, _mock_sleep, mock_os_kill, mock_remove,
    ):
        # os.kill(pid, 0) succeeds means process is alive
        mock_os_kill.return_value = None
        troshkad._bmc_stop_vbmcd("/tmp/vbmcd.pid")
        mock_remove.assert_not_called()

    @patch("os.path.exists", return_value=True)
    @patch("builtins.open", side_effect=PermissionError("denied"))
    def test_handles_permission_error(self, _mock_open, _mock_exists):
        # Should not raise
        troshkad._bmc_stop_vbmcd("/tmp/noperm.pid")


# ── _bmc_write_sushy_conf (extended) ──


class TestBmcWriteSushyConfExtended(unittest.TestCase):
    def test_ssl_cert_only_written_when_both_provided(self):
        m = mock_open()
        with patch("builtins.open", m):
            troshkad._bmc_write_sushy_conf(
                conf_path="/tmp/sushy-ssl.conf",
                bmc_ip="10.0.0.50",
                port=8443,
                pool_name="default",
                htpasswd_path="/tmp/htpasswd",
                dom_uuid="uuid-ssl",
                ssl_cert="/certs/cert.pem",
                ssl_key="/certs/key.pem",
            )
        handle = m()
        written = "".join(c.args[0] for c in handle.write.call_args_list)
        self.assertIn("SUSHY_EMULATOR_SSL_CERT = '/certs/cert.pem'", written)
        self.assertIn("SUSHY_EMULATOR_SSL_KEY = '/certs/key.pem'", written)
        self.assertIn("SUSHY_EMULATOR_ALLOWED_INSTANCES = ['uuid-ssl']", written)

    def test_none_ssl_cert_omits_ssl(self):
        m = mock_open()
        with patch("builtins.open", m):
            troshkad._bmc_write_sushy_conf(
                conf_path="/tmp/sushy.conf",
                bmc_ip="10.0.0.50",
                port=8000,
                pool_name="default",
                htpasswd_path="/tmp/htpasswd",
                dom_uuid="uuid-nossl",
                ssl_cert=None,
                ssl_key=None,
            )
        handle = m()
        written = "".join(c.args[0] for c in handle.write.call_args_list)
        self.assertNotIn("SSL_CERT", written)
        self.assertNotIn("SSL_KEY", written)

    def test_empty_dom_uuid_omits_allowed(self):
        m = mock_open()
        with patch("builtins.open", m):
            troshkad._bmc_write_sushy_conf(
                conf_path="/tmp/sushy.conf",
                bmc_ip="10.0.0.50",
                port=8000,
                pool_name="default",
                htpasswd_path="/tmp/htpasswd",
                dom_uuid="",
            )
        handle = m()
        written = "".join(c.args[0] for c in handle.write.call_args_list)
        self.assertNotIn("ALLOWED_INSTANCES", written)

    def test_none_dom_uuid_omits_allowed(self):
        """Falsy dom_uuid (None cast) should also omit ALLOWED_INSTANCES."""
        m = mock_open()
        with patch("builtins.open", m):
            troshkad._bmc_write_sushy_conf(
                conf_path="/tmp/sushy.conf",
                bmc_ip="10.0.0.50",
                port=8000,
                pool_name="pool1",
                htpasswd_path="/tmp/htpasswd",
                dom_uuid="",
            )
        handle = m()
        written = "".join(c.args[0] for c in handle.write.call_args_list)
        self.assertNotIn("ALLOWED_INSTANCES", written)


# ── _cleanup_stale_recert ──


class TestCleanupStaleRecert(unittest.TestCase):
    def setUp(self):
        self._orig_config = troshkad._config.copy()

    def tearDown(self):
        troshkad._config.clear()
        troshkad._config.update(self._orig_config)

    @patch("subprocess.run")
    @patch("os.rmdir")
    @patch("os.path.ismount", return_value=False)
    @patch("glob.glob", return_value=[])
    @patch("os.path.exists", return_value=False)
    def test_no_artifacts_is_noop(self, _mock_exists, _mock_glob, _mock_ismount, mock_rmdir, mock_run):
        mock_run.return_value = MagicMock(stdout="", returncode=0)
        troshkad._cleanup_stale_recert()
        # No dirs to remove, no nbd disconnects, no container removals
        mock_rmdir.assert_not_called()
        # Only the podman ps call should happen, no umount or qemu-nbd calls
        for c in mock_run.call_args_list:
            cmd = c[0][0] if c[0] else c[1].get("args", [])
            self.assertNotIn("umount", cmd)
            self.assertNotIn("qemu-nbd", cmd)

    @patch("subprocess.run")
    @patch("os.rmdir")
    @patch("os.path.ismount", return_value=True)
    def test_unmounts_stale_mount(self, _mock_ismount, mock_rmdir, mock_run):
        mock_run.return_value = MagicMock(stdout="", returncode=0)
        with patch("glob.glob", return_value=["/var/lib/troshka/local/tmp/recert-abc"]):
            with patch("os.path.exists", return_value=False):
                troshkad._cleanup_stale_recert()
        # First call should be umount
        umount_calls = [
            c for c in mock_run.call_args_list
            if "umount" in str(c)
        ]
        self.assertEqual(len(umount_calls), 1)

    @patch("subprocess.run")
    @patch("os.path.exists")
    @patch("os.rmdir")
    @patch("os.path.ismount", return_value=False)
    @patch("glob.glob", return_value=[])
    def test_disconnects_nbd_devices(self, _mock_glob, _mock_ismount, _mock_rmdir, mock_exists, mock_run):
        # nbd0p1 exists, others don't
        def exists_side_effect(path):
            return path == "/dev/nbd0p1"
        mock_exists.side_effect = exists_side_effect
        mock_run.return_value = MagicMock(stdout="", returncode=0)
        troshkad._cleanup_stale_recert()
        disconnect_calls = [
            c for c in mock_run.call_args_list
            if "qemu-nbd" in str(c)
        ]
        self.assertEqual(len(disconnect_calls), 1)

    @patch("subprocess.run")
    @patch("os.path.exists", return_value=False)
    @patch("os.rmdir")
    @patch("os.path.ismount", return_value=False)
    @patch("glob.glob", return_value=[])
    def test_removes_stale_podman_containers(self, _mock_glob, _mock_ismount, _mock_rmdir, _mock_exists, mock_run):
        # First subprocess.run is podman ps, return a stale container name
        def run_side_effect(cmd, **kwargs):
            if "podman" in cmd and "ps" in cmd:
                return MagicMock(stdout="recert-etcd-abc123\n", returncode=0)
            return MagicMock(stdout="", returncode=0)
        mock_run.side_effect = run_side_effect
        troshkad._cleanup_stale_recert()
        rm_calls = [
            c for c in mock_run.call_args_list
            if "rm" in str(c) and "recert-etcd-abc123" in str(c)
        ]
        self.assertEqual(len(rm_calls), 1)

    @patch("subprocess.run")
    @patch("os.path.exists", return_value=False)
    @patch("os.rmdir")
    @patch("os.path.ismount", return_value=False)
    @patch("glob.glob", return_value=["/var/lib/troshka/local/tmp/recert-xyz"])
    def test_rmdir_after_unmount(self, _mock_glob, _mock_ismount, mock_rmdir, _mock_exists, mock_run):
        mock_run.return_value = MagicMock(stdout="", returncode=0)
        troshkad._cleanup_stale_recert()
        mock_rmdir.assert_called_once_with("/var/lib/troshka/local/tmp/recert-xyz")


class TestCheckNfsHealth(unittest.TestCase):
    def setUp(self):
        self._orig_config = troshkad._config.copy()
        self._orig_healthy = troshkad._nfs_healthy
        self._orig_stale = troshkad._nfs_stale_since

    def tearDown(self):
        troshkad._config.update(self._orig_config)
        troshkad._nfs_healthy = self._orig_healthy
        troshkad._nfs_stale_since = self._orig_stale

    def test_returns_true_for_local_mode(self):
        troshkad._config["storage_mode"] = "local"
        result = troshkad._check_nfs_health()
        self.assertTrue(result)
        self.assertTrue(troshkad._nfs_healthy)

    @patch("os.path.ismount", return_value=False)
    def test_returns_false_when_not_mounted(self, mock_mount):
        troshkad._config["storage_mode"] = "shared"
        troshkad._config["shared_mount"] = "/var/lib/troshka/shared"
        troshkad._nfs_healthy = True
        result = troshkad._check_nfs_health()
        self.assertFalse(result)
        self.assertFalse(troshkad._nfs_healthy)

    @patch("os.statvfs")
    @patch("os.path.ismount", return_value=True)
    def test_returns_true_when_healthy(self, mock_mount, mock_statvfs):
        troshkad._config["storage_mode"] = "shared"
        troshkad._config["shared_mount"] = "/var/lib/troshka/shared"
        troshkad._nfs_healthy = False
        result = troshkad._check_nfs_health()
        self.assertTrue(result)
        self.assertTrue(troshkad._nfs_healthy)


class TestTryNfsRecovery(unittest.TestCase):
    def setUp(self):
        self._orig_config = troshkad._config.copy()

    def tearDown(self):
        troshkad._config.update(self._orig_config)

    @patch("builtins.open", side_effect=OSError("no fstab"))
    def test_returns_false_when_fstab_unreadable(self, mock_open_fn):
        troshkad._config["shared_mount"] = "/var/lib/troshka/shared"
        result = troshkad._try_nfs_recovery()
        self.assertFalse(result)

    @patch("builtins.open", mock_open(read_data="# comment\n"))
    def test_returns_false_when_no_fstab_entry(self):
        troshkad._config["shared_mount"] = "/var/lib/troshka/shared"
        result = troshkad._try_nfs_recovery()
        self.assertFalse(result)

    @patch("subprocess.run")
    @patch(
        "builtins.open",
        mock_open(
            read_data="nfs-server:/export /var/lib/troshka/shared nfs soft,timeo=50 0 0\n"
        ),
    )
    def test_successful_recovery(self, mock_run):
        troshkad._config["shared_mount"] = "/var/lib/troshka/shared"
        mock_run.return_value = MagicMock(returncode=0)
        result = troshkad._try_nfs_recovery()
        self.assertTrue(result)
        # Should call umount then mount
        self.assertEqual(mock_run.call_count, 2)

    @patch("subprocess.run")
    @patch(
        "builtins.open",
        mock_open(
            read_data="nfs-server:/export /var/lib/troshka/shared nfs soft,timeo=50 0 0\n"
        ),
    )
    def test_failed_remount(self, mock_run):
        troshkad._config["shared_mount"] = "/var/lib/troshka/shared"
        # umount succeeds, mount fails
        mock_run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=1, stderr="mount error"),
        ]
        result = troshkad._try_nfs_recovery()
        self.assertFalse(result)

    @patch("subprocess.run")
    @patch(
        "builtins.open",
        mock_open(
            read_data="nfs-server:/export /var/lib/troshka/shared nfs soft,timeo=50 0 0\n"
        ),
    )
    def test_mount_timeout(self, mock_run):
        troshkad._config["shared_mount"] = "/var/lib/troshka/shared"
        mock_run.side_effect = [
            MagicMock(returncode=0),
            subprocess.TimeoutExpired("mount", 30),
        ]
        result = troshkad._try_nfs_recovery()
        self.assertFalse(result)


class TestCleanupOldJobsExtended(unittest.TestCase):
    def test_removes_completed_old_jobs(self):
        old_time = time.localtime(time.time() - 7200)  # 2 hours ago
        old_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", old_time)
        job = {
            "job_id": "old-job-1",
            "status": "completed",
            "started_at": old_ts,
            "completed_at": old_ts,
        }
        with troshkad._jobs_lock:
            troshkad._jobs["old-job-1"] = job
        try:
            troshkad._cleanup_old_jobs()
            with troshkad._jobs_lock:
                self.assertNotIn("old-job-1", troshkad._jobs)
        finally:
            with troshkad._jobs_lock:
                troshkad._jobs.pop("old-job-1", None)

    def test_keeps_recent_completed_jobs(self):
        recent_time = time.localtime(time.time() - 60)  # 1 minute ago
        recent_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", recent_time)
        job = {
            "job_id": "recent-job-1",
            "status": "completed",
            "started_at": recent_ts,
            "completed_at": recent_ts,
        }
        with troshkad._jobs_lock:
            troshkad._jobs["recent-job-1"] = job
        try:
            troshkad._cleanup_old_jobs()
            with troshkad._jobs_lock:
                self.assertIn("recent-job-1", troshkad._jobs)
        finally:
            with troshkad._jobs_lock:
                troshkad._jobs.pop("recent-job-1", None)

    def test_keeps_running_jobs(self):
        old_ts = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.localtime(time.time() - 7200)
        )
        job = {
            "job_id": "running-job-1",
            "status": "running",
            "started_at": old_ts,
            "completed_at": None,
        }
        with troshkad._jobs_lock:
            troshkad._jobs["running-job-1"] = job
        try:
            troshkad._cleanup_old_jobs()
            with troshkad._jobs_lock:
                self.assertIn("running-job-1", troshkad._jobs)
        finally:
            with troshkad._jobs_lock:
                troshkad._jobs.pop("running-job-1", None)


class TestThreadingHTTPServerVerifyRequest(unittest.TestCase):
    def test_allows_non_banned_ip(self):
        # Ensure the IP is not banned
        ip = "192.168.1.100"
        with troshkad._rate_limit_lock:
            troshkad._banned_ips.pop(ip, None)
            troshkad._permabanned_ips.discard(ip)

        server = MagicMock(spec=troshkad.ThreadingHTTPServer)
        result = troshkad.ThreadingHTTPServer.verify_request(server, MagicMock(), (ip, 12345))
        self.assertTrue(result)

    def test_rejects_permabanned_ip(self):
        ip = "10.0.0.99"
        with troshkad._rate_limit_lock:
            troshkad._permabanned_ips.add(ip)
        try:
            server = MagicMock(spec=troshkad.ThreadingHTTPServer)
            result = troshkad.ThreadingHTTPServer.verify_request(
                server, MagicMock(), (ip, 12345)
            )
            self.assertFalse(result)
        finally:
            with troshkad._rate_limit_lock:
                troshkad._permabanned_ips.discard(ip)


class TestCheckNfsHealthProbeTimeout(unittest.TestCase):
    """Test _check_nfs_health when the NFS probe thread times out."""

    def setUp(self):
        self._orig_config = troshkad._config.copy()
        self._orig_healthy = troshkad._nfs_healthy
        self._orig_stale = troshkad._nfs_stale_since

    def tearDown(self):
        troshkad._config.update(self._orig_config)
        troshkad._nfs_healthy = self._orig_healthy
        troshkad._nfs_stale_since = self._orig_stale

    @patch("os.path.ismount", return_value=True)
    def test_probe_timeout_marks_unhealthy(self, mock_mount):
        """When the probe thread hangs (t.is_alive() after join), NFS is stale."""
        troshkad._config["storage_mode"] = "shared"
        troshkad._config["shared_mount"] = "/var/lib/troshka/shared"
        troshkad._nfs_healthy = True

        # Make threading.Thread.join return without the thread finishing
        # and thread stays alive (simulates D-state / hung NFS)
        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = True  # thread never completed

        with patch("threading.Thread", return_value=mock_thread):
            result = troshkad._check_nfs_health()
            self.assertFalse(result)
            self.assertFalse(troshkad._nfs_healthy)
            self.assertGreater(troshkad._nfs_stale_since, 0)

    @patch("os.path.ismount", return_value=True)
    def test_probe_oserror_marks_unhealthy(self, mock_mount):
        """When the probe gets an OSError, result[0] is False."""
        troshkad._config["storage_mode"] = "shared"
        troshkad._config["shared_mount"] = "/var/lib/troshka/shared"
        troshkad._nfs_healthy = True

        # statvfs raises OSError -> probe sets result[0] = False
        with patch("os.statvfs", side_effect=OSError("stale")):
            result = troshkad._check_nfs_health()
            self.assertFalse(result)
            self.assertFalse(troshkad._nfs_healthy)

    @patch("os.path.ismount", return_value=True)
    def test_probe_timeout_already_unhealthy_no_double_log(self, mock_mount):
        """When already unhealthy, stale_since is NOT reset."""
        troshkad._config["storage_mode"] = "shared"
        troshkad._config["shared_mount"] = "/var/lib/troshka/shared"
        troshkad._nfs_healthy = False
        troshkad._nfs_stale_since = 12345.0

        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = True

        with patch("threading.Thread", return_value=mock_thread):
            result = troshkad._check_nfs_health()
            self.assertFalse(result)
            # stale_since should NOT be updated (was already unhealthy)
            self.assertEqual(troshkad._nfs_stale_since, 12345.0)


class TestTryNfsRecoveryMountException(unittest.TestCase):
    """Test _try_nfs_recovery when mount raises a generic exception (line 311)."""

    def setUp(self):
        self._orig_config = troshkad._config.copy()

    def tearDown(self):
        troshkad._config.update(self._orig_config)

    @patch("subprocess.run")
    @patch(
        "builtins.open",
        mock_open(
            read_data="nfs-server:/export /var/lib/troshka/shared nfs soft,timeo=50 0 0\n"
        ),
    )
    def test_mount_generic_exception(self, mock_run):
        """When mount raises a non-timeout exception (line 311-313)."""
        troshkad._config["shared_mount"] = "/var/lib/troshka/shared"
        # umount succeeds, mount raises generic exception
        mock_run.side_effect = [
            MagicMock(returncode=0),  # umount -l
            RuntimeError("mount: permission denied"),  # mount
        ]
        result = troshkad._try_nfs_recovery()
        self.assertFalse(result)

    @patch("subprocess.run")
    @patch(
        "builtins.open",
        mock_open(
            read_data="nfs-server:/export /var/lib/troshka/shared nfs soft,timeo=50 0 0\n"
        ),
    )
    def test_umount_exception_still_tries_mount(self, mock_run):
        """When umount raises exception, recovery still tries mount."""
        troshkad._config["shared_mount"] = "/var/lib/troshka/shared"
        mock_run.side_effect = [
            RuntimeError("umount failed"),  # umount -l raises
            MagicMock(returncode=0),  # mount succeeds
        ]
        result = troshkad._try_nfs_recovery()
        self.assertTrue(result)


class TestRecordAuthFailurePermabannedEarlyReturn(unittest.TestCase):
    """Test _record_auth_failure early return for permabanned IPs (line 131)."""

    def setUp(self):
        self._ip = "10.99.99.99"
        with troshkad._rate_limit_lock:
            troshkad._fail_tracker.pop(self._ip, None)
            troshkad._banned_ips.pop(self._ip, None)
            troshkad._ban_history.pop(self._ip, None)
            troshkad._permabanned_ips.discard(self._ip)

    def tearDown(self):
        with troshkad._rate_limit_lock:
            troshkad._fail_tracker.pop(self._ip, None)
            troshkad._banned_ips.pop(self._ip, None)
            troshkad._ban_history.pop(self._ip, None)
            troshkad._permabanned_ips.discard(self._ip)

    def test_record_failure_noop_for_permabanned(self):
        """Calling _record_auth_failure on a permabanned IP is a no-op."""
        with troshkad._rate_limit_lock:
            troshkad._permabanned_ips.add(self._ip)

        # Record a failure — should return immediately without adding to tracker
        troshkad._record_auth_failure(self._ip)

        with troshkad._rate_limit_lock:
            self.assertNotIn(self._ip, troshkad._fail_tracker)
            # IP should still be permabanned
            self.assertIn(self._ip, troshkad._permabanned_ips)


if __name__ == "__main__":
    unittest.main()
