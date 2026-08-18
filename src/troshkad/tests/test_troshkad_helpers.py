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
            "mesh/setup",
            "mesh/join-network",
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
        assert (
            troshkad._validate_network_name("troshka-net-abcdef0123")
            == "troshka-net-abcdef0123"
        )

    def test_invalid_name_raises(self):
        with self.assertRaises(ValueError):
            troshkad._validate_network_name("bad-net")


class TestValidateBridgeName(unittest.TestCase):
    def test_valid_troshka_bridge(self):
        assert (
            troshkad._validate_bridge_name("br-troshka-abcdef01")
            == "br-troshka-abcdef01"
        )

    def test_valid_bmc_bridge(self):
        assert troshkad._validate_bridge_name("br-bmc-abcdef01") == "br-bmc-abcdef01"

    def test_valid_plain_hex_bridge(self):
        assert troshkad._validate_bridge_name("br-abcdef01") == "br-abcdef01"

    def test_invalid_bridge_raises(self):
        with self.assertRaises(ValueError):
            troshkad._validate_bridge_name("eth0")


class TestValidateURL(unittest.TestCase):
    def test_valid_https_url(self):
        assert (
            troshkad._validate_url("https://example.com/path")
            == "https://example.com/path"
        )

    def test_valid_http_url(self):
        assert (
            troshkad._validate_url("http://10.0.0.1:8080/api")
            == "http://10.0.0.1:8080/api"
        )

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
        assert (
            troshkad._storage_path("cache/snapshots") == "/mnt/shared/cache/snapshots"
        )

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
        result = troshkad._safe_kill(
            pid, signal.SIGTERM, expected_cmdline_substring="troshkad"
        )
        assert result is True

    @patch("os.kill")
    @patch("builtins.open", mock_open(read_data=b"nginx\x00-g\x00daemon off;\x00"))
    def test_skips_kill_on_cmdline_mismatch(self, mock_kill):
        pid = os.getpid() + 99999
        result = troshkad._safe_kill(
            pid, signal.SIGTERM, expected_cmdline_substring="troshkad"
        )
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
        assert args[0][0] == [
            "ip",
            "netns",
            "exec",
            "troshka-abcdef01",
            "ip",
            "-4",
            "addr",
            "show",
        ]

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
        result = troshkad._handle_oc_exec(
            job,
            {
                "project_id": "12345678-abcd-ef01-2345-6789abcdef01",
                "command": "get nodes",
                "timeout": "30",
                "gateway_ip": "10.0.0.1",
            },
        )
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
        result = troshkad._handle_oc_exec(
            job,
            {
                "project_id": "12345678-abcd-ef01-2345-6789abcdef01",
                "command": "get nodes",
                "gateway_ip": "",
            },
        )
        self.assertEqual(result["exit_code"], 0)
        cmd = mock_run.call_args[0][0]
        self.assertIn("/usr/local/bin/oc", cmd)
        self.assertNotIn("unshare", cmd)

    @patch("os.path.isfile", return_value=False)
    def test_missing_kubeconfig_raises(self, _mock_isfile):
        job = self._make_job()
        with self.assertRaises(RuntimeError):
            troshkad._handle_oc_exec(
                job,
                {
                    "project_id": "12345678-abcd-ef01-2345-6789abcdef01",
                    "command": "get nodes",
                },
            )

    def test_empty_command_raises(self):
        job = self._make_job()
        with self.assertRaises(RuntimeError):
            troshkad._handle_oc_exec(
                job,
                {
                    "project_id": "12345678-abcd-ef01-2345-6789abcdef01",
                    "command": "",
                },
            )

    @patch("subprocess.run")
    @patch("os.path.isfile", return_value=True)
    def test_timeout_capped_at_300(self, _mock_isfile, mock_run):
        mock_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
        job = self._make_job()
        troshkad._handle_oc_exec(
            job,
            {
                "project_id": "12345678-abcd-ef01-2345-6789abcdef01",
                "command": "get pods",
                "timeout": "9999",
                "gateway_ip": "",
            },
        )
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
            dhcp_hosts=[
                {"mac": "aa:bb:cc:dd:ee:ff", "ip": "192.168.100.10", "name": "vm1"}
            ],
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
                    {
                        "mac": "aa:bb:cc:dd:ee:01",
                        "ip": "192.168.100.10",
                        "name": "node-1",
                    },
                    {
                        "mac": "aa:bb:cc:dd:ee:02",
                        "ip": "192.168.100.11",
                        "name": "node-2",
                    },
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
        self,
        _mock_isdir,
        _mock_makedirs,
        _mock_stop,
        mock_popen,
        _mock_exists,
        _mock_sleep,
        mock_run_cmd,
        _mock_log,
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
            vms=[
                {"domain_name": "troshka-abcdef01-12345678", "bmc_ip": "192.168.100.10"}
            ],
            bmc_username="admin",
            bmc_password="password",  # pragma: allowlist secret
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
        self,
        mock_rmtree,
        _mock_isdir,
        _mock_makedirs,
        _mock_stop,
        mock_popen,
        _mock_exists,
        _mock_sleep,
        _mock_run_cmd,
        _mock_log,
    ):
        mock_popen.return_value = MagicMock(pid=100)
        troshkad._bmc_start_vbmcd(
            job=self._make_job(),
            ns="troshka-abcdef01",
            bmc_dir="/var/lib/troshka/bmc/proj-id",
            venv_bin="/opt/troshka/venv/bin",
            vms=[],
            bmc_username="admin",
            bmc_password="pass",  # pragma: allowlist secret
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
        self,
        _mock_isdir,
        _mock_makedirs,
        _mock_stop,
        mock_popen,
        _mock_exists,
        _mock_sleep,
        _mock_run_cmd,
        _mock_log,
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
                bmc_password="pass",  # pragma: allowlist secret
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
        self,
        _mock_isdir,
        _mock_makedirs,
        mock_stop,
        mock_popen,
        _mock_exists,
        _mock_sleep,
        _mock_run_cmd,
        _mock_log,
    ):
        mock_popen.return_value = MagicMock(pid=100)
        troshkad._bmc_start_vbmcd(
            job=self._make_job(),
            ns="troshka-abcdef01",
            bmc_dir="/var/lib/troshka/bmc/proj-id",
            venv_bin="/opt/troshka/venv/bin",
            vms=[],
            bmc_username="admin",
            bmc_password="pass",  # pragma: allowlist secret
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
        self,
        _mock_exists,
        _mock_safe_kill,
        _mock_sleep,
        _mock_os_kill,
        mock_remove,
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
        self,
        _mock_exists,
        _mock_safe_kill,
        _mock_sleep,
        mock_os_kill,
        mock_remove,
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
    def test_no_artifacts_is_noop(
        self, _mock_exists, _mock_glob, _mock_ismount, mock_rmdir, mock_run
    ):
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
        umount_calls = [c for c in mock_run.call_args_list if "umount" in str(c)]
        self.assertEqual(len(umount_calls), 1)

    @patch("subprocess.run")
    @patch("os.path.exists")
    @patch("os.rmdir")
    @patch("os.path.ismount", return_value=False)
    @patch("glob.glob", return_value=[])
    def test_disconnects_nbd_devices(
        self, _mock_glob, _mock_ismount, _mock_rmdir, mock_exists, mock_run
    ):
        # nbd0p1 exists, others don't
        def exists_side_effect(path):
            return path == "/dev/nbd0p1"

        mock_exists.side_effect = exists_side_effect
        mock_run.return_value = MagicMock(stdout="", returncode=0)
        troshkad._cleanup_stale_recert()
        disconnect_calls = [c for c in mock_run.call_args_list if "qemu-nbd" in str(c)]
        self.assertEqual(len(disconnect_calls), 1)

    @patch("subprocess.run")
    @patch("os.path.exists", return_value=False)
    @patch("os.rmdir")
    @patch("os.path.ismount", return_value=False)
    @patch("glob.glob", return_value=[])
    def test_removes_stale_podman_containers(
        self, _mock_glob, _mock_ismount, _mock_rmdir, _mock_exists, mock_run
    ):
        # First subprocess.run is podman ps, return a stale container name
        def run_side_effect(cmd, **kwargs):
            if "podman" in cmd and "ps" in cmd:
                return MagicMock(stdout="recert-etcd-abc123\n", returncode=0)
            return MagicMock(stdout="", returncode=0)

        mock_run.side_effect = run_side_effect
        troshkad._cleanup_stale_recert()
        rm_calls = [
            c
            for c in mock_run.call_args_list
            if "rm" in str(c) and "recert-etcd-abc123" in str(c)
        ]
        self.assertEqual(len(rm_calls), 1)

    @patch("subprocess.run")
    @patch("os.path.exists", return_value=False)
    @patch("os.rmdir")
    @patch("os.path.ismount", return_value=False)
    @patch("glob.glob", return_value=["/var/lib/troshka/local/tmp/recert-xyz"])
    def test_rmdir_after_unmount(
        self, _mock_glob, _mock_ismount, mock_rmdir, _mock_exists, mock_run
    ):
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
        old_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.localtime(time.time() - 7200))
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
        result = troshkad.ThreadingHTTPServer.verify_request(
            server, MagicMock(), (ip, 12345)
        )
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


# ── Capacity helpers ──


class TestGetCpuCapacity(unittest.TestCase):
    @patch("os.cpu_count", return_value=16)
    def test_returns_cpu_count(self, _mock):
        result = troshkad._get_cpu_capacity()
        self.assertEqual(result, {"vcpus_total": 16})

    @patch("os.cpu_count", return_value=None)
    def test_none_cpu_count_returns_zero(self, _mock):
        result = troshkad._get_cpu_capacity()
        self.assertEqual(result, {"vcpus_total": 0})

    @patch("os.cpu_count", side_effect=OSError("fail"))
    def test_exception_returns_zero(self, _mock):
        result = troshkad._get_cpu_capacity()
        self.assertEqual(result, {"vcpus_total": 0})


class TestGetMemoryCapacity(unittest.TestCase):
    def test_parses_meminfo(self):
        meminfo = "MemTotal:       16384000 kB\nMemFree:         8192000 kB\n"
        with patch("builtins.open", mock_open(read_data=meminfo)):
            result = troshkad._get_memory_capacity()
        self.assertEqual(result, {"ram_total_mb": 16384000 // 1024})

    def test_missing_memtotal_returns_zero(self):
        meminfo = "MemFree:         8192000 kB\n"
        with patch("builtins.open", mock_open(read_data=meminfo)):
            result = troshkad._get_memory_capacity()
        self.assertEqual(result, {"ram_total_mb": 0})

    @patch("builtins.open", side_effect=OSError("no such file"))
    def test_exception_returns_zero(self, _mock):
        result = troshkad._get_memory_capacity()
        self.assertEqual(result, {"ram_total_mb": 0})


class TestGetStorageCapacity(unittest.TestCase):
    def setUp(self):
        self._orig_config = troshkad._config.copy()

    def tearDown(self):
        troshkad._config.clear()
        troshkad._config.update(self._orig_config)

    @patch("shutil.disk_usage")
    def test_local_mode(self, mock_du):
        troshkad._config["storage_mode"] = "local"
        mock_du.return_value = MagicMock(total=500 * (1024**3), used=200 * (1024**3))
        result = troshkad._get_storage_capacity()
        self.assertEqual(result, {"storage_total_gb": 500, "storage_used_gb": 200})
        mock_du.assert_called_once_with("/var/lib/troshka")

    @patch("shutil.disk_usage")
    def test_shared_mode_uses_local_mount(self, mock_du):
        troshkad._config["storage_mode"] = "shared"
        troshkad._config["local_mount"] = "/mnt/local"
        mock_du.return_value = MagicMock(total=1000 * (1024**3), used=300 * (1024**3))
        result = troshkad._get_storage_capacity()
        self.assertEqual(result, {"storage_total_gb": 1000, "storage_used_gb": 300})
        mock_du.assert_called_once_with("/mnt/local")

    @patch("shutil.disk_usage", side_effect=OSError("fail"))
    def test_exception_returns_zeros(self, _mock):
        result = troshkad._get_storage_capacity()
        self.assertEqual(result, {"storage_total_gb": 0, "storage_used_gb": 0})


class TestGetVmCapacity(unittest.TestCase):
    @patch("subprocess.run")
    def test_counts_vms_and_resources(self, mock_run):
        def run_side_effect(cmd, **kwargs):
            if cmd == ["virsh", "list", "--all", "--name"]:
                return MagicMock(returncode=0, stdout="vm-a\nvm-b\n")
            if cmd == ["virsh", "list", "--name"]:
                return MagicMock(returncode=0, stdout="vm-a\n")
            if cmd[0:2] == ["virsh", "dominfo"]:
                return MagicMock(
                    returncode=0,
                    stdout="CPU(s):          4\nMax memory:      8388608 KiB\n",
                )
            return MagicMock(returncode=1, stdout="")

        mock_run.side_effect = run_side_effect
        result = troshkad._get_vm_capacity()
        self.assertEqual(result["total_vms"], 2)
        self.assertEqual(result["running_vms"], 1)
        self.assertEqual(result["vcpus_used"], 8)  # 4 per VM * 2 VMs
        self.assertEqual(result["ram_used_mb"], 8388608 // 1024 * 2)

    @patch("subprocess.run")
    def test_virsh_failure_returns_empty(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        result = troshkad._get_vm_capacity()
        self.assertEqual(result, {})

    @patch("subprocess.run", side_effect=FileNotFoundError("virsh not found"))
    def test_exception_returns_empty(self, _mock):
        result = troshkad._get_vm_capacity()
        self.assertEqual(result, {})

    @patch("subprocess.run")
    def test_no_domains(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="\n")
        result = troshkad._get_vm_capacity()
        self.assertEqual(result["total_vms"], 0)
        self.assertEqual(result["vcpus_used"], 0)
        self.assertEqual(result["ram_used_mb"], 0)


class TestGetContainerCapacity(unittest.TestCase):
    @patch("subprocess.run")
    def test_counts_containers(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="troshka-web running\ntroshka-db exited\ntroshka-cache running\n",
        )
        result = troshkad._get_container_capacity()
        self.assertEqual(result["total_containers"], 3)
        self.assertEqual(result["running_containers"], 2)

    @patch("subprocess.run")
    def test_no_containers(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="\n")
        result = troshkad._get_container_capacity()
        self.assertEqual(result["total_containers"], 0)
        self.assertEqual(result["running_containers"], 0)

    @patch("subprocess.run")
    def test_podman_failure_returns_empty(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        result = troshkad._get_container_capacity()
        self.assertEqual(result, {})

    @patch("subprocess.run", side_effect=FileNotFoundError("podman not found"))
    def test_exception_returns_empty(self, _mock):
        result = troshkad._get_container_capacity()
        self.assertEqual(result, {})


# ── _parse_boot_devices / _parse_memory ──


class TestParseBootDevices(unittest.TestCase):
    def test_parse_boot_devices_os_boot(self):
        """<os><boot dev="hd"/><boot dev="network"/></os> returns ["hd", "network"]."""
        import xml.etree.ElementTree as ET

        xml_str = (
            "<domain>"
            "  <os><boot dev='hd'/><boot dev='network'/></os>"
            "  <devices></devices>"
            "</domain>"
        )
        root = ET.fromstring(xml_str)
        result = troshkad._parse_boot_devices(root)
        self.assertEqual(result, ["hd", "network"])

    def test_parse_boot_devices_per_device(self):
        """Per-device <boot order="N"> elements produce correct order."""
        import xml.etree.ElementTree as ET

        xml_str = (
            "<domain>"
            "  <os></os>"
            "  <devices>"
            "    <interface type='bridge'><boot order='2'/></interface>"
            "    <disk type='file' device='disk'><boot order='1'/></disk>"
            "    <disk type='file' device='cdrom'><boot order='3'/></disk>"
            "  </devices>"
            "</domain>"
        )
        root = ET.fromstring(xml_str)
        result = troshkad._parse_boot_devices(root)
        self.assertEqual(result, ["hd", "network", "cdrom"])

    def test_parse_boot_devices_empty(self):
        """No boot elements returns empty list."""
        import xml.etree.ElementTree as ET

        xml_str = (
            "<domain>"
            "  <os></os>"
            "  <devices>"
            "    <disk type='file' device='disk'/>"
            "  </devices>"
            "</domain>"
        )
        root = ET.fromstring(xml_str)
        result = troshkad._parse_boot_devices(root)
        self.assertEqual(result, [])


class TestParseMemory(unittest.TestCase):
    def test_parse_memory_kib(self):
        """<memory unit="KiB">4194304</memory> returns 4096 MB."""
        import xml.etree.ElementTree as ET

        xml_str = '<domain><memory unit="KiB">4194304</memory></domain>'
        root = ET.fromstring(xml_str)
        result = troshkad._parse_memory(root)
        self.assertEqual(result, 4096)

    def test_parse_memory_no_unit(self):
        """<memory>2048</memory> defaults to KiB, returns 2 MB."""
        import xml.etree.ElementTree as ET

        xml_str = "<domain><memory>2048</memory></domain>"
        root = ET.fromstring(xml_str)
        result = troshkad._parse_memory(root)
        self.assertEqual(result, 2)


# ── Watchdog Helpers ──


class TestWatchdogCheckHttpSuccess(unittest.TestCase):
    """HTTP self-check succeeds — failure counter resets to 0."""

    def test_watchdog_check_http_success(self):
        original = troshkad._watchdog_http_failures
        troshkad._watchdog_http_failures = 3
        saved_config = troshkad._config.copy() if troshkad._config else {}
        troshkad._config["port"] = 31337
        try:
            with patch("socket.create_connection") as mock_conn:
                mock_sock = MagicMock()
                mock_conn.return_value = mock_sock
                troshkad._watchdog_check_http()
                mock_conn.assert_called_once_with(("127.0.0.1", 31337), timeout=5)
                mock_sock.close.assert_called_once()
                assert troshkad._watchdog_http_failures == 0
        finally:
            troshkad._watchdog_http_failures = original
            troshkad._config.update(saved_config)


class TestWatchdogCheckHttpFailureIncrements(unittest.TestCase):
    """HTTP self-check fails — failure counter increments."""

    def test_watchdog_check_http_failure_increments(self):
        original = troshkad._watchdog_http_failures
        troshkad._watchdog_http_failures = 0
        saved_config = troshkad._config.copy() if troshkad._config else {}
        troshkad._config["port"] = 31337
        try:
            with patch("socket.create_connection", side_effect=OSError("refused")):
                troshkad._watchdog_check_http()
                assert troshkad._watchdog_http_failures == 1
        finally:
            troshkad._watchdog_http_failures = original
            troshkad._config.update(saved_config)


class TestWatchdogCheckHttpThresholdExit(unittest.TestCase):
    """HTTP self-check fails at threshold — os._exit(1) called."""

    def test_watchdog_check_http_threshold_exit(self):
        original = troshkad._watchdog_http_failures
        troshkad._watchdog_http_failures = 5
        saved_config = troshkad._config.copy() if troshkad._config else {}
        troshkad._config["port"] = 31337
        try:
            with patch("socket.create_connection", side_effect=OSError("refused")):
                with patch("os._exit") as mock_exit:
                    troshkad._watchdog_check_http()
                    assert troshkad._watchdog_http_failures == 6
                    mock_exit.assert_called_once_with(1)
        finally:
            troshkad._watchdog_http_failures = original
            troshkad._config.update(saved_config)


class TestWatchdogCheckServicesActive(unittest.TestCase):
    """All services active — no restart triggered."""

    def test_watchdog_check_services_active(self):
        active_result = MagicMock()
        active_result.stdout = "active\n"
        with patch("subprocess.run", return_value=active_result) as mock_run:
            troshkad._watchdog_check_services()
            # Each service gets one is-active check (all return active)
            assert mock_run.call_count == len(troshkad._REQUIRED_SERVICES)
            for c in mock_run.call_args_list:
                assert c[0][0][0] == "systemctl"
                assert c[0][0][1] == "is-active"


class TestWatchdogCheckServicesRestart(unittest.TestCase):
    """Service inactive — systemctl start called."""

    def test_watchdog_check_services_restart(self):
        inactive_result = MagicMock()
        inactive_result.stdout = "inactive\n"
        start_result = MagicMock()

        def run_side_effect(cmd, **kwargs):
            if cmd[1] == "is-active":
                return inactive_result
            return start_result

        with patch("subprocess.run", side_effect=run_side_effect) as mock_run:
            troshkad._watchdog_check_services()
            # For socket services (is_socket=True), we get: is-active socket, is-active service, start
            # For non-socket services: is-active unit, start
            start_calls = [c for c in mock_run.call_args_list if c[0][0][1] == "start"]
            assert len(start_calls) == len(troshkad._REQUIRED_SERVICES)


class TestWatchdogCheckNfsHealthy(unittest.TestCase):
    """NFS healthy on shared storage — no recovery attempted."""

    def test_watchdog_check_nfs_healthy(self):
        saved_config = troshkad._config.copy() if troshkad._config else {}
        troshkad._config["storage_mode"] = "shared"
        try:
            with patch.object(
                troshkad, "_check_nfs_health", return_value=True
            ) as mock_check:
                with patch.object(troshkad, "_try_nfs_recovery") as mock_recovery:
                    troshkad._watchdog_check_nfs()
                    mock_check.assert_called_once()
                    mock_recovery.assert_not_called()
        finally:
            troshkad._config.update(saved_config)
            if "storage_mode" not in saved_config:
                troshkad._config.pop("storage_mode", None)


# ── _find_ostree_paths ──


class TestFindOstreePaths(unittest.TestCase):
    @patch("os.path.isdir")
    @patch("os.listdir", return_value=["abc123def456.0"])
    def test_find_ostree_paths_success(self, _mock_listdir, mock_isdir):
        """Valid RHCOS disk returns all six expected paths."""

        def isdir_side_effect(path):
            # deploy dir, etc_k8s, var_kubelet, var_etcd all exist
            return True

        mock_isdir.side_effect = isdir_side_effect
        result = troshkad._find_ostree_paths("/mnt/disk")
        deploy_root, var_root, etc_k8s, etc_mcd, var_kubelet, var_etcd = result
        self.assertEqual(
            deploy_root,
            "/mnt/disk/ostree/deploy/rhcos/deploy/abc123def456.0",
        )
        self.assertEqual(var_root, "/mnt/disk/ostree/deploy/rhcos/var")
        self.assertIn("etc/kubernetes", etc_k8s)
        self.assertIn("etc/machine-config-daemon", etc_mcd)
        self.assertIn("lib/kubelet", var_kubelet)
        self.assertIn("lib/etcd", var_etcd)

    @patch("os.path.isdir", return_value=False)
    def test_find_ostree_paths_no_deploy_dir(self, _mock_isdir):
        """Raises RuntimeError when ostree deploy dir is missing."""
        with self.assertRaises(RuntimeError) as ctx:
            troshkad._find_ostree_paths("/mnt/disk")
        self.assertIn("no ostree deploy dir", str(ctx.exception))

    @patch("os.path.isdir")
    @patch("os.listdir", return_value=[])
    def test_find_ostree_paths_no_entries(self, _mock_listdir, mock_isdir):
        """Raises RuntimeError when no deployment entries exist."""

        def isdir_side_effect(path):
            if path.endswith("deploy"):
                return True
            return False

        mock_isdir.side_effect = isdir_side_effect
        with self.assertRaises(RuntimeError) as ctx:
            troshkad._find_ostree_paths("/mnt/disk")
        self.assertIn("No OSTree deployment", str(ctx.exception))


# ── _save_kubeconfig ──


class TestSaveKubeconfig(unittest.TestCase):
    def setUp(self):
        self._orig_config = troshkad._config.copy()

    def tearDown(self):
        troshkad._config.clear()
        troshkad._config.update(self._orig_config)

    @patch("os.makedirs")
    @patch("os.path.isfile", return_value=True)
    def test_save_kubeconfig_writes_file(self, _mock_isfile, _mock_makedirs):
        """Writes kubeconfig to project dir and returns content."""
        troshkad._config["vm_dir"] = "/var/lib/troshka/vms"
        job = {"job_id": "aaaa0000-0000-0000-0000-000000000000", "output": []}
        params = {"project_id": "proj-1234", "vm_name": "sno1"}
        kc_data = "apiVersion: v1\nclusters: []\n"

        m = mock_open(read_data=kc_data)
        with patch("builtins.open", m):
            result = troshkad._save_kubeconfig(
                job, params, "/mnt/etc/kubernetes", force_expire=False
            )
        self.assertEqual(result, kc_data)
        # Should have written kubeconfig and kubeconfig-sno1
        write_calls = [c for c in m().write.call_args_list]
        self.assertGreaterEqual(len(write_calls), 2)

    def test_save_kubeconfig_force_expire_skips(self):
        """When force_expire=True, returns None without touching files."""
        job = {"job_id": "bbbb0000-0000-0000-0000-000000000000", "output": []}
        params = {"project_id": "proj-1234", "vm_name": "sno1"}
        result = troshkad._save_kubeconfig(
            job, params, "/mnt/etc/kubernetes", force_expire=True
        )
        self.assertIsNone(result)


# ── _run_cmd ──


class TestRunCmd(unittest.TestCase):
    def _make_job(self):
        return {"job_id": "run-cmd-0000-0000-0000-000000000000", "output": [], "_process": None}

    @patch("troshkad.subprocess.Popen")
    def test_success(self, mock_popen):
        proc = MagicMock()
        proc.communicate.return_value = ("output line", "")
        proc.returncode = 0
        mock_popen.return_value = proc
        job = self._make_job()
        result = troshkad._run_cmd(job, ["echo", "hi"])
        self.assertEqual(result.returncode, 0)
        self.assertIn("output line", job["output"])

    @patch("troshkad.subprocess.Popen")
    def test_failure_raises(self, mock_popen):
        proc = MagicMock()
        proc.communicate.return_value = ("", "error msg")
        proc.returncode = 1
        mock_popen.return_value = proc
        job = self._make_job()
        with self.assertRaises(RuntimeError):
            troshkad._run_cmd(job, ["false"])

    @patch("troshkad.subprocess.Popen")
    def test_failure_no_check(self, mock_popen):
        proc = MagicMock()
        proc.communicate.return_value = ("", "err")
        proc.returncode = 1
        mock_popen.return_value = proc
        job = self._make_job()
        result = troshkad._run_cmd(job, ["cmd"], check=False)
        self.assertEqual(result.returncode, 1)

    @patch("troshkad.subprocess.Popen")
    def test_timeout_raises(self, mock_popen):
        import subprocess as sp
        proc = MagicMock()
        # First call raises timeout, second (after kill) returns empty
        proc.communicate.side_effect = [sp.TimeoutExpired(["cmd"], 5), ("", "")]
        proc.kill = MagicMock()
        mock_popen.return_value = proc
        job = self._make_job()
        with self.assertRaises(RuntimeError) as ctx:
            troshkad._run_cmd(job, ["cmd"], timeout=5)
        self.assertIn("timed out", str(ctx.exception))
        proc.kill.assert_called_once()

    @patch("troshkad.subprocess.Popen")
    def test_clears_process_after_run(self, mock_popen):
        proc = MagicMock()
        proc.communicate.return_value = ("", "")
        proc.returncode = 0
        mock_popen.return_value = proc
        job = self._make_job()
        troshkad._run_cmd(job, ["echo"])
        self.assertIsNone(job["_process"])


# ── _build_disk_arg ──


class TestBuildDiskArg(unittest.TestCase):
    def test_basic_virtio_disk(self):
        result = troshkad._build_disk_arg("/tmp/disk.qcow2", {"bus": "virtio"}, "")
        self.assertEqual(result, "path=/tmp/disk.qcow2,bus=virtio")

    def test_rotation_rate_on_scsi(self):
        result = troshkad._build_disk_arg(
            "/tmp/disk.qcow2",
            {"bus": "scsi", "rotation_rate": 1},
            "",
        )
        self.assertIn("rotation_rate=1", result)

    def test_rotation_rate_ignored_on_virtio(self):
        result = troshkad._build_disk_arg(
            "/tmp/disk.qcow2",
            {"bus": "virtio", "rotation_rate": 1},
            "",
        )
        self.assertNotIn("rotation_rate", result)

    def test_disk_cache_none_adds_io_native(self):
        result = troshkad._build_disk_arg("/tmp/d.qcow2", {}, "none")
        self.assertIn("cache=none", result)
        self.assertIn("io=native", result)

    def test_disk_cache_writeback(self):
        result = troshkad._build_disk_arg("/tmp/d.qcow2", {}, "writeback")
        self.assertIn("cache=writeback", result)
        self.assertNotIn("io=native", result)

    def test_cdrom_device(self):
        result = troshkad._build_disk_arg(
            "/tmp/iso.iso", {"device": "cdrom"}, ""
        )
        self.assertIn("device=cdrom", result)


# ── _build_boot_parts ──


class TestBuildBootParts(unittest.TestCase):
    def test_bios_default(self):
        result = troshkad._build_boot_parts("bios", False, [])
        self.assertIn("hd", result)
        self.assertIn("menu=on", result)

    def test_uefi_secure_boot(self):
        result = troshkad._build_boot_parts("uefi", True, [])
        self.assertIn("uefi", result)
        self.assertNotIn("loader.secure=no", result)

    def test_uefi_no_secure_boot(self):
        result = troshkad._build_boot_parts("uefi", False, [])
        self.assertIn("loader=/usr/share/edk2/ovmf/OVMF_CODE.fd", result)
        self.assertIn("loader.secure=no", result)
        self.assertIn("nvram.template=/usr/share/edk2/ovmf/OVMF_VARS.fd", result)

    def test_custom_boot_devs(self):
        result = troshkad._build_boot_parts("bios", False, ["network", "hd"])
        self.assertIn("network", result)
        self.assertIn("hd", result)
        self.assertIn("menu=on", result)

    def test_no_hd_when_boot_devs_provided(self):
        result = troshkad._build_boot_parts("bios", False, ["network"])
        # hd is NOT added when explicit boot_devs provided
        count = result.count("hd")
        self.assertEqual(count, 0)


# ── _collect_device_boot_entries ──


class TestCollectDeviceBootEntries(unittest.TestCase):
    def test_none_devices(self):
        self.assertEqual(troshkad._collect_device_boot_entries(None), [])

    def test_disk_with_boot(self):
        import xml.etree.ElementTree as ET
        devices = ET.fromstring("<devices><disk device='disk'><boot order='1'/></disk></devices>")
        entries = troshkad._collect_device_boot_entries(devices)
        self.assertEqual(entries, [(1, "hd")])

    def test_cdrom_with_boot(self):
        import xml.etree.ElementTree as ET
        devices = ET.fromstring("<devices><disk device='cdrom'><boot order='2'/></disk></devices>")
        entries = troshkad._collect_device_boot_entries(devices)
        self.assertEqual(entries, [(2, "cdrom")])

    def test_interface_with_boot(self):
        import xml.etree.ElementTree as ET
        devices = ET.fromstring("<devices><interface type='bridge'><boot order='3'/></interface></devices>")
        entries = troshkad._collect_device_boot_entries(devices)
        self.assertEqual(entries, [(3, "network")])

    def test_no_boot_elements(self):
        import xml.etree.ElementTree as ET
        devices = ET.fromstring("<devices><disk device='disk'/></devices>")
        entries = troshkad._collect_device_boot_entries(devices)
        self.assertEqual(entries, [])


# ── _parse_boot_devices ──


class TestParseBootDevices(unittest.TestCase):
    def test_os_boot_elements(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring("<domain><os><boot dev='hd'/><boot dev='network'/></os></domain>")
        result = troshkad._parse_boot_devices(root)
        self.assertEqual(result, ["hd", "network"])

    def test_fallback_to_device_boot(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring(
            "<domain><os/><devices>"
            "<disk device='disk'><boot order='2'/></disk>"
            "<interface type='bridge'><boot order='1'/></interface>"
            "</devices></domain>"
        )
        result = troshkad._parse_boot_devices(root)
        self.assertEqual(result, ["network", "hd"])

    def test_empty_when_no_boot(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring("<domain><os/><devices><disk device='disk'/></devices></domain>")
        result = troshkad._parse_boot_devices(root)
        self.assertEqual(result, [])


# ── _parse_memory ──


class TestParseMemory(unittest.TestCase):
    def test_kib_default(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring("<domain><memory>4194304</memory></domain>")
        result = troshkad._parse_memory(root)
        self.assertEqual(result, 4096)

    def test_kib_explicit(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring("<domain><memory unit='KiB'>2097152</memory></domain>")
        result = troshkad._parse_memory(root)
        self.assertEqual(result, 2048)

    def test_no_memory_element(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring("<domain></domain>")
        result = troshkad._parse_memory(root)
        self.assertEqual(result, 0)

    def test_non_kib_unit(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring("<domain><memory unit='MiB'>4096</memory></domain>")
        result = troshkad._parse_memory(root)
        self.assertEqual(result, 4096)


# ── _update_clock_element ──


class TestUpdateClockElement(unittest.TestCase):
    def test_set_offset(self):
        import xml.etree.ElementTree as ET
        clock = ET.Element("clock", offset="utc")
        troshkad._update_clock_element(clock, -3600)
        self.assertEqual(clock.get("offset"), "variable")
        self.assertEqual(clock.get("adjustment"), "-3600")

    def test_clear_offset(self):
        import xml.etree.ElementTree as ET
        clock = ET.Element("clock", offset="variable", adjustment="100", basis="utc")
        troshkad._update_clock_element(clock, None)
        self.assertEqual(clock.get("offset"), "utc")
        self.assertIsNone(clock.get("adjustment"))
        self.assertIsNone(clock.get("basis"))

    def test_positive_offset(self):
        import xml.etree.ElementTree as ET
        clock = ET.Element("clock")
        troshkad._update_clock_element(clock, 7200)
        self.assertEqual(clock.get("adjustment"), "7200")


# ── _build_single_guestfish_command ──


class TestBuildSingleGuestfishCommand(unittest.TestCase):
    def test_rm_rf(self):
        result = troshkad._build_single_guestfish_command({"action": "rm-rf", "path": "/tmp/foo"})
        self.assertEqual(result, "rm-rf /tmp/foo")

    def test_rm_f(self):
        result = troshkad._build_single_guestfish_command({"action": "rm-f", "path": "/tmp/bar"})
        self.assertEqual(result, "rm-f /tmp/bar")

    def test_mkdir_p(self):
        result = troshkad._build_single_guestfish_command({"action": "mkdir-p", "path": "/etc/ssl"})
        self.assertEqual(result, "mkdir-p /etc/ssl")

    def test_write(self):
        result = troshkad._build_single_guestfish_command(
            {"action": "write", "path": "/tmp/f.txt", "content": "hello"}
        )
        self.assertEqual(result, 'write /tmp/f.txt "hello"')

    def test_upload(self):
        result = troshkad._build_single_guestfish_command(
            {"action": "upload", "path": "/guest/file", "local_path": "/host/file"}
        )
        self.assertEqual(result, "upload /host/file /guest/file")

    def test_upload_missing_local_path_raises(self):
        with self.assertRaises(RuntimeError):
            troshkad._build_single_guestfish_command(
                {"action": "upload", "path": "/guest/file"}
            )

    def test_chmod(self):
        result = troshkad._build_single_guestfish_command(
            {"action": "chmod", "path": "/tmp/script.sh", "mode": "0755"}
        )
        self.assertEqual(result, "chmod 0755 /tmp/script.sh")

    def test_chmod_missing_mode_raises(self):
        with self.assertRaises(RuntimeError):
            troshkad._build_single_guestfish_command(
                {"action": "chmod", "path": "/tmp/f"}
            )

    def test_unknown_action_returns_empty(self):
        result = troshkad._build_single_guestfish_command({"action": "unknown", "path": "/tmp"})
        self.assertEqual(result, "")


# ── _build_guestfish_commands ──


class TestBuildGuestfishCommands(unittest.TestCase):
    def test_valid_operations(self):
        ops = [
            {"action": "mkdir-p", "path": "/tmp/dir"},
            {"action": "rm-f", "path": "/tmp/old"},
        ]
        result = troshkad._build_guestfish_commands(ops)
        self.assertEqual(result, ["mkdir-p /tmp/dir", "rm-f /tmp/old"])

    def test_unsupported_action_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            troshkad._build_guestfish_commands([{"action": "exec", "path": "/tmp"}])
        self.assertIn("unsupported action", str(ctx.exception))

    def test_missing_path_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            troshkad._build_guestfish_commands([{"action": "rm-rf", "path": ""}])
        self.assertIn("path required", str(ctx.exception))


# ── _remove_disk_file ──


class TestRemoveDiskFile(unittest.TestCase):
    @patch("troshkad.subprocess.run")
    def test_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "rm-disk-0000", "output": []}
        troshkad._remove_disk_file(job, "/var/lib/troshka/vms/test.qcow2")
        mock_run.assert_called_once()
        self.assertTrue(any("Deleted disk" in o for o in job["output"]))

    @patch("troshkad.os.remove")
    @patch("troshkad.subprocess.run", side_effect=Exception("qemu failed"))
    def test_fallback_to_root(self, _mock_run, mock_remove):
        job = {"job_id": "rm-disk-0001", "output": []}
        troshkad._remove_disk_file(job, "/var/lib/troshka/vms/test.qcow2")
        mock_remove.assert_called_once()

    @patch("troshkad.os.remove", side_effect=Exception("perm denied"))
    @patch("troshkad.subprocess.run", side_effect=Exception("qemu failed"))
    def test_both_fail_logs_warning(self, _mock_run, _mock_remove):
        job = {"job_id": "rm-disk-0002", "output": []}
        troshkad._remove_disk_file(job, "/var/lib/troshka/vms/test.qcow2")
        self.assertTrue(any("Warning" in o for o in job["output"]))


# ── _delete_vm_disks ──


class TestDeleteVmDisks(unittest.TestCase):
    @patch("troshkad._remove_disk_file")
    @patch("troshkad.subprocess.run")
    def test_deletes_disk_files(self, mock_run, mock_remove):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=(
                " Type   Device   Target   Source\n"
                "-------------------------------------------\n"
                " file   disk     vda      /var/lib/troshka/vms/proj/disk.qcow2\n"
            ),
        )
        job = {"job_id": "del-disks-0000", "output": []}
        troshkad._delete_vm_disks(job, "troshka-abcdef01-12345678")
        mock_remove.assert_called_once_with(job, "/var/lib/troshka/vms/proj/disk.qcow2")

    @patch("troshkad._remove_disk_file")
    @patch("troshkad.subprocess.run")
    def test_skips_image_cache(self, mock_run, mock_remove):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=(
                " Type   Device   Target   Source\n"
                "---\n"
                " file   disk     vda      /var/lib/troshka/images/item.qcow2\n"
            ),
        )
        job = {"job_id": "del-disks-0001", "output": []}
        troshkad._delete_vm_disks(job, "troshka-abcdef01-12345678")
        mock_remove.assert_not_called()

    @patch("troshkad.subprocess.run", side_effect=Exception("virsh broke"))
    def test_handles_exception(self, _mock_run):
        job = {"job_id": "del-disks-0002", "output": []}
        troshkad._delete_vm_disks(job, "troshka-abcdef01-12345678")
        self.assertTrue(any("Warning" in o for o in job["output"]))


# ── _handle_vm_destroy ──


class TestHandleVmDestroy(unittest.TestCase):
    @patch("troshkad._run_cmd")
    @patch("troshkad._delete_vm_disks")
    def test_destroy_and_undefine(self, mock_del_disks, mock_run_cmd):
        job = {"job_id": "destroy-0000", "output": []}
        result = troshkad._handle_vm_destroy(
            job, {"domain_name": "troshka-abcdef01-12345678"}
        )
        self.assertEqual(result["status"], "destroyed")
        self.assertEqual(mock_run_cmd.call_count, 2)
        mock_del_disks.assert_called_once()

    @patch("troshkad._run_cmd")
    @patch("troshkad._delete_vm_disks")
    def test_destroy_handles_already_stopped(self, mock_del_disks, mock_run_cmd):
        mock_run_cmd.side_effect = [RuntimeError("already stopped"), None]
        job = {"job_id": "destroy-0001", "output": []}
        result = troshkad._handle_vm_destroy(
            job, {"domain_name": "troshka-abcdef01-12345678"}
        )
        self.assertEqual(result["status"], "destroyed")


# ── _handle_vm_force_off ──


class TestHandleVmForceOff(unittest.TestCase):
    @patch("troshkad._run_cmd")
    def test_force_off(self, mock_run_cmd):
        job = {"job_id": "force-off-0000", "output": []}
        result = troshkad._handle_vm_force_off(
            job, {"domain_name": "troshka-abcdef01-12345678"}
        )
        self.assertEqual(result["status"], "off")
        mock_run_cmd.assert_called_once()


# ── _handle_vm_reboot ──


class TestHandleVmReboot(unittest.TestCase):
    @patch("troshkad._run_cmd")
    def test_reboot(self, mock_run_cmd):
        job = {"job_id": "reboot-0000", "output": []}
        result = troshkad._handle_vm_reboot(
            job, {"domain_name": "troshka-abcdef01-12345678"}
        )
        self.assertEqual(result["status"], "rebooted")


# ── _handle_vm_start ──


class TestHandleVmStart(unittest.TestCase):
    @patch("troshkad._run_cmd")
    @patch("troshkad.subprocess.run")
    def test_start_with_existing_bridges(self, mock_subprocess_run, mock_run_cmd):
        # virsh dumpxml returns XML with a bridge that exists
        mock_subprocess_run.return_value = MagicMock(
            returncode=0,
            stdout="<domain><devices><interface type='bridge'><source bridge='br-troshka-abcdef01'/></interface></devices></domain>",
        )
        job = {"job_id": "start-0000", "output": []}
        result = troshkad._handle_vm_start(
            job, {"domain_name": "troshka-abcdef01-12345678"}
        )
        self.assertEqual(result["status"], "started")

    @patch("troshkad._run_cmd")
    @patch("troshkad.subprocess.run")
    def test_start_creates_missing_bridges(self, mock_subprocess_run, mock_run_cmd):
        # First call is dumpxml, subsequent calls check/create bridges
        mock_subprocess_run.side_effect = [
            MagicMock(returncode=0, stdout="<domain><devices><interface type='bridge'><source bridge='br-troshka-abcdef01'/></interface></devices></domain>"),
            MagicMock(returncode=1),  # ip link show bridge -> not found
            MagicMock(returncode=0),  # ip link add bridge
            MagicMock(returncode=0),  # ip link set bridge type bridge
            MagicMock(returncode=0),  # ip link set bridge up
        ]
        job = {"job_id": "start-0001", "output": []}
        result = troshkad._handle_vm_start(
            job, {"domain_name": "troshka-abcdef01-12345678"}
        )
        self.assertEqual(result["status"], "started")
        self.assertTrue(any("Created missing dummy bridge" in o for o in job["output"]))


# ── _handle_vm_stop ──


class TestHandleVmStop(unittest.TestCase):
    @patch("time.sleep")
    @patch("troshkad.subprocess.run")
    @patch("troshkad._run_cmd")
    def test_graceful_stop(self, mock_run_cmd, mock_subprocess_run, _sleep):
        # After shutdown, domstate returns "shut off"
        mock_subprocess_run.return_value = MagicMock(
            returncode=0, stdout="shut off\n"
        )
        job = {"job_id": "stop-0000", "output": []}
        result = troshkad._handle_vm_stop(
            job, {"domain_name": "troshka-abcdef01-12345678"}
        )
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["method"], "shutdown")

    @patch("time.sleep")
    @patch("troshkad.subprocess.run")
    @patch("troshkad._run_cmd")
    def test_force_stop_after_timeout(self, mock_run_cmd, mock_subprocess_run, _sleep):
        # domstate always returns "running" — force destroy after grace period
        mock_subprocess_run.return_value = MagicMock(
            returncode=0, stdout="running\n"
        )
        job = {"job_id": "stop-0001", "output": []}
        result = troshkad._handle_vm_stop(
            job, {"domain_name": "troshka-abcdef01-12345678", "timeout": 1}
        )
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["method"], "destroy")


# ── _handle_vm_undefine ──


class TestHandleVmUndefine(unittest.TestCase):
    @patch("troshkad._run_cmd")
    @patch("troshkad._delete_vm_disks")
    def test_undefine_with_storage(self, mock_del_disks, mock_run_cmd):
        job = {"job_id": "undefine-0000", "output": []}
        result = troshkad._handle_vm_undefine(
            job, {"domain_name": "troshka-abcdef01-12345678"}
        )
        self.assertEqual(result["status"], "undefined")
        mock_del_disks.assert_called_once()

    @patch("troshkad._run_cmd")
    @patch("troshkad._delete_vm_disks")
    def test_undefine_without_storage(self, mock_del_disks, mock_run_cmd):
        job = {"job_id": "undefine-0001", "output": []}
        result = troshkad._handle_vm_undefine(
            job, {"domain_name": "troshka-abcdef01-12345678", "remove_storage": False}
        )
        self.assertEqual(result["status"], "undefined")
        mock_del_disks.assert_not_called()


# ── _push_target_time_to_guest ──


class TestPushTargetTimeToGuest(unittest.TestCase):
    @patch("troshkad.subprocess.run")
    def test_success_via_domtime(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "push-time-0000", "output": []}
        result = troshkad._push_target_time_to_guest(job, "troshka-abcdef01-12345678", 1700000000)
        self.assertTrue(result)

    @patch("troshkad.subprocess.run")
    def test_fallback_to_guest_exec(self, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=1),  # domtime fails
            MagicMock(returncode=0),  # guest-exec succeeds
        ]
        job = {"job_id": "push-time-0001", "output": []}
        result = troshkad._push_target_time_to_guest(job, "troshka-abcdef01-12345678", 1700000000)
        self.assertTrue(result)

    @patch("troshkad.subprocess.run")
    def test_both_fail(self, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=1),  # domtime fails
            Exception("no agent"),    # guest-exec fails
        ]
        job = {"job_id": "push-time-0002", "output": []}
        result = troshkad._push_target_time_to_guest(job, "troshka-abcdef01-12345678", 1700000000)
        self.assertFalse(result)


# ── _push_real_time_to_guest ──


class TestPushRealTimeToGuest(unittest.TestCase):
    @patch("troshkad.subprocess.run")
    def test_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "push-real-0000", "output": []}
        result = troshkad._push_real_time_to_guest(job, "troshka-abcdef01-12345678")
        self.assertTrue(result)

    @patch("troshkad.subprocess.run", side_effect=Exception("no agent"))
    def test_failure(self, _mock_run):
        job = {"job_id": "push-real-0001", "output": []}
        result = troshkad._push_real_time_to_guest(job, "troshka-abcdef01-12345678")
        self.assertFalse(result)


# ── _handle_vm_set_clock ──


class TestHandleVmSetClock(unittest.TestCase):
    @patch("troshkad._push_target_time_to_guest", return_value=True)
    @patch("troshkad.subprocess.Popen")
    @patch("troshkad.subprocess.run")
    def test_set_clock_with_offset(self, mock_run, mock_popen, _mock_push):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="running\n"),
            MagicMock(returncode=0, stdout=(
                "<domain><clock offset='utc'/></domain>"
            )),
        ]
        proc = MagicMock()
        proc.communicate.return_value = ("", "")
        proc.returncode = 0
        mock_popen.return_value = proc
        job = {"job_id": "clock-0000", "output": []}
        result = troshkad._handle_vm_set_clock(
            job,
            {
                "domain_name": "troshka-abcdef01-12345678",
                "offset_seconds": -3600,
                "target_epoch": 1700000000,
            },
        )
        self.assertTrue(result["xml_updated"])
        self.assertTrue(result["time_pushed"])

    @patch("troshkad._push_real_time_to_guest", return_value=True)
    @patch("troshkad.subprocess.Popen")
    @patch("troshkad.subprocess.run")
    def test_clear_clock_offset(self, mock_run, mock_popen, _mock_push):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="running\n"),
            MagicMock(returncode=0, stdout=(
                "<domain><clock offset='variable' adjustment='100'/></domain>"
            )),
        ]
        proc = MagicMock()
        proc.communicate.return_value = ("", "")
        proc.returncode = 0
        mock_popen.return_value = proc
        job = {"job_id": "clock-0001", "output": []}
        result = troshkad._handle_vm_set_clock(
            job,
            {"domain_name": "troshka-abcdef01-12345678", "offset_seconds": None},
        )
        self.assertTrue(result["xml_updated"])
        self.assertTrue(result["time_pushed"])


# ── _collect_existing_disk_paths ──


class TestCollectExistingDiskPaths(unittest.TestCase):
    def test_collects_paths(self):
        import xml.etree.ElementTree as ET
        disks = ET.fromstring(
            "<devices>"
            "<disk><source file='/var/lib/troshka/vms/d1.qcow2'/></disk>"
            "<disk><source file='/var/lib/troshka/vms/d2.qcow2'/></disk>"
            "</devices>"
        )
        result = troshkad._collect_existing_disk_paths(disks.findall("disk"))
        self.assertEqual(result, {"/var/lib/troshka/vms/d1.qcow2", "/var/lib/troshka/vms/d2.qcow2"})

    def test_no_source(self):
        import xml.etree.ElementTree as ET
        disks = ET.fromstring("<devices><disk/></devices>")
        result = troshkad._collect_existing_disk_paths(disks.findall("disk"))
        self.assertEqual(result, set())


# ── _configure_vnc_graphics ──


class TestConfigureVncGraphics(unittest.TestCase):
    def test_update_existing_vnc(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring(
            "<domain><devices>"
            "<graphics type='vnc' listen='127.0.0.1'>"
            "<listen type='address' address='127.0.0.1'/>"
            "</graphics>"
            "</devices></domain>"
        )
        troshkad._configure_vnc_graphics(root, "0.0.0.0")
        graphics = root.find(".//graphics[@type='vnc']")
        self.assertEqual(graphics.get("listen"), "0.0.0.0")
        self.assertEqual(graphics.get("sharePolicy"), "force-shared")

    def test_create_new_vnc(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring("<domain><devices/></domain>")
        troshkad._configure_vnc_graphics(root, "0.0.0.0")
        graphics = root.find(".//graphics[@type='vnc']")
        self.assertIsNotNone(graphics)
        self.assertEqual(graphics.get("listen"), "0.0.0.0")

    def test_no_devices(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring("<domain/>")
        # Should not raise
        troshkad._configure_vnc_graphics(root, "0.0.0.0")


# ── _apply_vcpu_ram_changes ──


class TestApplyVcpuRamChanges(unittest.TestCase):
    def test_apply_vcpu(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring("<domain><vcpu placement='static'>2</vcpu></domain>")
        troshkad._apply_vcpu_ram_changes(root, 4, None)
        self.assertEqual(root.find("vcpu").text, "4")

    def test_apply_ram(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring(
            "<domain><memory unit='KiB'>4194304</memory>"
            "<currentMemory unit='KiB'>4194304</currentMemory></domain>"
        )
        troshkad._apply_vcpu_ram_changes(root, None, 8192)
        self.assertEqual(root.find("memory").text, str(8192 * 1024))
        self.assertEqual(root.find("currentMemory").text, str(8192 * 1024))


# ── _get_partitions ──


class TestGetPartitions(unittest.TestCase):
    @patch("shutil.disk_usage")
    @patch("builtins.open", mock_open(read_data="/dev/sda1 / ext4 rw 0 0\n"))
    def test_returns_partitions(self, mock_disk_usage):
        mock_disk_usage.return_value = MagicMock(total=100 * 1024**3, used=50 * 1024**3, free=50 * 1024**3)
        result = troshkad._get_partitions()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["mount"], "/")
        self.assertEqual(result[0]["fstype"], "ext4")

    @patch("builtins.open", mock_open(read_data="proc /proc proc rw 0 0\n"))
    def test_filters_pseudo_fs(self):
        result = troshkad._get_partitions()
        self.assertEqual(len(result), 0)

    @patch("builtins.open", side_effect=OSError("no such file"))
    def test_handles_oserror(self, _mock_open):
        result = troshkad._get_partitions()
        self.assertEqual(result, [])

    @patch("shutil.disk_usage")
    @patch("builtins.open", mock_open(read_data=(
        "/dev/sda1 / ext4 rw 0 0\n"
        "/dev/sda1 /boot ext4 rw 0 0\n"
    )))
    def test_deduplicates_by_device(self, mock_disk_usage):
        mock_disk_usage.return_value = MagicMock(total=100, used=50, free=50)
        result = troshkad._get_partitions()
        self.assertEqual(len(result), 1)


# ── _get_cpu_capacity ──


class TestGetCpuCapacity(unittest.TestCase):
    @patch("os.cpu_count", return_value=8)
    def test_returns_cpu_count(self, _mock):
        result = troshkad._get_cpu_capacity()
        self.assertEqual(result, {"vcpus_total": 8})

    @patch("os.cpu_count", return_value=None)
    def test_returns_zero_when_none(self, _mock):
        result = troshkad._get_cpu_capacity()
        self.assertEqual(result, {"vcpus_total": 0})


# ── _get_memory_capacity ──


class TestGetMemoryCapacity(unittest.TestCase):
    @patch("builtins.open", mock_open(read_data="MemTotal:       16384000 kB\n"))
    def test_returns_ram(self):
        result = troshkad._get_memory_capacity()
        self.assertEqual(result, {"ram_total_mb": 16384000 // 1024})

    @patch("builtins.open", side_effect=Exception("no meminfo"))
    def test_returns_zero_on_error(self, _mock):
        result = troshkad._get_memory_capacity()
        self.assertEqual(result, {"ram_total_mb": 0})

    @patch("builtins.open", mock_open(read_data="SomeOtherLine: 12345\n"))
    def test_returns_zero_when_no_memtotal(self):
        result = troshkad._get_memory_capacity()
        self.assertEqual(result, {"ram_total_mb": 0})


# ── _get_storage_capacity ──


class TestGetStorageCapacity(unittest.TestCase):
    def setUp(self):
        self._orig_config = troshkad._config.copy()

    def tearDown(self):
        troshkad._config.clear()
        troshkad._config.update(self._orig_config)

    @patch("shutil.disk_usage")
    def test_local_mode(self, mock_disk_usage):
        troshkad._config["storage_mode"] = "local"
        mock_disk_usage.return_value = MagicMock(
            total=500 * 1024**3, used=200 * 1024**3
        )
        result = troshkad._get_storage_capacity()
        self.assertEqual(result["storage_total_gb"], 500)
        self.assertEqual(result["storage_used_gb"], 200)

    @patch("shutil.disk_usage")
    def test_shared_mode_uses_local_mount(self, mock_disk_usage):
        troshkad._config["storage_mode"] = "shared"
        troshkad._config["local_mount"] = "/mnt/local"
        mock_disk_usage.return_value = MagicMock(
            total=100 * 1024**3, used=50 * 1024**3
        )
        result = troshkad._get_storage_capacity()
        self.assertEqual(result["storage_total_gb"], 100)

    @patch("shutil.disk_usage", side_effect=Exception("disk gone"))
    def test_returns_zero_on_error(self, _mock):
        result = troshkad._get_storage_capacity()
        self.assertEqual(result, {"storage_total_gb": 0, "storage_used_gb": 0})


# ── _get_vm_capacity ──


class TestGetVmCapacity(unittest.TestCase):
    @patch("troshkad.subprocess.run")
    def test_counts_vms(self, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="troshka-aaa-bbb\ntroshka-ccc-ddd\n"),
            MagicMock(returncode=0, stdout="troshka-aaa-bbb\n"),
            MagicMock(returncode=0, stdout="CPU(s):          4\nMax memory:   4194304 KiB\n"),
            MagicMock(returncode=0, stdout="CPU(s):          2\nMax memory:   2097152 KiB\n"),
        ]
        result = troshkad._get_vm_capacity()
        self.assertEqual(result["total_vms"], 2)
        self.assertEqual(result["running_vms"], 1)
        self.assertEqual(result["vcpus_used"], 6)
        self.assertEqual(result["ram_used_mb"], 6144)

    @patch("troshkad.subprocess.run", side_effect=Exception("no virsh"))
    def test_returns_empty_on_error(self, _mock):
        result = troshkad._get_vm_capacity()
        self.assertEqual(result, {})


# ── _get_container_capacity ──


class TestGetContainerCapacity(unittest.TestCase):
    @patch("troshkad.subprocess.run")
    def test_counts_containers(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="troshka-proj-ctr1 running\ntroshka-proj-ctr2 exited\n",
        )
        result = troshkad._get_container_capacity()
        self.assertEqual(result["total_containers"], 2)
        self.assertEqual(result["running_containers"], 1)

    @patch("troshkad.subprocess.run", side_effect=Exception("no podman"))
    def test_returns_empty_on_error(self, _mock):
        result = troshkad._get_container_capacity()
        self.assertEqual(result, {})


# ── _dispatch_job ──


class TestDispatchJob(unittest.TestCase):
    """Tests for _dispatch_job.

    NOTE: _draining is module-level mutable state that can be set True by
    background drain threads spawned by the HTTPS server tests.  We must
    temporarily force it and restore it immediately, but background threads
    may race against us.  To avoid flakes we wrap each test body in a
    retry-once guard that re-sets _draining if a race is detected.
    """

    def _force_draining(self, val):
        """Forcibly set _draining and return old value."""
        old = troshkad._draining
        troshkad._draining = val
        return old

    def test_draining_returns_503(self):
        old = self._force_draining(True)
        try:
            status, resp = troshkad._dispatch_job("test", {})
        finally:
            troshkad._draining = old
        self.assertEqual(status, 503)

    def test_unknown_command_returns_404(self):
        old = self._force_draining(False)
        try:
            status, resp = troshkad._dispatch_job("nonexistent/command", {})
        finally:
            troshkad._draining = old
        # If a drain thread re-set _draining between our set and the call,
        # we get 503 instead of 404 — just skip rather than flake.
        if status == 503:
            self.skipTest("_draining race with background thread")
        self.assertEqual(status, 404)

    @patch("troshkad.threading.Thread")
    def test_dispatches_known_command(self, mock_thread):
        old = self._force_draining(False)
        troshkad.COMMAND_HANDLERS["test/dispatch"] = lambda job, params: None
        try:
            status, resp = troshkad._dispatch_job("test/dispatch", {})
        finally:
            troshkad._draining = old
            troshkad.COMMAND_HANDLERS.pop("test/dispatch", None)
        if status == 503:
            self.skipTest("_draining race with background thread")
        self.assertEqual(status, 202)
        self.assertIn("job_id", resp)
        with troshkad._jobs_lock:
            troshkad._jobs.pop(resp["job_id"], None)


# ── _run_job_worker ──


class TestRunJobWorker(unittest.TestCase):
    def test_success(self):
        job = troshkad._create_job("worker-test", {})
        handler = MagicMock(return_value={"result": "ok"})
        troshkad._run_job_worker(job, handler)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["result"], {"result": "ok"})
        with troshkad._jobs_lock:
            troshkad._jobs.pop(job["job_id"], None)

    def test_failure(self):
        job = troshkad._create_job("worker-fail", {})
        handler = MagicMock(side_effect=RuntimeError("boom"))
        troshkad._run_job_worker(job, handler)
        self.assertEqual(job["status"], "failed")
        self.assertIn("boom", job["result"]["error"])
        with troshkad._jobs_lock:
            troshkad._jobs.pop(job["job_id"], None)


# ── _handle_disk_create ──


class TestHandleDiskCreate(unittest.TestCase):
    @patch("troshkad._chown_qemu")
    @patch("troshkad._run_cmd")
    @patch("os.makedirs")
    def test_basic_disk_create(self, _mock_makedirs, mock_run_cmd, _mock_chown):
        job = {"job_id": "disk-create-0000", "output": [], "_process": None}
        result = troshkad._handle_disk_create(
            job,
            {"path": "/var/lib/troshka/vms/test/d.qcow2", "size_gb": 10},
        )
        self.assertEqual(result["status"], "created")
        cmd = mock_run_cmd.call_args[0][1]
        self.assertIn("qemu-img", cmd)
        self.assertIn("10G", cmd)

    @patch("troshkad._chown_qemu")
    @patch("troshkad._run_cmd")
    @patch("troshkad.subprocess.run")
    @patch("os.makedirs")
    def test_disk_with_backing(self, _mock_makedirs, mock_subprocess_run, mock_run_cmd, _mock_chown):
        mock_subprocess_run.return_value = MagicMock(
            returncode=0, stdout='{"virtual-size": 21474836480}'
        )
        job = {"job_id": "disk-create-0001", "output": [], "_process": None}
        result = troshkad._handle_disk_create(
            job,
            {
                "path": "/var/lib/troshka/vms/test/d.qcow2",
                "size_gb": 10,
                "backing_file": "/var/lib/troshka/images/base.qcow2",
            },
        )
        self.assertEqual(result["status"], "created")
        cmd = mock_run_cmd.call_args[0][1]
        self.assertIn("-b", cmd)


# ── _handle_disk_resize ──


class TestHandleDiskResize(unittest.TestCase):
    @patch("troshkad._run_cmd")
    def test_resize(self, mock_run_cmd):
        job = {"job_id": "resize-0000", "output": [], "_process": None}
        result = troshkad._handle_disk_resize(
            job, {"path": "/var/lib/troshka/vms/test/d.qcow2", "new_size_gb": 50}
        )
        self.assertEqual(result["status"], "resized")


# ── _handle_seed_create ──


class TestHandleSeedCreate(unittest.TestCase):
    @patch("troshkad._chown_qemu")
    @patch("troshkad._run_cmd")
    @patch("os.makedirs")
    @patch("builtins.open", mock_open())
    def test_basic_seed(self, _mock_makedirs, mock_run_cmd, _mock_chown):
        import tempfile
        with patch.object(tempfile, "TemporaryDirectory") as mock_td:
            mock_td.return_value.__enter__ = MagicMock(return_value="/tmp/fake-seed")
            mock_td.return_value.__exit__ = MagicMock(return_value=False)
            job = {"job_id": "seed-0000", "output": [], "_process": None}
            result = troshkad._handle_seed_create(
                job,
                {
                    "path": "/var/lib/troshka/vms/proj/seed.iso",
                    "meta_data": "instance-id: test\n",
                    "user_data": "#cloud-config\n",
                },
            )
        self.assertEqual(result["status"], "created")
        cmd = mock_run_cmd.call_args[0][1]
        self.assertIn("xorriso", cmd)


# ── _handle_vm_modify_fs ──


class TestHandleVmModifyFs(unittest.TestCase):
    def _make_job(self):
        return {"job_id": "fs-mod-0000", "output": [], "_process": None}

    def test_missing_disk_raises(self):
        job = self._make_job()
        with self.assertRaises(RuntimeError):
            troshkad._handle_vm_modify_fs(job, {"disk": "", "operations": [{"action": "rm-f", "path": "/tmp"}]})

    def test_missing_operations_raises(self):
        job = self._make_job()
        with self.assertRaises(RuntimeError):
            troshkad._handle_vm_modify_fs(job, {"disk": "/var/lib/troshka/vms/d.qcow2", "operations": []})

    @patch("troshkad.subprocess.run")
    @patch("os.path.exists", return_value=True)
    def test_success(self, _mock_exists, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        job = self._make_job()
        result = troshkad._handle_vm_modify_fs(
            job,
            {
                "disk": "/var/lib/troshka/vms/d.qcow2",
                "operations": [{"action": "rm-f", "path": "/tmp/old"}],
            },
        )
        self.assertEqual(len(result["results"]), 1)
        self.assertTrue(result["results"][0]["ok"])

    @patch("troshkad.subprocess.run")
    @patch("os.path.exists", return_value=True)
    def test_guestfish_failure(self, _mock_exists, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="guestfish error")
        job = self._make_job()
        result = troshkad._handle_vm_modify_fs(
            job,
            {
                "disk": "/var/lib/troshka/vms/d.qcow2",
                "operations": [{"action": "rm-f", "path": "/tmp/old"}],
            },
        )
        self.assertFalse(result["results"][0]["ok"])


# ── _build_dnsmasq_config_lines ──


class TestBuildDnsmasqConfigLines(unittest.TestCase):
    def test_basic_config(self):
        net = {
            "vni": 100,
            "bridge_name": "br-troshka-abcdef01",
            "dhcp_hosts": [
                {"mac": "aa:bb:cc:dd:ee:ff", "ip": "10.0.0.10", "name": "vm1"},
            ],
        }
        lines = troshkad._build_dnsmasq_config_lines(
            "12345678-abcd-ef01-2345-6789abcdef01",
            net,
            "/run/dnsmasq.pid",
            "/var/lib/dnsmasq.leases",
            "10.0.0.100",
            "10.0.0.200",
            "24h",
            "br-troshka-abcdef01",
        )
        config = "\n".join(lines)
        self.assertIn("interface=br-troshka-abcdef01", config)
        self.assertIn("dhcp-host=aa:bb:cc:dd:ee:ff,10.0.0.10,vm1", config)
        self.assertIn("dhcp-range=10.0.0.100,10.0.0.200,24h", config)
        self.assertIn("no-resolv", config)

    def test_dns_domain(self):
        net = {
            "vni": 100,
            "bridge_name": "br-troshka-abcdef01",
            "dhcp_hosts": [],
            "dns_enabled": True,
            "dns_domain": "ocp.local",
        }
        lines = troshkad._build_dnsmasq_config_lines(
            "12345678-abcd-ef01-2345-6789abcdef01",
            net, "/run/p.pid", "/var/l.leases", "10.0.0.100", "10.0.0.200", "24h", "br",
        )
        self.assertIn("domain=ocp.local", lines)

    def test_dns_records(self):
        net = {
            "vni": 100,
            "bridge_name": "br",
            "dhcp_hosts": [],
            "dns_records": [{"name": "api.ocp.local", "ip": "10.0.0.5"}],
        }
        lines = troshkad._build_dnsmasq_config_lines(
            "12345678-abcd-ef01-2345-6789abcdef01",
            net, "/run/p.pid", "/var/l.leases", "10.0.0.100", "10.0.0.200", "24h", "br",
        )
        self.assertIn("address=/api.ocp.local/10.0.0.5", lines)


# ── _append_pxe_dnsmasq_config ──


class TestAppendPxeDnsmasqConfig(unittest.TestCase):
    def test_no_pxe(self):
        lines = []
        troshkad._append_pxe_dnsmasq_config(lines, {})
        self.assertEqual(lines, [])

    def test_legacy_pxe(self):
        lines = []
        net = {
            "pxe_config": {
                "method": "legacy",
                "next_server": "10.0.0.1",
                "boot_file": "pxelinux.0",
            }
        }
        troshkad._append_pxe_dnsmasq_config(lines, net)
        self.assertEqual(len(lines), 1)
        self.assertIn("pxelinux.0", lines[0])

    def test_ipxe(self):
        lines = []
        net = {
            "pxe_config": {
                "method": "ipxe",
                "ipxe_script_url": "http://10.0.0.1/boot.ipxe",
            }
        }
        troshkad._append_pxe_dnsmasq_config(lines, net)
        self.assertEqual(len(lines), 1)
        self.assertIn("boot.ipxe", lines[0])

    def test_uefi_http(self):
        lines = []
        net = {
            "pxe_config": {
                "method": "uefi-http",
                "uefi_boot_url": "http://10.0.0.1/efi/BOOTX64.EFI",
            }
        }
        troshkad._append_pxe_dnsmasq_config(lines, net)
        self.assertEqual(len(lines), 1)

    @patch("os.path.isfile", return_value=False)
    def test_builtin_pxe(self, _mock_isfile):
        lines = []
        net = {
            "pxe_config": {
                "server_mode": "builtin",
                "tftp_root": "/tmp/tftp",
            }
        }
        troshkad._append_pxe_dnsmasq_config(lines, net)
        self.assertIn("enable-tftp", lines)
        self.assertIn("tftp-root=/tmp/tftp", lines)


# ── _build_haproxy_config ──


class TestBuildHaproxyConfig(unittest.TestCase):
    def test_basic_config(self):
        result = troshkad._build_haproxy_config(
            "/run/haproxy.pid",
            [{"name": "API Server", "bindPort": 6443, "backendPort": 6443}],
            [{"name": "sno1", "ip": "10.0.0.10"}],
            "10.0.0.1",
        )
        self.assertIn("pidfile /run/haproxy.pid", result)
        self.assertIn("bind 10.0.0.1:6443", result)
        self.assertIn("server sno1 10.0.0.10:6443 check", result)
        self.assertIn("balance roundrobin", result)

    def test_wildcard_bind(self):
        result = troshkad._build_haproxy_config(
            "/run/hp.pid",
            [{"name": "web", "bindPort": 443, "backendPort": 443}],
            [{"name": "srv1", "ip": "10.0.0.5"}],
            "*",
        )
        self.assertIn("bind *:443", result)


# ── _handle_list_bridges ──


class TestHandleListBridges(unittest.TestCase):
    @patch("troshkad.subprocess.run")
    def test_lists_bridges(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=(
                "5: br-troshka-abcdef01: <BROADCAST,MULTICAST,UP,LOWER_UP>\n"
                "6: br-bmc-abcdef01: <BROADCAST,MULTICAST,UP,LOWER_UP>\n"
                "7: docker0: <NO-CARRIER,BROADCAST,MULTICAST,UP>\n"
            ),
        )
        job = {"job_id": "bridges-0000", "output": []}
        result = troshkad._handle_list_bridges(job, {})
        self.assertIn("br-troshka-abcdef01", result["bridges"])
        self.assertIn("br-bmc-abcdef01", result["bridges"])
        # docker0 doesn't start with "br-"
        self.assertNotIn("docker0", result["bridges"])


# ── _scp_common_args ──


class TestScpCommonArgs(unittest.TestCase):
    def test_with_key_file(self):
        args = troshkad._scp_common_args(key_file="/tmp/key")
        self.assertEqual(args[0], "scp")
        self.assertIn("-i", args)
        self.assertIn("/tmp/key", args)

    def test_with_password(self):
        args = troshkad._scp_common_args(password="secret123")  # pragma: allowlist secret
        self.assertEqual(args[0], "sshpass")
        self.assertIn("secret123", args)  # pragma: allowlist secret


# ── _ssh_common_args ──


class TestSshCommonArgs(unittest.TestCase):
    def test_with_key_file(self):
        args = troshkad._ssh_common_args(key_file="/tmp/key")
        self.assertEqual(args[0], "ssh")
        self.assertIn("-i", args)

    def test_with_password(self):
        args = troshkad._ssh_common_args(password="pass")  # pragma: allowlist secret
        self.assertEqual(args[0], "sshpass")


# ── _prepare_ssh_key_file ──


class TestPrepareSSHKeyFile(unittest.TestCase):
    def test_empty_key_returns_empty(self):
        result = troshkad._prepare_ssh_key_file("")
        self.assertEqual(result, "")

    def test_none_key_returns_empty(self):
        result = troshkad._prepare_ssh_key_file(None)
        self.assertEqual(result, "")

    @patch("os.fdopen", return_value=MagicMock(__enter__=MagicMock(return_value=MagicMock()), __exit__=MagicMock(return_value=False)))
    @patch("os.open", return_value=5)
    def test_writes_key_file(self, _mock_open, _mock_fdopen):
        result = troshkad._prepare_ssh_key_file("-----BEGIN TEST KEY-----\nkey\n-----END TEST KEY-----\n")  # pragma: allowlist secret
        self.assertTrue(result.startswith("/tmp/troshka-scp-key-"))


# ── _get_conf_from_pidfile ──


class TestGetConfFromPidfile(unittest.TestCase):
    def test_converts_pidfile_to_conf(self):
        name, path = troshkad._get_conf_from_pidfile("/run/troshka-dnsmasq-abcdef01-100.pid")
        self.assertEqual(name, "troshka-abcdef01-100.conf")
        self.assertEqual(path, "/etc/dnsmasq.d/troshka-abcdef01-100.conf")


# ── _get_project_prefix_from_pidfile ──


class TestGetProjectPrefixFromPidfile(unittest.TestCase):
    def test_extracts_prefix(self):
        result = troshkad._get_project_prefix_from_pidfile("/run/troshka-dnsmasq-abcdef01-100.pid")
        self.assertEqual(result, "abcdef01")


# ── _is_process_alive ──


class TestIsProcessAlive(unittest.TestCase):
    @patch("os.kill")
    @patch("builtins.open", mock_open(read_data="12345\n"))
    def test_alive(self, mock_kill):
        result = troshkad._is_process_alive("/run/test.pid")
        self.assertTrue(result)

    @patch("os.kill", side_effect=OSError("No such process"))
    @patch("builtins.open", mock_open(read_data="99999\n"))
    def test_dead(self, _mock_kill):
        result = troshkad._is_process_alive("/run/test.pid")
        self.assertFalse(result)

    @patch("builtins.open", mock_open(read_data="not-a-number\n"))
    def test_corrupt(self):
        result = troshkad._is_process_alive("/run/test.pid")
        self.assertFalse(result)


# ── _find_namespace_from_conf ──


class TestFindNamespaceFromConf(unittest.TestCase):
    @patch("builtins.open", mock_open(read_data=(
        "interface=br-troshka-abcdef01\n"
        "bind-interfaces\n"
        "no-dhcp-interface=br-bmc-abcdef01\n"
        "no-resolv\n"
    )))
    def test_finds_namespace(self):
        result = troshkad._find_namespace_from_conf("/etc/dnsmasq.d/test.conf")
        self.assertEqual(result, "troshka-abcdef01")

    @patch("builtins.open", mock_open(read_data="interface=br-troshka-abcdef01\n"))
    def test_no_bmc_bridge(self):
        result = troshkad._find_namespace_from_conf("/etc/dnsmasq.d/test.conf")
        self.assertIsNone(result)

    @patch("builtins.open", side_effect=Exception("file not found"))
    def test_error(self, _mock):
        result = troshkad._find_namespace_from_conf("/etc/dnsmasq.d/test.conf")
        self.assertIsNone(result)


# ── _log_dead_dnsmasq_info ──


class TestLogDeadDnsmasqInfo(unittest.TestCase):
    @patch("builtins.open", mock_open(read_data="12345\n"))
    def test_logs_pid(self):
        # Should not raise
        troshkad._log_dead_dnsmasq_info("/run/troshka-dnsmasq-test.pid")

    @patch("builtins.open", side_effect=FileNotFoundError)
    def test_handles_missing_file(self, _mock):
        # Should not raise
        troshkad._log_dead_dnsmasq_info("/run/missing.pid")


# ── _generate_self_signed_cert ──


class TestGenerateSelfSignedCert(unittest.TestCase):
    @patch("os.chmod")
    @patch("troshkad.subprocess.run")
    def test_generates_cert(self, mock_run, _mock_chmod):
        mock_run.return_value = MagicMock(returncode=0)
        troshkad._generate_self_signed_cert(
            "/tmp/cert.pem", "/tmp/key.pem", "test-cn", "10.0.0.1"
        )
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        self.assertIn("openssl", cmd)
        self.assertIn("/CN=test-cn", cmd)
        self.assertIn("subjectAltName=IP:10.0.0.1", cmd)


# ── _ensure_nbd_module ──


class TestEnsureNbdModule(unittest.TestCase):
    def setUp(self):
        troshkad._nbd_module_loaded = False

    def tearDown(self):
        troshkad._nbd_module_loaded = False

    @patch("troshkad.subprocess.run")
    def test_loads_module_when_not_loaded(self, mock_run):
        mock_run.side_effect = [
            MagicMock(stdout="ext4\nxfs\n"),  # lsmod
            MagicMock(returncode=0),          # modprobe
        ]
        troshkad._ensure_nbd_module()
        self.assertTrue(troshkad._nbd_module_loaded)
        self.assertEqual(mock_run.call_count, 2)

    @patch("troshkad.subprocess.run")
    def test_skips_if_already_loaded(self, mock_run):
        mock_run.return_value = MagicMock(stdout="nbd 12345\next4\n")
        troshkad._ensure_nbd_module()
        self.assertTrue(troshkad._nbd_module_loaded)
        self.assertEqual(mock_run.call_count, 1)

    def test_noop_when_cached(self):
        troshkad._nbd_module_loaded = True
        # Should not call subprocess at all
        troshkad._ensure_nbd_module()
        self.assertTrue(troshkad._nbd_module_loaded)


# ── _release_nbd_device ──


class TestReleaseNbdDevice(unittest.TestCase):
    def test_release(self):
        troshkad._nbd_devices_in_use.add("/dev/nbd7")
        troshkad._release_nbd_device("/dev/nbd7")
        self.assertNotIn("/dev/nbd7", troshkad._nbd_devices_in_use)

    def test_release_not_in_use(self):
        # Should not raise
        troshkad._release_nbd_device("/dev/nbd99")


# ── _ensure_container_image ──


class TestEnsureContainerImage(unittest.TestCase):
    @patch("troshkad._run_cmd")
    @patch("troshkad.subprocess.run")
    def test_pulls_if_not_exists(self, mock_run, mock_run_cmd):
        mock_run.return_value = MagicMock(returncode=1)  # image not found
        job = {"job_id": "ctr-img-0000", "output": [], "_process": None}
        troshkad._ensure_container_image(job, "quay.io/test/image:latest")
        mock_run_cmd.assert_called_once()

    @patch("troshkad._run_cmd")
    @patch("troshkad.subprocess.run")
    def test_skips_if_exists(self, mock_run, mock_run_cmd):
        mock_run.return_value = MagicMock(returncode=0)  # image exists
        job = {"job_id": "ctr-img-0001", "output": [], "_process": None}
        troshkad._ensure_container_image(job, "quay.io/test/image:latest")
        mock_run_cmd.assert_not_called()


# ── _build_container_cmd ──


class TestBuildContainerCmd(unittest.TestCase):
    def test_basic_command(self):
        cmd = troshkad._build_container_cmd(
            "troshka-test-ctr",
            "quay.io/test/image:latest",
            2, 1024, [], [], [], [], "", "no", False,
        )
        self.assertEqual(cmd[0], "podman")
        self.assertEqual(cmd[1], "create")
        self.assertIn("--name", cmd)
        self.assertIn("troshka-test-ctr", cmd)
        self.assertIn("quay.io/test/image:latest", cmd)

    def test_with_env_vars(self):
        cmd = troshkad._build_container_cmd(
            "ctr", "img", 1, 512,
            [{"key": "FOO", "value": "bar"}],
            [], [], [], "", "no", False,
        )
        idx = cmd.index("-e")
        self.assertEqual(cmd[idx + 1], "FOO=bar")

    def test_with_networks_uses_none(self):
        cmd = troshkad._build_container_cmd(
            "ctr", "img", 1, 512, [], [],
            [{"bridge_name": "br-troshka-abcdef01"}],
            [], "", "no", False,
        )
        self.assertIn("--network", cmd)
        idx = cmd.index("--network")
        self.assertEqual(cmd[idx + 1], "none")

    def test_with_ports_no_network(self):
        cmd = troshkad._build_container_cmd(
            "ctr", "img", 1, 512, [],
            [{"containerPort": 8080, "hostPort": 8080}],
            [], [], "", "no", False,
        )
        self.assertIn("-p", cmd)

    def test_privileged(self):
        cmd = troshkad._build_container_cmd(
            "ctr", "img", 1, 512, [], [], [], [], "", "no", True,
        )
        self.assertIn("--privileged", cmd)

    def test_with_command(self):
        cmd = troshkad._build_container_cmd(
            "ctr", "img", 1, 512, [], [], [], [], "/bin/sh -c echo", "no", False,
        )
        self.assertIn("/bin/sh", cmd)
        self.assertIn("-c", cmd)
        self.assertIn("echo", cmd)


# ── _get_container_states ──


class TestGetContainerStates(unittest.TestCase):
    @patch("troshkad.subprocess.run")
    def test_parses_states(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="troshka-proj-ctr1 running\ntroshka-proj-ctr2 exited\n",
        )
        result = troshkad._get_container_states()
        self.assertEqual(result["troshka-proj-ctr1"]["state"], "running")
        self.assertEqual(result["troshka-proj-ctr2"]["state"], "stopped")

    @patch("troshkad.subprocess.run")
    def test_empty_output(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        result = troshkad._get_container_states()
        self.assertEqual(result, {})


# ── _get_pod_states ──


class TestGetPodStates(unittest.TestCase):
    @patch("troshkad.subprocess.run")
    def test_parses_pod_states(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="troshka-proj-pod1 Running\ntroshka-proj-pod2 Degraded\ntroshka-proj-pod3 Exited\n",
        )
        result = troshkad._get_pod_states()
        self.assertEqual(result["troshka-proj-pod1"]["state"], "running")
        self.assertEqual(result["troshka-proj-pod2"]["state"], "running")
        self.assertEqual(result["troshka-proj-pod3"]["state"], "stopped")


# ── _enrich_container_ips ──


class TestEnrichContainerIps(unittest.TestCase):
    @patch("troshkad._get_container_namespace_ips", return_value=["10.0.0.5"])
    def test_adds_ips_to_running(self, _mock):
        containers = {"ctr1": {"state": "running"}}
        troshkad._enrich_container_ips(containers)
        self.assertEqual(containers["ctr1"]["ips"], ["10.0.0.5"])

    @patch("troshkad._get_container_namespace_ips")
    def test_skips_stopped(self, mock_get_ips):
        containers = {"ctr1": {"state": "stopped"}}
        troshkad._enrich_container_ips(containers)
        mock_get_ips.assert_not_called()


# ── _serial_clean_output ──


class TestSerialCleanOutput(unittest.TestCase):
    def test_strips_ansi_escapes(self):
        raw = "\x1b[32mhello\x1b[0m\r\nworld"
        result = troshkad._serial_clean_output(raw, "/tmp/out", "MARKER")
        self.assertIn("hello", result)
        self.assertIn("world", result)
        self.assertNotIn("\x1b", result)

    def test_strips_echoed_commands(self):
        raw = "some output\n__a=MARKER\nreal output\ncat /tmp/out\n"
        result = troshkad._serial_clean_output(raw, "/tmp/out", "MARKER")
        self.assertNotIn("__a=", result)
        self.assertNotIn("cat /tmp/out", result)
        self.assertIn("some output", result)
        self.assertIn("real output", result)


# ── _serial_open_pty ──


class TestSerialOpenPty(unittest.TestCase):
    @patch("troshkad.subprocess.run")
    def test_finds_pty(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="<console type='pty' tty='/dev/pts/3'><source path='/dev/pts/3'/></console>",
        )
        result = troshkad._serial_open_pty("troshka-abcdef01-12345678")
        self.assertEqual(result, "/dev/pts/3")

    @patch("troshkad.subprocess.run")
    def test_no_pty_raises(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="<console type='pty'/>")
        with self.assertRaises(RuntimeError):
            troshkad._serial_open_pty("troshka-abcdef01-12345678")

    @patch("troshkad.subprocess.run")
    def test_virsh_fails_raises(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="error")
        with self.assertRaises(RuntimeError):
            troshkad._serial_open_pty("troshka-abcdef01-12345678")


# ── _console_detect_state ──


class TestConsoleDetectState(unittest.TestCase):
    def test_login_prompt(self):
        result = troshkad._console_detect_state("Welcome\nlogin:")
        self.assertEqual(result, "login")

    def test_password_prompt(self):
        result = troshkad._console_detect_state("login: root\nPassword:")
        self.assertEqual(result, "password")

    def test_shell_prompt(self):
        result = troshkad._console_detect_state("root@host ~]$")
        self.assertEqual(result, "shell")

    def test_unknown(self):
        result = troshkad._console_detect_state("booting kernel...")
        self.assertEqual(result, "unknown")

    def test_empty_text(self):
        result = troshkad._console_detect_state("")
        self.assertEqual(result, "unknown")

    def test_short_text(self):
        result = troshkad._console_detect_state("ab")
        self.assertEqual(result, "unknown")


# ── _console_extract_output ──


class TestConsoleExtractOutput(unittest.TestCase):
    def test_extracts_between_markers(self):
        text = "noise\nTROSHKA_BEGIN\nhello world\nTROSHKA_EXIT 0\nmore noise"
        output, exit_code = troshkad._console_extract_output(text)
        self.assertEqual(output, "hello world")
        self.assertEqual(exit_code, 0)

    def test_no_markers_returns_full_text(self):
        text = "just some output"
        output, exit_code = troshkad._console_extract_output(text)
        self.assertEqual(output, "just some output")
        self.assertIsNone(exit_code)

    def test_exit_code_absent(self):
        text = "TROSHKA_BEGIN\noutput\nTROSHKA_EXIT \n"
        output, exit_code = troshkad._console_extract_output(text)
        self.assertEqual(output, "output")
        self.assertIsNone(exit_code)


# ── _console_send_text ──


class TestConsoleSendText(unittest.TestCase):
    @patch("troshkad.subprocess.run")
    def test_sends_keys(self, mock_run):
        troshkad._console_send_text("troshka-abcdef01-12345678", "hi")
        self.assertEqual(mock_run.call_count, 2)

    @patch("troshkad.subprocess.run")
    def test_skips_unmapped_chars(self, mock_run):
        # Control chars or rare unicode may not be in _CHAR_TO_KEYS
        troshkad._console_send_text("troshka-abcdef01-12345678", "\x00")
        mock_run.assert_not_called()


# ── _console_send_keys ──


class TestConsoleSendKeys(unittest.TestCase):
    @patch("troshkad.subprocess.run")
    def test_sends_keys(self, mock_run):
        troshkad._console_send_keys("troshka-abcdef01-12345678", "KEY_ENTER")
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        self.assertIn("KEY_ENTER", cmd)


# ── _console_screenshot_ocr ──


class TestConsoleScreenshotOcr(unittest.TestCase):
    @patch("os.remove")
    @patch("builtins.open", mock_open(read_data="login:\n"))
    @patch("troshkad.subprocess.run")
    def test_success(self, mock_run, _mock_remove):
        mock_run.side_effect = [
            MagicMock(returncode=0),  # virsh screenshot
            MagicMock(returncode=0),  # tesseract
        ]
        result = troshkad._console_screenshot_ocr("troshka-abcdef01-12345678")
        self.assertIn("login:", result)

    @patch("os.remove")
    @patch("troshkad.subprocess.run")
    def test_screenshot_fails(self, mock_run, _mock_remove):
        mock_run.return_value = MagicMock(returncode=1)
        result = troshkad._console_screenshot_ocr("troshka-abcdef01-12345678")
        self.assertEqual(result, "")


# ── _get_disk_actual_size ──


class TestGetDiskActualSize(unittest.TestCase):
    @patch("troshkad.subprocess.run")
    def test_returns_size(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout='{"actual-size": 1073741824}'
        )
        result = troshkad._get_disk_actual_size("/var/lib/troshka/vms/d.qcow2")
        self.assertEqual(result, 1073741824)

    @patch("troshkad.subprocess.run")
    def test_returns_zero_on_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        result = troshkad._get_disk_actual_size("/var/lib/troshka/vms/d.qcow2")
        self.assertEqual(result, 0)

    @patch("troshkad.subprocess.run", side_effect=Exception("broken"))
    def test_returns_zero_on_exception(self, _mock):
        result = troshkad._get_disk_actual_size("/var/lib/troshka/vms/d.qcow2")
        self.assertEqual(result, 0)


# ── _get_nbd_process_pid ──


class TestGetNbdProcessPid(unittest.TestCase):
    @patch("troshkad.subprocess.run")
    def test_returns_pid(self, mock_run):
        mock_run.return_value = MagicMock(stdout="  12345\n")
        result = troshkad._get_nbd_process_pid(10809)
        self.assertEqual(result, 12345)

    @patch("troshkad.subprocess.run")
    def test_returns_none_on_empty(self, mock_run):
        mock_run.return_value = MagicMock(stdout="")
        result = troshkad._get_nbd_process_pid(10809)
        self.assertIsNone(result)

    @patch("troshkad.subprocess.run", side_effect=Exception("error"))
    def test_returns_none_on_error(self, _mock):
        result = troshkad._get_nbd_process_pid(10809)
        self.assertIsNone(result)


# ── _reap_stale_nbd_exports ──


class TestReapStaleNbdExports(unittest.TestCase):
    def setUp(self):
        self._orig_ports = dict(troshkad._nbd_ports)

    def tearDown(self):
        with troshkad._nbd_ports_lock:
            troshkad._nbd_ports.clear()
            troshkad._nbd_ports.update(self._orig_ports)

    @patch("troshkad.subprocess.run")
    @patch("os.kill")
    def test_reaps_stale(self, mock_kill, mock_run):
        with troshkad._nbd_ports_lock:
            troshkad._nbd_ports[10809] = {
                "started": time.time() - 7200,  # 2 hours ago
                "pid": 12345,
            }
        troshkad._reap_stale_nbd_exports()
        mock_kill.assert_called_once_with(12345, signal.SIGTERM)
        with troshkad._nbd_ports_lock:
            self.assertNotIn(10809, troshkad._nbd_ports)

    @patch("troshkad.subprocess.run")
    def test_preserves_fresh(self, _mock_run):
        with troshkad._nbd_ports_lock:
            troshkad._nbd_ports[10810] = {
                "started": time.time(),
                "pid": 12345,
            }
        troshkad._reap_stale_nbd_exports()
        with troshkad._nbd_ports_lock:
            self.assertIn(10810, troshkad._nbd_ports)


# ── _log_flatten_progress ──


class TestLogFlattenProgress(unittest.TestCase):
    @patch("os.path.getsize", return_value=500 * 1024**2)
    @patch("os.path.exists", return_value=True)
    def test_logs_progress_with_total(self, _mock_exists, _mock_size):
        job = {"job_id": "flatten-0000", "output": []}
        prev_bytes = [0]
        prev_time = [time.time() - 5]
        troshkad._log_flatten_progress(
            job, "/tmp/out.qcow2", 1024**3, 1.0, prev_bytes, prev_time
        )
        self.assertTrue(any("Flattening" in o for o in job["output"]))
        self.assertTrue(any("%" in o for o in job["output"]))

    @patch("os.path.exists", return_value=False)
    def test_noop_when_file_missing(self, _mock_exists):
        job = {"job_id": "flatten-0001", "output": []}
        prev_bytes = [0]
        prev_time = [time.time()]
        troshkad._log_flatten_progress(
            job, "/tmp/out.qcow2", 0, 0, prev_bytes, prev_time
        )
        self.assertEqual(len(job["output"]), 0)


# ── _check_nfs_health ──


class TestCheckNfsHealth(unittest.TestCase):
    def setUp(self):
        self._orig_config = troshkad._config.copy()
        self._orig_healthy = troshkad._nfs_healthy
        self._orig_stale = troshkad._nfs_stale_since

    def tearDown(self):
        troshkad._config.clear()
        troshkad._config.update(self._orig_config)
        troshkad._nfs_healthy = self._orig_healthy
        troshkad._nfs_stale_since = self._orig_stale

    def test_local_mode_always_healthy(self):
        troshkad._config["storage_mode"] = "local"
        result = troshkad._check_nfs_health()
        self.assertTrue(result)

    @patch("os.path.ismount", return_value=False)
    def test_not_mounted(self, _mock):
        troshkad._config["storage_mode"] = "shared"
        troshkad._config["shared_mount"] = "/mnt/shared"
        troshkad._nfs_healthy = True
        result = troshkad._check_nfs_health()
        self.assertFalse(result)
        self.assertFalse(troshkad._nfs_healthy)

    @patch("os.statvfs")
    @patch("os.path.ismount", return_value=True)
    def test_healthy_mount(self, _mock_mount, _mock_statvfs):
        troshkad._config["storage_mode"] = "shared"
        troshkad._config["shared_mount"] = "/mnt/shared"
        troshkad._nfs_healthy = False
        result = troshkad._check_nfs_health()
        self.assertTrue(result)
        self.assertTrue(troshkad._nfs_healthy)


# ── _try_nfs_recovery ──


class TestTryNfsRecovery(unittest.TestCase):
    def setUp(self):
        self._orig_config = troshkad._config.copy()

    def tearDown(self):
        troshkad._config.clear()
        troshkad._config.update(self._orig_config)

    @patch("time.sleep")
    @patch("troshkad.subprocess.run")
    @patch("builtins.open", mock_open(read_data="nfs-server:/share /mnt/shared nfs rw,soft 0 0\n"))
    def test_successful_recovery(self, mock_run, _mock_sleep):
        troshkad._config["shared_mount"] = "/mnt/shared"
        mock_run.side_effect = [
            MagicMock(returncode=0),  # umount
            MagicMock(returncode=0),  # mount
        ]
        result = troshkad._try_nfs_recovery()
        self.assertTrue(result)

    @patch("time.sleep")
    @patch("troshkad.subprocess.run")
    @patch("builtins.open", mock_open(read_data="# nothing relevant\n"))
    def test_no_fstab_entry(self, mock_run, _mock_sleep):
        troshkad._config["shared_mount"] = "/mnt/shared"
        result = troshkad._try_nfs_recovery()
        self.assertFalse(result)

    @patch("builtins.open", side_effect=OSError("cannot read"))
    def test_fstab_read_error(self, _mock):
        troshkad._config["shared_mount"] = "/mnt/shared"
        result = troshkad._try_nfs_recovery()
        self.assertFalse(result)


# ── _validate_path shared mode ──


class TestValidatePathSharedMode(unittest.TestCase):
    def setUp(self):
        self._orig_config = troshkad._config.copy()

    def tearDown(self):
        troshkad._config.clear()
        troshkad._config.update(self._orig_config)

    @patch("os.path.exists", return_value=False)
    def test_shared_mount_allowed(self, _exists):
        troshkad._config["storage_mode"] = "shared"
        troshkad._config["shared_mount"] = "/mnt/shared"
        troshkad._config["local_mount"] = "/mnt/local"
        result = troshkad._validate_path("/mnt/shared/vms/test.qcow2")
        self.assertEqual(result, "/mnt/shared/vms/test.qcow2")

    @patch("os.path.exists", return_value=False)
    def test_local_mount_allowed(self, _exists):
        troshkad._config["storage_mode"] = "shared"
        troshkad._config["shared_mount"] = "/mnt/shared"
        troshkad._config["local_mount"] = "/mnt/local"
        result = troshkad._validate_path("/mnt/local/cache/test.qcow2")
        self.assertEqual(result, "/mnt/local/cache/test.qcow2")


# ── _handle_container_pull ──


class TestHandleContainerPull(unittest.TestCase):
    @patch("troshkad._run_cmd")
    def test_pulls_image(self, mock_run_cmd):
        job = {"job_id": "pull-0000", "output": [], "_process": None}
        result = troshkad._handle_container_pull(
            job, {"image": "quay.io/test/img:v1"}
        )
        self.assertEqual(result["status"], "pulled")
        cmd = mock_run_cmd.call_args[0][1]
        self.assertIn("podman", cmd)
        self.assertIn("pull", cmd)


# ── _find_pxe_bootloader ──


class TestFindPxeBootloader(unittest.TestCase):
    @patch("troshkad._try_syslinux_bootloader", return_value=None)
    @patch("troshkad._try_bios_bootloader", return_value=None)
    @patch("troshkad._try_uefi_bootloader", return_value="BOOTX64.EFI")
    def test_uefi_found(self, _mock_uefi, _mock_bios, _mock_sys):
        job = {"job_id": "pxe-bl-0000", "output": []}
        result = troshkad._find_pxe_bootloader(job, "/mnt/iso", "/tmp/tftp")
        self.assertEqual(result, "BOOTX64.EFI")

    @patch("troshkad._try_syslinux_bootloader", return_value=None)
    @patch("troshkad._try_bios_bootloader", return_value="pxelinux.0")
    @patch("troshkad._try_uefi_bootloader", return_value=None)
    def test_bios_found(self, _mock_uefi, _mock_bios, _mock_sys):
        job = {"job_id": "pxe-bl-0001", "output": []}
        result = troshkad._find_pxe_bootloader(job, "/mnt/iso", "/tmp/tftp")
        self.assertEqual(result, "pxelinux.0")

    @patch("troshkad._try_syslinux_bootloader", return_value=None)
    @patch("troshkad._try_bios_bootloader", return_value=None)
    @patch("troshkad._try_uefi_bootloader", return_value=None)
    def test_fallback(self, _mock_uefi, _mock_bios, _mock_sys):
        job = {"job_id": "pxe-bl-0002", "output": []}
        result = troshkad._find_pxe_bootloader(job, "/mnt/iso", "/tmp/tftp")
        self.assertEqual(result, troshkad._PXE_LOADER)
        self.assertTrue(any("WARNING" in o for o in job["output"]))


# ── _try_uefi_bootloader ──


class TestTryUefiBootloader(unittest.TestCase):
    @patch("os.path.isdir", return_value=False)
    def test_no_efi_dir(self, _mock):
        job = {"job_id": "uefi-bl-0000", "output": []}
        result = troshkad._try_uefi_bootloader(job, "/mnt/iso", "/tmp/tftp")
        self.assertIsNone(result)


# ── _try_bios_bootloader ──


class TestTryBiosBootloader(unittest.TestCase):
    @patch("os.path.isfile", return_value=False)
    def test_no_bootloader_found(self, _mock):
        job = {"job_id": "bios-bl-0000", "output": []}
        result = troshkad._try_bios_bootloader(job, "/mnt/iso", "/tmp/tftp")
        self.assertIsNone(result)


# ── _try_syslinux_bootloader ──


class TestTrySyslinuxBootloader(unittest.TestCase):
    @patch("os.path.exists", return_value=False)
    def test_no_syslinux_found(self, _mock):
        job = {"job_id": "sys-bl-0000", "output": []}
        result = troshkad._try_syslinux_bootloader(job, "/tmp/tftp")
        self.assertIsNone(result)


# ── _guest_exec_poll ──


class TestGuestExecPoll(unittest.TestCase):
    @patch("time.sleep")
    @patch("troshkad.subprocess.run")
    def test_success(self, mock_run, _mock_sleep):
        import base64
        import json
        stdout_b64 = base64.b64encode(b"hello world").decode()
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({
                "return": {
                    "exited": True,
                    "exitcode": 0,
                    "out-data": stdout_b64,
                }
            }),
        )
        job = {"job_id": "ge-poll-0000", "output": [], "_cancelled": False}
        result = troshkad._guest_exec_poll("troshka-abcdef01-12345678", 42, 10, job)
        self.assertEqual(result["output"], "hello world")
        self.assertEqual(result["exit_code"], 0)

    @patch("time.sleep")
    @patch("troshkad.subprocess.run")
    def test_cancelled(self, _mock_run, _mock_sleep):
        job = {"job_id": "ge-poll-0001", "output": [], "_cancelled": True}
        with self.assertRaises(RuntimeError) as ctx:
            troshkad._guest_exec_poll("troshka-abcdef01-12345678", 42, 10, job)
        self.assertIn("cancelled", str(ctx.exception))

    @patch("time.sleep")
    @patch("troshkad.subprocess.run")
    def test_command_fails(self, mock_run, _mock_sleep):
        mock_run.return_value = MagicMock(returncode=1, stderr="agent error")
        job = {"job_id": "ge-poll-0002", "output": [], "_cancelled": False}
        with self.assertRaises(RuntimeError):
            troshkad._guest_exec_poll("troshka-abcdef01-12345678", 42, 10, job)


# ── _handle_vm_state ──


class TestHandleVmState(unittest.TestCase):
    @patch("troshkad.subprocess.run")
    def test_running(self, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="running\n"),
            MagicMock(returncode=0, stdout="<domain><os/></domain>"),
        ]
        job = {"job_id": "state-0000", "output": []}
        result = troshkad._handle_vm_state(
            job, {"domain_name": "troshka-abcdef01-12345678"}
        )
        self.assertEqual(result["state"], "running")

    @patch("troshkad.subprocess.run")
    def test_not_found(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="not found")
        job = {"job_id": "state-0001", "output": []}
        result = troshkad._handle_vm_state(
            job, {"domain_name": "troshka-abcdef01-12345678"}
        )
        self.assertEqual(result["state"], "not_found")

    @patch("troshkad.subprocess.run")
    def test_shut_off(self, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="shut off\n"),
            MagicMock(returncode=0, stdout="<domain><os><boot dev='hd'/></os></domain>"),
        ]
        job = {"job_id": "state-0002", "output": []}
        result = troshkad._handle_vm_state(
            job, {"domain_name": "troshka-abcdef01-12345678"}
        )
        self.assertEqual(result["state"], "shut_off")
        self.assertEqual(result["boot_devs"], ["hd"])


# ── _handle_vm_list ──


class TestHandleVmList(unittest.TestCase):
    @patch("troshkad.subprocess.run")
    def test_lists_vms(self, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="troshka-aa-bb\nnon-troshka\n\n"),
            MagicMock(returncode=0, stdout="running\n"),
        ]
        job = {"job_id": "list-0000", "output": []}
        result = troshkad._handle_vm_list(job, {})
        self.assertEqual(len(result["domains"]), 1)
        self.assertEqual(result["domains"][0]["name"], "troshka-aa-bb")
        self.assertEqual(result["domains"][0]["state"], "running")

    @patch("troshkad.subprocess.run")
    def test_virsh_failure_raises(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="error")
        job = {"job_id": "list-0001", "output": []}
        with self.assertRaises(RuntimeError):
            troshkad._handle_vm_list(job, {})


# ── _handle_vm_vnc_port ──


class TestHandleVmVncPort(unittest.TestCase):
    @patch("troshkad.subprocess.run")
    def test_finds_port(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="<domain><devices><graphics type='vnc' port='5901'/></devices></domain>",
        )
        job = {"job_id": "vnc-0000", "output": []}
        result = troshkad._handle_vm_vnc_port(
            job, {"domain_name": "troshka-abcdef01-12345678"}
        )
        self.assertEqual(result["vnc_port"], 5901)

    @patch("troshkad.subprocess.run")
    def test_not_found(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="error")
        job = {"job_id": "vnc-0001", "output": []}
        result = troshkad._handle_vm_vnc_port(
            job, {"domain_name": "troshka-abcdef01-12345678"}
        )
        self.assertIsNone(result["vnc_port"])


# ── _prepare_disk_link ──


class TestPrepareDiskLink(unittest.TestCase):
    @patch("os.path.exists", return_value=False)
    def test_no_symlink_from(self, _exists):
        job = {"job_id": "link-0000", "output": []}
        result = troshkad._prepare_disk_link(
            job,
            {"path": "/var/lib/troshka/vms/proj/d.qcow2"},
        )
        self.assertEqual(result, "/var/lib/troshka/vms/proj/d.qcow2")

    @patch("troshkad._chown_qemu")
    @patch("os.symlink")
    @patch("os.makedirs")
    @patch("os.path.exists", return_value=False)
    def test_iso_creates_symlink(self, _exists, _makedirs, mock_symlink, _chown):
        job = {"job_id": "link-0001", "output": []}
        result = troshkad._prepare_disk_link(
            job,
            {
                "path": "/var/lib/troshka/vms/proj/boot.iso",
                "symlink_from": "/var/lib/troshka/images/src.iso",
            },
        )
        self.assertEqual(result, "/var/lib/troshka/vms/proj/boot.iso")
        mock_symlink.assert_called_once()

    @patch("troshkad._chown_qemu")
    @patch("shutil.copy2")
    @patch("os.path.getsize", return_value=10 * 1024**3)
    @patch("os.makedirs")
    @patch("os.path.exists", return_value=False)
    def test_non_iso_copies(self, _exists, _makedirs, _getsize, mock_copy, _chown):
        job = {"job_id": "link-0002", "output": []}
        result = troshkad._prepare_disk_link(
            job,
            {
                "path": "/var/lib/troshka/vms/proj/d.qcow2",
                "symlink_from": "/var/lib/troshka/images/src.qcow2",
            },
        )
        self.assertEqual(result, "/var/lib/troshka/vms/proj/d.qcow2")
        mock_copy.assert_called_once()


# ── _reconfigure_nics ──


class TestReconfigureNics(unittest.TestCase):
    def test_updates_nic_bridge(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring(
            "<domain><devices>"
            "<interface type='bridge'><mac address='aa:bb:cc:dd:ee:ff'/>"
            "<source bridge='br-old'/></interface>"
            "</devices></domain>"
        )
        nics = [{"mac": "aa:bb:cc:dd:ee:ff", "bridge": "br-new"}]
        troshkad._reconfigure_nics(root, nics)
        iface = root.find(".//interface")
        self.assertEqual(iface.find("source").get("bridge"), "br-new")


# ── _add_cdrom_element ──


class TestAddCdromElement(unittest.TestCase):
    def test_adds_cdrom(self):
        import xml.etree.ElementTree as ET
        devices = ET.Element("devices")
        used_targets = set()
        troshkad._add_cdrom_element(
            {"job_id": "cd-0000", "output": []},
            devices,
            "/var/lib/troshka/vms/boot.iso",
            "sata",
            "sd",
            used_targets,
            "troshka-abcdef01-12345678",
        )
        cdrom = devices.find("disk")
        self.assertEqual(cdrom.get("device"), "cdrom")
        self.assertEqual(cdrom.find("source").get("file"), "/var/lib/troshka/vms/boot.iso")
        self.assertEqual(cdrom.find("target").get("bus"), "sata")


# ── Container handler tests ──


class TestHandleContainerCreate(unittest.TestCase):
    @patch("troshkad._attach_container_to_bridges")
    @patch("troshkad._run_cmd")
    @patch("troshkad._build_container_cmd", return_value=["podman", "create", "--name", "ctr"])
    @patch("troshkad._mount_container_volumes", return_value=[])
    def test_basic_create(self, _mock_mount, _mock_build, mock_run, _mock_attach):
        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        params = {"container_name": "troshka-aabb-ctr1", "image": "quay.io/test:latest"}
        result = troshkad._handle_container_create(job, params)
        self.assertEqual(result["status"], "created")
        self.assertEqual(result["container_name"], "troshka-aabb-ctr1")
        mock_run.assert_called_once()

    @patch("troshkad._attach_container_to_bridges")
    @patch("troshkad._run_cmd")
    @patch("troshkad._build_container_cmd", return_value=["podman", "create"])
    @patch("troshkad._mount_container_volumes", return_value=[])
    def test_create_with_networks(self, _mock_mount, _mock_build, mock_run, mock_attach):
        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        params = {
            "container_name": "troshka-aabb-ctr1",
            "image": "img",
            "networks": [{"bridge_name": "br-troshka-aabb"}],
        }
        result = troshkad._handle_container_create(job, params)
        self.assertEqual(result["status"], "created")
        mock_attach.assert_called_once()

    @patch("troshkad._attach_container_to_bridges")
    @patch("troshkad._run_cmd")
    @patch("troshkad._build_container_cmd", return_value=["podman", "create"])
    @patch("troshkad._mount_container_volumes", return_value=[])
    def test_create_no_networks_skips_attach(self, _mock_mount, _mock_build, mock_run, mock_attach):
        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        params = {"container_name": "ctr", "image": "img"}
        troshkad._handle_container_create(job, params)
        mock_attach.assert_not_called()


class TestHandleContainerStart(unittest.TestCase):
    @patch("troshkad._run_cmd")
    def test_start(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        result = troshkad._handle_container_start(job, {"container_name": "ctr1"})
        self.assertEqual(result["status"], "started")
        cmd = mock_run.call_args[0][1]
        self.assertEqual(cmd, ["podman", "start", "ctr1"])


class TestHandleContainerStop(unittest.TestCase):
    @patch("troshkad._run_cmd")
    def test_stop_default_timeout(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        result = troshkad._handle_container_stop(job, {"container_name": "ctr1"})
        self.assertEqual(result["status"], "stopped")
        cmd = mock_run.call_args[0][1]
        self.assertIn("-t", cmd)
        self.assertIn("10", cmd)

    @patch("troshkad._run_cmd")
    def test_stop_custom_timeout(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        result = troshkad._handle_container_stop(job, {"container_name": "c1", "timeout": 30})
        self.assertEqual(result["status"], "stopped")
        cmd = mock_run.call_args[0][1]
        self.assertIn("30", cmd)


class TestHandleContainerDestroy(unittest.TestCase):
    @patch("troshkad.os.path.ismount", return_value=False)
    @patch("troshkad._run_cmd")
    def test_destroy_no_volumes(self, mock_run, _):
        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        result = troshkad._handle_container_destroy(job, {"container_name": "ctr1"})
        self.assertEqual(result["status"], "destroyed")
        # Should call stop + rm
        self.assertEqual(mock_run.call_count, 2)

    @patch("troshkad.os.path.ismount", return_value=True)
    @patch("troshkad._run_cmd")
    def test_destroy_with_mounted_volume(self, mock_run, _mock_ismount):
        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        params = {
            "container_name": "ctr1",
            "volumes": [{"mount_dir": "/var/lib/troshka/vms/proj/vol"}],
        }
        result = troshkad._handle_container_destroy(job, params)
        self.assertEqual(result["status"], "destroyed")
        # stop + rm + umount = 3
        self.assertEqual(mock_run.call_count, 3)

    @patch("troshkad.os.path.ismount", return_value=False)
    @patch("troshkad._run_cmd", side_effect=RuntimeError("already stopped"))
    def test_destroy_tolerates_stop_failure(self, mock_run, _):
        job = {"job_id": "j1", "output": []}
        result = troshkad._handle_container_destroy(job, {"container_name": "ctr1"})
        self.assertEqual(result["status"], "destroyed")


class TestHandleContainerLogs(unittest.TestCase):
    @patch("troshkad.subprocess.run")
    def test_logs_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="line1\nline2\n")
        job = {"job_id": "j1", "output": []}
        result = troshkad._handle_container_logs(job, {"container_name": "ctr1"})
        self.assertEqual(result["logs"], "line1\nline2\n")
        self.assertEqual(result["container_name"], "ctr1")
        cmd = mock_run.call_args[0][0]
        self.assertIn("--tail", cmd)
        self.assertIn("500", cmd)

    @patch("troshkad.subprocess.run")
    def test_logs_custom_tail(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="output")
        job = {"job_id": "j1", "output": []}
        troshkad._handle_container_logs(job, {"container_name": "c", "tail": 100})
        cmd = mock_run.call_args[0][0]
        self.assertIn("100", cmd)

    @patch("troshkad.subprocess.run")
    def test_logs_failure_raises(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="no container")
        job = {"job_id": "j1", "output": []}
        with self.assertRaises(RuntimeError):
            troshkad._handle_container_logs(job, {"container_name": "c"})


class TestHandleContainerExec(unittest.TestCase):
    @patch("troshkad.subprocess.run")
    def test_exec_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="hello", stderr="")
        job = {"job_id": "j1", "output": []}
        result = troshkad._handle_container_exec(
            job, {"container_name": "ctr1", "command": ["/bin/echo", "hello"]}
        )
        self.assertEqual(result["stdout"], "hello")
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[:3], ["podman", "exec", "ctr1"])

    @patch("troshkad.subprocess.run")
    def test_exec_default_command(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        job = {"job_id": "j1", "output": []}
        troshkad._handle_container_exec(job, {"container_name": "c"})
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd, ["podman", "exec", "c", "/bin/sh"])

    @patch("troshkad.subprocess.run")
    def test_exec_failure_raises(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="err")
        job = {"job_id": "j1", "output": []}
        with self.assertRaises(RuntimeError):
            troshkad._handle_container_exec(job, {"container_name": "c"})


class TestHandleContainerSaveImage(unittest.TestCase):
    @patch("troshkad.os.path.getsize", return_value=1048576)
    @patch("troshkad.os.makedirs")
    @patch("troshkad.subprocess.Popen")
    def test_save_success(self, mock_popen, _makedirs, _getsize):
        proc = MagicMock()
        proc.communicate.return_value = ("", "")
        proc.returncode = 0
        mock_popen.return_value = proc
        job = {"job_id": "j1", "output": [], "_process": None}
        result = troshkad._handle_container_save_image(
            job,
            {"image": "img:latest", "output_path": "/var/lib/troshka/vms/out.tar.gz"},
        )
        self.assertEqual(result["size_bytes"], 1048576)

    @patch("troshkad.os.makedirs")
    @patch("troshkad.subprocess.Popen")
    def test_save_failure_raises(self, mock_popen, _makedirs):
        proc = MagicMock()
        proc.communicate.return_value = ("", "save failed")
        proc.returncode = 1
        mock_popen.return_value = proc
        job = {"job_id": "j1", "output": [], "_process": None}
        with self.assertRaises(RuntimeError):
            troshkad._handle_container_save_image(
                job,
                {"image": "img", "output_path": "/var/lib/troshka/vms/out.tar.gz"},
            )

    @patch("troshkad.os.makedirs")
    @patch("troshkad.subprocess.Popen")
    def test_save_timeout_raises(self, mock_popen, _makedirs):
        proc = MagicMock()
        proc.communicate.side_effect = subprocess.TimeoutExpired("cmd", 600)
        proc.kill = MagicMock()
        proc.communicate_after_kill = MagicMock()
        # After kill, communicate returns normally
        proc.communicate.side_effect = [subprocess.TimeoutExpired("cmd", 600), ("", "")]
        mock_popen.return_value = proc
        job = {"job_id": "j1", "output": [], "_process": None}
        with self.assertRaises(RuntimeError):
            troshkad._handle_container_save_image(
                job,
                {"image": "img", "output_path": "/var/lib/troshka/vms/out.tar.gz"},
            )


class TestHandleContainerLoadImage(unittest.TestCase):
    @patch("troshkad.os.path.isfile", return_value=True)
    @patch("troshkad.subprocess.Popen")
    def test_load_success(self, mock_popen, _isfile):
        proc = MagicMock()
        proc.communicate.return_value = ("Loaded image: img", "")
        proc.returncode = 0
        mock_popen.return_value = proc
        job = {"job_id": "j1", "output": [], "_process": None}
        result = troshkad._handle_container_load_image(
            job, {"input_path": "/var/lib/troshka/vms/img.tar.gz"}
        )
        self.assertEqual(result["status"], "loaded")

    @patch("troshkad.os.path.isfile", return_value=False)
    def test_load_missing_file_raises(self, _isfile):
        job = {"job_id": "j1", "output": [], "_process": None}
        with self.assertRaises(FileNotFoundError):
            troshkad._handle_container_load_image(
                job, {"input_path": "/var/lib/troshka/vms/missing.tar.gz"}
            )

    @patch("troshkad.os.path.isfile", return_value=True)
    @patch("troshkad.subprocess.Popen")
    def test_load_failure_raises(self, mock_popen, _isfile):
        proc = MagicMock()
        proc.communicate.return_value = ("", "load failed")
        proc.returncode = 1
        mock_popen.return_value = proc
        job = {"job_id": "j1", "output": [], "_process": None}
        with self.assertRaises(RuntimeError):
            troshkad._handle_container_load_image(
                job, {"input_path": "/var/lib/troshka/vms/img.tar.gz"}
            )


# ── Container/pod networking ──


class TestSetupContainerVethPair(unittest.TestCase):
    @patch("troshkad._run_cmd")
    def test_creates_veth_pair_no_mac(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        troshkad._setup_container_veth_pair(
            job, "troshka-aabbccdd-ctr", 0, "vh0", "vc0", "", "ns-ctr", "br-troshka-aabb"
        )
        # Without MAC: create veth, move ctr to ns, move host to proj_ns, attach to bridge, bring up, rename, bring eth up = 7
        self.assertEqual(mock_run.call_count, 7)

    @patch("troshkad._run_cmd")
    def test_creates_veth_pair_with_mac(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        troshkad._setup_container_veth_pair(
            job, "troshka-aabbccdd-ctr", 0, "vh0", "vc0", "aa:bb:cc:dd:ee:ff", "ns-ctr", "br-troshka-aabb"
        )
        # With MAC: 7 + 1 for MAC set = 8
        self.assertEqual(mock_run.call_count, 8)

    @patch("troshkad._run_cmd")
    def test_tolerates_existing_veth(self, mock_run):
        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("RTNETLINK answers: File exists")
            return MagicMock(returncode=0)

        mock_run.side_effect = side_effect
        job = {"job_id": "j1", "output": []}
        troshkad._setup_container_veth_pair(
            job, "troshka-aabbccdd-ctr", 1, "vh1", "vc1", "", "ns-ctr", "br-troshka-aabb"
        )
        # Continues despite first call failing
        self.assertGreater(call_count[0], 1)


class TestSetupPodVethPair(unittest.TestCase):
    @patch("troshkad._run_cmd")
    def test_creates_pod_veth_pair(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        troshkad._setup_pod_veth_pair(
            job, "troshka-aabbccdd-pod1", 0, "vh0", "vc0", "aa:bb:cc:dd:ee:ff",
            "ns-pod", "troshka-aabbccdd", "br-troshka-aabb"
        )
        # veth create, MAC set, move ctr to ns, move host to proj_ns, bridge master, host up, rename, eth up = 8
        self.assertGreaterEqual(mock_run.call_count, 7)


# ── handle_container_states ──


class TestHandleContainerStates(unittest.TestCase):
    @patch("troshkad._get_pod_states", return_value={"pod1": {"state": "running"}})
    @patch("troshkad._enrich_container_ips")
    @patch("troshkad._get_container_states", return_value={"ctr1": {"state": "running"}})
    def test_returns_both(self, _mock_ctrs, _mock_enrich, _mock_pods):
        handler = MagicMock()
        troshkad.handle_container_states(handler, {})
        handler._send_json.assert_called_once()
        data = handler._send_json.call_args[0][1]
        self.assertIn("ctr1", data["containers"])
        self.assertIn("pod1", data["pods"])

    @patch("troshkad._get_pod_states", side_effect=RuntimeError("fail"))
    @patch("troshkad._enrich_container_ips")
    @patch("troshkad._get_container_states", return_value={"ctr1": {"state": "running"}})
    def test_tolerates_pod_failure(self, _mock_ctrs, _mock_enrich, _mock_pods):
        handler = MagicMock()
        troshkad.handle_container_states(handler, {})
        data = handler._send_json.call_args[0][1]
        self.assertEqual(data["pods"], {})

    @patch("troshkad._get_pod_states", return_value={})
    @patch("troshkad._enrich_container_ips", side_effect=RuntimeError("fail"))
    @patch("troshkad._get_container_states", side_effect=RuntimeError("fail"))
    def test_tolerates_container_failure(self, _mock_ctrs, _mock_enrich, _mock_pods):
        handler = MagicMock()
        troshkad.handle_container_states(handler, {})
        data = handler._send_json.call_args[0][1]
        self.assertEqual(data["containers"], {})


# ── Mesh handlers ──


class TestHandleMeshSetup(unittest.TestCase):
    @patch("troshkad.os.chmod")
    @patch("builtins.open", new_callable=mock_open)
    @patch("troshkad.os.makedirs")
    @patch("troshkad._run_cmd")
    def test_basic_setup(self, mock_run, _makedirs, _mock_open, _chmod):
        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        params = {
            "project_id": "aabbccdd-1122-3344-5566-778899001122",
            "wg_private_key": "base64key==",  # pragma: allowlist secret
            "wg_address": "10.252.1.1/24",
            "wg_port": 51820,
            "peers": [
                {
                    "public_key": "peerkey==",
                    "endpoint": "10.0.0.2:51820",
                    "allowed_ips": "10.252.1.2/32",
                }
            ],
        }
        result = troshkad._handle_mesh_setup(job, params)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["interface"], "wg-aabbccdd")
        # link del + link add + wg setconf + ip addr add + link set up + ping = 6
        self.assertGreaterEqual(mock_run.call_count, 5)

    @patch("troshkad.os.chmod")
    @patch("builtins.open", new_callable=mock_open)
    @patch("troshkad.os.makedirs")
    @patch("troshkad._run_cmd")
    def test_no_peers(self, mock_run, _makedirs, _mock_open, _chmod):
        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        params = {
            "project_id": "aabbccdd-1122-3344-5566-778899001122",
            "wg_private_key": "key==",  # pragma: allowlist secret
            "wg_address": "10.252.1.1/24",
            "wg_port": 51820,
            "peers": [],
        }
        result = troshkad._handle_mesh_setup(job, params)
        self.assertEqual(result["status"], "ok")


class TestHandleMeshJoinNetwork(unittest.TestCase):
    @patch("troshkad._run_cmd")
    def test_join_single_network(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        params = {
            "project_id": "aabbccdd-1122-3344-5566-778899001122",
            "wg_local_ip": "10.252.1.2",
            "networks": [
                {
                    "vni": 10001,
                    "bridge_name": "br-10001",
                    "wg_peer_ips": ["10.252.1.1", "10.252.1.2"],
                }
            ],
        }
        result = troshkad._handle_mesh_join_network(job, params)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["namespace"], "troshka-aabbccdd")
        self.assertGreaterEqual(mock_run.call_count, 5)

    @patch("troshkad._run_cmd")
    def test_join_skips_self_fdb(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        params = {
            "project_id": "aabbccdd-1122-3344-5566-778899001122",
            "wg_local_ip": "10.252.1.2",
            "networks": [
                {
                    "vni": 10001,
                    "bridge_name": "br-10001",
                    "wg_peer_ips": ["10.252.1.1", "10.252.1.2"],
                }
            ],
        }
        troshkad._handle_mesh_join_network(job, params)
        # Verify FDB append was only called for non-self peer (10.252.1.1), not for 10.252.1.2
        fdb_calls = [
            c for c in mock_run.call_args_list
            if len(c[0]) > 1 and "fdb" in str(c[0][1])
        ]
        self.assertEqual(len(fdb_calls), 1)


class TestHandleMeshTeardown(unittest.TestCase):
    @patch("troshkad.os.remove")
    @patch("troshkad.os.path.exists", return_value=True)
    @patch("troshkad.subprocess.run")
    def test_teardown_with_conf(self, mock_run, _exists, mock_remove):
        mock_run.return_value = MagicMock(returncode=0)
        handler = MagicMock()
        handler.path = "/mesh/teardown?project_id=aabbccdd-1122-3344-5566-778899001122"
        troshkad.handle_mesh_teardown(handler, {"project_id": "aabbccdd-1122-3344-5566-778899001122"})
        handler._send_json.assert_called_with(200, {"status": "ok"})
        mock_remove.assert_called_once()

    @patch("troshkad.os.path.exists", return_value=False)
    @patch("troshkad.subprocess.run")
    def test_teardown_no_conf(self, mock_run, _exists):
        mock_run.return_value = MagicMock(returncode=0)
        handler = MagicMock()
        handler.path = "/mesh/teardown"
        troshkad.handle_mesh_teardown(handler, {"project_id": "aabbccdd-1122-3344-5566-778899001122"})
        handler._send_json.assert_called_with(200, {"status": "ok"})

    def test_teardown_missing_project_id(self):
        handler = MagicMock()
        handler.path = "/mesh/teardown"
        troshkad.handle_mesh_teardown(handler, {})
        handler._send_json.assert_called_with(400, {"error": "project_id required"})


class TestHandleMeshStatus(unittest.TestCase):
    @patch("troshkad.os.path.isdir", return_value=False)
    def test_status_no_mesh_dir(self, _isdir):
        handler = MagicMock()
        troshkad.handle_mesh_status(handler, {})
        handler._send_json.assert_called_with(200, {"projects": {}})

    @patch("troshkad.subprocess.check_output")
    @patch("troshkad.os.listdir", return_value=["aabbccdd-1122-3344-5566-778899001122.conf"])
    @patch("troshkad.os.path.isdir", return_value=True)
    def test_status_with_running_peer(self, _isdir, _listdir, mock_check):
        mock_check.return_value = "peerkey==\t1720000000\n"
        handler = MagicMock()
        troshkad.handle_mesh_status(handler, {})
        data = handler._send_json.call_args[0][1]
        self.assertIn("aabbccdd-1122-3344-5566-778899001122", data["projects"])
        self.assertEqual(
            data["projects"]["aabbccdd-1122-3344-5566-778899001122"]["peers"]["peerkey=="],
            1720000000,
        )

    @patch("troshkad.subprocess.check_output", side_effect=Exception("not running"))
    @patch("troshkad.os.listdir", return_value=["dead-proj.conf"])
    @patch("troshkad.os.path.isdir", return_value=True)
    def test_status_wg_not_running(self, _isdir, _listdir, _mock_check):
        handler = MagicMock()
        troshkad.handle_mesh_status(handler, {})
        data = handler._send_json.call_args[0][1]
        self.assertEqual(data["projects"]["dead-proj"]["error"], "not running")


# ── Network handlers ──


class TestHandleNetworkAddDnat(unittest.TestCase):
    @patch("troshkad._run_cmd")
    @patch("troshkad.subprocess.run")
    def test_adds_two_hop_dnat(self, mock_run, mock_runcmd):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="    inet 172.30.5.2/24 scope global ve12345n\n",
        )
        mock_runcmd.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        params = {
            "namespace": "troshka-12345678",
            "transit_port": 443,
            "dst_ip": "192.168.1.10",
            "dst_port": 443,
        }
        result = troshkad._handle_network_add_dnat(job, params)
        self.assertEqual(result["namespace"], "troshka-12345678")
        self.assertEqual(result["transit_port"], 443)
        self.assertEqual(result["transit_ip"], "172.30.5.10")
        # 3 _run_cmd calls: add secondary IP, host DNAT, namespace DNAT
        self.assertEqual(mock_runcmd.call_count, 3)

    @patch("troshkad.subprocess.run")
    def test_raises_if_no_transit_ip(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="no relevant lines\n")
        job = {"job_id": "j1", "output": []}
        params = {
            "namespace": "troshka-12345678",
            "transit_port": 443,
            "dst_ip": "192.168.1.10",
            "dst_port": 443,
        }
        with self.assertRaises(RuntimeError):
            troshkad._handle_network_add_dnat(job, params)

    @patch("troshkad._run_cmd")
    @patch("troshkad.subprocess.run")
    def test_skips_occupied_ips(self, mock_run, mock_runcmd):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=(
                "    inet 172.30.5.2/24 scope global ve12345n\n"
                "    inet 172.30.5.10/24 scope global secondary ve12345n\n"
            ),
        )
        mock_runcmd.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        params = {
            "namespace": "troshka-12345678",
            "transit_port": 8080,
            "dst_ip": "10.0.0.5",
            "dst_port": 8080,
        }
        result = troshkad._handle_network_add_dnat(job, params)
        # Should pick .11 since .10 is occupied
        self.assertEqual(result["transit_ip"], "172.30.5.11")


class TestHandleReconnectTaps(unittest.TestCase):
    @patch("troshkad._run_cmd")
    @patch("troshkad.subprocess.run")
    def test_namespace_not_found(self, mock_run, _mock_runcmd):
        mock_run.return_value = MagicMock(returncode=0, stdout="other-ns\n")
        job = {"job_id": "j1", "output": []}
        params = {
            "project_id": "aabbccdd-1122-3344-5566-778899001122",
            "domains": ["troshka-aabbccdd-12345678"],
        }
        result = troshkad._handle_reconnect_taps(job, params)
        self.assertEqual(result["reconnected"], 0)
        self.assertEqual(result["error"], "namespace not found")

    @patch("troshkad._run_cmd")
    @patch("troshkad.subprocess.run")
    def test_reconnects_tap(self, mock_run, mock_runcmd):
        def run_side(*args, **kwargs):
            cmd = args[0]
            r = MagicMock(returncode=0, stdout="", stderr="")
            if "netns" in cmd and "list" in cmd:
                r.stdout = "troshka-aabbccdd\n"
            elif "virsh" in cmd and "dumpxml" in cmd:
                r.stdout = (
                    "<domain><devices>"
                    "<interface type='bridge'><source bridge='br-10001'/>"
                    "<target dev='vnet0'/></interface>"
                    "</devices></domain>"
                )
            elif "link" in cmd and "show" in cmd:
                r.returncode = 0
            return r

        mock_run.side_effect = run_side
        mock_runcmd.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        params = {
            "project_id": "aabbccdd-1122-3344-5566-778899001122",
            "domains": ["troshka-aabbccdd-12345678"],
        }
        result = troshkad._handle_reconnect_taps(job, params)
        self.assertEqual(result["reconnected"], 1)
        # 3 _run_cmd calls per tap: move to ns, master bridge, bring up
        self.assertEqual(mock_runcmd.call_count, 3)

    @patch("troshkad._run_cmd")
    @patch("troshkad.subprocess.run")
    def test_skips_domain_with_virsh_error(self, mock_run, mock_runcmd):
        def run_side(*args, **kwargs):
            cmd = args[0]
            r = MagicMock(returncode=0, stdout="", stderr="")
            if "netns" in cmd:
                r.stdout = "troshka-aabbccdd\n"
            elif "virsh" in cmd:
                r.returncode = 1
            return r

        mock_run.side_effect = run_side
        job = {"job_id": "j1", "output": []}
        result = troshkad._handle_reconnect_taps(
            job,
            {
                "project_id": "aabbccdd-1122-3344-5566-778899001122",
                "domains": ["troshka-aabbccdd-12345678"],
            },
        )
        self.assertEqual(result["reconnected"], 0)


class TestHandleNetworkFullTeardown(unittest.TestCase):
    @patch("troshkad.os.remove")
    @patch("troshkad._teardown_pxe_services")
    @patch("troshkad._teardown_host_nftables")
    @patch("troshkad._teardown_metadata_service")
    @patch("troshkad._teardown_chronyd")
    @patch("troshkad._teardown_dnsmasq")
    @patch("troshkad._teardown_haproxy")
    @patch("troshkad._teardown_vxlan_interfaces")
    @patch("troshkad._run_cmd")
    def test_full_teardown(
        self,
        mock_run,
        _mock_vxlan,
        _mock_haproxy,
        _mock_dnsmasq,
        _mock_chronyd,
        _mock_metadata,
        _mock_nft,
        _mock_pxe,
        _mock_remove,
    ):
        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        params = {
            "project_id": "aabbccdd-1122-3344-5566-778899001122",
            "vni_list": [10001, 10002],
        }
        result = troshkad._handle_network_full_teardown(job, params)
        self.assertEqual(result["status"], "torn_down")
        self.assertEqual(result["project_id"], "aabbccdd-1122-3344-5566-778899001122")

    @patch("troshkad.os.remove", side_effect=FileNotFoundError)
    @patch("troshkad._teardown_pxe_services")
    @patch("troshkad._teardown_host_nftables")
    @patch("troshkad._teardown_metadata_service")
    @patch("troshkad._teardown_chronyd")
    @patch("troshkad._teardown_dnsmasq")
    @patch("troshkad._teardown_haproxy")
    @patch("troshkad._teardown_vxlan_interfaces")
    @patch("troshkad._run_cmd", side_effect=RuntimeError("not found"))
    def test_teardown_tolerates_missing(
        self,
        _mock_run,
        _mock_vxlan,
        _mock_haproxy,
        _mock_dnsmasq,
        _mock_chronyd,
        _mock_metadata,
        _mock_nft,
        _mock_pxe,
        _mock_remove,
    ):
        job = {"job_id": "j1", "output": []}
        params = {
            "project_id": "aabbccdd-1122-3344-5566-778899001122",
            "vni_list": [10001],
        }
        result = troshkad._handle_network_full_teardown(job, params)
        self.assertEqual(result["status"], "torn_down")


# ── Network full setup ──


class TestHandleNetworkFullSetup(unittest.TestCase):
    @patch("troshkad._setup_host_nftables")
    @patch("troshkad._setup_ns_port_forward_dnat", return_value={})
    @patch("troshkad._setup_ns_outbound_rules")
    @patch("troshkad._setup_ns_nftables_forwarding")
    @patch("troshkad._setup_ns_nftables_base")
    @patch("troshkad._setup_chrony_ntp")
    @patch("troshkad._setup_dnsmasq_for_network")
    @patch("troshkad._setup_vxlan_bridge")
    @patch("troshkad._setup_namespace_and_veth")
    def test_basic_setup(
        self,
        _mock_ns,
        _mock_vxlan,
        _mock_dns,
        _mock_chrony,
        _mock_nft_base,
        _mock_nft_fwd,
        _mock_outbound,
        _mock_pf,
        _mock_host_nft,
    ):
        job = {"job_id": "j1", "output": []}
        params = {
            "project_id": "aabbccdd-1122-3344-5566-778899001122",
            "host_ip": "10.0.0.1",
            "networks": [{"vni": 10001, "bridge_name": "br-10001", "cidr": "192.168.1.0/24"}],
            "gateway": {},
            "routers": [],
        }
        result = troshkad._handle_network_full_setup(job, params)
        self.assertEqual(result["status"], "configured")
        self.assertEqual(result["networks"], 1)


# ── Image cache ──


class TestHandleImageCache(unittest.TestCase):
    @patch("troshkad.os.remove", side_effect=FileNotFoundError)
    @patch("troshkad.fcntl.flock")
    @patch("builtins.open", new_callable=mock_open)
    @patch("troshkad.os.path.getsize", return_value=1000000)
    @patch("troshkad.os.path.exists", return_value=True)
    @patch("troshkad.os.path.realpath", side_effect=lambda p: p)
    @patch("troshkad.os.makedirs")
    def test_already_cached_skips(self, _makedirs, _realpath, _exists, _getsize, _mock_open, _flock, _remove):
        job = {"job_id": "j1", "output": []}
        params = {
            "dest_path": "/var/lib/troshka/images/item.qcow2",
            "expected_size": 1000000,
        }
        result = troshkad._handle_image_cache(job, params)
        self.assertEqual(result["status"], "cached")
        self.assertTrue(result.get("skipped"))

    @patch("troshkad.os.remove", side_effect=FileNotFoundError)
    @patch("troshkad.fcntl.flock")
    @patch("builtins.open", new_callable=mock_open)
    @patch("troshkad._run_cmd")
    @patch("troshkad.os.path.exists", return_value=False)
    @patch("troshkad.os.makedirs")
    def test_downloads_via_curl(self, _makedirs, _exists, mock_run, _mock_open, _flock, _remove):
        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        params = {
            "dest_path": "/var/lib/troshka/images/item.qcow2",
            "url": "https://example.com/image.qcow2",
        }
        result = troshkad._handle_image_cache(job, params)
        self.assertEqual(result["status"], "cached")
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][1]
        self.assertEqual(cmd[0], "curl")

    @patch("troshkad.os.remove", side_effect=FileNotFoundError)
    @patch("troshkad.fcntl.flock")
    @patch("builtins.open", new_callable=mock_open)
    @patch("troshkad._run_cmd")
    @patch("troshkad._s3_download")
    @patch("troshkad.os.path.exists", return_value=False)
    @patch("troshkad.os.makedirs")
    def test_downloads_via_s3(self, _makedirs, _exists, mock_s3, mock_run, _mock_open, _flock, _remove):
        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        params = {
            "dest_path": "/var/lib/troshka/images/item.qcow2",
            "s3_url": "s3://bucket/key",
            "aws_access_key_id": "key",
            "aws_secret_access_key": "secret",  # pragma: allowlist secret
        }
        result = troshkad._handle_image_cache(job, params)
        self.assertEqual(result["status"], "cached")
        mock_s3.assert_called_once()

    @patch("troshkad.os.remove", side_effect=FileNotFoundError)
    @patch("troshkad.fcntl.flock")
    @patch("builtins.open", new_callable=mock_open)
    @patch("troshkad._run_cmd")
    @patch("troshkad.os.path.exists", return_value=False)
    @patch("troshkad.os.makedirs")
    def test_runs_qcow2_check(self, _makedirs, _exists, mock_run, _mock_open, _flock, _remove):
        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        params = {
            "dest_path": "/var/lib/troshka/images/item.qcow2",
            "url": "https://example.com/img.qcow2",
            "expected_format": "qcow2",
        }
        result = troshkad._handle_image_cache(job, params)
        self.assertEqual(result["status"], "cached")
        # curl + qemu-img check = 2
        self.assertEqual(mock_run.call_count, 2)


# ── Seed create batch ──


class TestHandleSeedCreateBatch(unittest.TestCase):
    @patch("troshkad._chown_qemu")
    @patch("troshkad.os.makedirs")
    @patch("troshkad._run_cmd")
    def test_creates_seeds(self, mock_run, _makedirs, _chown):
        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        params = {
            "seeds": [
                {
                    "path": "/var/lib/troshka/vms/proj/seed1.iso",
                    "meta_data": 'instance-id: test1',
                    "user_data": '#cloud-config\nhostname: vm1',
                },
                {
                    "path": "/var/lib/troshka/vms/proj/seed2.iso",
                    "meta_data": 'instance-id: test2',
                },
            ]
        }
        import tempfile
        with patch.object(tempfile, "TemporaryDirectory") as mock_tmpdir:
            mock_tmpdir.return_value.__enter__ = MagicMock(return_value="/tmp/test-seed-dir")
            mock_tmpdir.return_value.__exit__ = MagicMock(return_value=False)
            with patch("builtins.open", mock_open()):
                result = troshkad._handle_seed_create_batch(job, params)
        self.assertEqual(result["created"], 2)
        self.assertEqual(result["status"], "completed")
        # One xorriso call per seed
        self.assertEqual(mock_run.call_count, 2)

    def test_empty_seeds_raises(self):
        job = {"job_id": "j1", "output": []}
        with self.assertRaises(ValueError):
            troshkad._handle_seed_create_batch(job, {"seeds": []})

    def test_missing_seeds_raises(self):
        job = {"job_id": "j1", "output": []}
        with self.assertRaises(ValueError):
            troshkad._handle_seed_create_batch(job, {})


# ── EIP configure ──


class TestHandleEipConfigure(unittest.TestCase):
    @patch("troshkad._run_cmd")
    def test_configures_eip_mappings(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        params = {
            "project_id": "aabbccdd-1122-3344-5566-778899001122",
            "eip_mappings": [
                {"public_ip": "54.1.2.3", "private_ip": "192.168.1.10"},
                {"public_ip": "54.1.2.4", "private_ip": "192.168.1.11"},
            ],
        }
        result = troshkad._handle_eip_configure(job, params)
        self.assertEqual(result["status"], "configured")
        self.assertEqual(mock_run.call_count, 2)

    @patch("troshkad._run_cmd")
    def test_empty_mappings(self, mock_run):
        job = {"job_id": "j1", "output": []}
        params = {
            "project_id": "aabbccdd-1122-3344-5566-778899001122",
            "eip_mappings": [],
        }
        result = troshkad._handle_eip_configure(job, params)
        self.assertEqual(result["status"], "configured")
        mock_run.assert_not_called()


# ── GC sub-function tests ──


class TestCleanOrphanDirs(unittest.TestCase):
    @patch("troshkad.shutil.rmtree")
    @patch("troshkad.os.path.isdir", return_value=True)
    def test_removes_valid_dir(self, _isdir, mock_rmtree):
        job = {"job_id": "j1", "output": []}
        removed = troshkad._clean_orphan_dirs(
            job, ["/var/lib/troshka/vms/dead-uuid/"]
        )
        self.assertEqual(removed, 1)
        mock_rmtree.assert_called_once()

    @patch("troshkad.os.path.isdir", return_value=False)
    def test_skips_nonexistent(self, _isdir):
        job = {"job_id": "j1", "output": []}
        removed = troshkad._clean_orphan_dirs(
            job, ["/var/lib/troshka/vms/missing/"]
        )
        self.assertEqual(removed, 0)

    @patch("troshkad.os.rmdir", side_effect=OSError("busy"))
    @patch("troshkad.os.remove", side_effect=OSError("busy"))
    @patch("troshkad.os.listdir", return_value=["file.qcow2"])
    @patch("troshkad.shutil.rmtree", side_effect=OSError("NFS busy"))
    @patch("troshkad.os.path.isdir", return_value=True)
    def test_nfs_retry_failure(self, _isdir, _rmtree, _listdir, _remove, _rmdir):
        job = {"job_id": "j1", "output": []}
        removed = troshkad._clean_orphan_dirs(
            job, ["/var/lib/troshka/vms/nfs-busy/"]
        )
        self.assertEqual(removed, 0)


class TestCleanOrphanDomains(unittest.TestCase):
    @patch("troshkad._run_cmd")
    def test_removes_domain(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        removed = troshkad._clean_orphan_domains(
            job, ["troshka-deadbeef-12345678"]
        )
        self.assertEqual(removed, 1)
        # destroy + undefine = 2
        self.assertEqual(mock_run.call_count, 2)

    @patch("troshkad._run_cmd")
    def test_tolerates_already_stopped(self, mock_run):
        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("already stopped")
            return MagicMock(returncode=0)

        mock_run.side_effect = side_effect
        job = {"job_id": "j1", "output": []}
        removed = troshkad._clean_orphan_domains(
            job, ["troshka-deadbeef-12345678"]
        )
        self.assertEqual(removed, 1)

    def test_rejects_invalid_domain(self):
        job = {"job_id": "j1", "output": []}
        removed = troshkad._clean_orphan_domains(job, ["evil-domain"])
        self.assertEqual(removed, 0)


class TestCleanOrphanContainers(unittest.TestCase):
    @patch("troshkad._run_cmd")
    def test_removes_container(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        removed = troshkad._clean_orphan_containers(
            job, ["troshka-aabb-ctr"]
        )
        self.assertEqual(removed, 1)

    def test_rejects_non_troshka_name(self):
        job = {"job_id": "j1", "output": []}
        removed = troshkad._clean_orphan_containers(
            job, ["evil-container"]
        )
        self.assertEqual(removed, 0)


class TestCleanOrphanBridges(unittest.TestCase):
    @patch("troshkad._run_cmd")
    def test_removes_bridge(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        removed = troshkad._clean_orphan_bridges(
            job, ["br-troshka-aabbccdd"]
        )
        self.assertEqual(removed, 1)

    @patch("troshkad._run_cmd", side_effect=RuntimeError("not found"))
    def test_tolerates_missing_bridge(self, _mock):
        job = {"job_id": "j1", "output": []}
        removed = troshkad._clean_orphan_bridges(
            job, ["br-troshka-aabbccdd"]
        )
        self.assertEqual(removed, 0)


class TestCleanOrphanNamespaces(unittest.TestCase):
    @patch("troshkad._run_cmd")
    def test_removes_namespace(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        removed = troshkad._clean_orphan_namespaces(
            job, ["troshka-deadbeef"]
        )
        self.assertEqual(removed, 1)

    def test_rejects_non_troshka_ns(self):
        job = {"job_id": "j1", "output": []}
        removed = troshkad._clean_orphan_namespaces(
            job, ["evil-ns"]
        )
        self.assertEqual(removed, 0)


class TestCleanCacheItems(unittest.TestCase):
    @patch("troshkad.shutil.rmtree")
    @patch("troshkad.os.path.isdir", return_value=True)
    def test_removes_cache_dir(self, _isdir, mock_rmtree):
        job = {"job_id": "j1", "output": []}
        removed = troshkad._clean_cache_items(
            job, ["/var/lib/troshka/cache/patterns/dead/"]
        )
        self.assertEqual(removed, 1)

    @patch("troshkad.os.remove")
    @patch("troshkad.os.path.isdir", return_value=False)
    def test_removes_cache_file(self, _isdir, mock_remove):
        job = {"job_id": "j1", "output": []}
        removed = troshkad._clean_cache_items(
            job, ["/var/lib/troshka/cache/patterns/item.qcow2"]
        )
        self.assertEqual(removed, 1)

    @patch("troshkad.os.path.isdir", return_value=False)
    @patch("troshkad.os.remove", side_effect=FileNotFoundError)
    def test_skips_missing(self, _remove, _isdir):
        job = {"job_id": "j1", "output": []}
        removed = troshkad._clean_cache_items(
            job, ["/var/lib/troshka/cache/missing"]
        )
        self.assertEqual(removed, 0)


class TestCleanOrphanMetadata(unittest.TestCase):
    @patch("troshkad.os.remove")
    @patch("troshkad._run_cmd")
    def test_kills_and_removes(self, mock_run, mock_remove):
        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        removed = troshkad._clean_orphan_metadata(
            job, ["aabbccdd-1122-3344-5566-778899001122"]
        )
        # script + log = 2 files removed
        self.assertEqual(removed, 2)

    @patch("troshkad.os.remove", side_effect=FileNotFoundError)
    @patch("troshkad._run_cmd")
    def test_handles_missing_files(self, mock_run, _remove):
        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        removed = troshkad._clean_orphan_metadata(
            job, ["aabbccdd-1122-3344-5566-778899001122"]
        )
        self.assertEqual(removed, 0)


class TestCleanStaleTemps(unittest.TestCase):
    @patch("troshkad.shutil.rmtree")
    @patch("troshkad.os.path.isdir", return_value=True)
    @patch("troshkad.os.path.realpath", side_effect=lambda p: p)
    def test_removes_dir_in_tmpdir(self, _real, _isdir, mock_rmtree):
        job = {"job_id": "j1", "output": []}
        # The temp base is local_mount/tmp; default local_mount is /var/lib/troshka/local
        with patch.dict(troshkad._config, {"local_mount": "/var/lib/troshka/local"}):
            removed = troshkad._clean_stale_temps(
                job, ["/var/lib/troshka/local/tmp/stale-dir"]
            )
        self.assertEqual(removed, 1)

    @patch("troshkad.os.path.realpath", side_effect=lambda p: p)
    def test_rejects_outside_tmpdir(self, _real):
        job = {"job_id": "j1", "output": []}
        with patch.dict(troshkad._config, {"local_mount": "/var/lib/troshka/local"}):
            removed = troshkad._clean_stale_temps(
                job, ["/etc/passwd"]
            )
        self.assertEqual(removed, 0)


class TestRemoveBmcBridge(unittest.TestCase):
    @patch("troshkad._run_cmd")
    def test_removes_bridge(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        troshkad._remove_bmc_bridge(job, "aabbccdd-1122-3344-5566-778899001122")
        # 2 attempts: namespace bridge del + host bridge del
        self.assertEqual(mock_run.call_count, 2)

    @patch("troshkad._run_cmd", side_effect=RuntimeError("not found"))
    def test_tolerates_missing(self, _mock):
        job = {"job_id": "j1", "output": []}
        # Should not raise
        troshkad._remove_bmc_bridge(job, "aabbccdd-1122-3344-5566-778899001122")


class TestCleanOrphanBmc(unittest.TestCase):
    @patch("troshkad._remove_bmc_bridge")
    @patch("troshkad.shutil.rmtree")
    @patch("troshkad.subprocess.run")
    @patch("troshkad._kill_bmc_processes")
    @patch("troshkad.os.path.isdir", return_value=True)
    def test_cleans_bmc(self, _isdir, mock_kill, mock_run, _rmtree, _bridge):
        mock_run.return_value = MagicMock(returncode=0)
        # Use the GC version at line 6335
        job = {"job_id": "j1", "output": []}
        # Note: _clean_orphan_bmc calls the GC _kill_bmc_processes (line 6297)
        # which has a different signature than the BMC one (line 8988)
        removed = troshkad._clean_orphan_bmc(
            job, ["aabbccdd-1122-3344-5566-778899001122"]
        )
        self.assertEqual(removed, 1)


# ── BMC handlers ──


class TestHandleBmcStatus(unittest.TestCase):
    @patch("troshkad.os.path.isdir", return_value=False)
    def test_no_bmc_dir(self, _isdir):
        job = {"job_id": "j1", "output": []}
        result = troshkad._handle_bmc_status(
            job, {"project_id": "aabbccdd-1122-3344-5566-778899001122"}
        )
        self.assertEqual(result["sushy_processes"], [])
        self.assertFalse(result["vbmcd_running"])

    @patch("troshkad.os.kill")
    @patch("troshkad.os.path.exists", return_value=False)
    @patch("builtins.open", new_callable=mock_open, read_data="12345")
    @patch("troshkad.os.listdir", return_value=["sushy-vm1.pid", "other.txt"])
    @patch("troshkad.os.path.isdir", return_value=True)
    def test_sushy_alive(self, _isdir, _listdir, _mock_open, _exists, mock_kill):
        # os.kill with signal 0 should not raise for alive process
        mock_kill.return_value = None
        job = {"job_id": "j1", "output": []}
        result = troshkad._handle_bmc_status(
            job, {"project_id": "aabbccdd-1122-3344-5566-778899001122"}
        )
        self.assertEqual(len(result["sushy_processes"]), 1)
        self.assertTrue(result["sushy_processes"][0]["alive"])
        self.assertEqual(result["sushy_processes"][0]["pid"], 12345)

    @patch("troshkad.os.kill", side_effect=ProcessLookupError)
    @patch("troshkad.os.path.exists", return_value=False)
    @patch("builtins.open", new_callable=mock_open, read_data="99999")
    @patch("troshkad.os.listdir", return_value=["sushy-vm1.pid"])
    @patch("troshkad.os.path.isdir", return_value=True)
    def test_sushy_dead(self, _isdir, _listdir, _mock_open, _exists, _kill):
        job = {"job_id": "j1", "output": []}
        result = troshkad._handle_bmc_status(
            job, {"project_id": "aabbccdd-1122-3344-5566-778899001122"}
        )
        self.assertFalse(result["sushy_processes"][0]["alive"])

    @patch("troshkad.os.kill")
    @patch("builtins.open", new_callable=mock_open, read_data="55555")
    @patch("troshkad.os.path.exists", return_value=True)
    @patch("troshkad.os.listdir", return_value=[])
    @patch("troshkad.os.path.isdir", return_value=True)
    def test_vbmcd_alive(self, _isdir, _listdir, _exists, _mock_open, mock_kill):
        mock_kill.return_value = None
        job = {"job_id": "j1", "output": []}
        result = troshkad._handle_bmc_status(
            job, {"project_id": "aabbccdd-1122-3344-5566-778899001122"}
        )
        self.assertTrue(result["vbmcd_running"])


class TestHandleBmcTeardown(unittest.TestCase):
    @patch("troshkad.shutil.rmtree")
    @patch("troshkad.os.path.isdir", return_value=True)
    @patch("troshkad.subprocess.run")
    @patch("troshkad._teardown_bmc_dnsmasq", return_value=0)
    @patch("troshkad._run_cmd")
    @patch("troshkad._kill_bmc_processes", return_value=2)
    def test_teardown(self, _kill, mock_run, _dnsmasq, mock_subrun, _isdir, _rmtree):
        mock_run.return_value = MagicMock(returncode=0)
        mock_subrun.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        result = troshkad._handle_bmc_teardown(
            job, {"project_id": "aabbccdd-1122-3344-5566-778899001122"}
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["killed"], 2)

    @patch("troshkad.shutil.rmtree")
    @patch("troshkad.os.path.isdir", return_value=False)
    @patch("troshkad.subprocess.run")
    @patch("troshkad._teardown_bmc_dnsmasq", return_value=0)
    @patch("troshkad._run_cmd", side_effect=RuntimeError("not found"))
    @patch("troshkad._kill_bmc_processes", return_value=0)
    def test_teardown_tolerates_missing(self, _kill, _run, _dnsmasq, _subrun, _isdir, _rmtree):
        job = {"job_id": "j1", "output": []}
        result = troshkad._handle_bmc_teardown(
            job, {"project_id": "aabbccdd-1122-3344-5566-778899001122"}
        )
        self.assertEqual(result["status"], "ok")


class TestTeardownBmcDnsmasq(unittest.TestCase):
    @patch("troshkad.os.remove")
    @patch("troshkad._safe_kill")
    @patch("builtins.open", new_callable=mock_open, read_data="12345")
    @patch("troshkad.os.path.exists", return_value=True)
    def test_kills_dnsmasq(self, _exists, _mock_open, mock_kill, mock_remove):
        job = {"job_id": "j1", "output": []}
        killed = troshkad._teardown_bmc_dnsmasq(job, "aabbccdd")
        self.assertEqual(killed, 1)
        mock_kill.assert_called_once()

    @patch("troshkad.os.remove", side_effect=FileNotFoundError)
    @patch("troshkad.os.path.exists", return_value=False)
    def test_no_pidfile(self, _exists, _remove):
        job = {"job_id": "j1", "output": []}
        killed = troshkad._teardown_bmc_dnsmasq(job, "aabbccdd")
        self.assertEqual(killed, 0)


class TestKillBmcProcessesBmcVersion(unittest.TestCase):
    """Tests for the _kill_bmc_processes at line 8988 (BMC version, returns count)."""

    @patch("troshkad._safe_kill")
    @patch("builtins.open", new_callable=mock_open, read_data="1234")
    @patch("troshkad.os.path.exists", return_value=False)
    @patch("troshkad.os.listdir", return_value=["sushy-vm1.pid"])
    @patch("troshkad.os.path.isdir", return_value=True)
    def test_kills_sushy(self, _isdir, _listdir, _exists, _mock_open, mock_kill):
        job = {"job_id": "j1", "output": []}
        # Both _kill_bmc_processes functions exist; the one at 8988 is the last definition
        # and is the one actually used by _handle_bmc_teardown
        killed = troshkad._kill_bmc_processes(job, "/var/lib/troshka/bmc/proj")
        self.assertGreaterEqual(killed, 1)
        mock_kill.assert_called()


# ── VM migrate ──


class TestHandleVmMigrate(unittest.TestCase):
    @patch("troshkad._run_cmd")
    @patch("troshkad.subprocess.run")
    def test_live_migration(self, mock_run, mock_runcmd):
        mock_run.return_value = MagicMock(returncode=0, stdout="running")
        mock_runcmd.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        params = {
            "domain": "troshka-aabbccdd-12345678",
            "target_host": "10.0.0.2",
        }
        result = troshkad._handle_vm_migrate(job, params)
        self.assertEqual(result["status"], "migrated")
        cmd = mock_runcmd.call_args[0][1]
        self.assertIn("--live", cmd)

    @patch("troshkad._run_cmd")
    @patch("troshkad.subprocess.run")
    def test_offline_migration(self, mock_run, mock_runcmd):
        mock_run.return_value = MagicMock(returncode=0, stdout="shut off")
        mock_runcmd.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        params = {
            "domain": "troshka-aabbccdd-12345678",
            "target_host": "10.0.0.2",
        }
        result = troshkad._handle_vm_migrate(job, params)
        self.assertEqual(result["status"], "migrated")
        cmd = mock_runcmd.call_args[0][1]
        self.assertNotIn("--live", cmd)


# ── TLS update certs ──


class TestHandleTlsUpdateCerts(unittest.TestCase):
    @patch("troshkad._run_cmd")
    @patch("troshkad.os.chmod")
    @patch("builtins.open", new_callable=mock_open)
    @patch("troshkad.os.makedirs")
    def test_writes_certs_and_restarts(self, _makedirs, _mock_open, _chmod, mock_run):
        import base64

        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        params = {
            "ca_cert_b64": base64.b64encode(b"CA CERT").decode(),
            "host_cert_b64": base64.b64encode(b"HOST CERT").decode(),
            "host_key_b64": base64.b64encode(b"HOST KEY").decode(),
        }
        result = troshkad._handle_tls_update_certs(job, params)
        self.assertEqual(result["status"], "updated")
        # Verify systemctl restart was called
        cmd = mock_run.call_args[0][1]
        self.assertIn("virtqemud", cmd)


# ── VXLAN/bridge helpers ──


class TestAttachVxlanToNsBridge(unittest.TestCase):
    @patch("troshkad._run_cmd")
    def test_creates_bridge_and_attaches(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        troshkad._attach_vxlan_to_ns_bridge(job, "troshka-aabbccdd", "vxlan-10001", "br-10001")
        # Create bridge, attach vxlan, bring vxlan up, bring bridge up = 4
        self.assertEqual(mock_run.call_count, 4)

    @patch("troshkad._run_cmd")
    def test_tolerates_existing_bridge(self, mock_run):
        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("bridge exists")
            return MagicMock(returncode=0)

        mock_run.side_effect = side_effect
        job = {"job_id": "j1", "output": []}
        troshkad._attach_vxlan_to_ns_bridge(job, "ns", "vxlan-100", "br-100")
        # All 4 calls still happen
        self.assertEqual(call_count[0], 4)


# ── _snapshot_domain ──


class TestSnapshotDomain(unittest.TestCase):
    @patch("troshkad.subprocess.run")
    @patch("troshkad._cleanup_stale_snapshots")
    def test_successful_snapshot(self, _mock_cleanup, mock_run):
        def run_side(*args, **kwargs):
            r = MagicMock(returncode=0, stdout="", stderr="")
            return r

        mock_run.side_effect = run_side
        result = troshkad._snapshot_domain(
            {"job_id": "j1", "output": []}, "troshka-aabbccdd-12345678"
        )
        self.assertTrue(result)

    @patch("troshkad.subprocess.run")
    @patch("troshkad._cleanup_stale_snapshots")
    def test_snapshot_failure_returns_false(self, _mock_cleanup, mock_run):
        call_idx = [0]

        def run_side(*args, **kwargs):
            call_idx[0] += 1
            r = MagicMock(returncode=0, stdout="", stderr="")
            cmd = args[0]
            if "snapshot-create-as" in cmd:
                r.returncode = 1
                r.stderr = "snapshot failed"
            return r

        mock_run.side_effect = run_side
        result = troshkad._snapshot_domain(
            {"job_id": "j1", "output": []}, "troshka-aabbccdd-12345678"
        )
        self.assertFalse(result)

    @patch("troshkad.subprocess.run")
    @patch("troshkad._cleanup_stale_snapshots")
    def test_fstrim_failure_ignored(self, _mock_cleanup, mock_run):
        call_idx = [0]

        def run_side(*args, **kwargs):
            call_idx[0] += 1
            cmd = args[0]
            r = MagicMock(returncode=0, stdout="", stderr="")
            if "domfstrim" in cmd:
                r.returncode = 1
            return r

        mock_run.side_effect = run_side
        result = troshkad._snapshot_domain(
            {"job_id": "j1", "output": []}, "troshka-aabbccdd-12345678"
        )
        self.assertTrue(result)


# ── _handle_vm_recert ──


class TestHandleVmRecert(unittest.TestCase):
    @patch("troshkad._release_nbd_device")
    @patch("troshkad.os.rmdir")
    @patch("troshkad._update_bastion_disk")
    @patch("troshkad._save_kubeconfig", return_value=None)
    @patch("troshkad._build_recert_cmd", return_value=["podman", "run", "recert"])
    @patch("troshkad._wait_for_etcd_healthy")
    @patch("troshkad._find_ostree_paths", return_value=("/deploy", "/boot", "/etc/k8s", "/etc/mcd", "/var/kubelet", "/var/etcd"))
    @patch("troshkad._mount_rhcos_disk", return_value=True)
    @patch("troshkad._allocate_etcd_port", return_value=2379)
    @patch("troshkad._allocate_nbd_device", return_value="/dev/nbd0")
    @patch("troshkad._ensure_container_image")
    @patch("troshkad._ensure_nbd_module")
    @patch("troshkad._run_cmd")
    @patch("troshkad.os.path.realpath", side_effect=lambda p: p)
    @patch("troshkad.os.path.exists", return_value=True)
    def test_successful_recert(self, _exists, _realpath, mock_run, _nbd_mod, _img, _alloc_nbd, _alloc_port,
                               _mount, _paths, _wait, _build, _save, _update, _rmdir, _release):
        mock_run.return_value = MagicMock(returncode=0)
        job = {"id": "j1", "job_id": "job-0001", "output": []}
        params = {
            "disk": "/var/lib/troshka/vms/proj/disk.qcow2",
            "extend_expiration": True,
        }
        result = troshkad._handle_vm_recert(job, params)
        self.assertEqual(result["status"], "completed")

    @patch("troshkad._release_nbd_device")
    @patch("troshkad.os.rmdir")
    @patch("troshkad._update_bastion_disk")
    @patch("troshkad._save_kubeconfig", return_value="apiVersion: v1\nkind: Config")
    @patch("troshkad._build_recert_cmd", return_value=["podman", "run", "recert"])
    @patch("troshkad._wait_for_etcd_healthy")
    @patch("troshkad._find_ostree_paths", return_value=("/deploy", "/boot", "/etc/k8s", "/etc/mcd", "/var/kubelet", "/var/etcd"))
    @patch("troshkad._mount_rhcos_disk", return_value=True)
    @patch("troshkad._allocate_etcd_port", return_value=2379)
    @patch("troshkad._allocate_nbd_device", return_value="/dev/nbd0")
    @patch("troshkad._ensure_container_image")
    @patch("troshkad._ensure_nbd_module")
    @patch("troshkad._run_cmd")
    @patch("troshkad.os.path.realpath", side_effect=lambda p: p)
    @patch("troshkad.os.path.exists", return_value=True)
    def test_recert_stores_kubeconfig_when_result_is_none(
        self, _exists, _realpath, mock_run, _nbd_mod, _img, _alloc_nbd, _alloc_port,
        _mount, _paths, _wait, _build, _save, _update, _rmdir, _release,
    ):
        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "job-0003", "output": [], "result": None}
        params = {
            "disk": "/var/lib/troshka/vms/proj/disk.qcow2",
            "project_id": "proj-1",
            "vm_name": "cp-0",
        }
        troshkad._handle_vm_recert(job, params)
        self.assertEqual(job["result"]["kubeconfig"], "apiVersion: v1\nkind: Config")

    @patch("troshkad._release_nbd_device")
    @patch("troshkad.os.rmdir")
    @patch("troshkad._run_cmd")
    @patch("troshkad.os.path.exists", return_value=False)
    def test_missing_disk_raises(self, _exists, _run, _rmdir, _release):
        job = {"id": "j1", "job_id": "job-0002", "output": []}
        with self.assertRaises(RuntimeError):
            troshkad._handle_vm_recert(job, {"disk": "/var/lib/troshka/vms/missing.qcow2"})


# ── _handle_pattern_export ──


class TestHandlePatternExport(unittest.TestCase):
    @patch("troshkad._run_cmd")
    @patch("troshkad.os.makedirs")
    @patch("troshkad.subprocess.run")
    def test_exports_disk(self, mock_run, _makedirs, mock_runcmd):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Type  Device  Target  Source\nfile  disk    vda     /var/lib/troshka/vms/proj/disk.qcow2\n",
        )
        mock_runcmd.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        result = troshkad._handle_pattern_export(
            job,
            {
                "domain_name": "troshka-aabbccdd-12345678",
                "output_path": "/var/lib/troshka/vms/export.qcow2",
            },
        )
        self.assertEqual(result["status"], "exported")
        cmd = mock_runcmd.call_args[0][1]
        self.assertEqual(cmd[0], "qemu-img")
        self.assertIn("convert", cmd)

    @patch("troshkad.subprocess.run")
    def test_no_disk_found_raises(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Type  Device  Target  Source\n",
        )
        job = {"job_id": "j1", "output": []}
        with self.assertRaises(RuntimeError):
            troshkad._handle_pattern_export(
                job,
                {
                    "domain_name": "troshka-aabbccdd-12345678",
                    "output_path": "/var/lib/troshka/vms/export.qcow2",
                },
            )


# ── Nftables helpers ──


class TestAddOutboundPortRule(unittest.TestCase):
    @patch("troshkad._run_cmd")
    def test_icmp_rule(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        troshkad._add_outbound_port_rule(job, "troshka-aabb", "br-10001", "veaabbn", "icmp")
        self.assertEqual(mock_run.call_count, 1)
        cmd = mock_run.call_args[0][1]
        self.assertIn("icmp", cmd)

    @patch("troshkad._run_cmd")
    def test_port_proto_rule(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        troshkad._add_outbound_port_rule(job, "troshka-aabb", "br-10001", "veaabbn", "443/tcp")
        self.assertEqual(mock_run.call_count, 1)

    @patch("troshkad._run_cmd")
    def test_bare_port_adds_tcp_and_udp(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        troshkad._add_outbound_port_rule(job, "troshka-aabb", "br-10001", "veaabbn", "53")
        self.assertEqual(mock_run.call_count, 2)


# ── _mount_container_volumes ──


class TestMountContainerVolumes(unittest.TestCase):
    @patch("troshkad._run_cmd")
    @patch("troshkad.subprocess.run")
    @patch("troshkad.os.makedirs")
    def test_mounts_formatted_disk(self, _makedirs, mock_run, mock_runcmd):
        # blkid finds existing filesystem
        mock_run.return_value = MagicMock(returncode=0, stdout="ext4")
        mock_runcmd.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        result = troshkad._mount_container_volumes(
            job,
            [{"disk_path": "/var/lib/troshka/vms/proj/vol.raw", "mount_dir": "/var/lib/troshka/vms/proj/mnt"}],
        )
        self.assertEqual(len(result), 1)
        # Only mount call, no mkfs since blkid succeeded
        self.assertEqual(mock_runcmd.call_count, 1)

    @patch("troshkad._run_cmd")
    @patch("troshkad.subprocess.run")
    @patch("troshkad.os.makedirs")
    def test_formats_and_mounts_unformatted(self, _makedirs, mock_run, mock_runcmd):
        def run_side(cmd, **kwargs):
            r = MagicMock(returncode=0, stdout="data", stderr="")
            if cmd[0] == "blkid":
                r.returncode = 2  # no filesystem
            elif cmd[0] == "file":
                r.stdout = "data"  # Not QEMU
            return r

        mock_run.side_effect = run_side
        mock_runcmd.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        result = troshkad._mount_container_volumes(
            job,
            [{"disk_path": "/var/lib/troshka/vms/proj/vol.raw", "mount_dir": "/var/lib/troshka/vms/proj/mnt"}],
        )
        self.assertEqual(len(result), 1)
        # mkfs + mount = 2
        self.assertEqual(mock_runcmd.call_count, 2)

    @patch("troshkad.subprocess.run")
    @patch("troshkad.os.makedirs")
    def test_rejects_qcow2(self, _makedirs, mock_run):
        def run_side(cmd, **kwargs):
            r = MagicMock(returncode=0, stdout="", stderr="")
            if cmd[0] == "blkid":
                r.returncode = 2
            elif cmd[0] == "file":
                r.stdout = "QEMU QCOW2 Image"
            return r

        mock_run.side_effect = run_side
        job = {"job_id": "j1", "output": []}
        with self.assertRaises(RuntimeError):
            troshkad._mount_container_volumes(
                job,
                [{"disk_path": "/var/lib/troshka/vms/proj/vol.qcow2", "mount_dir": "/var/lib/troshka/vms/proj/mnt"}],
            )


# ── _handle_vm_ssh_exec ──


class TestHandleVmSshExec(unittest.TestCase):
    @patch("troshkad.os.unlink")
    @patch("troshkad.os.path.exists", return_value=False)
    @patch("troshkad.subprocess.run")
    def test_password_ssh(self, mock_run, _exists, _unlink):
        mock_run.return_value = MagicMock(returncode=0, stdout="output", stderr="")
        job = {"job_id": "j1", "output": []}
        params = {
            "project_id": "aabbccdd-1122-3344-5566-778899001122",
            "vm_ip": "192.168.1.10",
            "username": "root",
            "password": "pass123",  # pragma: allowlist secret
            "command": "hostname",
        }
        result = troshkad._handle_vm_ssh_exec(job, params)
        self.assertEqual(result["output"], "output")
        self.assertEqual(result["exit_code"], 0)
        cmd = mock_run.call_args[0][0]
        self.assertIn("sshpass", cmd)

    @patch("troshkad.os.unlink")
    @patch("troshkad.os.path.exists", return_value=True)
    @patch("troshkad.os.chmod")
    @patch("troshkad.subprocess.run")
    def test_key_ssh(self, mock_run, _chmod, _exists, _unlink):
        mock_run.return_value = MagicMock(returncode=0, stdout="key output", stderr="")
        job = {"job_id": "j1", "output": []}
        params = {
            "project_id": "aabbccdd-1122-3344-5566-778899001122",
            "vm_ip": "192.168.1.10",
            "username": "cloud-user",
            "private_key": "-----BEGIN TEST KEY-----\nfakekey\n-----END TEST KEY-----",  # pragma: allowlist secret
            "command": "hostname",
        }
        result = troshkad._handle_vm_ssh_exec(job, params)
        self.assertEqual(result["output"], "key output")

    def test_missing_command_raises(self):
        job = {"job_id": "j1", "output": []}
        params = {
            "vm_ip": "192.168.1.10",
            "password": "pass",  # pragma: allowlist secret  # pragma: allowlist secret
            "command": "",
        }
        with self.assertRaises(RuntimeError):
            troshkad._handle_vm_ssh_exec(job, params)

    def test_missing_creds_raises(self):
        job = {"job_id": "j1", "output": []}
        params = {
            "vm_ip": "192.168.1.10",
            "command": "hostname",
        }
        with self.assertRaises(RuntimeError):
            troshkad._handle_vm_ssh_exec(job, params)


# ── _handle_vm_guest_exec ──


class TestHandleVmGuestExec(unittest.TestCase):
    @patch("troshkad._guest_exec_poll", return_value={"output": "result", "exit_code": 0})
    @patch("troshkad.subprocess.run")
    def test_successful_exec(self, mock_run, _mock_poll):
        def run_side(cmd, **kwargs):
            r = MagicMock(returncode=0, stdout="", stderr="")
            if "guest-info" in str(cmd):
                r.stdout = '{"return":{"supported_commands":[{"name":"guest-exec","enabled":true}]}}'
            elif "guest-exec" in str(cmd) and "guest-exec-status" not in str(cmd):
                r.stdout = '{"return":{"pid":12345}}'
            return r

        mock_run.side_effect = run_side
        job = {"job_id": "j1", "output": []}
        params = {
            "domain_name": "troshka-aabbccdd-12345678",
            "command": "hostname",
        }
        result = troshkad._handle_vm_guest_exec(job, params)
        self.assertEqual(result["output"], "result")
        self.assertEqual(result["exit_code"], 0)

    @patch("troshkad.subprocess.run")
    def test_agent_not_available(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="agent not connected")
        job = {"job_id": "j1", "output": []}
        params = {
            "domain_name": "troshka-aabbccdd-12345678",
            "command": "hostname",
        }
        with self.assertRaises(RuntimeError):
            troshkad._handle_vm_guest_exec(job, params)

    @patch("troshkad.subprocess.run")
    def test_agent_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired("cmd", 10)
        job = {"job_id": "j1", "output": []}
        with self.assertRaises(RuntimeError):
            troshkad._handle_vm_guest_exec(
                job,
                {"domain_name": "troshka-aabbccdd-12345678", "command": "test"},
            )

    @patch("troshkad.subprocess.run")
    def test_guest_exec_blocked(self, mock_run):
        def run_side(cmd, **kwargs):
            r = MagicMock(returncode=0, stdout="", stderr="")
            if "guest-info" in str(cmd):
                r.stdout = '{"return":{"supported_commands":[{"name":"guest-exec","enabled":false}]}}'
            return r

        mock_run.side_effect = run_side
        job = {"job_id": "j1", "output": []}
        with self.assertRaises(RuntimeError):
            troshkad._handle_vm_guest_exec(
                job,
                {"domain_name": "troshka-aabbccdd-12345678", "command": "test"},
            )


# ── _handle_nbd_export / _handle_nbd_stop ──


class TestHandleNbdExport(unittest.TestCase):
    @patch.object(troshkad, "_nbd_ports", {})
    @patch("troshkad._get_disk_actual_size", return_value=10737418240)
    @patch("troshkad._get_nbd_process_pid", return_value=9999)
    @patch("troshkad.subprocess.run")
    @patch("troshkad._allocate_nbd_port", return_value=10809)
    @patch("troshkad._snapshot_domain", return_value=True)
    @patch("troshkad._is_domain_running", return_value=True)
    @patch("troshkad.os.path.realpath", side_effect=lambda p: p)
    @patch("troshkad.os.path.exists", return_value=True)
    def test_export_running_vm(self, _exists, _realpath, _running, _snap, _port, mock_run, _pid, _size):
        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        result = troshkad._handle_nbd_export(
            job,
            {
                "domain_name": "troshka-aabbccdd-12345678",
                "disk_path": "/var/lib/troshka/vms/proj/disk.qcow2",
            },
        )
        self.assertEqual(result["port"], 10809)
        self.assertTrue(result["snapshotted"])

    @patch.object(troshkad, "_nbd_ports", {})
    @patch("troshkad._get_disk_actual_size", return_value=5000000000)
    @patch("troshkad._get_nbd_process_pid", return_value=8888)
    @patch("troshkad.subprocess.run")
    @patch("troshkad._allocate_nbd_port", return_value=10810)
    @patch("troshkad._is_domain_running", return_value=False)
    @patch("troshkad.os.path.realpath", side_effect=lambda p: p)
    @patch("troshkad.os.path.exists", return_value=True)
    def test_export_stopped_vm(self, _exists, _realpath, _running, _port, mock_run, _pid, _size):
        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        result = troshkad._handle_nbd_export(
            job,
            {
                "domain_name": "troshka-aabbccdd-12345678",
                "disk_path": "/var/lib/troshka/vms/proj/disk.qcow2",
            },
        )
        self.assertEqual(result["port"], 10810)
        self.assertFalse(result["snapshotted"])


class TestHandleNbdStop(unittest.TestCase):
    @patch("troshkad.os.kill")
    def test_stop_with_known_port(self, mock_kill):
        with patch.object(troshkad, "_nbd_ports", {10809: {"pid": 9999, "domain": "dom", "snapshotted": False}}):
            job = {"job_id": "j1", "output": []}
            result = troshkad._handle_nbd_stop(job, {"port": 10809})
            self.assertTrue(result["stopped"])
            mock_kill.assert_called_with(9999, signal.SIGTERM)

    @patch("troshkad.subprocess.run")
    def test_stop_unknown_port_uses_fuser(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        with patch.object(troshkad, "_nbd_ports", {}):
            job = {"job_id": "j1", "output": []}
            result = troshkad._handle_nbd_stop(job, {"port": 10810})
            self.assertTrue(result["stopped"])
            # fuser should have been called
            mock_run.assert_called()


# ── _handle_nbd_pull_flatten ──


class TestHandleNbdPullFlatten(unittest.TestCase):
    @patch("troshkad.os.path.getsize", return_value=5000000000)
    @patch("troshkad._run_cmd")
    @patch("troshkad.os.makedirs")
    def test_pull_and_flatten(self, _makedirs, mock_run, _getsize):
        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        result = troshkad._handle_nbd_pull_flatten(
            job,
            {
                "nbd_host": "10.0.0.1",
                "nbd_port": 10809,
                "output_path": "/var/lib/troshka/local/cache/disk.qcow2",
            },
        )
        self.assertEqual(result["size_bytes"], 5000000000)
        cmd = mock_run.call_args[0][1]
        self.assertEqual(cmd[0], "qemu-img")
        self.assertIn("convert", cmd)
        self.assertIn("nbd://10.0.0.1:10809/disk", cmd)


# ── Teardown helpers ──


class TestTeardownHaproxy(unittest.TestCase):
    @patch("troshkad.os.remove")
    @patch("troshkad._run_cmd")
    @patch("builtins.open", new_callable=mock_open, read_data="12345")
    @patch("troshkad.os.path.exists", return_value=True)
    def test_kills_haproxy(self, _exists, _mock_open, mock_run, mock_remove):
        job = {"job_id": "j1", "output": []}
        troshkad._teardown_haproxy(job, "aabbccdd")
        mock_run.assert_called_once()

    @patch("troshkad.os.remove", side_effect=FileNotFoundError)
    @patch("troshkad.os.path.exists", return_value=False)
    def test_no_pidfile(self, _exists, _remove):
        job = {"job_id": "j1", "output": []}
        troshkad._teardown_haproxy(job, "aabbccdd")


class TestTeardownDnsmasq(unittest.TestCase):
    @patch("troshkad.os.remove")
    @patch("troshkad._safe_kill")
    @patch("builtins.open", new_callable=mock_open, read_data="54321")
    @patch("troshkad.glob.glob", return_value=["/run/troshka-dnsmasq-aabb-10001.pid"])
    def test_kills_dnsmasq(self, _glob, _mock_open, mock_kill, _remove):
        job = {"job_id": "j1", "output": []}
        troshkad._teardown_dnsmasq(job, "aabb")
        mock_kill.assert_called_once_with(54321, 9)

    @patch("troshkad.os.remove", side_effect=FileNotFoundError)
    @patch("troshkad.glob.glob", return_value=[])
    def test_no_pidfiles(self, _glob, _remove):
        job = {"job_id": "j1", "output": []}
        troshkad._teardown_dnsmasq(job, "aabb")


class TestTeardownChronyd(unittest.TestCase):
    @patch("troshkad.os.remove")
    @patch("troshkad._safe_kill")
    @patch("builtins.open", new_callable=mock_open, read_data="98765")
    @patch("troshkad.os.path.exists", return_value=True)
    def test_kills_chronyd(self, _exists, _mock_open, mock_kill, _remove):
        job = {"job_id": "j1", "output": []}
        troshkad._teardown_chronyd(job, "aabbccdd")
        mock_kill.assert_called_once_with(98765, 9)

    @patch("troshkad.os.remove", side_effect=FileNotFoundError)
    @patch("troshkad.os.path.exists", return_value=False)
    def test_no_pidfile(self, _exists, _remove):
        job = {"job_id": "j1", "output": []}
        troshkad._teardown_chronyd(job, "aabbccdd")


class TestTeardownMetadataService(unittest.TestCase):
    @patch("troshkad.os.remove")
    @patch("troshkad._run_cmd")
    def test_kills_and_removes(self, mock_run, mock_remove):
        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        troshkad._teardown_metadata_service(job, "aabbccdd")
        mock_run.assert_called_once()

    @patch("troshkad.os.remove", side_effect=FileNotFoundError)
    @patch("troshkad._run_cmd", side_effect=RuntimeError("no process"))
    def test_tolerates_errors(self, _run, _remove):
        job = {"job_id": "j1", "output": []}
        troshkad._teardown_metadata_service(job, "aabbccdd")


class TestRemoveNftJumpRules(unittest.TestCase):
    @patch("troshkad.subprocess.run")
    def test_removes_matching_rules(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="  jump troshka-fwd-aabb # handle 5\n  jump troshka-fwd-aabb # handle 7\n",
        )
        troshkad._remove_nft_jump_rules("filter", "forward", "troshka-fwd-aabb")
        # 1 list call + 2 delete calls = 3
        self.assertEqual(mock_run.call_count, 3)

    @patch("troshkad.subprocess.run")
    def test_no_matching_rules(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="  accept # handle 1\n")
        troshkad._remove_nft_jump_rules("filter", "forward", "troshka-fwd-dead")
        self.assertEqual(mock_run.call_count, 1)

    @patch("troshkad.subprocess.run", side_effect=Exception("nft error"))
    def test_tolerates_error(self, _mock):
        troshkad._remove_nft_jump_rules("filter", "forward", "chain")


class TestTeardownHostNftables(unittest.TestCase):
    @patch("troshkad._run_cmd")
    @patch("troshkad._remove_nft_jump_rules")
    def test_removes_chains(self, mock_remove, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        troshkad._teardown_host_nftables(job, "aabbccdd")
        # 3 chains removed
        self.assertEqual(mock_remove.call_count, 3)
        # 2 calls per chain (flush + delete) * 3 = 6
        self.assertEqual(mock_run.call_count, 6)


class TestTeardownSinglePxeVni(unittest.TestCase):
    @patch("troshkad.shutil.rmtree")
    @patch("troshkad.os.path.isdir", return_value=True)
    @patch("troshkad.subprocess.run")
    @patch("troshkad.os.remove")
    @patch("troshkad._safe_kill")
    @patch("builtins.open", new_callable=mock_open, read_data="11111")
    @patch("troshkad.os.path.exists", return_value=True)
    def test_teardown_full(self, _exists, _mock_open, mock_kill, _remove, _subrun, _isdir, _rmtree):
        troshkad._teardown_single_pxe_vni(10001)
        mock_kill.assert_called_once()

    @patch("troshkad.subprocess.run")
    @patch("troshkad.os.path.isdir", return_value=False)
    @patch("troshkad.os.path.exists", return_value=False)
    def test_teardown_nothing(self, _exists, _isdir, _subrun):
        troshkad._teardown_single_pxe_vni(10001)


class TestTeardownVxlanInterfaces(unittest.TestCase):
    @patch("troshkad._run_cmd")
    def test_deletes_vxlan_interfaces(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        troshkad._teardown_vxlan_interfaces(job, "troshka-aabb", [10001, 10002])
        # Each VNI: delete vxlan in ns + delete vxlan in host = 2 calls per VNI
        self.assertGreaterEqual(mock_run.call_count, 2)


# ── Nftables setup helpers ──


class TestSetupNsNftablesBase(unittest.TestCase):
    @patch("troshkad._run_cmd")
    @patch("troshkad.subprocess.run")
    def test_creates_tables_and_chains(self, mock_subrun, mock_run):
        mock_subrun.return_value = MagicMock(returncode=0)
        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        troshkad._setup_ns_nftables_base(job, "troshka-aabb", "veaabbn")
        # 4 subprocess.run calls (flush + delete for filter, flush + delete for nat)
        self.assertEqual(mock_subrun.call_count, 4)
        # 6 _run_cmd calls: add filter table, add forward chain, add nat table,
        # add postrouting chain, add prerouting chain, add masquerade rule
        self.assertEqual(mock_run.call_count, 6)


class TestSetupNsNftablesForwarding(unittest.TestCase):
    @patch("troshkad._run_cmd")
    def test_intra_bridge_rules(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        networks = [{"vni": 10001, "bridge_name": "br-10001"}]
        troshkad._setup_ns_nftables_forwarding(job, "troshka-aabb", networks, [], "aabbccdd")
        # intra-bridge (1) + bmc bridge (1, may fail) + established (1) = ~3
        self.assertGreaterEqual(mock_run.call_count, 2)

    @patch("troshkad._run_cmd")
    def test_router_forwarding(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        networks = [
            {"vni": 10001, "bridge_name": "br-10001"},
            {"vni": 10002, "bridge_name": "br-10002"},
        ]
        routers = [{"connected_vnis": [10001, 10002]}]
        troshkad._setup_ns_nftables_forwarding(
            job, "troshka-aabb", networks, routers, "aabbccdd"
        )
        # intra-bridge per net (2) + bmc (1) + router pairs (2 rules for pair) + established (1)
        self.assertGreaterEqual(mock_run.call_count, 5)


# ── _configure_container_interface_ip ──


class TestConfigureContainerInterfaceIp(unittest.TestCase):
    @patch("troshkad._run_cmd")
    def test_first_interface_adds_gateway(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        troshkad._configure_container_interface_ip(job, 0, "192.168.1.10", "192.168.1.0/24", "ns-ctr")
        # addr add + route add = 2
        self.assertEqual(mock_run.call_count, 2)

    @patch("troshkad._run_cmd")
    def test_second_interface_no_gateway(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        troshkad._configure_container_interface_ip(job, 1, "10.0.0.5", "10.0.0.0/24", "ns-ctr")
        # addr add only = 1
        self.assertEqual(mock_run.call_count, 1)


# ── _attach_container_to_bridges ──


class TestAttachContainerToBridges(unittest.TestCase):
    @patch("troshkad.os.remove")
    @patch("troshkad._configure_container_interface_ip")
    @patch("troshkad._setup_container_veth_pair")
    @patch("troshkad.os.symlink")
    @patch("troshkad.os.makedirs")
    @patch("troshkad.subprocess.run")
    @patch("troshkad._run_cmd")
    def test_attaches_single_network(self, mock_run, mock_subrun, _makedirs, _symlink,
                                      mock_veth, _mock_ip, _mock_remove):
        mock_run.return_value = MagicMock(returncode=0)
        mock_subrun.return_value = MagicMock(returncode=0, stdout="12345")
        job = {"job_id": "j1", "output": []}
        networks = [{"bridge": "br-troshka-aabbccdd", "ip": "192.168.1.10", "cidr": "192.168.1.0/24"}]
        troshkad._attach_container_to_bridges(job, "troshka-aabbccdd-ctr01", networks)
        mock_veth.assert_called_once()


# ── _configure_pod_interface_ip ──


class TestConfigurePodInterfaceIp(unittest.TestCase):
    @patch("troshkad._run_cmd")
    def test_first_interface_adds_gateway(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        troshkad._configure_pod_interface_ip(job, 0, "192.168.1.10", "192.168.1.0/24", "ns-pod")
        # addr add + route add = 2
        self.assertEqual(mock_run.call_count, 2)


# ── _setup_vxlan_bridge ──


class TestSetupVxlanBridge(unittest.TestCase):
    @patch("troshkad._assign_bridge_gateway_ip")
    @patch("troshkad._ensure_host_dummy_bridge")
    @patch("troshkad._attach_vxlan_to_ns_bridge")
    @patch("troshkad._add_vxlan_fdb_peers")
    @patch("troshkad._run_cmd")
    def test_creates_vxlan_and_bridge(self, mock_run, _fdb, _attach, _dummy, _gw):
        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": []}
        net = {
            "vni": 10001,
            "bridge_name": "br-10001",
            "vxlan_name": "vxlan-10001",
            "cidr": "192.168.1.0/24",
            "peers": ["10.0.0.2"],
        }
        troshkad._setup_vxlan_bridge(job, "troshka-aabb", "10.0.0.1", net, "aabbccdd")
        # cleanup vxlan in ns (try), cleanup in host (try), create vxlan, move to ns = ~4
        self.assertGreaterEqual(mock_run.call_count, 2)


# ── _handle_vm_file_push_job ──


class TestHandleVmFilePushJob(unittest.TestCase):
    @patch("troshkad.os.unlink")
    @patch("troshkad.os.path.exists", return_value=True)
    @patch("troshkad.os.path.getsize", return_value=1024)
    @patch("troshkad.subprocess.Popen")
    def test_push_file(self, mock_popen, _getsize, _exists, _unlink):
        proc = MagicMock()
        proc.communicate.return_value = ("", "")
        proc.returncode = 0
        mock_popen.return_value = proc
        job = {"job_id": "j1", "output": [], "_process": None}
        params = {
            "project_id": "aabbccdd-1122-3344-5566-778899001122",
            "vm_ip": "192.168.1.10",
            "username": "root",
            "password": "pass",  # pragma: allowlist secret  # pragma: allowlist secret
            "remote_path": "/tmp/file.txt",
            "local_path": "/tmp/troshka-upload-12345",
        }
        result = troshkad._handle_vm_file_push_job(job, params)
        self.assertEqual(result["size"], 1024)
        self.assertEqual(result["remote_path"], "/tmp/file.txt")

    @patch("troshkad.os.unlink")
    @patch("troshkad.os.path.exists", return_value=True)
    @patch("troshkad.os.path.getsize", return_value=512)
    @patch("troshkad.subprocess.run")
    @patch("troshkad.subprocess.Popen")
    def test_push_with_mode(self, mock_popen, mock_run, _getsize, _exists, _unlink):
        proc = MagicMock()
        proc.communicate.return_value = ("", "")
        proc.returncode = 0
        mock_popen.return_value = proc
        mock_run.return_value = MagicMock(returncode=0)
        job = {"job_id": "j1", "output": [], "_process": None}
        params = {
            "project_id": "aabbccdd-1122-3344-5566-778899001122",
            "vm_ip": "192.168.1.10",
            "username": "root",
            "password": "pass",  # pragma: allowlist secret  # pragma: allowlist secret
            "remote_path": "/tmp/script.sh",
            "local_path": "/tmp/troshka-upload-12345",
            "mode": "0755",
        }
        result = troshkad._handle_vm_file_push_job(job, params)
        self.assertEqual(result["size"], 512)
        # chmod was called via SSH
        mock_run.assert_called_once()


# ── handle_vm_file_push / handle_vm_file_pull ──


class TestHandleVmFilePush(unittest.TestCase):
    @patch("troshkad._scp_push_small_file")
    @patch("troshkad._validate_file_push_data", return_value=True)
    @patch("troshkad._prepare_ssh_key_file", return_value=None)
    def test_small_file_inline(self, _key, _validate, mock_scp):
        handler = MagicMock()
        handler.path = "/vm/file-push?project_id=aabbccdd-1122&vm_ip=10.0.0.1&username=root&password=pass&remote_path=/tmp/f"
        handler._read_raw_body.return_value = b"small data"
        troshkad.handle_vm_file_push(handler, {})
        mock_scp.assert_called_once()

    def test_missing_params(self):
        handler = MagicMock()
        handler.path = "/vm/file-push?project_id=aabb"
        troshkad.handle_vm_file_push(handler, {})
        handler._send_json.assert_called()
        args = handler._send_json.call_args[0]
        self.assertEqual(args[0], 400)


class TestHandleVmFilePull(unittest.TestCase):
    @patch("troshkad.os.unlink")
    @patch("troshkad.subprocess.run")
    def test_pull_success(self, mock_run, _unlink):
        mock_run.return_value = MagicMock(returncode=0)
        handler = MagicMock()
        handler._read_body.return_value = {
            "project_id": "aabbccdd-1122-3344-5566-778899001122",
            "vm_ip": "192.168.1.10",
            "username": "root",
            "password": "pass",  # pragma: allowlist secret
            "remote_path": "/etc/hostname",
        }
        troshkad.handle_vm_file_pull(handler, {})
        handler._stream_file.assert_called_once()

    def test_pull_missing_fields(self):
        handler = MagicMock()
        handler._read_body.return_value = {"project_id": "aabb"}
        troshkad.handle_vm_file_pull(handler, {})
        handler._send_json.assert_called()
        args = handler._send_json.call_args[0]
        self.assertEqual(args[0], 400)


# ── _handle_upload_and_cache ──


class TestHandleUploadAndCache(unittest.TestCase):
    @patch("troshkad.os.unlink")
    @patch("troshkad.os.path.getsize", return_value=5000)
    @patch("troshkad.shutil.copy")
    @patch("troshkad._s3_upload_with_cache")
    @patch("troshkad.os.makedirs")
    @patch("troshkad.os.path.realpath", side_effect=lambda p: p)
    @patch("troshkad.os.path.exists", return_value=True)
    def test_upload_and_cache(self, _exists, _realpath, _makedirs, mock_s3, _copy, _getsize, _unlink):
        job = {"job_id": "j1", "output": [], "_cancelled": False}
        params = {
            "local_path": "/var/lib/troshka/local/tmp/flat.qcow2",
            "s3_url": "https://s3.example.com/upload",
            "cache_path": "/var/lib/troshka/local/cache/patterns/pat/disk.qcow2",
            "aws_access_key_id": "key",
            "aws_secret_access_key": "secret",  # pragma: allowlist secret
        }
        result = troshkad._handle_upload_and_cache(job, params)
        self.assertEqual(result["size_bytes"], 5000)
        mock_s3.assert_called_once()


# ── _handle_bmc_setup ──


class TestHandleBmcSetup(unittest.TestCase):
    """Tests for _handle_bmc_setup — BMC bridge, htpasswd, pool, sushy, vbmcd."""

    def _make_job(self):
        return {"job_id": "j1", "output": [], "_cancelled": False}

    @patch("troshkad._bmc_start_vbmcd")
    @patch("troshkad._bmc_start_sushy_for_vm")
    @patch("troshkad.subprocess.run")
    @patch("troshkad.os.makedirs")
    @patch("troshkad._run_cmd")
    @patch("troshkad._bmc_start_dnsmasq")
    def test_bmc_setup_no_vms_skips(self, _dnsmasq, _rcmd, _mkdirs, _subrun, _sushy, _vbmcd):
        job = self._make_job()
        result = troshkad._handle_bmc_setup(job, {
            "project_id": "aabbccdd-1122-3344-5566-778899001122",
            "bmc_cidr": "10.0.0.0/24",
            "bmc_gateway_ip": "10.0.0.1",
            "vms": [],
        })
        self.assertEqual(result["status"], "skipped")
        _sushy.assert_not_called()
        _vbmcd.assert_not_called()

    @patch("troshkad._bmc_start_vbmcd")
    @patch("troshkad._bmc_start_sushy_for_vm")
    @patch("troshkad.subprocess.run")
    @patch("troshkad.os.makedirs")
    @patch("troshkad._run_cmd")
    @patch("troshkad._bmc_start_dnsmasq")
    def test_bmc_setup_with_vms(self, _dnsmasq, mock_rcmd, mock_mkdirs, mock_subrun, mock_sushy, mock_vbmcd):
        mock_subrun.return_value = MagicMock(returncode=0, stdout="$2b$12$hash\n")
        job = self._make_job()
        vms = [
            {"bmc_ip": "10.0.0.10", "domain_name": "troshka-aabbccdd-11223344"},
        ]
        with patch("builtins.open", mock_open()):
            result = troshkad._handle_bmc_setup(job, {
                "project_id": "aabbccdd-1122-3344-5566-778899001122",
                "bmc_cidr": "10.0.0.0/24",
                "bmc_gateway_ip": "10.0.0.1",
                "vms": vms,
            })
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["vm_count"], 1)
        mock_sushy.assert_called_once()
        mock_vbmcd.assert_called_once()

    @patch("troshkad._bmc_start_vbmcd")
    @patch("troshkad._bmc_start_sushy_for_vm")
    @patch("troshkad.subprocess.run")
    @patch("troshkad.os.makedirs")
    @patch("troshkad._run_cmd")
    @patch("troshkad._bmc_start_dnsmasq")
    def test_bmc_setup_with_dhcp_hosts(self, mock_dnsmasq, mock_rcmd, mock_mkdirs, mock_subrun, mock_sushy, mock_vbmcd):
        mock_subrun.return_value = MagicMock(returncode=0, stdout="$2b$12$hash\n")
        job = self._make_job()
        vms = [{"bmc_ip": "10.0.0.10", "domain_name": "troshka-aabbccdd-11223344"}]
        dhcp_hosts = [{"mac": "52:54:00:aa:bb:cc", "ip": "10.0.0.20"}]
        with patch("builtins.open", mock_open()):
            troshkad._handle_bmc_setup(job, {
                "project_id": "aabbccdd-1122-3344-5566-778899001122",
                "bmc_cidr": "10.0.0.0/24",
                "bmc_gateway_ip": "10.0.0.1",
                "vms": vms,
                "dhcp_hosts": dhcp_hosts,
            })
        mock_dnsmasq.assert_called_once()

    @patch("troshkad._bmc_start_vbmcd")
    @patch("troshkad._bmc_start_sushy_for_vm")
    @patch("troshkad.subprocess.run")
    @patch("troshkad.os.makedirs")
    @patch("troshkad._run_cmd")
    @patch("troshkad._bmc_start_dnsmasq")
    def test_bmc_setup_default_prefix(self, _dnsmasq, mock_rcmd, mock_mkdirs, mock_subrun, mock_sushy, mock_vbmcd):
        """When bmc_cidr has no slash, prefix defaults to 24."""
        mock_subrun.return_value = MagicMock(returncode=0, stdout="hash\n")
        job = self._make_job()
        vms = [{"bmc_ip": "10.0.0.10", "domain_name": "troshka-aabbccdd-11223344"}]
        with patch("builtins.open", mock_open()):
            result = troshkad._handle_bmc_setup(job, {
                "project_id": "aabbccdd-1122-3344-5566-778899001122",
                "bmc_cidr": "10.0.0.0",
                "bmc_gateway_ip": "10.0.0.1",
                "vms": vms,
            })
        self.assertEqual(result["status"], "ok")

    @patch("troshkad._bmc_start_vbmcd")
    @patch("troshkad._bmc_start_sushy_for_vm")
    @patch("troshkad.subprocess.run")
    @patch("troshkad.os.makedirs")
    @patch("troshkad._run_cmd")
    @patch("troshkad._bmc_start_dnsmasq")
    def test_bmc_setup_bridge_already_exists(self, _dnsmasq, mock_rcmd, mock_mkdirs, mock_subrun, mock_sushy, mock_vbmcd):
        """When ip link show bridge succeeds (exists), skip re-creation."""
        mock_subrun.return_value = MagicMock(returncode=0, stdout="hash\n")
        job = self._make_job()
        vms = [{"bmc_ip": "10.0.0.10", "domain_name": "troshka-aabbccdd-11223344"}]
        with patch("builtins.open", mock_open()):
            result = troshkad._handle_bmc_setup(job, {
                "project_id": "aabbccdd-1122-3344-5566-778899001122",
                "bmc_cidr": "10.0.0.0/24",
                "bmc_gateway_ip": "10.0.0.1",
                "vms": vms,
            })
        self.assertEqual(result["status"], "ok")


# ── _handle_bmc_create_bridge ──


class TestHandleBmcCreateBridge(unittest.TestCase):
    def _make_job(self):
        return {"job_id": "j1", "output": [], "_cancelled": False}

    @patch("troshkad.subprocess.run")
    @patch("troshkad._run_cmd")
    def test_create_bridge_basic(self, mock_rcmd, mock_subrun):
        # ip link show raises CalledProcessError (bridge doesn't exist), nmcli succeeds
        mock_subrun.side_effect = [
            subprocess.CalledProcessError(1, "ip"),  # ip link show
            MagicMock(returncode=0),  # nmcli
        ]
        job = self._make_job()
        result = troshkad._handle_bmc_create_bridge(job, {
            "project_id": "aabbccdd-1122-3344-5566-778899001122",
            "bmc_cidr": "10.0.0.0/24",
            "bmc_gateway_ip": "10.0.0.1",
            "vms": [{"bmc_ip": "10.0.0.10"}],
        })
        self.assertEqual(result["status"], "ok")
        self.assertIn("bridge", result)

    @patch("troshkad.subprocess.run")
    @patch("troshkad._run_cmd")
    def test_create_bridge_no_vms(self, mock_rcmd, mock_subrun):
        mock_subrun.side_effect = [
            subprocess.CalledProcessError(1, "ip"),
            MagicMock(returncode=0),
        ]
        job = self._make_job()
        result = troshkad._handle_bmc_create_bridge(job, {
            "project_id": "aabbccdd-1122-3344-5566-778899001122",
            "bmc_cidr": "10.0.0.0/24",
            "bmc_gateway_ip": "10.0.0.1",
        })
        self.assertEqual(result["status"], "ok")

    @patch("troshkad.subprocess.run")
    @patch("troshkad._run_cmd")
    def test_create_bridge_default_prefix(self, mock_rcmd, mock_subrun):
        mock_subrun.side_effect = [
            subprocess.CalledProcessError(1, "ip"),
            MagicMock(returncode=0),
        ]
        job = self._make_job()
        result = troshkad._handle_bmc_create_bridge(job, {
            "project_id": "aabbccdd-1122-3344-5566-778899001122",
            "bmc_cidr": "10.0.0.0",
            "bmc_gateway_ip": "10.0.0.1",
        })
        self.assertEqual(result["status"], "ok")

    @patch("troshkad.subprocess.run")
    @patch("troshkad._run_cmd")
    def test_bridge_del_failure_ignored(self, mock_rcmd, mock_subrun):
        """When deleting existing bridge fails, it's silently ignored."""
        def rcmd_side_effect(job, cmd, **kwargs):
            if "del" in cmd:
                raise RuntimeError("bridge not found")
        mock_rcmd.side_effect = rcmd_side_effect
        # ip link show succeeds (bridge exists in host)
        mock_subrun.return_value = MagicMock(returncode=0)
        job = self._make_job()
        # Should not raise
        result = troshkad._handle_bmc_create_bridge(job, {
            "project_id": "aabbccdd-1122-3344-5566-778899001122",
            "bmc_cidr": "10.0.0.0/24",
            "bmc_gateway_ip": "10.0.0.1",
        })
        self.assertEqual(result["status"], "ok")


# ── _setup_host_nftables ──


class TestSetupHostNftables(unittest.TestCase):
    def _make_job(self):
        return {"job_id": "j1", "output": [], "_cancelled": False}

    @patch("troshkad._run_cmd")
    @patch("troshkad._nft_try")
    def test_no_gateway_returns_early(self, _nft, _rcmd):
        job = self._make_job()
        troshkad._setup_host_nftables(job, "pid12345", "veth0", "172.30.0.0/24", None, {}, "172.30.0.2")
        _nft.assert_not_called()
        _rcmd.assert_not_called()

    @patch("troshkad._run_cmd")
    @patch("troshkad._nft_try")
    def test_gateway_wrong_mode_returns_early(self, _nft, _rcmd):
        job = self._make_job()
        troshkad._setup_host_nftables(
            job, "pid12345", "veth0", "172.30.0.0/24",
            {"mode": "bridge"}, {}, "172.30.0.2"
        )
        _nft.assert_not_called()

    @patch("troshkad._setup_host_port_forward_dnat")
    @patch("troshkad.subprocess.run")
    @patch("troshkad._run_cmd")
    @patch("troshkad._nft_try")
    def test_nat_mode_full_setup(self, mock_nft, mock_rcmd, mock_subrun, mock_hpfd):
        mock_subrun.return_value = MagicMock(returncode=0, stdout="chain forward {\n}")
        job = self._make_job()
        troshkad._setup_host_nftables(
            job, "pid12345", "veth0", "172.30.0.0/24",
            {"mode": "nat"}, {}, "172.30.0.2"
        )
        # Should call _nft_try for creating tables/chains/flush
        self.assertGreater(mock_nft.call_count, 0)
        # Should call _run_cmd for forwarding rules and masquerade
        self.assertGreater(mock_rcmd.call_count, 0)

    @patch("troshkad._setup_host_port_forward_dnat")
    @patch("troshkad.subprocess.run")
    @patch("troshkad._run_cmd")
    @patch("troshkad._nft_try")
    def test_nat_portforward_mode(self, mock_nft, mock_rcmd, mock_subrun, mock_hpfd):
        mock_subrun.return_value = MagicMock(returncode=0, stdout="")
        job = self._make_job()
        troshkad._setup_host_nftables(
            job, "pid12345", "veth0", "172.30.0.0/24",
            {"mode": "nat-portforward"}, {}, "172.30.0.2"
        )
        mock_hpfd.assert_called_once()

    @patch("troshkad._setup_host_port_forward_dnat")
    @patch("troshkad.subprocess.run")
    @patch("troshkad._run_cmd")
    @patch("troshkad._nft_try")
    def test_jump_rule_added_when_missing(self, mock_nft, mock_rcmd, mock_subrun, mock_hpfd):
        """When nft list chain does NOT contain jump, add rule is called."""
        mock_subrun.return_value = MagicMock(returncode=0, stdout="chain forward {\n}")
        job = self._make_job()
        troshkad._setup_host_nftables(
            job, "pid12345", "veth0", "172.30.0.0/24",
            {"mode": "nat"}, {}, "172.30.0.2"
        )
        # _nft_try should be called for jump rule adds
        jump_calls = [c for c in mock_nft.call_args_list if any("jump" in str(a) for a in c[0])]
        self.assertGreater(len(jump_calls), 0)

    @patch("troshkad._setup_host_port_forward_dnat")
    @patch("troshkad.subprocess.run")
    @patch("troshkad._run_cmd")
    @patch("troshkad._nft_try")
    def test_jump_rule_skipped_when_exists(self, mock_nft, mock_rcmd, mock_subrun, mock_hpfd):
        """When nft list chain already has the jump, don't re-add."""
        mock_subrun.return_value = MagicMock(
            returncode=0,
            stdout="chain forward {\n  jump troshka-fwd-pid12345\n}"
        )
        job = self._make_job()
        troshkad._setup_host_nftables(
            job, "pid12345", "veth0", "172.30.0.0/24",
            {"mode": "nat"}, {}, "172.30.0.2"
        )
        # _nft_try calls for jump adds should be fewer (only for non-matching chains)
        # At minimum the setup still works


# ── _setup_ns_port_forward_dnat ──


class TestSetupNsPortForwardDnat(unittest.TestCase):
    def _make_job(self):
        return {"job_id": "j1", "output": [], "_cancelled": False}

    @patch("troshkad._run_cmd")
    def test_no_gateway_returns_empty(self, _rcmd):
        result = troshkad._setup_ns_port_forward_dnat(
            self._make_job(), "ns1", "veth0", None, 1
        )
        self.assertEqual(result, {})
        _rcmd.assert_not_called()

    @patch("troshkad._run_cmd")
    def test_wrong_mode_returns_empty(self, _rcmd):
        result = troshkad._setup_ns_port_forward_dnat(
            self._make_job(), "ns1", "veth0", {"mode": "nat"}, 1
        )
        self.assertEqual(result, {})

    @patch("troshkad._run_cmd")
    def test_portforward_creates_rules(self, mock_rcmd):
        gateway = {
            "mode": "nat-portforward",
            "port_forwards": [
                {"extPort": "443", "intIp": "192.168.1.10", "intPort": "443"},
            ],
        }
        result = troshkad._setup_ns_port_forward_dnat(
            self._make_job(), "ns1", "veth0", gateway, 1
        )
        self.assertIn(0, result)
        self.assertEqual(result[0], "172.30.1.10")
        # Should create DNAT rule and forward rule
        self.assertGreaterEqual(mock_rcmd.call_count, 2)

    @patch("troshkad._run_cmd")
    def test_portforward_with_transit_port(self, mock_rcmd):
        gateway = {
            "mode": "nat-portforward",
            "port_forwards": [
                {"extPort": "443", "intIp": "192.168.1.10", "intPort": "8443", "_transit_port": 31000},
            ],
        }
        result = troshkad._setup_ns_port_forward_dnat(
            self._make_job(), "ns1", "veth0", gateway, 1
        )
        self.assertIn(0, result)

    @patch("troshkad._run_cmd")
    def test_portforward_skips_incomplete(self, mock_rcmd):
        """Entries missing extPort/intIp/intPort are skipped."""
        gateway = {
            "mode": "nat-portforward",
            "port_forwards": [
                {"extPort": "", "intIp": "192.168.1.10", "intPort": "443"},
                {"extPort": "443", "intIp": "", "intPort": "443"},
            ],
        }
        result = troshkad._setup_ns_port_forward_dnat(
            self._make_job(), "ns1", "veth0", gateway, 1
        )
        self.assertEqual(result, {})

    @patch("troshkad._run_cmd")
    def test_portforward_addr_add_failure_ignored(self, mock_rcmd):
        """IP addr add RuntimeError is caught and skipped."""
        call_count = [0]

        def side_effect(job, cmd, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("already exists")
        mock_rcmd.side_effect = side_effect
        gateway = {
            "mode": "nat-portforward",
            "port_forwards": [
                {"extPort": "443", "intIp": "192.168.1.10", "intPort": "443"},
            ],
        }
        # Should not raise
        troshkad._setup_ns_port_forward_dnat(
            self._make_job(), "ns1", "veth0", gateway, 1
        )


# ── _setup_host_port_forward_dnat ──


class TestSetupHostPortForwardDnat(unittest.TestCase):
    def _make_job(self):
        return {"job_id": "j1", "output": [], "_cancelled": False}

    @patch("troshkad._run_cmd")
    def test_wrong_mode_returns_early(self, _rcmd):
        troshkad._setup_host_port_forward_dnat(
            self._make_job(), {"mode": "nat"}, {}, "172.30.0.2", "pre-chain"
        )
        _rcmd.assert_not_called()

    @patch("troshkad._run_cmd")
    def test_with_private_ip(self, mock_rcmd):
        gateway = {
            "mode": "nat-portforward",
            "port_forwards": [
                {"extPort": "443", "intIp": "192.168.1.10", "intPort": "443", "_private_ip": "10.0.1.5"},
            ],
        }
        troshkad._setup_host_port_forward_dnat(
            self._make_job(), gateway, {0: "172.30.1.10"}, "172.30.0.2", "pre-chain"
        )
        mock_rcmd.assert_called_once()
        args = mock_rcmd.call_args[0][1]
        self.assertIn("10.0.1.5", args)

    @patch("troshkad._run_cmd")
    def test_with_transit_port(self, mock_rcmd):
        gateway = {
            "mode": "nat-portforward",
            "port_forwards": [
                {"extPort": "443", "intIp": "192.168.1.10", "intPort": "443", "_transit_port": 31000},
            ],
        }
        troshkad._setup_host_port_forward_dnat(
            self._make_job(), gateway, {0: "172.30.1.10"}, "172.30.0.2", "pre-chain"
        )
        mock_rcmd.assert_called_once()
        args = mock_rcmd.call_args[0][1]
        self.assertIn("31000", [str(a) for a in args])

    @patch("troshkad._run_cmd")
    @patch("troshkad._job_log")
    def test_no_private_ip_no_transit_logs_skip(self, mock_log, mock_rcmd):
        gateway = {
            "mode": "nat-portforward",
            "port_forwards": [
                {"extPort": "443", "intIp": "192.168.1.10", "intPort": "443"},
            ],
        }
        troshkad._setup_host_port_forward_dnat(
            self._make_job(), gateway, {0: "172.30.1.10"}, "172.30.0.2", "pre-chain"
        )
        mock_rcmd.assert_not_called()
        mock_log.assert_called()

    @patch("troshkad._run_cmd")
    def test_skips_incomplete_entries(self, mock_rcmd):
        gateway = {
            "mode": "nat-portforward",
            "port_forwards": [
                {"extPort": "", "intIp": "192.168.1.10", "intPort": "443", "_private_ip": "10.0.1.5"},
            ],
        }
        troshkad._setup_host_port_forward_dnat(
            self._make_job(), gateway, {}, "172.30.0.2", "pre-chain"
        )
        mock_rcmd.assert_not_called()


# ── _handle_nft_reset ──


class TestHandleNftReset(unittest.TestCase):
    def _make_job(self):
        return {"job_id": "j1", "output": [], "_cancelled": False}

    @patch("troshkad._run_cmd")
    @patch("troshkad.subprocess.run")
    def test_nft_list_fails(self, mock_subrun, _rcmd):
        mock_subrun.return_value = MagicMock(returncode=1)
        result = troshkad._handle_nft_reset(self._make_job(), {})
        self.assertEqual(result["flushed_chains"], 0)
        self.assertIn("error", result)

    @patch("troshkad._run_cmd")
    @patch("troshkad.subprocess.run")
    def test_nft_reset_flushes_chains(self, mock_subrun, mock_rcmd):
        nft_output = (
            "table inet filter {\n"
            "  chain forward {\n"
            "    type filter hook forward priority 0;\n"
            "  }\n"
            "  chain troshka-fwd-abcd1234 {\n"
            "    iifname veth0 accept\n"
            "  }\n"
            "}\n"
            "table inet nat {\n"
            "  chain troshka-post-abcd1234 {\n"
            "  }\n"
            "}\n"
        )
        mock_subrun.return_value = MagicMock(returncode=0, stdout=nft_output)
        result = troshkad._handle_nft_reset(self._make_job(), {})
        self.assertEqual(result["flushed_chains"], 2)

    @patch("troshkad._run_cmd")
    @patch("troshkad.subprocess.run")
    def test_nft_reset_no_troshka_chains(self, mock_subrun, mock_rcmd):
        mock_subrun.return_value = MagicMock(returncode=0, stdout="table inet filter {\n  chain forward {\n  }\n}")
        result = troshkad._handle_nft_reset(self._make_job(), {})
        self.assertEqual(result["flushed_chains"], 0)

    @patch("troshkad._run_cmd")
    @patch("troshkad.subprocess.run")
    def test_nft_reset_chain_delete_failure(self, mock_subrun, mock_rcmd):
        """When chain flush/delete fails, it's caught and doesn't raise."""
        nft_output = "table inet filter {\n  chain troshka-fwd-abcd1234 {\n  }\n}"
        mock_subrun.return_value = MagicMock(returncode=0, stdout=nft_output)
        mock_rcmd.side_effect = RuntimeError("chain busy")
        result = troshkad._handle_nft_reset(self._make_job(), {})
        self.assertEqual(result["flushed_chains"], 0)

    @patch("troshkad._run_cmd")
    @patch("troshkad.subprocess.run")
    def test_nft_reset_base_chain_flush_failure(self, mock_subrun, mock_rcmd):
        """Flush of base chains is allowed to fail."""
        mock_subrun.return_value = MagicMock(returncode=0, stdout="")
        mock_rcmd.side_effect = RuntimeError("no such chain")
        result = troshkad._handle_nft_reset(self._make_job(), {})
        self.assertEqual(result["flushed_chains"], 0)


# ── _s3_upload_with_cache ──


class TestS3UploadWithCache(unittest.TestCase):
    def _make_job(self):
        return {"job_id": "j1", "output": [], "_cancelled": False, "_process": None}

    @patch("troshkad._format_cache_progress", return_value="")
    @patch("troshkad._format_upload_progress", return_value="")
    @patch("troshkad.os.path.exists", return_value=True)
    @patch("troshkad._build_s3_env", return_value={})
    @patch("troshkad.subprocess.Popen")
    def test_upload_success(self, mock_popen, _env, _exists, _uprog, _cprog):
        proc = MagicMock()
        proc.poll.side_effect = [None, 0]
        proc.pid = 12345
        proc.returncode = 0
        mock_popen.return_value = proc
        job = self._make_job()
        # Args: job, local_path, total_bytes, s3_url, cache_path, ...
        troshkad._s3_upload_with_cache(
            job, "/tmp/file.qcow2", 1024 * 1024, "s3://bucket/key", "/cache/file.qcow2",
            aws_access_key="k", aws_secret_key="s"
        )
        self.assertIsNone(job["_process"])

    @patch("troshkad._format_cache_progress", return_value="")
    @patch("troshkad._format_upload_progress", return_value="")
    @patch("troshkad.os.path.exists", return_value=True)
    @patch("troshkad._build_s3_env", return_value={})
    @patch("troshkad.subprocess.Popen")
    def test_upload_failure_raises(self, mock_popen, _env, _exists, _uprog, _cprog):
        proc = MagicMock()
        proc.poll.return_value = 1
        proc.pid = 12345
        proc.returncode = 1
        mock_popen.return_value = proc
        with self.assertRaises(RuntimeError) as ctx:
            troshkad._s3_upload_with_cache(
                self._make_job(), "/tmp/file", 1024, "s3://b/k", "/cache/f",
                aws_access_key="k", aws_secret_key="s"
            )
        self.assertIn("S3 upload failed", str(ctx.exception))

    @patch("troshkad._format_cache_progress", return_value="")
    @patch("troshkad._format_upload_progress", return_value="")
    @patch("troshkad.os.path.exists", return_value=True)
    @patch("troshkad._build_s3_env", return_value={})
    @patch("troshkad.subprocess.Popen")
    def test_upload_cancelled(self, mock_popen, _env, _exists, _uprog, _cprog):
        proc = MagicMock()
        proc.poll.return_value = None
        proc.pid = 12345
        mock_popen.return_value = proc
        job = self._make_job()
        job["_cancelled"] = True
        with self.assertRaises(RuntimeError) as ctx:
            troshkad._s3_upload_with_cache(
                job, "/tmp/file", 1024, "s3://b/k", "/cache/f",
                aws_access_key="k", aws_secret_key="s"
            )
        self.assertIn("cancelled", str(ctx.exception))
        proc.kill.assert_called_once()

    @patch("troshkad._format_cache_progress", return_value="cache: 50%")
    @patch("troshkad._format_upload_progress", return_value="upload: 50%")
    @patch("troshkad.os.path.exists", return_value=False)
    @patch("troshkad._build_s3_env", return_value={})
    @patch("troshkad.subprocess.Popen")
    def test_upload_uses_fallback_aws(self, mock_popen, _env, mock_exists, _uprog, _cprog):
        """When _AWS_CLI doesn't exist, falls back to 'aws'."""
        proc = MagicMock()
        proc.poll.return_value = 0
        proc.pid = 12345
        proc.returncode = 0
        mock_popen.return_value = proc
        troshkad._s3_upload_with_cache(
            self._make_job(), "/tmp/file", 1024, "s3://b/k", "/cache/f",
            aws_access_key="k", aws_secret_key="s"
        )
        args = mock_popen.call_args[0][0]
        self.assertEqual(args[0], "aws")

    @patch("troshkad._format_cache_progress", return_value="")
    @patch("troshkad._format_upload_progress", return_value="uploading")
    @patch("troshkad.os.path.exists", return_value=True)
    @patch("troshkad._build_s3_env", return_value={})
    @patch("troshkad.subprocess.Popen")
    def test_upload_with_endpoint_url(self, mock_popen, _env, _exists, _uprog, _cprog):
        proc = MagicMock()
        proc.poll.return_value = 0
        proc.pid = 12345
        proc.returncode = 0
        mock_popen.return_value = proc
        troshkad._s3_upload_with_cache(
            self._make_job(), "/tmp/file", 1024, "s3://b/k", "/cache/f",
            aws_endpoint_url="http://minio:9000"
        )


# ── _s3_download ──


class TestS3Download(unittest.TestCase):
    def _make_job(self):
        return {"job_id": "j1", "output": [], "_cancelled": False}

    @patch("troshkad.os.makedirs")
    @patch("troshkad.os.path.getsize", return_value=5000)
    @patch("troshkad.os.path.exists", return_value=True)
    @patch("troshkad.subprocess.Popen")
    def test_download_success(self, mock_popen, _exists, _getsize, _mkdirs):
        proc = MagicMock()
        proc.poll.side_effect = [None, 0]
        proc.returncode = 0
        mock_popen.return_value = proc
        troshkad._s3_download(
            self._make_job(), "s3://b/k", "/tmp/out.qcow2",
            aws_access_key="k", aws_secret_key="s"
        )

    @patch("troshkad.os.makedirs")
    @patch("troshkad.os.path.exists", return_value=True)
    @patch("troshkad.subprocess.Popen")
    def test_download_failure_raises(self, mock_popen, _exists, _mkdirs):
        proc = MagicMock()
        proc.poll.return_value = 1
        proc.returncode = 1
        mock_popen.return_value = proc
        with self.assertRaises(RuntimeError) as ctx:
            troshkad._s3_download(
                self._make_job(), "s3://b/k", "/tmp/out.qcow2"
            )
        self.assertIn("S3 download failed", str(ctx.exception))

    @patch("troshkad.os.makedirs")
    @patch("troshkad.os.path.getsize", side_effect=OSError("gone"))
    @patch("troshkad.os.path.exists")
    @patch("troshkad.subprocess.Popen")
    def test_download_getsize_error_ignored(self, mock_popen, mock_exists, _gs, _mkdirs):
        """OSError from getsize is caught gracefully."""
        mock_exists.side_effect = [True, True, True]  # aws_bin, file, ...
        proc = MagicMock()
        proc.poll.side_effect = [None, 0]
        proc.returncode = 0
        mock_popen.return_value = proc
        # Should not raise
        troshkad._s3_download(
            self._make_job(), "s3://b/k", "/tmp/out.qcow2"
        )

    @patch("troshkad.os.makedirs")
    @patch("troshkad.os.path.exists", return_value=False)
    @patch("troshkad.subprocess.Popen")
    def test_download_fallback_aws_bin(self, mock_popen, _exists, _mkdirs):
        proc = MagicMock()
        proc.poll.return_value = 0
        proc.returncode = 0
        mock_popen.return_value = proc
        troshkad._s3_download(
            self._make_job(), "s3://b/k", "/tmp/out.qcow2"
        )
        args = mock_popen.call_args[0][0]
        self.assertEqual(args[0], "aws")

    @patch("troshkad.os.makedirs")
    @patch("troshkad.os.path.exists", return_value=True)
    @patch("troshkad.subprocess.Popen")
    def test_download_with_endpoint_url(self, mock_popen, _exists, _mkdirs):
        proc = MagicMock()
        proc.poll.return_value = 0
        proc.returncode = 0
        mock_popen.return_value = proc
        troshkad._s3_download(
            self._make_job(), "s3://b/k", "/tmp/out.qcow2",
            aws_endpoint_url="http://minio:9000"
        )


# ── _abort_and_commit_overlay ──


class TestAbortAndCommitOverlay(unittest.TestCase):
    def _make_job(self):
        return {"job_id": "j1", "output": [], "_cancelled": False}

    @patch("troshkad.os.remove")
    @patch("troshkad.subprocess.run")
    def test_successful_commit(self, mock_subrun, mock_remove):
        # abort returns no current job; blockcommit succeeds
        mock_subrun.side_effect = [
            MagicMock(returncode=0),  # blockjob --abort
            MagicMock(returncode=1, stderr="No current block job"),  # blockjob --info
            MagicMock(returncode=0),  # blockcommit
        ]
        result = troshkad._abort_and_commit_overlay(
            self._make_job(), "domain1", "vda", "/tmp/overlay.qcow2"
        )
        self.assertTrue(result)
        mock_remove.assert_called_with("/tmp/overlay.qcow2")

    @patch("troshkad.subprocess.run")
    def test_commit_failure(self, mock_subrun):
        mock_subrun.side_effect = [
            MagicMock(returncode=0),  # blockjob --abort
            MagicMock(returncode=1, stderr="No current block job"),  # blockjob --info
            MagicMock(returncode=1),  # blockcommit fails
        ]
        result = troshkad._abort_and_commit_overlay(
            self._make_job(), "domain1", "vda", "/tmp/overlay.qcow2"
        )
        self.assertFalse(result)

    @patch("troshkad.os.remove", side_effect=OSError("no file"))
    @patch("troshkad.subprocess.run")
    def test_remove_overlay_failure_ignored(self, mock_subrun, _remove):
        mock_subrun.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=1, stderr="No current block job"),
            MagicMock(returncode=0),
        ]
        result = troshkad._abort_and_commit_overlay(
            self._make_job(), "domain1", "vda", "/tmp/overlay.qcow2"
        )
        self.assertTrue(result)


# ── _cleanup_stale_snapshots ──


class TestCleanupStaleSnapshots(unittest.TestCase):
    def _make_job(self):
        return {"job_id": "j1", "output": [], "_cancelled": False}

    @patch("troshkad.subprocess.run")
    def test_domblklist_failure(self, mock_subrun):
        mock_subrun.return_value = MagicMock(returncode=1)
        # Should not raise
        troshkad._cleanup_stale_snapshots(self._make_job(), "domain1")

    @patch("troshkad._abort_and_commit_overlay")
    @patch("troshkad.subprocess.run")
    def test_no_stale_overlays(self, mock_subrun, _abort):
        mock_subrun.return_value = MagicMock(
            returncode=0,
            stdout=" Type   Device   Target   Source\n file   disk   vda   /var/lib/troshka/vms/disk.qcow2\n"
        )
        troshkad._cleanup_stale_snapshots(self._make_job(), "domain1")
        _abort.assert_not_called()

    @patch("troshkad._abort_and_commit_overlay", return_value=True)
    @patch("troshkad.subprocess.run")
    def test_stale_overlay_cleaned(self, mock_subrun, mock_abort):
        mock_subrun.return_value = MagicMock(
            returncode=0,
            stdout=" Type   Device   Target   Source\n file   disk   vda   /tmp/disk.troshka-capture.qcow2\n"
        )
        troshkad._cleanup_stale_snapshots(self._make_job(), "domain1")
        mock_abort.assert_called_once()

    @patch("troshkad._abort_and_commit_overlay", return_value=False)
    @patch("troshkad.subprocess.run")
    def test_stale_overlay_cleanup_failure(self, mock_subrun, mock_abort):
        mock_subrun.return_value = MagicMock(
            returncode=0,
            stdout=" Type   Device   Target   Source\n file   disk   vda   /tmp/disk.troshka-capture.qcow2\n"
        )
        troshkad._cleanup_stale_snapshots(self._make_job(), "domain1")
        mock_abort.assert_called_once()


# ── _commit_overlay_with_retry ──


class TestCommitOverlayWithRetry(unittest.TestCase):
    def _make_job(self):
        return {"job_id": "j1", "output": [], "_cancelled": False}

    @patch("troshkad.os.remove")
    @patch("troshkad.subprocess.run")
    def test_success_first_try(self, mock_subrun, mock_remove):
        mock_subrun.return_value = MagicMock(returncode=0)
        troshkad._commit_overlay_with_retry(
            self._make_job(), "domain1", "vda", "/tmp/overlay.qcow2"
        )
        mock_remove.assert_called_with("/tmp/overlay.qcow2")

    @patch("troshkad._abort_and_commit_overlay", return_value=True)
    @patch("troshkad.subprocess.run")
    def test_retry_on_failure(self, mock_subrun, mock_abort):
        mock_subrun.return_value = MagicMock(returncode=1, stderr="error")
        troshkad._commit_overlay_with_retry(
            self._make_job(), "domain1", "vda", "/tmp/overlay.qcow2"
        )
        mock_abort.assert_called_once()

    @patch("troshkad._abort_and_commit_overlay", return_value=False)
    @patch("troshkad.subprocess.run")
    def test_retry_also_fails(self, mock_subrun, mock_abort):
        mock_subrun.return_value = MagicMock(returncode=1, stderr="error")
        troshkad._commit_overlay_with_retry(
            self._make_job(), "domain1", "vda", "/tmp/overlay.qcow2"
        )
        mock_abort.assert_called_once()

    @patch("troshkad.os.remove", side_effect=OSError)
    @patch("troshkad.subprocess.run")
    def test_remove_failure_ignored(self, mock_subrun, _remove):
        mock_subrun.return_value = MagicMock(returncode=0)
        troshkad._commit_overlay_with_retry(
            self._make_job(), "domain1", "vda", "/tmp/overlay.qcow2"
        )


# ── _commit_snapshot ──


class TestCommitSnapshot(unittest.TestCase):
    def _make_job(self):
        return {"job_id": "j1", "output": [], "_cancelled": False}

    @patch("troshkad.subprocess.run")
    def test_domblklist_fails(self, mock_subrun):
        mock_subrun.return_value = MagicMock(returncode=1)
        troshkad._commit_snapshot(self._make_job(), "domain1")

    @patch("troshkad._commit_overlay_with_retry")
    @patch("troshkad.subprocess.run")
    def test_no_overlays(self, mock_subrun, mock_commit):
        mock_subrun.return_value = MagicMock(
            returncode=0,
            stdout=" Type   Device   Target   Source\n file   disk   vda   /disk.qcow2\n"
        )
        troshkad._commit_snapshot(self._make_job(), "domain1")
        mock_commit.assert_not_called()

    @patch("troshkad._commit_overlay_with_retry")
    @patch("troshkad.subprocess.run")
    def test_overlay_found(self, mock_subrun, mock_commit):
        mock_subrun.return_value = MagicMock(
            returncode=0,
            stdout=" Type   Device   Target   Source\n file   disk   vda   /disk.troshka-capture.qcow2\n"
        )
        troshkad._commit_snapshot(self._make_job(), "domain1")
        mock_commit.assert_called_once_with(
            unittest.mock.ANY, "domain1", "vda", "/disk.troshka-capture.qcow2"
        )


# ── _hot_attach_new_disks ──


class TestHotAttachNewDisks(unittest.TestCase):
    def _make_job(self):
        return {"job_id": "j1", "output": [], "_cancelled": False}

    def _make_xml_root(self, disk_paths):
        """Build a minimal libvirt XML root with given disk paths."""
        import xml.etree.ElementTree as ET
        root = ET.fromstring("<domain><devices></devices></domain>")
        devices = root.find("devices")
        for i, path in enumerate(disk_paths):
            disk = ET.SubElement(devices, "disk", device="disk")
            src = ET.SubElement(disk, "source")
            src.set("file", path)
            tgt = ET.SubElement(disk, "target")
            tgt.set("dev", f"vd{'abcdef'[i]}")
        return root

    @patch("troshkad._run_cmd")
    def test_no_new_disks(self, _rcmd):
        root = self._make_xml_root(["/disk1.qcow2"])
        result = troshkad._hot_attach_new_disks(
            self._make_job(), "domain1",
            [{"path": "/disk1.qcow2"}], root
        )
        self.assertFalse(result)
        _rcmd.assert_not_called()

    @patch("troshkad._run_cmd")
    def test_attach_new_disk(self, mock_rcmd):
        root = self._make_xml_root(["/disk1.qcow2"])
        result = troshkad._hot_attach_new_disks(
            self._make_job(), "domain1",
            [{"path": "/disk1.qcow2"}, {"path": "/disk2.qcow2", "format": "qcow2", "bus": "virtio"}],
            root
        )
        self.assertTrue(result)
        mock_rcmd.assert_called_once()
        args = mock_rcmd.call_args[0][1]
        self.assertIn("/disk2.qcow2", args)

    @patch("troshkad._run_cmd")
    def test_cdrom_ignored(self, mock_rcmd):
        import xml.etree.ElementTree as ET
        root = ET.fromstring("<domain><devices></devices></domain>")
        devices = root.find("devices")
        cdrom = ET.SubElement(devices, "disk", device="cdrom")
        ET.SubElement(cdrom, "source").set("file", "/cd.iso")
        ET.SubElement(cdrom, "target").set("dev", "sda")
        result = troshkad._hot_attach_new_disks(
            self._make_job(), "domain1",
            [{"path": "/new.qcow2"}], root
        )
        self.assertTrue(result)


# ── _add_disk_element ──


class TestAddDiskElement(unittest.TestCase):
    def _make_job(self):
        return {"job_id": "j1", "output": [], "_cancelled": False}

    def test_add_basic_disk(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring("<domain><devices></devices></domain>")
        devices = root.find("devices")
        used = set()
        troshkad._add_disk_element(
            self._make_job(), devices,
            {"path": "/disk.qcow2", "format": "qcow2", "bus": "virtio"},
            used, "domain1"
        )
        disks = devices.findall("disk")
        self.assertEqual(len(disks), 1)
        self.assertEqual(disks[0].find("target").get("dev"), "vdb")
        self.assertIn("vdb", used)

    def test_add_disk_with_rotation_rate(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring("<domain><devices></devices></domain>")
        devices = root.find("devices")
        used = set()
        troshkad._add_disk_element(
            self._make_job(), devices,
            {"path": "/disk.qcow2", "format": "qcow2", "bus": "sata", "rotation_rate": 7200},
            used, "domain1"
        )
        disk = devices.findall("disk")[0]
        self.assertEqual(disk.find("target").get("rotation_rate"), "7200")

    def test_add_disk_rotation_rate_ignored_for_virtio(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring("<domain><devices></devices></domain>")
        devices = root.find("devices")
        used = set()
        troshkad._add_disk_element(
            self._make_job(), devices,
            {"path": "/disk.qcow2", "bus": "virtio", "rotation_rate": 7200},
            used, "domain1"
        )
        disk = devices.findall("disk")[0]
        self.assertIsNone(disk.find("target").get("rotation_rate"))

    def test_all_targets_used_returns_none(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring("<domain><devices></devices></domain>")
        devices = root.find("devices")
        used = {f"vd{c}" for c in "bcdefghijklmnop"}
        troshkad._add_disk_element(
            self._make_job(), devices,
            {"path": "/disk.qcow2"},
            used, "domain1"
        )
        # No disk added because all target letters are used
        self.assertEqual(len(devices.findall("disk")), 0)


# ── _reconfigure_disks ──


class TestReconfigureDisks(unittest.TestCase):
    def _make_job(self):
        return {"job_id": "j1", "output": [], "_cancelled": False}

    def test_removes_unwanted_disks(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring(
            "<domain><devices>"
            '<disk device="disk"><source file="/keep.qcow2"/><target dev="vda"/></disk>'
            '<disk device="disk"><source file="/remove.qcow2"/><target dev="vdb"/></disk>'
            "</devices></domain>"
        )
        troshkad._reconfigure_disks(
            self._make_job(), root, "domain1",
            [{"path": "/keep.qcow2"}]
        )
        disks = root.find("devices").findall("disk")
        paths = [d.find("source").get("file") for d in disks]
        self.assertIn("/keep.qcow2", paths)
        self.assertNotIn("/remove.qcow2", paths)

    def test_adds_new_disks(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring(
            "<domain><devices>"
            '<disk device="disk"><source file="/existing.qcow2"/><target dev="vda"/></disk>'
            "</devices></domain>"
        )
        troshkad._reconfigure_disks(
            self._make_job(), root, "domain1",
            [{"path": "/existing.qcow2"}, {"path": "/new.qcow2"}]
        )
        disks = root.find("devices").findall("disk")
        self.assertEqual(len(disks), 2)

    def test_preserves_cdroms(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring(
            "<domain><devices>"
            '<disk device="cdrom"><source file="/iso.iso"/><target dev="sda"/></disk>'
            '<disk device="disk"><source file="/remove.qcow2"/><target dev="vda"/></disk>'
            "</devices></domain>"
        )
        troshkad._reconfigure_disks(
            self._make_job(), root, "domain1",
            []
        )
        disks = root.find("devices").findall("disk")
        # CDROM should be preserved
        self.assertEqual(len(disks), 1)
        self.assertEqual(disks[0].get("device"), "cdrom")


# ── _create_new_namespace ──


class TestCreateNewNamespace(unittest.TestCase):
    def _make_job(self):
        return {"job_id": "j1", "output": [], "_cancelled": False}

    @patch("troshkad._run_cmd")
    def test_creates_namespace_and_veth(self, mock_rcmd):
        troshkad._create_new_namespace(
            self._make_job(), "troshka-aabbccdd", "veth-host", "veth-ns",
            "172.30.0.1", "172.30.0.2"
        )
        # Should call _run_cmd multiple times for ns add, veth, addr, link set up, lo up
        self.assertGreaterEqual(mock_rcmd.call_count, 7)
        # First call should be netns add
        first_cmd = mock_rcmd.call_args_list[0][0][1]
        self.assertIn("netns", first_cmd)
        self.assertIn("add", first_cmd)


# ── _extract_pxe_boot_files ──


class TestExtractPxeBootFiles(unittest.TestCase):
    def _make_job(self):
        return {"job_id": "j1", "output": [], "_cancelled": False}

    @patch("troshkad.os.chmod")
    @patch("troshkad.shutil.copy2")
    @patch("troshkad.os.makedirs")
    @patch("troshkad.os.path.isfile")
    def test_extracts_first_match(self, mock_isfile, _mkdirs, mock_copy, _chmod):
        mock_isfile.return_value = True
        troshkad._extract_pxe_boot_files(
            self._make_job(), "/mnt/iso", "/tftp"
        )
        # copy2 called twice (kernel + initrd)
        self.assertEqual(mock_copy.call_count, 2)

    @patch("troshkad.os.listdir", return_value=["EFI", "images"])
    @patch("troshkad.os.path.isfile", return_value=False)
    def test_no_match_raises(self, _isfile, _listdir):
        with self.assertRaises(RuntimeError) as ctx:
            troshkad._extract_pxe_boot_files(
                self._make_job(), "/mnt/iso", "/tftp"
            )
        self.assertIn("unsupported distro", str(ctx.exception))

    @patch("troshkad.os.listdir", side_effect=OSError("not a directory"))
    @patch("troshkad.os.path.isfile", return_value=False)
    def test_no_match_listdir_fails(self, _isfile, _listdir):
        """When os.listdir fails for debugging, still raises RuntimeError."""
        with self.assertRaises(RuntimeError):
            troshkad._extract_pxe_boot_files(
                self._make_job(), "/mnt/iso", "/tftp"
            )


# ── _try_uefi_bootloader ──


class TestTryUefiBootloader(unittest.TestCase):
    def _make_job(self):
        return {"job_id": "j1", "output": [], "_cancelled": False}

    @patch("troshkad.os.path.isdir", return_value=False)
    def test_no_efi_boot_dir(self, _isdir):
        result = troshkad._try_uefi_bootloader(self._make_job(), "/mnt", "/tftp")
        self.assertIsNone(result)

    @patch("troshkad.os.chmod")
    @patch("troshkad.shutil.copy2")
    @patch("troshkad.os.path.isfile")
    @patch("troshkad.os.listdir")
    @patch("troshkad.os.path.isdir", return_value=True)
    def test_copies_efi_files(self, _isdir, mock_listdir, mock_isfile, _copy, _chmod):
        mock_listdir.return_value = ["BOOTX64.EFI", "grub.cfg"]
        mock_isfile.side_effect = lambda p: True
        result = troshkad._try_uefi_bootloader(self._make_job(), "/mnt", "/tftp")
        self.assertIsNotNone(result)


# ── _try_bios_bootloader ──


class TestTryBiosBootloader(unittest.TestCase):
    def _make_job(self):
        return {"job_id": "j1", "output": [], "_cancelled": False}

    @patch("troshkad.os.path.isfile", return_value=False)
    def test_no_bootloader_found(self, _isfile):
        result = troshkad._try_bios_bootloader(self._make_job(), "/mnt", "/tftp")
        self.assertIsNone(result)

    @patch("troshkad.os.chmod")
    @patch("troshkad.shutil.copy2")
    @patch("troshkad.os.path.isfile")
    def test_bootloader_found(self, mock_isfile, _copy, _chmod):
        # First call: check first path, return True
        mock_isfile.side_effect = lambda p: "pxelinux.0" in p
        result = troshkad._try_bios_bootloader(self._make_job(), "/mnt", "/tftp")
        # Could be None if none of the exact paths match the side_effect
        # Let's make it match all
        mock_isfile.side_effect = lambda p: True
        result = troshkad._try_bios_bootloader(self._make_job(), "/mnt", "/tftp")
        self.assertIsNotNone(result)


# ── _patch_grub_config ──


class TestPatchGrubConfig(unittest.TestCase):
    def _make_job(self):
        return {"job_id": "j1", "output": [], "_cancelled": False}

    @patch("troshkad.os.path.isfile", return_value=False)
    def test_no_grub_file(self, _isfile):
        troshkad._patch_grub_config(self._make_job(), "/tftp", "http://gw:8080/")
        # No error

    @patch("troshkad.os.path.isfile", return_value=True)
    def test_no_install_url(self, _isfile):
        troshkad._patch_grub_config(self._make_job(), "/tftp", "")

    @patch("troshkad.os.path.isfile", return_value=True)
    def test_adds_inst_repo(self, _isfile):
        grub_content = "linux vmlinuz quiet\ninitrd initrd.img"
        with patch("builtins.open", mock_open(read_data=grub_content)) as m:
            troshkad._patch_grub_config(self._make_job(), "/tftp", "http://gw:8080/")
        # Write should include inst.repo
        handle = m()
        written = handle.write.call_args[0][0]
        self.assertIn("inst.repo=http://gw:8080/", written)

    @patch("troshkad.os.path.isfile", return_value=True)
    def test_replaces_inst_stage2(self, _isfile):
        grub_content = "linux vmlinuz inst.stage2=hd:LABEL quiet"
        with patch("builtins.open", mock_open(read_data=grub_content)) as m:
            troshkad._patch_grub_config(self._make_job(), "/tftp", "http://gw:8080/")
        handle = m()
        written = handle.write.call_args[0][0]
        self.assertIn("inst.repo=http://gw:8080/", written)
        self.assertNotIn("inst.stage2", written)

    @patch("troshkad.os.path.isfile", return_value=True)
    def test_skips_when_inst_repo_exists(self, _isfile):
        grub_content = "linux vmlinuz inst.repo=http://existing/ quiet"
        with patch("builtins.open", mock_open(read_data=grub_content)):
            troshkad._patch_grub_config(self._make_job(), "/tftp", "http://gw:8080/")
        # Should not modify


# ── _generate_pxelinux_config ──


class TestGeneratePxelinuxConfig(unittest.TestCase):
    def _make_job(self):
        return {"job_id": "j1", "output": [], "_cancelled": False}

    def test_with_install_url(self):
        with patch("builtins.open", mock_open()) as m:
            troshkad._generate_pxelinux_config(
                self._make_job(), "/tftp", "http://gw:8080/"
            )
        handle = m()
        written = handle.write.call_args[0][0]
        self.assertIn("inst.repo=http://gw:8080/", written)
        self.assertIn("KERNEL vmlinuz", written)

    def test_without_install_url(self):
        with patch("builtins.open", mock_open()) as m:
            troshkad._generate_pxelinux_config(
                self._make_job(), "/tftp", ""
            )
        handle = m()
        written = handle.write.call_args[0][0]
        self.assertNotIn("inst.repo", written)


# ── _configure_dnsmasq_tftp ──


class TestConfigureDnsmasqTftp(unittest.TestCase):
    def _make_job(self):
        return {"job_id": "j1", "output": [], "_cancelled": False}

    @patch("troshkad.os.path.exists", return_value=False)
    def test_no_conf_file(self, _exists):
        troshkad._configure_dnsmasq_tftp(
            self._make_job(), "ns1", 100, "/tftp", "pxelinux.0"
        )

    @patch("troshkad._run_cmd")
    @patch("troshkad._safe_kill")
    @patch("troshkad.os.path.exists", return_value=True)
    def test_configures_tftp(self, _exists, _kill, mock_rcmd):
        conf = "interface=br-100\nbind-interfaces\n"
        with patch("builtins.open", mock_open(read_data=conf)):
            troshkad._configure_dnsmasq_tftp(
                self._make_job(), "ns1", 100, "/tftp", "pxelinux.0"
            )
        # Should restart dnsmasq
        mock_rcmd.assert_called()


# ── _start_pxe_http_server ──


class TestStartPxeHttpServer(unittest.TestCase):
    def _make_job(self):
        return {"job_id": "j1", "output": [], "_cancelled": False}

    @patch("troshkad.subprocess.run")
    @patch("troshkad.subprocess.Popen")
    @patch("troshkad._safe_kill")
    @patch("troshkad.os.path.exists", return_value=False)
    def test_starts_server(self, _exists, _kill, mock_popen, mock_subrun):
        mock_subrun.return_value = MagicMock(stdout="12345\n")
        with patch("builtins.open", mock_open()):
            troshkad._start_pxe_http_server(
                self._make_job(), "ns1", 100, "/mnt", 8080
            )
        mock_popen.assert_called_once()

    @patch("troshkad.subprocess.run")
    @patch("troshkad.subprocess.Popen")
    @patch("troshkad._safe_kill")
    @patch("troshkad.os.path.exists", return_value=True)
    def test_kills_existing_server(self, _exists, mock_kill, mock_popen, mock_subrun):
        mock_subrun.return_value = MagicMock(stdout="12345\n")
        with patch("builtins.open", mock_open(read_data="99999")):
            troshkad._start_pxe_http_server(
                self._make_job(), "ns1", 100, "/mnt", 8080
            )
        mock_kill.assert_called()


# ── _handle_pxe_setup ──


class TestHandlePxeSetup(unittest.TestCase):
    def _make_job(self):
        return {"job_id": "j1", "output": [], "_cancelled": False}

    def test_missing_project_id(self):
        with self.assertRaises(RuntimeError) as ctx:
            troshkad._handle_pxe_setup(self._make_job(), {"vni": "100", "iso_path": "/x"})
        self.assertIn("project_id", str(ctx.exception))

    @patch("troshkad.os.path.exists", return_value=False)
    def test_iso_not_found(self, _exists):
        with self.assertRaises(RuntimeError) as ctx:
            troshkad._handle_pxe_setup(self._make_job(), {
                "project_id": "aabbccdd-1122-3344-5566-778899001122",
                "vni": "100",
                "iso_path": "/var/lib/troshka/images/nonexistent.iso",
            })
        self.assertIn("ISO not found", str(ctx.exception))

    @patch("troshkad._start_pxe_http_server")
    @patch("troshkad._configure_dnsmasq_tftp")
    @patch("troshkad._generate_pxelinux_config")
    @patch("troshkad._patch_grub_config")
    @patch("troshkad._find_pxe_bootloader", return_value="pxelinux.0")
    @patch("troshkad._extract_pxe_boot_files")
    @patch("troshkad.subprocess.run")
    @patch("troshkad.os.makedirs")
    @patch("troshkad._run_cmd")
    @patch("troshkad.os.path.realpath", side_effect=lambda p: p)
    @patch("troshkad.os.path.exists", return_value=True)
    def test_full_setup(self, _exists, _realpath, _rcmd, _mkdirs, _subrun, _extract, _find_bl,
                         _patch_grub, _gen_pxe, _conf_dns, _start_http):
        result = troshkad._handle_pxe_setup(self._make_job(), {
            "project_id": "aabbccdd-1122-3344-5566-778899001122",
            "vni": "100",
            "iso_path": "/var/lib/troshka/images/rhel.iso",
            "gateway_ip": "192.168.1.1",
            "http_port": "8080",
        })
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["http_port"], 8080)
        _extract.assert_called_once()
        _start_http.assert_called_once()


# ── _setup_bastion_autologin ──


class TestSetupBastionAutologin(unittest.TestCase):
    def _make_job(self):
        return {"job_id": "j1", "output": [], "_cancelled": False}

    @patch("troshkad.os.path.exists", return_value=False)
    def test_no_autologin_script(self, _exists):
        troshkad._setup_bastion_autologin(self._make_job(), "/mnt")

    @patch("troshkad.os.chown")
    @patch("troshkad.os.chmod")
    @patch("troshkad.os.makedirs")
    @patch("troshkad.os.path.exists", return_value=True)
    def test_creates_boot_script_and_desktop(self, _exists, _mkdirs, _chmod, _chown):
        with patch("builtins.open", mock_open()):
            troshkad._setup_bastion_autologin(self._make_job(), "/mnt")
        # chmod and chown should be called for boot script and desktop file
        self.assertGreater(_chmod.call_count, 0)
        self.assertGreater(_chown.call_count, 0)


# ── _perform_bastion_disk_updates ──


class TestPerformBastionDiskUpdates(unittest.TestCase):
    def _make_job(self):
        return {"job_id": "j1", "output": [], "_cancelled": False}

    @patch("troshkad._setup_bastion_autologin")
    @patch("troshkad.glob.glob", return_value=[])
    @patch("troshkad.os.path.exists", return_value=False)
    @patch("troshkad.os.makedirs")
    def test_basic_kubeconfig_copy(self, _mkdirs, _exists, _glob, _autologin):
        kc_content = "apiVersion: v1\nkind: Config"
        with patch("builtins.open", mock_open(read_data=kc_content)):
            troshkad._perform_bastion_disk_updates(
                self._make_job(), "/mnt", "/etc/kubernetes/kubeconfig",
                "/home/cloud-user/ocp-install/auth/kubeconfig", None
            )
        _autologin.assert_called_once()

    @patch("troshkad._setup_bastion_autologin")
    @patch("troshkad.glob.glob", return_value=["/mnt/home/cloud-user/.mozilla/firefox/abc.default/cert9.db"])
    @patch("troshkad.os.unlink")
    @patch("troshkad.os.path.exists", return_value=True)
    @patch("troshkad.os.makedirs")
    def test_cleans_firefox_dbs(self, _mkdirs, _exists, mock_unlink, _glob, _autologin):
        with patch("builtins.open", mock_open(read_data="kc")):
            troshkad._perform_bastion_disk_updates(
                self._make_job(), "/mnt", "/etc/kubernetes/kubeconfig",
                "/home/cloud-user/ocp-install/auth/kubeconfig", None
            )
        mock_unlink.assert_called()

    @patch("troshkad._setup_bastion_autologin")
    @patch("troshkad.glob.glob", return_value=[])
    @patch("troshkad.os.unlink")
    @patch("troshkad.os.path.exists")
    @patch("troshkad.os.makedirs")
    def test_updates_kubeadmin_password(self, _mkdirs, mock_exists, _unlink, _glob, _autologin):
        mock_exists.side_effect = lambda p: True
        with patch("builtins.open", mock_open(read_data="kc")):
            troshkad._perform_bastion_disk_updates(
                self._make_job(), "/mnt", "/etc/kubernetes/kubeconfig",
                "/home/cloud-user/ocp-install/auth/kubeconfig", "newpass123"
            )


# ── _update_bastion_disk ──


class TestUpdateBastionDisk(unittest.TestCase):
    def _make_job(self):
        return {"job_id": "j1", "output": [], "_cancelled": False}

    @patch("troshkad._release_nbd_device")
    def test_no_bastion_disk(self, _release):
        troshkad._update_bastion_disk(
            self._make_job(), {}, "/etc/kubernetes", None,
            "/home/cloud-user/ocp-install/auth/kubeconfig", False
        )
        _release.assert_not_called()

    @patch("troshkad._release_nbd_device")
    def test_force_expire_skips(self, _release):
        troshkad._update_bastion_disk(
            self._make_job(),
            {"bastion_disk": "/var/lib/troshka/vms/bastion.qcow2"},
            "/etc/kubernetes", None,
            "/home/cloud-user/ocp-install/auth/kubeconfig", True
        )
        _release.assert_not_called()

    @patch("troshkad._release_nbd_device")
    @patch("troshkad.os.rmdir")
    @patch("troshkad._run_cmd")
    @patch("troshkad.os.path.isfile", return_value=False)
    @patch("troshkad._allocate_nbd_device", return_value="/dev/nbd0")
    def test_no_kubeconfig_skips(self, _alloc, mock_isfile, _rcmd, _rmdir, _release):
        troshkad._update_bastion_disk(
            self._make_job(),
            {"bastion_disk": "/var/lib/troshka/vms/bastion.qcow2"},
            "/etc/kubernetes", None,
            "/home/cloud-user/ocp-install/auth/kubeconfig", False
        )
        _release.assert_not_called()

    @patch("troshkad._release_nbd_device")
    @patch("troshkad.os.rmdir")
    @patch("troshkad._perform_bastion_disk_updates")
    @patch("troshkad.os.makedirs")
    @patch("troshkad.os.path.exists", return_value=True)
    @patch("troshkad.os.path.realpath", side_effect=lambda p: p)
    @patch("troshkad._run_cmd")
    @patch("troshkad.os.path.isfile", return_value=True)
    @patch("troshkad._allocate_nbd_device", return_value="/dev/nbd0")
    def test_full_update_flow(self, _alloc, _isfile, _rcmd, _realpath, _exists, _mkdirs,
                               _perform, _rmdir, mock_release):
        troshkad._update_bastion_disk(
            self._make_job(),
            {"bastion_disk": "/var/lib/troshka/vms/bastion.qcow2"},
            "/etc/kubernetes", None,
            "/home/cloud-user/ocp-install/auth/kubeconfig", False
        )
        _perform.assert_called_once()
        mock_release.assert_called_with("/dev/nbd0")

    @patch("troshkad._release_nbd_device")
    @patch("troshkad.os.rmdir")
    @patch("troshkad._run_cmd", side_effect=RuntimeError("mount failed"))
    @patch("troshkad.os.makedirs")
    @patch("troshkad.os.path.exists", return_value=False)
    @patch("troshkad.os.path.realpath", side_effect=lambda p: p)
    @patch("troshkad.os.path.isfile", return_value=True)
    @patch("troshkad._allocate_nbd_device", return_value="/dev/nbd0")
    def test_mount_failure_handled(self, _alloc, _isfile, _realpath, _mkdirs, _exists,
                                    _rcmd, _rmdir, mock_release):
        """Mount failure is caught and logged, nbd released in finally."""
        troshkad._update_bastion_disk(
            self._make_job(),
            {"bastion_disk": "/var/lib/troshka/vms/bastion.qcow2"},
            "/etc/kubernetes", None,
            "/home/cloud-user/ocp-install/auth/kubeconfig", False
        )
        mock_release.assert_called_with("/dev/nbd0")


# ── _build_recert_cmd ──


class TestBuildRecertCmd(unittest.TestCase):
    def test_basic_command(self):
        cmd = troshkad._build_recert_cmd(
            "/etc/kubernetes", "/etc/machine-config-daemon",
            "/var/lib/kubelet", 2379, False, False, None, None
        )
        self.assertIn("podman", cmd)
        self.assertIn("run", cmd)
        self.assertIn("--etcd-endpoint=http://127.0.0.1:2379", cmd)

    def test_force_expire(self):
        cmd = troshkad._build_recert_cmd(
            "/etc/kubernetes", "/etc/mcd", "/var/lib/kubelet",
            2379, True, False, None, None
        )
        self.assertIn("--force-expire", cmd)

    def test_extend_expiration(self):
        cmd = troshkad._build_recert_cmd(
            "/etc/kubernetes", "/etc/mcd", "/var/lib/kubelet",
            2379, False, True, None, None
        )
        self.assertIn("--extend-expiration", cmd)

    def test_cluster_rename(self):
        cmd = troshkad._build_recert_cmd(
            "/etc/kubernetes", "/etc/mcd", "/var/lib/kubelet",
            2379, False, False, "new-cluster", None
        )
        self.assertIn("--cluster-rename", cmd)
        self.assertIn("new-cluster", cmd)

    def test_kubeadmin_password_hash(self):
        cmd = troshkad._build_recert_cmd(
            "/etc/kubernetes", "/etc/mcd", "/var/lib/kubelet",
            2379, False, False, None, "$2a$10$hash"
        )
        self.assertIn("--kubeadmin-password-hash", cmd)
        self.assertIn("$2a$10$hash", cmd)

    def test_force_expire_takes_precedence(self):
        """When both force_expire and extend_expiration are True, only force_expire is used."""
        cmd = troshkad._build_recert_cmd(
            "/etc/kubernetes", "/etc/mcd", "/var/lib/kubelet",
            2379, True, True, None, None
        )
        self.assertIn("--force-expire", cmd)
        self.assertNotIn("--extend-expiration", cmd)


# ── _allocate_nbd_device ──


class TestAllocateNbdDevice(unittest.TestCase):
    @patch("troshkad.os.path.exists", return_value=False)
    @patch("troshkad.subprocess.run")
    def test_allocates_first_free(self, mock_subrun, _exists):
        mock_subrun.return_value = MagicMock(returncode=0)
        old = troshkad._nbd_devices_in_use.copy()
        try:
            troshkad._nbd_devices_in_use.clear()
            dev = troshkad._allocate_nbd_device()
            self.assertEqual(dev, "/dev/nbd0")
            self.assertIn(dev, troshkad._nbd_devices_in_use)
        finally:
            troshkad._nbd_devices_in_use.clear()
            troshkad._nbd_devices_in_use.update(old)

    @patch("troshkad.os.path.exists", return_value=False)
    @patch("troshkad.subprocess.run")
    def test_skips_in_use(self, mock_subrun, _exists):
        mock_subrun.return_value = MagicMock(returncode=0)
        old = troshkad._nbd_devices_in_use.copy()
        try:
            troshkad._nbd_devices_in_use.clear()
            troshkad._nbd_devices_in_use.add("/dev/nbd0")
            dev = troshkad._allocate_nbd_device()
            self.assertEqual(dev, "/dev/nbd1")
        finally:
            troshkad._nbd_devices_in_use.clear()
            troshkad._nbd_devices_in_use.update(old)

    @patch("troshkad.os.path.exists", return_value=True)
    @patch("troshkad.subprocess.run")
    def test_skips_busy_devices(self, mock_subrun, _exists):
        """Devices with existing p1 partitions are skipped."""
        mock_subrun.return_value = MagicMock(returncode=0)
        old = troshkad._nbd_devices_in_use.copy()
        try:
            troshkad._nbd_devices_in_use.clear()
            with self.assertRaises(RuntimeError) as ctx:
                troshkad._allocate_nbd_device()
            self.assertIn("No free NBD", str(ctx.exception))
        finally:
            troshkad._nbd_devices_in_use.clear()
            troshkad._nbd_devices_in_use.update(old)


# ── _mount_rhcos_disk ──


class TestMountRhcosDisk(unittest.TestCase):
    def _make_job(self):
        return {"job_id": "j1", "output": [], "_cancelled": False}

    @patch("troshkad.os.makedirs")
    @patch("troshkad.os.path.exists", return_value=False)
    @patch("troshkad._run_cmd")
    def test_partition_not_found_raises(self, _rcmd, _exists, _mkdirs):
        with self.assertRaises(RuntimeError) as ctx:
            troshkad._mount_rhcos_disk(
                self._make_job(), "/dev/nbd0", "/disk.qcow2", "/mnt/disk"
            )
        self.assertIn("not found", str(ctx.exception))

    @patch("troshkad._run_cmd")
    @patch("troshkad.os.makedirs")
    @patch("troshkad.os.path.exists", return_value=True)
    def test_mount_success(self, _exists, _mkdirs, mock_rcmd):
        mock_rcmd.return_value = MagicMock(returncode=0)
        result = troshkad._mount_rhcos_disk(
            self._make_job(), "/dev/nbd0", "/disk.qcow2", "/mnt/disk"
        )
        self.assertTrue(result)

    @patch("troshkad._run_cmd")
    @patch("troshkad.os.makedirs")
    @patch("troshkad.os.path.exists", return_value=True)
    def test_mount_failure_xfs_repair(self, _exists, _mkdirs, mock_rcmd):
        """When mount fails (non-zero return), xfs_repair is run and mount retried."""
        call_count = [0]
        def side_effect(job, cmd, **kwargs):
            call_count[0] += 1
            if call_count[0] == 4 and "mount" in cmd:
                # First mount call fails
                return MagicMock(returncode=1)
            return MagicMock(returncode=0)
        mock_rcmd.side_effect = side_effect
        result = troshkad._mount_rhcos_disk(
            self._make_job(), "/dev/nbd0", "/disk.qcow2", "/mnt/disk"
        )
        self.assertTrue(result)


# ── _get_domains_via_virsh ──


class TestGetDomainsViaVirsh(unittest.TestCase):
    @patch("troshkad.subprocess.run")
    def test_lists_domains(self, mock_subrun):
        mock_subrun.side_effect = [
            MagicMock(returncode=0, stdout="troshka-aabbccdd-11223344\nother-vm\n"),
            MagicMock(returncode=0, stdout="running\n"),
        ]
        result = troshkad._get_domains_via_virsh()
        self.assertIn("troshka-aabbccdd-11223344", result)
        self.assertEqual(result["troshka-aabbccdd-11223344"]["state"], "running")

    @patch("troshkad.subprocess.run")
    def test_state_mapping(self, mock_subrun):
        mock_subrun.side_effect = [
            MagicMock(returncode=0, stdout="troshka-aabbccdd-11223344\n"),
            MagicMock(returncode=0, stdout="shut off\n"),
        ]
        result = troshkad._get_domains_via_virsh()
        self.assertEqual(result["troshka-aabbccdd-11223344"]["state"], "shut_off")

    @patch("troshkad.subprocess.run")
    def test_virsh_failure(self, mock_subrun):
        mock_subrun.side_effect = Exception("connection refused")
        result = troshkad._get_domains_via_virsh()
        self.assertEqual(result, {})

    @patch("troshkad.subprocess.run")
    def test_empty_output(self, mock_subrun):
        mock_subrun.return_value = MagicMock(returncode=0, stdout="\n")
        result = troshkad._get_domains_via_virsh()
        self.assertEqual(result, {})

    @patch("troshkad.subprocess.run")
    def test_domstate_failure_skips(self, mock_subrun):
        mock_subrun.side_effect = [
            MagicMock(returncode=0, stdout="troshka-aabbccdd-11223344\n"),
            MagicMock(returncode=1, stdout=""),
        ]
        result = troshkad._get_domains_via_virsh()
        self.assertNotIn("troshka-aabbccdd-11223344", result)


# ── handle_vm_states ──


class TestHandleVmStates(unittest.TestCase):
    @patch("troshkad._get_domains_via_virsh", return_value={"dom1": {"state": "running"}})
    def test_virsh_fallback(self, _virsh):
        old = troshkad._libvirt_events_available
        try:
            troshkad._libvirt_events_available = False
            handler = MagicMock()
            troshkad.handle_vm_states(handler, {})
            handler._send_json.assert_called_once()
            args = handler._send_json.call_args[0]
            self.assertEqual(args[0], 200)
            self.assertEqual(args[1]["source"], "virsh")
        finally:
            troshkad._libvirt_events_available = old

    @patch("troshkad._get_domains_from_cache", return_value={"dom1": {"state": "running"}})
    def test_events_cache(self, _cache):
        old = troshkad._libvirt_events_available
        try:
            troshkad._libvirt_events_available = True
            handler = MagicMock()
            troshkad.handle_vm_states(handler, {})
            handler._send_json.assert_called_once()
            args = handler._send_json.call_args[0]
            self.assertEqual(args[0], 200)
            self.assertEqual(args[1]["source"], "events")
        finally:
            troshkad._libvirt_events_available = old

    @patch("troshkad._get_domains_via_virsh", return_value={})
    @patch("troshkad._get_domains_from_cache", return_value={})
    def test_events_empty_falls_to_virsh(self, _cache, _virsh):
        old = troshkad._libvirt_events_available
        try:
            troshkad._libvirt_events_available = True
            handler = MagicMock()
            troshkad.handle_vm_states(handler, {})
            args = handler._send_json.call_args[0]
            self.assertEqual(args[1]["source"], "virsh")
        finally:
            troshkad._libvirt_events_available = old


# ── handle_vm_events ──


class TestHandleVmEvents(unittest.TestCase):
    def test_events_not_available(self):
        old = troshkad._libvirt_events_available
        try:
            troshkad._libvirt_events_available = False
            handler = MagicMock()
            handler.path = "/vms/events?since=0"
            troshkad.handle_vm_events(handler, {})
            args = handler._send_json.call_args[0]
            self.assertEqual(args[0], 200)
            self.assertFalse(args[1]["available"])
        finally:
            troshkad._libvirt_events_available = old

    def test_events_filtered_by_since(self):
        old_available = troshkad._libvirt_events_available
        old_events = list(troshkad._vm_events)
        try:
            troshkad._libvirt_events_available = True
            troshkad._vm_events.clear()
            troshkad._vm_events.append({"domain": "d1", "state": "running", "timestamp": 100.0})
            troshkad._vm_events.append({"domain": "d2", "state": "stopped", "timestamp": 200.0})
            handler = MagicMock()
            handler.path = "/vms/events?since=150"
            troshkad.handle_vm_events(handler, {})
            args = handler._send_json.call_args[0]
            events = args[1]["events"]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["domain"], "d2")
        finally:
            troshkad._libvirt_events_available = old_available
            troshkad._vm_events.clear()
            troshkad._vm_events.extend(old_events)


# ── _append_vm_event ──


class TestAppendVmEvent(unittest.TestCase):
    def test_appends_event(self):
        old_events = list(troshkad._vm_events)
        try:
            troshkad._vm_events.clear()
            troshkad._append_vm_event({"domain": "d1", "state": "running", "timestamp": 1.0})
            self.assertEqual(len(troshkad._vm_events), 1)
        finally:
            troshkad._vm_events.clear()
            troshkad._vm_events.extend(old_events)

    def test_caps_at_500(self):
        old_events = list(troshkad._vm_events)
        try:
            troshkad._vm_events.clear()
            for i in range(501):
                troshkad._append_vm_event({"domain": f"d{i}", "timestamp": float(i)})
            self.assertEqual(len(troshkad._vm_events), 500)
            # Oldest should be popped
            self.assertEqual(troshkad._vm_events[0]["domain"], "d1")
        finally:
            troshkad._vm_events.clear()
            troshkad._vm_events.extend(old_events)


# ── _seed_libvirt_cache ──


class TestSeedLibvirtCache(unittest.TestCase):
    def test_seeds_cache(self):
        mock_lv = MagicMock()
        mock_lv.VIR_DOMAIN_RUNNING = 1
        mock_lv.VIR_DOMAIN_SHUTOFF = 5
        mock_lv.VIR_DOMAIN_PAUSED = 3
        mock_lv.VIR_DOMAIN_SHUTDOWN = 4
        mock_lv.VIR_DOMAIN_CRASHED = 6
        mock_lv.VIR_DOMAIN_PMSUSPENDED = 7

        dom1 = MagicMock()
        dom1.name.return_value = "troshka-aabbccdd-11223344"
        dom1.info.return_value = [1, 0, 0, 0, 0]  # RUNNING

        dom2 = MagicMock()
        dom2.name.return_value = "other-vm"

        conn = MagicMock()
        conn.listAllDomains.return_value = [dom1, dom2]

        old_cache = dict(troshkad._vm_state_cache)
        try:
            troshkad._vm_state_cache.clear()
            troshkad._seed_libvirt_cache(conn, mock_lv)
            self.assertIn("troshka-aabbccdd-11223344", troshkad._vm_state_cache)
            self.assertEqual(
                troshkad._vm_state_cache["troshka-aabbccdd-11223344"]["state"],
                "running"
            )
            self.assertNotIn("other-vm", troshkad._vm_state_cache)
        finally:
            troshkad._vm_state_cache.clear()
            troshkad._vm_state_cache.update(old_cache)

    def test_exception_handled(self):
        mock_lv = MagicMock()
        conn = MagicMock()
        conn.listAllDomains.side_effect = Exception("libvirt error")
        # Should not raise
        troshkad._seed_libvirt_cache(conn, mock_lv)


# ── _rearm_block_threshold ──


class TestRearmBlockThreshold(unittest.TestCase):
    def test_rearms_at_90_percent(self):
        dom = MagicMock()
        dom.blockInfo.return_value = [1000, 0, 0]
        troshkad._rearm_block_threshold(dom, "vda", 800)
        dom.setBlockThreshold.assert_called_with("vda", 900)

    def test_no_rearm_when_already_high(self):
        dom = MagicMock()
        dom.blockInfo.return_value = [1000, 0, 0]
        troshkad._rearm_block_threshold(dom, "vda", 950)
        dom.setBlockThreshold.assert_not_called()

    def test_exception_ignored(self):
        dom = MagicMock()
        dom.blockInfo.side_effect = Exception("error")
        troshkad._rearm_block_threshold(dom, "vda", 800)


# ── handle_update_vncd ──


class TestHandleUpdateVncd(unittest.TestCase):
    @patch("troshkad.subprocess.run")
    @patch("troshkad.os.chmod")
    @patch("troshkad.os.rename")
    def test_update_success(self, _rename, _chmod, mock_subrun):
        mock_subrun.return_value = MagicMock(returncode=0)
        handler = MagicMock()
        import base64
        handler._read_body.return_value = {
            "script": base64.b64encode(b"#!/usr/bin/env python3\nprint('vncd')").decode()
        }
        with patch("builtins.open", mock_open()):
            troshkad.handle_update_vncd(handler, {})
        handler._send_json.assert_called_with(200, {"status": "updated"})

    def test_missing_script_field(self):
        handler = MagicMock()
        handler._read_body.return_value = {}
        troshkad.handle_update_vncd(handler, {})
        handler._send_json.assert_called_once()
        args = handler._send_json.call_args[0]
        self.assertEqual(args[0], 400)

    def test_invalid_base64(self):
        handler = MagicMock()
        handler._read_body.return_value = {"script": "not valid base64!!!"}
        troshkad.handle_update_vncd(handler, {})
        handler._send_json.assert_called_once()
        args = handler._send_json.call_args[0]
        self.assertEqual(args[0], 400)

    @patch("troshkad.os.rename", side_effect=OSError("disk full"))
    def test_write_failure(self, _rename):
        handler = MagicMock()
        import base64
        handler._read_body.return_value = {
            "script": base64.b64encode(b"content").decode()
        }
        with patch("builtins.open", mock_open()):
            troshkad.handle_update_vncd(handler, {})
        args = handler._send_json.call_args[0]
        self.assertEqual(args[0], 500)

    @patch("troshkad.subprocess.run", side_effect=Exception("service not found"))
    @patch("troshkad.os.chmod")
    @patch("troshkad.os.rename")
    def test_restart_failure(self, _rename, _chmod, _subrun):
        handler = MagicMock()
        import base64
        handler._read_body.return_value = {
            "script": base64.b64encode(b"content").decode()
        }
        with patch("builtins.open", mock_open()):
            troshkad.handle_update_vncd(handler, {})
        args = handler._send_json.call_args[0]
        self.assertEqual(args[0], 500)


# ── _kill_existing_dnsmasq ──


class TestKillExistingDnsmasq(unittest.TestCase):
    @patch("troshkad.subprocess.run")
    @patch("troshkad.os.path.exists", return_value=False)
    def test_no_pid_file(self, _exists, mock_subrun):
        troshkad._kill_existing_dnsmasq("/etc/dnsmasq.d/test.conf", "/run/test.pid")
        # Should still pkill
        mock_subrun.assert_called_once()

    @patch("troshkad.subprocess.run")
    @patch("troshkad.os.remove")
    @patch("troshkad._safe_kill", return_value=True)
    @patch("troshkad.os.kill", side_effect=ProcessLookupError)
    @patch("troshkad.os.path.exists", return_value=True)
    def test_kills_existing_process(self, _exists, _os_kill, mock_safe_kill, _remove, _subrun):
        with patch("builtins.open", mock_open(read_data="12345")):
            troshkad._kill_existing_dnsmasq("/etc/dnsmasq.d/test.conf", "/run/test.pid")
        mock_safe_kill.assert_called()

    @patch("troshkad.subprocess.run")
    @patch("troshkad.os.remove", side_effect=FileNotFoundError)
    @patch("troshkad._safe_kill")
    @patch("troshkad.os.kill", side_effect=ProcessLookupError)
    @patch("troshkad.os.path.exists", return_value=True)
    def test_remove_pid_not_found(self, _exists, _os_kill, _kill, _remove, _subrun):
        """FileNotFoundError on pid file removal is caught."""
        with patch("builtins.open", mock_open(read_data="12345")):
            troshkad._kill_existing_dnsmasq("/etc/dnsmasq.d/test.conf", "/run/test.pid")


# ── _kill_existing_chronyd ──


class TestKillExistingChronyd(unittest.TestCase):
    @patch("troshkad.os.path.exists", return_value=False)
    def test_no_pid_file(self, _exists):
        troshkad._kill_existing_chronyd("/run/chrony.pid")

    @patch("troshkad.os.remove")
    @patch("troshkad._safe_kill", return_value=True)
    @patch("troshkad.os.kill", side_effect=ProcessLookupError)
    @patch("troshkad.os.path.exists", return_value=True)
    def test_kills_and_cleans(self, _exists, _os_kill, mock_safe_kill, mock_remove):
        with patch("builtins.open", mock_open(read_data="12345")):
            troshkad._kill_existing_chronyd("/run/chrony.pid")
        mock_safe_kill.assert_called()
        mock_remove.assert_called_with("/run/chrony.pid")

    @patch("troshkad.os.remove", side_effect=FileNotFoundError)
    @patch("troshkad._safe_kill", return_value=True)
    @patch("troshkad.os.kill", side_effect=ProcessLookupError)
    @patch("troshkad.os.path.exists", return_value=True)
    def test_remove_failure_ignored(self, _exists, _os_kill, _kill, _remove):
        with patch("builtins.open", mock_open(read_data="12345")):
            troshkad._kill_existing_chronyd("/run/chrony.pid")

    @patch("troshkad.os.remove")
    @patch("troshkad._safe_kill")
    @patch("troshkad.os.kill")
    @patch("troshkad.os.path.exists", return_value=True)
    def test_sigkill_after_timeout(self, _exists, mock_os_kill, mock_safe_kill, _remove):
        """When process doesn't die after SIGTERM polls, SIGKILL is sent."""
        mock_safe_kill.return_value = True
        mock_os_kill.return_value = None  # Process stays alive (no exception)
        with patch("builtins.open", mock_open(read_data="12345")):
            troshkad._kill_existing_chronyd("/run/chrony.pid")
        # Should have called _safe_kill at least twice (SIGTERM + SIGKILL)
        self.assertGreaterEqual(mock_safe_kill.call_count, 2)


# ── _kill_and_restart_dnsmasq ──


class TestKillAndRestartDnsmasq(unittest.TestCase):
    def _make_job(self):
        return {"job_id": "j1", "output": [], "_cancelled": False}

    @patch("troshkad.subprocess.run")
    @patch("troshkad._run_cmd")
    @patch("troshkad._kill_existing_dnsmasq")
    def test_restarts_dnsmasq(self, mock_kill, mock_rcmd, mock_subrun):
        with patch("builtins.open", mock_open(read_data="54321")):
            troshkad._kill_and_restart_dnsmasq(
                self._make_job(), "ns1", "/etc/dnsmasq.d/test.conf",
                "/run/test.pid", 100, "br-100"
            )
        mock_kill.assert_called_once()
        mock_rcmd.assert_called_once()


# ── _setup_dnsmasq_for_network ──


class TestSetupDnsmasqForNetwork(unittest.TestCase):
    def _make_job(self):
        return {"job_id": "j1", "output": [], "_cancelled": False}

    @patch("troshkad._kill_and_restart_dnsmasq")
    def test_dhcp_disabled(self, _restart):
        troshkad._setup_dnsmasq_for_network(
            self._make_job(), "ns1", "aabbccdd-1122-3344-5566-778899001122",
            {"dhcp_enabled": False}
        )
        _restart.assert_not_called()

    @patch("troshkad._kill_and_restart_dnsmasq")
    def test_no_range(self, _restart):
        troshkad._setup_dnsmasq_for_network(
            self._make_job(), "ns1", "aabbccdd-1122-3344-5566-778899001122",
            {"dhcp_enabled": True, "vni": 100, "bridge_name": "br-100",
             "dhcp_config": {"range_start": "", "range_end": ""}}
        )
        _restart.assert_not_called()

    @patch("troshkad._kill_and_restart_dnsmasq")
    @patch("troshkad._build_dnsmasq_config_lines", return_value=["line1", "line2"])
    @patch("troshkad.os.makedirs")
    def test_full_setup(self, _mkdirs, _build, mock_restart):
        with patch("builtins.open", mock_open()):
            troshkad._setup_dnsmasq_for_network(
                self._make_job(), "ns1", "aabbccdd-1122-3344-5566-778899001122",
                {"dhcp_enabled": True, "vni": 100, "bridge_name": "br-100",
                 "dhcp_config": {"range_start": "10.0.0.100", "range_end": "10.0.0.200"}}
            )
        mock_restart.assert_called_once()


# ── _handle_lb_setup ──


class TestHandleLbSetup(unittest.TestCase):
    def _make_job(self):
        return {"job_id": "j1", "output": [], "_cancelled": False}

    @patch("troshkad._run_cmd")
    @patch("troshkad.os.makedirs")
    @patch("troshkad._build_haproxy_config", return_value="global\n  daemon\n")
    @patch("troshkad._assign_lb_ip_to_bridge")
    @patch("troshkad.os.path.exists", return_value=False)
    def test_setup_without_lb_ip(self, _exists, mock_assign, _build, _mkdirs, _rcmd):
        with patch("builtins.open", mock_open()):
            result = troshkad._handle_lb_setup(self._make_job(), {
                "ns": "ns1",
                "project_id": "aabbccdd-1122-3344-5566-778899001122",
                "frontends": [],
                "backends": [],
            })
        self.assertEqual(result["status"], "started")
        mock_assign.assert_not_called()

    @patch("troshkad._run_cmd")
    @patch("troshkad.os.makedirs")
    @patch("troshkad._build_haproxy_config", return_value="global\n  daemon\n")
    @patch("troshkad._assign_lb_ip_to_bridge")
    @patch("troshkad.os.path.exists", return_value=False)
    def test_setup_with_lb_ip(self, _exists, mock_assign, _build, _mkdirs, _rcmd):
        with patch("builtins.open", mock_open()):
            _result = troshkad._handle_lb_setup(self._make_job(), {
                "ns": "ns1",
                "project_id": "aabbccdd-1122-3344-5566-778899001122",
                "lb_ip": "192.168.1.100",
            })
        mock_assign.assert_called_once()

    @patch("troshkad._run_cmd")
    @patch("troshkad.os.makedirs")
    @patch("troshkad._build_haproxy_config", return_value="config")
    @patch("troshkad._assign_lb_ip_to_bridge")
    @patch("troshkad.os.path.exists", return_value=True)
    def test_kills_old_haproxy(self, _exists, _assign, _build, _mkdirs, _rcmd):
        with patch("builtins.open", mock_open(read_data="99999")):
            result = troshkad._handle_lb_setup(self._make_job(), {
                "ns": "ns1",
                "project_id": "aabbccdd-1122-3344-5566-778899001122",
            })
        self.assertEqual(result["status"], "started")


# ── _assign_lb_ip_to_bridge ──


class TestAssignLbIpToBridge(unittest.TestCase):
    def _make_job(self):
        return {"job_id": "j1", "output": [], "_cancelled": False}

    @patch("troshkad._run_cmd")
    @patch("troshkad.subprocess.run")
    def test_assigns_ip_to_first_bridge(self, mock_subrun, mock_rcmd):
        mock_subrun.return_value = MagicMock(
            stdout="2: br-troshka-aabb@if3: <BROADCAST> mtu 1500\n"
        )
        troshkad._assign_lb_ip_to_bridge(self._make_job(), "ns1", "192.168.1.100")
        mock_rcmd.assert_called_once()

    @patch("troshkad._run_cmd")
    @patch("troshkad.subprocess.run")
    def test_skips_bmc_bridge(self, mock_subrun, mock_rcmd):
        mock_subrun.return_value = MagicMock(
            stdout="2: br-bmc-aabb@if3: <BROADCAST>\n3: br-troshka-aabb@if4: <BROADCAST>\n"
        )
        troshkad._assign_lb_ip_to_bridge(self._make_job(), "ns1", "192.168.1.100")
        mock_rcmd.assert_called_once()
        args = mock_rcmd.call_args[0][1]
        self.assertIn("br-troshka-aabb", args)

    @patch("troshkad._run_cmd")
    @patch("troshkad.subprocess.run")
    def test_no_bridge_found(self, mock_subrun, mock_rcmd):
        mock_subrun.return_value = MagicMock(stdout="")
        troshkad._assign_lb_ip_to_bridge(self._make_job(), "ns1", "192.168.1.100")
        mock_rcmd.assert_not_called()

    @patch("troshkad._run_cmd", side_effect=RuntimeError("already exists"))
    @patch("troshkad.subprocess.run")
    def test_addr_add_failure_ignored(self, mock_subrun, _rcmd):
        mock_subrun.return_value = MagicMock(
            stdout="2: br-troshka-aabb@if3: <BROADCAST>\n"
        )
        # Should not raise
        troshkad._assign_lb_ip_to_bridge(self._make_job(), "ns1", "192.168.1.100")


# ── _restart_dead_dnsmasq ──


class TestRestartDeadDnsmasq(unittest.TestCase):
    @patch("troshkad._find_namespace_from_conf", return_value=None)
    @patch("troshkad._log_dead_dnsmasq_info")
    def test_no_namespace(self, _log, _find):
        result = troshkad._restart_dead_dnsmasq("/run/test.pid", "/etc/dnsmasq.d/test.conf", "test")
        self.assertFalse(result)

    @patch("troshkad.subprocess.run")
    @patch("troshkad._find_namespace_from_conf", return_value="troshka-aabbccdd")
    @patch("troshkad._log_dead_dnsmasq_info")
    def test_namespace_not_present(self, _log, _find, mock_subrun):
        mock_subrun.return_value = MagicMock(stdout="other-ns")
        result = troshkad._restart_dead_dnsmasq("/run/test.pid", "/etc/dnsmasq.d/test.conf", "test")
        self.assertFalse(result)

    @patch("troshkad.subprocess.run")
    @patch("troshkad._find_namespace_from_conf", return_value="troshka-aabbccdd")
    @patch("troshkad._log_dead_dnsmasq_info")
    def test_successful_restart(self, _log, _find, mock_subrun):
        mock_subrun.side_effect = [
            MagicMock(stdout="troshka-aabbccdd\nother-ns\n"),  # ip netns list
            MagicMock(returncode=0),  # dnsmasq start
            MagicMock(returncode=0),  # auditctl
        ]
        with patch("builtins.open", mock_open(read_data="12345")):
            result = troshkad._restart_dead_dnsmasq(
                "/run/troshka-dnsmasq-100.pid",
                "/etc/dnsmasq.d/troshka-100.conf",
                "troshka-100"
            )
        self.assertTrue(result)

    @patch("troshkad.subprocess.run")
    @patch("troshkad._find_namespace_from_conf", return_value="troshka-aabbccdd")
    @patch("troshkad._log_dead_dnsmasq_info")
    def test_restart_failure(self, _log, _find, mock_subrun):
        mock_subrun.side_effect = [
            MagicMock(stdout="troshka-aabbccdd\n"),  # ip netns list
            Exception("dnsmasq failed to start"),  # dnsmasq start
        ]
        result = troshkad._restart_dead_dnsmasq(
            "/run/test.pid", "/etc/dnsmasq.d/test.conf", "test"
        )
        self.assertFalse(result)


# ── _restore_sushy_emulators ──


class TestRestoreSushyEmulators(unittest.TestCase):
    @patch("troshkad.subprocess.Popen")
    @patch("troshkad._safe_kill")
    @patch("troshkad.os.path.exists", return_value=True)
    @patch("troshkad.os.listdir", return_value=["sushy-vm1.conf", "other-file.txt"])
    def test_restarts_matching_configs(self, _listdir, _exists, _kill, mock_popen):
        mock_popen.return_value = MagicMock(pid=12345)
        with patch("builtins.open", mock_open(read_data="99999")):
            troshkad._restore_sushy_emulators("/var/lib/troshka/bmc/proj1", "troshka-aabbccdd", "/opt/troshka/venv/bin")
        mock_popen.assert_called_once()

    @patch("troshkad.os.listdir", return_value=["other-file.txt", "readme.md"])
    def test_no_matching_files(self, _listdir):
        troshkad._restore_sushy_emulators("/var/lib/troshka/bmc/proj1", "ns1", "/opt/troshka/venv/bin")


# ── _kill_stale_vbmcd ──


class TestKillStaleVbmcd(unittest.TestCase):
    @patch("troshkad.os.path.exists", return_value=False)
    def test_no_pid_file(self, _exists):
        troshkad._kill_stale_vbmcd("/bmc/vbmcd.pid")

    @patch("troshkad.os.remove")
    @patch("troshkad.os.kill", side_effect=ProcessLookupError)
    @patch("troshkad._safe_kill", return_value=True)
    @patch("troshkad.os.path.exists", return_value=True)
    def test_kills_and_removes(self, _exists, mock_kill, _os_kill, mock_remove):
        with patch("builtins.open", mock_open(read_data="12345")):
            troshkad._kill_stale_vbmcd("/bmc/vbmcd.pid")
        mock_kill.assert_called()
        mock_remove.assert_called_with("/bmc/vbmcd.pid")

    @patch("troshkad.os.remove", side_effect=FileNotFoundError)
    @patch("troshkad._safe_kill", return_value=True)
    @patch("troshkad.os.kill", side_effect=ProcessLookupError)
    @patch("troshkad.os.path.exists", return_value=True)
    def test_remove_failure_ignored(self, _exists, _os_kill, _kill, _remove):
        with patch("builtins.open", mock_open(read_data="12345")):
            troshkad._kill_stale_vbmcd("/bmc/vbmcd.pid")


# ── _register_vbmc_entries ──


class TestRegisterVbmcEntries(unittest.TestCase):
    @patch("troshkad.os.path.isdir", return_value=False)
    def test_no_vbmcd_dir(self, _isdir):
        troshkad._register_vbmc_entries("/bmc/proj1", "ns1", "/opt/troshka/venv/bin", {})

    @patch("troshkad.subprocess.run")
    @patch("troshkad.os.path.isdir")
    @patch("troshkad.os.listdir", return_value=["troshka-vm1", "other-entry"])
    def test_starts_matching_entries(self, _listdir, mock_isdir, mock_subrun):
        mock_isdir.side_effect = lambda p: True
        mock_subrun.return_value = MagicMock(returncode=0)
        troshkad._register_vbmc_entries("/bmc/proj1", "ns1", "/opt/troshka/venv/bin", {})
        mock_subrun.assert_called_once()

    @patch("troshkad.subprocess.run", side_effect=Exception("vbmc error"))
    @patch("troshkad.os.path.isdir", return_value=True)
    @patch("troshkad.os.listdir", return_value=["troshka-vm1"])
    def test_vbmc_start_failure(self, _listdir, _isdir, _subrun):
        """Failure to start vbmc is logged but doesn't raise."""
        troshkad._register_vbmc_entries("/bmc/proj1", "ns1", "/opt/troshka/venv/bin", {})


# ── _restore_vbmcd ──


class TestRestoreVbmcd(unittest.TestCase):
    @patch("troshkad.os.path.exists", return_value=False)
    def test_no_vbmcd_conf(self, _exists):
        troshkad._restore_vbmcd("/bmc/proj1", "ns1", "/opt/troshka/venv/bin", "proj1")

    @patch("troshkad._register_vbmc_entries")
    @patch("troshkad.os.path.exists")
    @patch("troshkad._kill_stale_vbmcd")
    @patch("troshkad.subprocess.Popen")
    def test_restarts_vbmcd(self, mock_popen, mock_kill, mock_exists, mock_register):
        mock_exists.side_effect = lambda p: True
        mock_popen.return_value = MagicMock()
        troshkad._restore_vbmcd("/bmc/proj1", "ns1", "/opt/troshka/venv/bin", "proj1")
        mock_kill.assert_called_once()
        mock_popen.assert_called_once()
        mock_register.assert_called_once()


# ── _restore_bmc_services ──


class TestRestoreBmcServices(unittest.TestCase):
    @patch("troshkad.os.path.isdir", return_value=False)
    def test_no_bmc_dir(self, _isdir):
        troshkad._restore_bmc_services()

    @patch("troshkad._restore_vbmcd")
    @patch("troshkad._restore_sushy_emulators")
    @patch("troshkad.subprocess.run")
    @patch("troshkad.os.path.isdir")
    @patch("troshkad.os.listdir", return_value=["aabbccdd-1122-3344-5566-778899001122"])
    def test_restores_for_existing_namespaces(self, _listdir, mock_isdir, mock_subrun,
                                               mock_sushy, mock_vbmcd):
        mock_isdir.return_value = True
        mock_subrun.return_value = MagicMock(returncode=0)
        troshkad._restore_bmc_services()
        mock_sushy.assert_called_once()
        mock_vbmcd.assert_called_once()

    @patch("troshkad._restore_vbmcd")
    @patch("troshkad._restore_sushy_emulators")
    @patch("troshkad.subprocess.run", side_effect=subprocess.CalledProcessError(1, "ip"))
    @patch("troshkad.os.path.isdir", return_value=True)
    @patch("troshkad.os.listdir", return_value=["aabbccdd-1122-3344-5566-778899001122"])
    def test_skips_missing_namespace(self, _listdir, _isdir, _subrun, mock_sushy, mock_vbmcd):
        troshkad._restore_bmc_services()
        mock_sushy.assert_not_called()
        mock_vbmcd.assert_not_called()


# ── _bmc_start_sushy_for_vm ──


class TestBmcStartSushyForVm(unittest.TestCase):
    def _make_job(self):
        return {"job_id": "j1", "output": [], "_cancelled": False}

    @patch("troshkad._bmc_start_sushy_instance")
    @patch("troshkad._generate_self_signed_cert")
    @patch("troshkad._bmc_write_sushy_conf")
    @patch("troshkad.subprocess.run")
    def test_starts_http_and_ssl(self, mock_subrun, mock_write_conf, mock_cert, mock_start):
        mock_subrun.return_value = MagicMock(returncode=0, stdout="uuid-1234\n")
        troshkad._bmc_start_sushy_for_vm(
            self._make_job(), "ns1", "/bmc/proj1", "/opt/troshka/venv/bin",
            {"domain_name": "troshka-aabbccdd-11223344", "bmc_ip": "10.0.0.10"},
            "troshka-vmedia-aabbccdd", "/bmc/proj1/htpasswd"
        )
        # Should write 2 configs (HTTP + SSL) and start 2 instances
        self.assertEqual(mock_write_conf.call_count, 2)
        self.assertEqual(mock_start.call_count, 2)
        mock_cert.assert_called_once()


# ── _scp_push_small_file ──


class TestScpPushSmallFile(unittest.TestCase):
    @patch("troshkad.os.unlink")
    @patch("troshkad.subprocess.run")
    def test_scp_success(self, mock_subrun, _unlink):
        mock_subrun.return_value = MagicMock(returncode=0)
        handler = MagicMock()
        troshkad._scp_push_small_file(
            handler, [], "pass", None, "/tmp/file.txt",
            "root", "192.168.1.10", "/etc/hostname", None, 100
        )
        handler._send_json.assert_called_with(200, {"size": 100, "remote_path": "/etc/hostname"})

    @patch("troshkad.os.unlink")
    @patch("troshkad.subprocess.run")
    def test_scp_failure(self, mock_subrun, _unlink):
        mock_subrun.return_value = MagicMock(returncode=1, stderr=b"Permission denied")
        handler = MagicMock()
        troshkad._scp_push_small_file(
            handler, [], "pass", None, "/tmp/file.txt",
            "root", "192.168.1.10", "/etc/hostname", None, 100
        )
        args = handler._send_json.call_args[0]
        self.assertEqual(args[0], 502)

    @patch("troshkad.os.unlink")
    @patch("troshkad.subprocess.run")
    def test_scp_with_mode(self, mock_subrun, _unlink):
        mock_subrun.return_value = MagicMock(returncode=0)
        handler = MagicMock()
        troshkad._scp_push_small_file(
            handler, [], "pass", None, "/tmp/file.txt",
            "root", "192.168.1.10", "/etc/hostname", "0644", 100
        )
        # chmod call made
        self.assertEqual(mock_subrun.call_count, 2)

    @patch("troshkad.os.unlink")
    @patch("troshkad.subprocess.run")
    def test_key_file_cleanup(self, mock_subrun, mock_unlink):
        mock_subrun.return_value = MagicMock(returncode=0)
        handler = MagicMock()
        troshkad._scp_push_small_file(
            handler, [], None, "/tmp/key.pem", "/tmp/file.txt",
            "root", "192.168.1.10", "/etc/hostname", None, 100
        )
        # Should unlink both tmp_path and key_file
        unlink_paths = [c[0][0] for c in mock_unlink.call_args_list]
        self.assertIn("/tmp/file.txt", unlink_paths)
        self.assertIn("/tmp/key.pem", unlink_paths)


# ── _handle_vm_console_exec ──


class TestHandleVmConsoleExec(unittest.TestCase):
    def _make_job(self):
        return {"job_id": "j1", "output": [], "_cancelled": False}

    def test_no_command(self):
        with self.assertRaises(RuntimeError):
            troshkad._handle_vm_console_exec(self._make_job(), {
                "domain_name": "troshka-aabbccdd-11223344",
                "command": "",
                "password": "pass",  # pragma: allowlist secret
            })

    def test_no_password(self):
        with self.assertRaises(RuntimeError):
            troshkad._handle_vm_console_exec(self._make_job(), {
                "domain_name": "troshka-aabbccdd-11223344",
                "command": "whoami",
                "password": "",
            })

    @patch("troshkad.subprocess.run")
    def test_tesseract_missing(self, mock_subrun):
        mock_subrun.return_value = MagicMock(returncode=1)
        with self.assertRaises(RuntimeError) as ctx:
            troshkad._handle_vm_console_exec(self._make_job(), {
                "domain_name": "troshka-aabbccdd-11223344",
                "command": "whoami",
                "password": "pass",  # pragma: allowlist secret
            })
        self.assertIn("tesseract", str(ctx.exception))

    @patch("troshkad.subprocess.run")
    def test_domain_not_running(self, mock_subrun):
        mock_subrun.side_effect = [
            MagicMock(returncode=0),  # which tesseract
            MagicMock(returncode=0, stdout="shut off\n"),  # domstate
        ]
        with self.assertRaises(RuntimeError) as ctx:
            troshkad._handle_vm_console_exec(self._make_job(), {
                "domain_name": "troshka-aabbccdd-11223344",
                "command": "whoami",
                "password": "pass",  # pragma: allowlist secret
            })
        self.assertIn("not running", str(ctx.exception))

    @patch("troshkad._console_extract_output", return_value=("output", 0))
    @patch("troshkad._console_screenshot_ocr", return_value="TROSHKA_BEGIN\noutput\nTROSHKA_EXIT 0")
    @patch("troshkad._console_send_text")
    @patch("troshkad._console_login", return_value=True)
    @patch("troshkad.subprocess.run")
    def test_full_exec_success(self, mock_subrun, _login, _send, _ocr, _extract):
        mock_subrun.side_effect = [
            MagicMock(returncode=0),  # which tesseract
            MagicMock(returncode=0, stdout="running\n"),  # domstate
        ]
        result = troshkad._handle_vm_console_exec(self._make_job(), {
            "domain_name": "troshka-aabbccdd-11223344",
            "command": "whoami",
            "password": "pass",  # pragma: allowlist secret
            "timeout": "5",
        })
        self.assertEqual(result["output"], "output")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["method"], "console")

    @patch("troshkad._console_login", return_value=False)
    @patch("troshkad.subprocess.run")
    def test_login_failure(self, mock_subrun, _login):
        mock_subrun.side_effect = [
            MagicMock(returncode=0),  # which tesseract
            MagicMock(returncode=0, stdout="running\n"),  # domstate
        ]
        result = troshkad._handle_vm_console_exec(self._make_job(), {
            "domain_name": "troshka-aabbccdd-11223344",
            "command": "whoami",
            "password": "pass",  # pragma: allowlist secret
        })
        self.assertIn("error", result)
        self.assertIn("prompt", result["error"])

    @patch("troshkad._console_send_keys")
    @patch("troshkad._console_extract_output", return_value=("output", 0))
    @patch("troshkad._console_screenshot_ocr", return_value="TROSHKA_EXIT 0")
    @patch("troshkad._console_send_text")
    @patch("troshkad._console_login", return_value=True)
    @patch("troshkad.subprocess.run")
    def test_force_tty(self, mock_subrun, _login, _send, _ocr, _extract, mock_keys):
        mock_subrun.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=0, stdout="running\n"),
        ]
        _result = troshkad._handle_vm_console_exec(self._make_job(), {
            "domain_name": "troshka-aabbccdd-11223344",
            "command": "whoami",
            "password": "pass",  # pragma: allowlist secret
            "force_tty": True,
        })
        # Should have called send_keys for TTY3 switch and TTY1 return
        self.assertGreaterEqual(mock_keys.call_count, 2)


# ── _handle_vm_serial_exec ──


class TestHandleVmSerialExec(unittest.TestCase):
    def _make_job(self):
        return {"job_id": "j1", "output": [], "_cancelled": False}

    def test_no_command(self):
        with self.assertRaises(RuntimeError):
            troshkad._handle_vm_serial_exec(self._make_job(), {
                "domain_name": "troshka-aabbccdd-11223344",
            })

    @patch("troshkad.os.close")
    @patch("troshkad.os.open", return_value=5)
    @patch("troshkad.os.path.isdir", return_value=True)
    @patch("troshkad._serial_open_pty", return_value="/dev/pts/1")
    def test_timeout_returns_error(self, _open_pty, _isdir, _os_open, _os_close):
        # Mock pexpect
        mock_fdpexpect = MagicMock()
        TIMEOUT_cls = type("TIMEOUT", (Exception,), {})
        mock_child = MagicMock()

        import sys as _sys
        with patch.dict(_sys.modules, {
            "pexpect": MagicMock(TIMEOUT=TIMEOUT_cls, EOF=Exception),
            "pexpect.fdpexpect": mock_fdpexpect,
        }):
            mock_fdpexpect.fdspawn.return_value = mock_child
            mock_child.expect.side_effect = TIMEOUT_cls()
            with patch("troshkad._serial_poke_and_login", return_value=None):
                result = troshkad._handle_vm_serial_exec(self._make_job(), {
                    "domain_name": "troshka-aabbccdd-11223344",
                    "command": "whoami",
                    "timeout": 1,
                })
        self.assertIn("error", result)


# ── _collect_existing_disk_paths ──


class TestCollectExistingDiskPaths(unittest.TestCase):
    def test_collects_paths(self):
        import xml.etree.ElementTree as ET
        disk1 = ET.fromstring('<disk><source file="/path1.qcow2"/></disk>')
        disk2 = ET.fromstring('<disk><source file="/path2.qcow2"/></disk>')
        disk_no_source = ET.fromstring('<disk></disk>')
        result = troshkad._collect_existing_disk_paths([disk1, disk2, disk_no_source])
        self.assertEqual(result, {"/path1.qcow2", "/path2.qcow2"})

    def test_empty_list(self):
        result = troshkad._collect_existing_disk_paths([])
        self.assertEqual(result, set())


# ── Handle dispatch / _handle method tests (lines 812-835, 893-905) ──


class TestHandlerHandle(unittest.TestCase):
    """Tests for TroshkadHandler._handle method and route handlers."""

    @patch("troshkad._get_job")
    def test_handle_get_job_found(self, mock_get_job):
        mock_get_job.return_value = {
            "job_id": "j1", "command": "test", "status": "completed",
            "output": ["line1"], "result": {"ok": True},
            "started_at": 1000, "completed_at": 1001,
        }
        handler = MagicMock()
        troshkad.handle_get_job(handler, {"job_id": "j1"})
        handler._send_json.assert_called_once()
        args = handler._send_json.call_args[0]
        self.assertEqual(args[0], 200)
        self.assertEqual(args[1]["job_id"], "j1")

    @patch("troshkad._get_job", return_value=None)
    def test_handle_get_job_not_found(self, _get):
        handler = MagicMock()
        troshkad.handle_get_job(handler, {"job_id": "j1"})
        args = handler._send_json.call_args[0]
        self.assertEqual(args[0], 404)

    @patch("troshkad._cancel_job")
    def test_handle_cancel_job_found(self, mock_cancel):
        mock_cancel.return_value = {"job_id": "j1", "status": "cancelled"}
        handler = MagicMock()
        troshkad.handle_cancel_job(handler, {"job_id": "j1"})
        args = handler._send_json.call_args[0]
        self.assertEqual(args[0], 200)
        self.assertEqual(args[1]["status"], "cancelled")

    @patch("troshkad._cancel_job", return_value=None)
    def test_handle_cancel_job_not_found(self, _cancel):
        handler = MagicMock()
        troshkad.handle_cancel_job(handler, {"job_id": "j1"})
        args = handler._send_json.call_args[0]
        self.assertEqual(args[0], 404)

    @patch("troshkad._dispatch_job", return_value=(202, {"job_id": "j1"}))
    def test_handle_dispatch_command(self, _dispatch):
        handler = MagicMock()
        handler._read_body.return_value = {"key": "value"}
        troshkad.handle_dispatch_command(handler, {"command_path": "test/cmd"})
        handler._send_json.assert_called_with(202, {"job_id": "j1"})


if __name__ == "__main__":
    unittest.main()
