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
subprocess.run(
    [
        "openssl",
        "req",
        "-x509",
        "-newkey",
        "ec",
        "-pkeyopt",
        "ec_paramgen_curve:prime256v1",
        "-nodes",
        "-days",
        "1",
        "-subj",
        "/CN=localhost",
        "-keyout",
        KEY_PATH,
        "-out",
        CERT_PATH,
    ],
    capture_output=True,
    check=True,
)

# Write test config
with open(CONF_PATH, "w") as f:
    json.dump(
        {
            "port": TEST_PORT,
            "token": TEST_TOKEN,
            "tls_cert": CERT_PATH,
            "tls_key": KEY_PATH,
            "host_id": "test-host-id",
            "max_concurrent_jobs": 2,
            "drain_timeout_seconds": 5,
        },
        f,
    )

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
            assert (
                resp.status == expect_status
            ), f"Expected {expect_status}, got {resp.status}"
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
        cls.server_thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True
        )
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

    def test_job_dispatch_and_poll(self):
        """Test job dispatch, polling until completion, and result retrieval."""

        # Register a test handler
        def test_echo_handler(job, params):
            time.sleep(0.2)
            return {"echo": params.get("msg")}

        troshkad.COMMAND_HANDLERS["_test/echo"] = test_echo_handler

        try:
            # Dispatch job
            status, body = _make_request(
                "/commands/_test/echo", method="POST", body={"msg": "hello"}
            )
            self.assertEqual(status, 202)
            self.assertIn("job_id", body)
            self.assertEqual(body["status"], "running")
            job_id = body["job_id"]

            # Poll until completed
            for _ in range(20):
                time.sleep(0.1)
                status, job = _make_request(f"/jobs/{job_id}")
                self.assertEqual(status, 200)
                if job["status"] == "completed":
                    break

            self.assertEqual(job["status"], "completed")
            self.assertIsNotNone(job["result"])
            self.assertEqual(job["result"]["echo"], "hello")
        finally:
            del troshkad.COMMAND_HANDLERS["_test/echo"]

    def test_max_concurrent_jobs_returns_503(self):
        """Test that max_concurrent_jobs limit is enforced."""
        barrier = threading.Event()

        def slow_handler(job, params):
            barrier.wait()
            return {"done": True}

        troshkad.COMMAND_HANDLERS["_test/slow"] = slow_handler

        try:
            # Fill up 2 slots (max_concurrent_jobs=2 in test config)
            status1, body1 = _make_request(
                "/commands/_test/slow", method="POST", body={}
            )
            self.assertEqual(status1, 202)
            status2, body2 = _make_request(
                "/commands/_test/slow", method="POST", body={}
            )
            self.assertEqual(status2, 202)

            # Third should return 503
            status3, body3 = _make_request(
                "/commands/_test/slow", method="POST", body={}
            )
            self.assertEqual(status3, 503)
            self.assertIn("max_concurrent_jobs", body3["error"])
        finally:
            barrier.set()
            del troshkad.COMMAND_HANDLERS["_test/slow"]

    def test_draining_rejects_new_jobs(self):
        """Test that draining status rejects new jobs."""

        def test_handler(job, params):
            return {"done": True}

        troshkad.COMMAND_HANDLERS["_test/drain"] = test_handler

        try:
            troshkad._draining = True
            status, body = _make_request(
                "/commands/_test/drain", method="POST", body={}
            )
            self.assertEqual(status, 503)
            self.assertEqual(body["status"], "draining")
        finally:
            troshkad._draining = False
            del troshkad.COMMAND_HANDLERS["_test/drain"]

    def test_update_validates_syntax(self):
        """Test that update endpoint rejects invalid Python syntax."""
        import base64

        invalid_script = "def broken("
        encoded_script = base64.b64encode(invalid_script.encode()).decode()
        status, body = _make_request(
            "/admin/update",
            method="POST",
            body={"script": encoded_script, "version": "test-version"},
        )
        self.assertEqual(status, 400)
        self.assertIn("syntax", body["error"].lower())

    def test_update_accepts_valid_script(self):
        """Test that update endpoint accepts valid Python script."""
        import base64
        import unittest.mock

        # Use the current troshkad.py file as a valid script
        with open(os.path.join(os.path.dirname(__file__), "..", "troshkad.py")) as f:
            valid_script = f.read()
        encoded_script = base64.b64encode(valid_script.encode()).decode()

        # Mock the restart function to prevent actual restart
        restart_event = threading.Event()

        def mock_restart(script_path, new_path):
            restart_event.set()

        try:
            with unittest.mock.patch.object(
                troshkad, "_do_update_restart", mock_restart
            ):
                status, body = _make_request(
                    "/admin/update",
                    method="POST",
                    body={"script": encoded_script, "version": "test-version"},
                )
                self.assertEqual(status, 200)
                self.assertEqual(body["status"], "restarting")

                # Wait for restart to be called
                restart_called = restart_event.wait(timeout=2)
                self.assertTrue(restart_called, "Restart was not called")
        finally:
            troshkad._draining = False

    def test_disk_usage_returns_stats(self):
        """Test that /host/disk-usage returns partition list."""
        status, body = _make_request("/host/disk-usage")
        self.assertEqual(status, 200)
        self.assertIn("partitions", body)
        self.assertIsInstance(body["partitions"], list)
        # On macOS dev machine, /proc/mounts doesn't exist so partitions may be empty
        for p in body["partitions"]:
            self.assertIn("free_bytes", p)
            self.assertIn("total_bytes", p)
            self.assertIn("used_pct", p)
            self.assertIsInstance(p["free_bytes"], (int, float))
            self.assertIsInstance(p["total_bytes"], (int, float))
            self.assertIsInstance(p["used_pct"], (int, float))
            self.assertGreaterEqual(p["used_pct"], 0)
            self.assertLessEqual(p["used_pct"], 100)


from unittest.mock import patch, MagicMock


def _mock_popen(returncode=0, stdout="", stderr=""):
    """Create a mock Popen instance that works with _run_cmd."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate.return_value = (stdout, stderr)
    return proc


class TestVmHandlers(unittest.TestCase):
    """Unit tests for VM command handlers — mock subprocess."""

    @patch("troshkad.subprocess.Popen")
    def test_vm_create_calls_virt_install(self, mock_popen):
        mock_popen.return_value = _mock_popen(stdout="Domain created")
        job = troshkad._create_job(
            "vms/create",
            {
                "domain_name": "troshka-aabbccdd-11223344",
                "vcpus": 2,
                "ram_mb": 4096,
                "disks": [
                    {
                        "path": "/var/lib/troshka/vms/proj/aabb-1122.qcow2",
                        "bus": "virtio",
                    }
                ],
                "networks": [{"bridge": "br-troshka-abc", "model": "virtio"}],
                "seed_iso": "/var/lib/troshka/vms/proj/aabb-seed.iso",
            },
        )
        _result = troshkad._handle_vm_create(job, job["params"])
        self.assertTrue(mock_popen.called)
        cmd = mock_popen.call_args_list[0][0][0]
        self.assertEqual(cmd[0], "virt-install")
        self.assertIn("--name", cmd)
        self.assertIn("troshka-aabbccdd-11223344", cmd)

    @patch("troshkad.subprocess.Popen")
    def test_vm_create_uefi_uses_q35(self, mock_popen):
        mock_popen.return_value = _mock_popen(stdout="Domain created")
        job = troshkad._create_job(
            "vms/create",
            {
                "domain_name": "troshka-aabbccdd-11223344",
                "vcpus": 2,
                "ram_mb": 4096,
                "disks": [
                    {
                        "path": "/var/lib/troshka/vms/proj/aabb-1122.qcow2",
                        "bus": "virtio",
                    }
                ],
                "networks": [{"bridge": "br-troshka-abc", "model": "virtio"}],
                "firmware": "uefi",
            },
        )
        troshkad._handle_vm_create(job, job["params"])
        cmd = mock_popen.call_args_list[0][0][0]
        machine_idx = cmd.index("--machine")
        self.assertEqual(cmd[machine_idx + 1], "q35")

    @patch("troshkad.subprocess.Popen")
    def test_vm_create_explicit_i440fx(self, mock_popen):
        mock_popen.return_value = _mock_popen(stdout="Domain created")
        job = troshkad._create_job(
            "vms/create",
            {
                "domain_name": "troshka-aabbccdd-11223344",
                "vcpus": 2,
                "ram_mb": 4096,
                "disks": [
                    {
                        "path": "/var/lib/troshka/vms/proj/aabb-1122.qcow2",
                        "bus": "virtio",
                    }
                ],
                "networks": [{"bridge": "br-troshka-abc", "model": "virtio"}],
                "firmware": "bios",
                "machine_type": "i440fx",
            },
        )
        troshkad._handle_vm_create(job, job["params"])
        cmd = mock_popen.call_args_list[0][0][0]
        machine_idx = cmd.index("--machine")
        self.assertEqual(cmd[machine_idx + 1], "i440fx")

    @patch("troshkad.subprocess.Popen")
    def test_vm_create_bios_without_machine_omits_flag(self, mock_popen):
        mock_popen.return_value = _mock_popen(stdout="Domain created")
        job = troshkad._create_job(
            "vms/create",
            {
                "domain_name": "troshka-aabbccdd-11223344",
                "vcpus": 2,
                "ram_mb": 4096,
                "disks": [
                    {
                        "path": "/var/lib/troshka/vms/proj/aabb-1122.qcow2",
                        "bus": "virtio",
                    }
                ],
                "networks": [{"bridge": "br-troshka-abc", "model": "virtio"}],
                "firmware": "bios",
            },
        )
        troshkad._handle_vm_create(job, job["params"])
        cmd = mock_popen.call_args_list[0][0][0]
        self.assertNotIn("--machine", cmd)

    @patch("troshkad.subprocess.Popen")
    def test_vm_create_usb_input(self, mock_popen):
        mock_popen.return_value = _mock_popen(stdout="Domain created")
        job = troshkad._create_job(
            "vms/create",
            {
                "domain_name": "troshka-aabbccdd-11223344",
                "vcpus": 2,
                "ram_mb": 4096,
                "disks": [
                    {
                        "path": "/var/lib/troshka/vms/proj/aabb-1122.qcow2",
                        "bus": "virtio",
                    }
                ],
                "networks": [{"bridge": "br-troshka-abc", "model": "virtio"}],
                "video_model": "vga",
                "input_model": "usb",
            },
        )
        troshkad._handle_vm_create(job, job["params"])
        cmd = mock_popen.call_args_list[0][0][0]
        video_idx = cmd.index("--video")
        self.assertEqual(cmd[video_idx + 1], "vga")
        self.assertIn("type=keyboard,bus=usb", cmd)
        self.assertIn("type=tablet,bus=usb", cmd)

    @patch("troshkad.subprocess.Popen")
    def test_vm_destroy_calls_virsh(self, mock_popen):
        mock_popen.return_value = _mock_popen()
        job = troshkad._create_job(
            "vms/destroy", {"domain_name": "troshka-aabb1122-11223344"}
        )
        troshkad._handle_vm_destroy(job, job["params"])
        calls = [c[0][0] for c in mock_popen.call_args_list]
        self.assertTrue(any("destroy" in c for c in calls))
        self.assertTrue(any("undefine" in c for c in calls))

    @patch("troshkad.subprocess.run")
    @patch("troshkad.subprocess.Popen")
    def test_vm_start(self, mock_popen, mock_run):
        mock_popen.return_value = _mock_popen(stdout="Domain started")
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        job = troshkad._create_job(
            "vms/start", {"domain_name": "troshka-aabb1122-11223344"}
        )
        troshkad._handle_vm_start(job, job["params"])
        cmd = mock_popen.call_args[0][0]
        self.assertEqual(cmd[:2], ["virsh", "start"])

    @patch("troshkad.subprocess.run")
    @patch("troshkad.subprocess.Popen")
    def test_vm_stop(self, mock_popen, mock_run):
        mock_popen.return_value = _mock_popen(stdout="Domain stopped")
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
        job = troshkad._create_job(
            "vms/stop", {"domain_name": "troshka-aabb1122-11223344"}
        )
        troshkad._handle_vm_stop(job, job["params"])
        cmd = mock_popen.call_args[0][0]
        self.assertEqual(cmd[:2], ["virsh", "shutdown"])

    def test_vm_create_rejects_invalid_domain(self):
        """Domain name must match troshka-{hex}-{hex} pattern."""
        job = troshkad._create_job(
            "vms/create",
            {
                "domain_name": "evil; rm -rf /",
                "vcpus": 2,
                "ram_mb": 4096,
                "disks": [],
                "networks": [],
            },
        )
        with self.assertRaises(ValueError):
            troshkad._handle_vm_create(job, job["params"])


class TestStorageHandlers(unittest.TestCase):

    @patch("troshkad.os.makedirs")
    @patch("troshkad.subprocess.Popen")
    def test_disk_create_qcow2(self, mock_popen, mock_makedirs):
        mock_popen.return_value = _mock_popen()
        job = troshkad._create_job(
            "disks/create",
            {
                "path": "/var/lib/troshka/vms/proj-id/aabb-1122.qcow2",
                "size_gb": 20,
                "format": "qcow2",
            },
        )
        result = troshkad._handle_disk_create(job, job["params"])
        cmd = mock_popen.call_args[0][0]
        self.assertEqual(cmd[0], "qemu-img")
        self.assertIn("create", cmd)
        self.assertEqual(result["status"], "created")

    @patch("troshkad.os.makedirs")
    @patch("troshkad.subprocess.Popen")
    def test_disk_create_with_backing(self, mock_popen, mock_makedirs):
        mock_popen.return_value = _mock_popen()
        job = troshkad._create_job(
            "disks/create",
            {
                "path": "/var/lib/troshka/vms/proj-id/aabb-1122.qcow2",
                "size_gb": 20,
                "format": "qcow2",
                "backing_file": "/var/lib/troshka/images/base.qcow2",
            },
        )
        troshkad._handle_disk_create(job, job["params"])
        cmd = mock_popen.call_args[0][0]
        self.assertIn("-b", cmd)

    @patch("troshkad.subprocess.Popen")
    def test_disk_resize(self, mock_popen):
        mock_popen.return_value = _mock_popen()
        job = troshkad._create_job(
            "disks/resize",
            {
                "path": "/var/lib/troshka/vms/proj-id/aabb-1122.qcow2",
                "new_size_gb": 40,
            },
        )
        troshkad._handle_disk_resize(job, job["params"])
        cmd = mock_popen.call_args[0][0]
        self.assertEqual(cmd[:2], ["qemu-img", "resize"])

    @patch("troshkad.os.makedirs")
    @patch("troshkad.subprocess.Popen")
    def test_seed_create(self, mock_popen, mock_makedirs):
        mock_popen.return_value = _mock_popen()
        with patch("tempfile.TemporaryDirectory") as mock_tempdir:
            mock_tempdir.return_value.__enter__.return_value = "/tmp/test-tmpdir"
            job = troshkad._create_job(
                "seeds/create",
                {
                    "path": "/var/lib/troshka/vms/proj-id/aabb-seed.iso",
                    "meta_data": "instance-id: test",
                    "user_data": "#cloud-config\npassword: test",
                },
            )
            with patch("builtins.open", unittest.mock.mock_open()):
                troshkad._handle_seed_create(job, job["params"])
            cmd = mock_popen.call_args[0][0]
            self.assertEqual(cmd[0], "xorriso")

    def test_disk_create_rejects_bad_path(self):
        job = troshkad._create_job(
            "disks/create",
            {
                "path": "/etc/passwd",
                "size_gb": 20,
                "format": "qcow2",
            },
        )
        with self.assertRaises(ValueError):
            troshkad._handle_disk_create(job, job["params"])


class TestNetworkHandlers(unittest.TestCase):

    @patch("troshkad.subprocess.Popen")
    def test_network_setup(self, mock_popen):
        mock_popen.return_value = _mock_popen()
        job = troshkad._create_job(
            "networks/setup",
            {
                "network_name": "troshka-net-aabb",
                "cidr": "192.168.100.0/24",
                "vni": 10001,
                "bridge_name": "br-troshka-aabb",
                "project_id": "aabbccdd-1122-3344-5566-778899001122",
            },
        )
        result = troshkad._handle_network_setup(job, job["params"])
        self.assertEqual(result["status"], "configured")

    @patch("troshkad.subprocess.Popen")
    def test_network_teardown(self, mock_popen):
        mock_popen.return_value = _mock_popen()
        job = troshkad._create_job(
            "networks/teardown",
            {
                "network_name": "troshka-net-aabb",
                "project_id": "aabbccdd-1122-3344-5566-778899001122",
            },
        )
        result = troshkad._handle_network_teardown(job, job["params"])
        self.assertEqual(result["status"], "removed")


class TestOpsHandlers(unittest.TestCase):

    @patch("troshkad.os.makedirs")
    @patch("troshkad.subprocess.run")
    @patch("troshkad.subprocess.Popen")
    def test_snapshot_create(self, mock_popen, mock_run, mock_makedirs):
        mock_popen.return_value = _mock_popen()

        def run_side_effect(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            if "domblklist" in cmd:
                result.stdout = "Type       Device  Target     Source\nfile       disk    vda        /var/lib/troshka/vms/proj/disk.qcow2\n"
            if "domstate" in cmd:
                result.stdout = "shut off\n"
            return result

        mock_run.side_effect = run_side_effect
        job = troshkad._create_job(
            "snapshots/create",
            {
                "domain_name": "troshka-aabbccdd-11223344",
                "output_path": "/var/lib/troshka/tmp/snapshot.qcow2",
            },
        )
        result = troshkad._handle_snapshot_create(job, job["params"])
        self.assertEqual(result["status"], "created")


class TestHostEndpoints(unittest.TestCase):
    """Unit tests for host management command handlers."""

    @patch("troshkad.subprocess.Popen")
    def test_resize_storage(self, mock_popen):
        """Test that resize-storage runs xfs_growfs."""
        mock_popen.return_value = _mock_popen(stdout="Done")
        proc_mounts_content = "/dev/nvme1n1p1 /var/lib/troshka xfs rw 0 0\n"
        import io

        real_open = open

        def mock_open(path, *args, **kwargs):
            if path == "/proc/mounts":
                return io.StringIO(proc_mounts_content)
            return real_open(path, *args, **kwargs)

        job = troshkad._create_job("host/resize-storage", {})
        with patch("builtins.open", side_effect=mock_open):
            with patch("troshkad.os.statvfs") as mock_statvfs, patch(
                "troshkad.os.path.exists", return_value=False
            ):
                mock_statvfs.return_value = MagicMock(f_blocks=1000, f_frsize=4096)
                result = troshkad._handle_resize_storage(job, job["params"])
        self.assertTrue(mock_popen.called)
        cmd = mock_popen.call_args[0][0]
        self.assertEqual(cmd, ["xfs_growfs", "/var/lib/troshka"])
        self.assertEqual(result["status"], "resized")

    @patch("troshkad.os.remove")
    def test_files_remove(self, mock_remove):
        """Test that files/remove removes valid paths."""
        job = troshkad._create_job(
            "files/remove", {"paths": ["/var/lib/troshka/vms/test/disk.qcow2"]}
        )
        result = troshkad._handle_files_remove(job, job["params"])
        mock_remove.assert_called_once_with("/var/lib/troshka/vms/test/disk.qcow2")
        self.assertEqual(result["removed"], 1)

    def test_files_remove_rejects_bad_path(self):
        """Test that files/remove rejects paths outside /var/lib/troshka."""
        job = troshkad._create_job("files/remove", {"paths": ["/etc/passwd"]})
        with self.assertRaises(ValueError) as ctx:
            troshkad._handle_files_remove(job, job["params"])
        self.assertIn("/var/lib/troshka", str(ctx.exception))


class TestGcEndpoints(unittest.TestCase):
    """Unit tests for garbage collection endpoints."""

    @patch("troshkad.subprocess.run")
    @patch("troshkad.os.listdir")
    @patch("troshkad.os.path.exists")
    @patch("troshkad.os.path.isdir")
    def test_gc_discover_finds_orphans(
        self, mock_isdir, mock_exists, mock_listdir, mock_run
    ):
        """Test gc/discover scans and finds orphaned resources."""
        # Mock filesystem
        mock_exists.return_value = True
        mock_isdir.return_value = True
        mock_listdir.return_value = ["known-uuid", "orphan-uuid"]

        # Mock virsh list
        def run_side_effect(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            if "virsh" in cmd and "list" in cmd:
                result.stdout = "troshka-aabb-1122\ntroshka-dead-beef\n"
            elif "ip" in cmd and "link" in cmd:
                result.stdout = "2: br-troshka-aabb: <BROADCAST,MULTICAST> mtu 1500\n"
            elif "ip" in cmd and "netns" in cmd:
                result.stdout = "troshka-deadbeef\n"
            return result

        mock_run.side_effect = run_side_effect

        job = troshkad._create_job(
            "gc/discover",
            {
                "known_project_ids": ["known-uuid"],
                "known_domains": ["troshka-aabb-1122"],
            },
        )
        result = troshkad._handle_gc_discover(job, job["params"])

        # Check orphan dirs found
        self.assertIn("/var/lib/troshka/vms/orphan-uuid/", result["orphan_dirs"])
        # Check orphan domains found
        self.assertIn("troshka-dead-beef", result["orphan_domains"])
        # Check orphan bridges found
        self.assertIn("br-troshka-aabb", result["orphan_bridges"])
        # Check orphan namespaces found
        self.assertIn("troshka-deadbeef", result["orphan_namespaces"])

    @patch("troshkad.subprocess.Popen")
    @patch("troshkad.shutil.rmtree")
    @patch("troshkad.os.path.isdir")
    def test_gc_clean_removes_items(self, mock_isdir, mock_rmtree, mock_popen):
        """Test gc/clean removes specified orphaned resources."""
        mock_popen.return_value = _mock_popen()
        mock_isdir.return_value = True

        job = troshkad._create_job(
            "gc/clean",
            {
                "orphan_dirs": ["/var/lib/troshka/vms/dead-uuid/"],
                "orphan_domains": ["troshka-dead-beef"],
                "orphan_bridges": ["br-troshka-dead"],
                "orphan_namespaces": ["troshka-deadbeef"],
                "cache_items": [],
            },
        )
        result = troshkad._handle_gc_clean(job, job["params"])

        # Check that rmtree was called for orphan dirs
        mock_rmtree.assert_called()
        self.assertGreaterEqual(result["removed_dirs"], 0)
        self.assertGreaterEqual(result["removed_domains"], 0)


class TestLibraryImportEndpoint(unittest.TestCase):
    """Unit tests for library/import endpoint."""

    @patch("troshkad.os.path.getsize")
    @patch("troshkad.os.makedirs")
    @patch("troshkad.subprocess.Popen")
    def test_import_download_only(self, mock_popen, mock_makedirs, mock_getsize):
        """Test import with download only (no flatten, no S3 multipart)."""
        mock_popen.return_value = _mock_popen()
        mock_getsize.return_value = 1024

        job = troshkad._create_job(
            "library/import",
            {
                "download_url": "https://example.com/image.qcow2",
                "cache_path": "/var/lib/troshka/images/item-123.qcow2",
            },
        )
        result = troshkad._handle_library_import(job, job["params"])

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["size_bytes"], 1024)
        # curl should have been called
        cmd = mock_popen.call_args_list[0][0][0]
        self.assertEqual(cmd[0], "curl")

    @patch("troshkad.os.rename")
    @patch("troshkad.os.path.getsize")
    @patch("troshkad.os.makedirs")
    @patch("troshkad.subprocess.Popen")
    def test_import_with_flatten(
        self, mock_popen, mock_makedirs, mock_getsize, mock_rename
    ):
        """Test import with flatten=true runs qemu-img convert."""
        mock_popen.return_value = _mock_popen()
        mock_getsize.return_value = 2048

        job = troshkad._create_job(
            "library/import",
            {
                "download_url": "https://example.com/image.qcow2",
                "cache_path": "/var/lib/troshka/images/item-123.qcow2",
                "flatten": True,
            },
        )
        _result = troshkad._handle_library_import(job, job["params"])

        # Check that qemu-img convert was called
        cmds = [c[0][0] for c in mock_popen.call_args_list]
        self.assertTrue(any(c[0] == "qemu-img" for c in cmds if c))

    @patch("troshkad.os.makedirs")
    def test_import_rejects_bad_url(self, mock_makedirs):
        """Test that import rejects non-http(s) URLs."""
        job = troshkad._create_job(
            "library/import",
            {
                "download_url": "file:///etc/passwd",
                "cache_path": "/var/lib/troshka/images/item-123.qcow2",
            },
        )
        with self.assertRaises(ValueError):
            troshkad._handle_library_import(job, job["params"])


class TestCaptureEndpoints(unittest.TestCase):
    """Unit tests for snapshot/pattern capture endpoints."""

    @patch("troshkad.shutil.copy")
    @patch("troshkad.os.path.getsize")
    @patch("troshkad.os.makedirs")
    @patch("troshkad.subprocess.run")
    @patch("troshkad.subprocess.Popen")
    def test_snapshot_capture(
        self, mock_popen, mock_run, mock_makedirs, mock_getsize, mock_copy
    ):
        """Test snapshot capture: get disk path, flatten, upload, cache."""
        mock_popen.return_value = _mock_popen()
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Type  Device  Target  Source\nfile  disk    vda     /var/lib/troshka/vms/proj/disk.qcow2\n",
            stderr="",
        )
        mock_getsize.return_value = 12345

        with patch("tempfile.TemporaryDirectory") as mock_tempdir:
            mock_tempdir.return_value.__enter__.return_value = "/tmp/test-tmpdir"

            job = troshkad._create_job(
                "snapshots/capture",
                {
                    "domain_name": "troshka-aabbccdd-11223344",
                    "disk_index": 0,
                    "presigned_url": "https://s3.example.com/upload",
                    "cache_path": "/var/lib/troshka/cache/snapshots/item/disk.qcow2",
                },
            )
            result = troshkad._handle_snapshot_capture(job, job["params"])

        self.assertEqual(result["status"], "uploaded")
        self.assertEqual(result["size_bytes"], 12345)

    @patch("troshkad.shutil.copy")
    @patch("troshkad.os.path.getsize")
    @patch("troshkad.os.makedirs")
    @patch("troshkad.os.path.exists")
    @patch("troshkad.os.path.realpath", side_effect=lambda p: p)
    @patch("troshkad.subprocess.check_output")
    @patch("troshkad.subprocess.Popen")
    def test_pattern_capture(
        self,
        mock_popen,
        mock_check_output,
        mock_realpath,
        mock_exists,
        mock_makedirs,
        mock_getsize,
        mock_copy,
    ):
        """Test pattern capture-direct: capture multiple disks."""
        mock_popen.return_value = _mock_popen()
        mock_check_output.return_value = b'{"virtual-size": 107374182400}'
        mock_exists.return_value = True
        mock_getsize.return_value = 54321

        with patch("tempfile.TemporaryDirectory") as mock_tempdir:
            mock_tempdir.return_value.__enter__.return_value = "/tmp/test-tmpdir"
            with patch.object(troshkad, "_s3_upload_with_cache"):
                job = troshkad._create_job(
                    "patterns/capture-direct",
                    {
                        "domain_name": "troshka-aabbccdd-11223344",
                        "disks": [
                            {
                                "disk_path": "/var/lib/troshka/vms/proj/disk.qcow2",
                                "s3_url": "https://s3.example.com/upload",
                                "cache_path": "/var/lib/troshka/cache/patterns/pat/disk.qcow2",
                            }
                        ],
                    },
                )
                result = troshkad._handle_pattern_capture_direct(job, job["params"])

        self.assertEqual(result["status"], "uploaded")
        self.assertEqual(len(result["disks"]), 1)
        self.assertEqual(result["disks"][0]["size_bytes"], 54321)


class TestMetadataHandlers(unittest.TestCase):
    """Unit tests for metadata service deployment."""

    @patch("troshkad.subprocess.Popen")
    @patch("troshkad._cleanup_stale_metadata_ips")
    @patch("builtins.open", new_callable=unittest.mock.mock_open)
    @patch("troshkad.os.makedirs")
    def test_metadata_deploy(self, mock_makedirs, mock_open, mock_cleanup, mock_popen):
        mock_popen.return_value = _mock_popen()
        job = troshkad._create_job(
            "metadata/deploy",
            {
                "project_id": "aabbccdd-1122-3344-5566-778899001122",
                "bridges": ["br-10001"],
                "vm_configs": {
                    "aa:bb:cc:dd:ee:ff": {
                        "vm_name": "test",
                        "userdata": "#cloud-config\nhostname: test",
                        "metadata": '{"instance-id": "test-vm"}',
                    }
                },
                "namespace": "troshka-aabbccdd",
            },
        )
        result = troshkad._handle_metadata_deploy(job, job["params"])
        self.assertEqual(result["status"], "started")
        mock_cleanup.assert_called_once_with(job, "troshka-aabbccdd", ["br-10001"])
        self.assertIn("pid", result)

        # Verify script was written
        mock_open.assert_called()
        write_calls = [c for c in mock_open().write.call_args_list]
        self.assertGreater(len(write_calls), 0, "Script should have been written")

        # Verify metadata IP was added to bridge
        calls = [c[0][0] for c in mock_popen.call_args_list]
        ip_add_calls = [c for c in calls if "ip" in c and "addr" in c and "add" in c]
        self.assertGreater(
            len(ip_add_calls), 0, "Should have added metadata IP to bridge"
        )


class TestVmStateHandler(unittest.TestCase):
    """Tests for vms/state handler."""

    @patch("troshkad.subprocess.run")
    def test_vm_state_running(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="running\n", stderr="")
        job = troshkad._create_job(
            "vms/state", {"domain_name": "troshka-aabbccdd-11223344"}
        )
        result = troshkad._handle_vm_state(job, job["params"])
        self.assertEqual(result["domain"], "troshka-aabbccdd-11223344")
        self.assertEqual(result["state"], "running")

    @patch("troshkad.subprocess.run")
    def test_vm_state_shut_off(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="shut off\n", stderr="")
        job = troshkad._create_job(
            "vms/state", {"domain_name": "troshka-aabbccdd-11223344"}
        )
        result = troshkad._handle_vm_state(job, job["params"])
        self.assertEqual(result["state"], "shut_off")

    @patch("troshkad.subprocess.run")
    def test_vm_state_not_found(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="Domain not found"
        )
        job = troshkad._create_job(
            "vms/state", {"domain_name": "troshka-aabbccdd-11223344"}
        )
        result = troshkad._handle_vm_state(job, job["params"])
        self.assertEqual(result["state"], "not_found")

    def test_vm_state_rejects_invalid_domain(self):
        job = troshkad._create_job("vms/state", {"domain_name": "evil; rm -rf /"})
        with self.assertRaises(ValueError):
            troshkad._handle_vm_state(job, job["params"])


class TestVmListHandler(unittest.TestCase):
    """Tests for vms/list handler."""

    @patch("troshkad.subprocess.run")
    def test_vm_list(self, mock_run):
        def run_side_effect(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            if "list" in cmd:
                result.stdout = "troshka-aabb1122-11223344\ntroshka-ccdd5566-55667788\nother-domain\n"
            elif "domstate" in cmd:
                if "aabb1122" in cmd[2]:
                    result.stdout = "running\n"
                else:
                    result.stdout = "shut off\n"
            result.stderr = ""
            return result

        mock_run.side_effect = run_side_effect
        job = troshkad._create_job("vms/list", {})
        result = troshkad._handle_vm_list(job, job["params"])
        self.assertEqual(len(result["domains"]), 2)
        self.assertEqual(result["domains"][0]["name"], "troshka-aabb1122-11223344")
        self.assertEqual(result["domains"][0]["state"], "running")
        self.assertEqual(result["domains"][1]["state"], "shut_off")


class TestVmVncPortHandler(unittest.TestCase):
    """Tests for vms/vnc-port handler."""

    @patch("troshkad.subprocess.run")
    def test_vnc_port_found(self, mock_run):
        xml = """<domain>
          <devices>
            <graphics type='vnc' port='5900' autoport='yes' listen='127.0.0.1'/>
          </devices>
        </domain>"""
        mock_run.return_value = MagicMock(returncode=0, stdout=xml, stderr="")
        job = troshkad._create_job(
            "vms/vnc-port", {"domain_name": "troshka-aabbccdd-11223344"}
        )
        result = troshkad._handle_vm_vnc_port(job, job["params"])
        self.assertEqual(result["vnc_port"], 5900)

    @patch("troshkad.subprocess.run")
    def test_vnc_port_autoport(self, mock_run):
        xml = """<domain>
          <devices>
            <graphics type='vnc' port='-1' autoport='yes'/>
          </devices>
        </domain>"""
        mock_run.return_value = MagicMock(returncode=0, stdout=xml, stderr="")
        job = troshkad._create_job(
            "vms/vnc-port", {"domain_name": "troshka-aabbccdd-11223344"}
        )
        result = troshkad._handle_vm_vnc_port(job, job["params"])
        self.assertIsNone(result["vnc_port"])

    @patch("troshkad.subprocess.run")
    def test_vnc_port_domain_not_found(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="Domain not found"
        )
        job = troshkad._create_job(
            "vms/vnc-port", {"domain_name": "troshka-aabbccdd-11223344"}
        )
        result = troshkad._handle_vm_vnc_port(job, job["params"])
        self.assertIsNone(result["vnc_port"])


class TestVmConfigHandler(unittest.TestCase):
    """Tests for vms/config handler."""

    @patch("troshkad.subprocess.run")
    def test_vm_config(self, mock_run):
        xml = """<domain type='kvm'>
          <vcpu placement='static'>4</vcpu>
          <memory unit='KiB'>8388608</memory>
          <os>
            <type>hvm</type>
            <boot dev='hd'/>
            <boot dev='network'/>
          </os>
          <devices>
            <interface type='bridge'>
              <source bridge='br-10001'/>
              <mac address='52:54:00:aa:bb:cc'/>
            </interface>
            <disk type='file' device='disk'>
              <source file='/var/lib/troshka/vms/proj/disk.qcow2'/>
              <target dev='vda' bus='virtio'/>
            </disk>
            <disk type='file' device='cdrom'>
              <source file='/var/lib/troshka/vms/proj/seed.iso'/>
              <target dev='sda' bus='sata'/>
            </disk>
          </devices>
        </domain>"""
        mock_run.return_value = MagicMock(returncode=0, stdout=xml, stderr="")
        job = troshkad._create_job(
            "vms/config", {"domain_name": "troshka-aabbccdd-11223344"}
        )
        result = troshkad._handle_vm_config(job, job["params"])
        self.assertEqual(result["vcpus"], 4)
        self.assertEqual(result["ram_mb"], 8192)
        self.assertEqual(result["boot_devs"], ["hd", "network"])
        self.assertEqual(len(result["nics"]), 1)
        self.assertEqual(result["nics"][0]["bridge"], "br-10001")
        self.assertEqual(result["nics"][0]["mac"], "52:54:00:aa:bb:cc")
        self.assertEqual(result["disks"], ["/var/lib/troshka/vms/proj/disk.qcow2"])
        self.assertEqual(result["cdroms"], ["/var/lib/troshka/vms/proj/seed.iso"])


class TestVmReconfigureHandler(unittest.TestCase):
    """Tests for vms/reconfigure handler."""

    SAMPLE_XML = """<domain type='kvm'>
      <name>troshka-aabbccdd-11223344</name>
      <vcpu placement='static'>2</vcpu>
      <memory unit='KiB'>4194304</memory>
      <currentMemory unit='KiB'>4194304</currentMemory>
      <os>
        <type>hvm</type>
        <boot dev='hd'/>
      </os>
      <devices>
        <interface type='bridge'>
          <source bridge='br-10001'/>
          <mac address='52:54:00:aa:bb:cc'/>
          <model type='virtio'/>
        </interface>
        <disk type='file' device='disk'>
          <source file='/var/lib/troshka/vms/proj/disk.qcow2'/>
          <target dev='vdb' bus='virtio'/>
          <driver name='qemu' type='qcow2'/>
        </disk>
        <graphics type='vnc' port='-1' autoport='yes' listen='127.0.0.1'>
          <listen type='address' address='127.0.0.1'/>
        </graphics>
      </devices>
    </domain>"""

    @patch("troshkad.subprocess.Popen")
    @patch("troshkad.subprocess.run")
    def test_reconfigure_vcpus_and_ram(self, mock_run, mock_popen):
        # domstate: running
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="running\n", stderr=""),  # domstate
            MagicMock(returncode=0, stdout=self.SAMPLE_XML, stderr=""),  # dumpxml
        ]
        # virsh destroy, virsh define /dev/stdin, virsh start
        mock_popen.side_effect = [
            _mock_popen(stdout="Domain destroyed"),  # destroy
            _mock_popen(stdout="Domain defined"),  # define
            _mock_popen(stdout="Domain started"),  # start
        ]

        job = troshkad._create_job(
            "vms/reconfigure",
            {
                "domain_name": "troshka-aabbccdd-11223344",
                "vcpus": 8,
                "ram_mb": 16384,
                "restart": True,
            },
        )
        result = troshkad._handle_vm_reconfigure(job, job["params"])
        self.assertEqual(result["status"], "reconfigured")
        self.assertTrue(result["restarted"])

        # Check virsh define was called with stdin pipe
        define_call = mock_popen.call_args_list[1]
        self.assertIn("define", define_call[0][0])

    @patch("troshkad.subprocess.Popen")
    @patch("troshkad.subprocess.run")
    def test_reconfigure_no_restart_when_stopped(self, mock_run, mock_popen):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="shut off\n", stderr=""),  # domstate
            MagicMock(returncode=0, stdout=self.SAMPLE_XML, stderr=""),  # dumpxml
        ]
        mock_popen.side_effect = [
            _mock_popen(stdout="Domain defined"),  # define
        ]

        job = troshkad._create_job(
            "vms/reconfigure",
            {
                "domain_name": "troshka-aabbccdd-11223344",
                "boot_devs": ["network", "hd"],
            },
        )
        result = troshkad._handle_vm_reconfigure(job, job["params"])
        self.assertEqual(result["status"], "reconfigured")
        self.assertFalse(result["restarted"])

    def test_reconfigure_rejects_invalid_domain(self):
        job = troshkad._create_job("vms/reconfigure", {"domain_name": "evil; rm -rf /"})
        with self.assertRaises(ValueError):
            troshkad._handle_vm_reconfigure(job, job["params"])


class TestVmUndefineHandler(unittest.TestCase):
    """Tests for vms/undefine handler."""

    @patch("troshkad.subprocess.run")
    @patch("troshkad.subprocess.Popen")
    def test_undefine_with_storage(self, mock_popen, mock_run):
        mock_popen.return_value = _mock_popen()
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        job = troshkad._create_job(
            "vms/undefine",
            {
                "domain_name": "troshka-aabbccdd-11223344",
                "remove_storage": True,
            },
        )
        result = troshkad._handle_vm_undefine(job, job["params"])
        self.assertEqual(result["status"], "undefined")
        # Disks are deleted manually via _delete_vm_disks, then undefine with --nvram
        calls = [c[0][0] for c in mock_popen.call_args_list]
        undefine_calls = [c for c in calls if "undefine" in c]
        self.assertGreater(len(undefine_calls), 0)
        self.assertIn("--nvram", undefine_calls[0])

    @patch("troshkad.subprocess.Popen")
    def test_undefine_without_storage(self, mock_popen):
        mock_popen.return_value = _mock_popen()
        job = troshkad._create_job(
            "vms/undefine",
            {
                "domain_name": "troshka-aabbccdd-11223344",
                "remove_storage": False,
            },
        )
        result = troshkad._handle_vm_undefine(job, job["params"])
        self.assertEqual(result["status"], "undefined")
        calls = [c[0][0] for c in mock_popen.call_args_list]
        undefine_calls = [c for c in calls if "undefine" in c]
        self.assertIn("--nvram", undefine_calls[0])
        self.assertNotIn("--remove-all-storage", undefine_calls[0])

    def test_undefine_rejects_invalid_domain(self):
        job = troshkad._create_job("vms/undefine", {"domain_name": "evil; rm -rf /"})
        with self.assertRaises(ValueError):
            troshkad._handle_vm_undefine(job, job["params"])


class TestMeshSetup(unittest.TestCase):
    """Tests for mesh/setup handler — WireGuard interface setup."""

    @patch("troshkad.os.chmod")
    @patch("builtins.open", new_callable=unittest.mock.mock_open)
    @patch("troshkad.os.makedirs")
    @patch("troshkad.subprocess.Popen")
    def test_mesh_setup_creates_wg_interface(
        self, mock_popen, mock_makedirs, mock_open_fn, mock_chmod
    ):
        mock_popen.return_value = _mock_popen()
        job = troshkad._create_job(
            "mesh/setup",
            {
                "project_id": "aabbccdd-1122-3344-5566-778899001122",
                "wg_private_key": "dGVzdC1rZXk=",  # pragma: allowlist secret
                "wg_address": "10.252.1.1/24",
                "wg_port": 51820,
                "peers": [
                    {
                        "public_key": "cGVlcl9rZXk=",
                        "endpoint": "10.0.0.2:51820",
                        "allowed_ips": "10.252.1.2/32",
                    }
                ],
            },
        )
        result = troshkad._handle_mesh_setup(job, job["params"])

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["interface"], "wg-aabbccdd")

        # Verify commands were issued
        cmds = [c[0][0] for c in mock_popen.call_args_list]
        # Should delete old interface, add new one, setconf, addr add, link set up, ping
        ip_link_add = [
            c for c in cmds if "link" in c and "add" in c and "wireguard" in c
        ]
        self.assertEqual(len(ip_link_add), 1)
        wg_setconf = [c for c in cmds if "wg" in c and "setconf" in c]
        self.assertEqual(len(wg_setconf), 1)
        addr_add = [
            c for c in cmds if "addr" in c and "add" in c and "10.252.1.1/24" in c
        ]
        self.assertEqual(len(addr_add), 1)

        # Verify conf written
        mock_open_fn.assert_called()
        mock_makedirs.assert_called_with("/var/lib/troshka/mesh", exist_ok=True)
        mock_chmod.assert_called_with(
            "/var/lib/troshka/mesh/aabbccdd-1122-3344-5566-778899001122.conf",
            0o600,
        )

    @patch("troshkad.os.chmod")
    @patch("builtins.open", new_callable=unittest.mock.mock_open)
    @patch("troshkad.os.makedirs")
    @patch("troshkad.subprocess.Popen")
    def test_mesh_setup_multiple_peers(
        self, mock_popen, mock_makedirs, mock_open_fn, mock_chmod
    ):
        mock_popen.return_value = _mock_popen()
        job = troshkad._create_job(
            "mesh/setup",
            {
                "project_id": "aabbccdd-1122-3344-5566-778899001122",
                "wg_private_key": "dGVzdC1rZXk=",  # pragma: allowlist secret
                "wg_address": "10.252.1.1/24",
                "wg_port": 51820,
                "peers": [
                    {
                        "public_key": "a2V5MQ==",
                        "endpoint": "10.0.0.2:51820",
                        "allowed_ips": "10.252.1.2/32",
                    },
                    {
                        "public_key": "a2V5Mg==",
                        "endpoint": "10.0.0.3:51820",
                        "allowed_ips": "10.252.1.3/32",
                    },
                ],
            },
        )
        result = troshkad._handle_mesh_setup(job, job["params"])
        self.assertEqual(result["status"], "ok")
        # Verify ping was called for each peer
        cmds = [c[0][0] for c in mock_popen.call_args_list]
        ping_calls = [c for c in cmds if "ping" in c]
        self.assertEqual(len(ping_calls), 2)

    @patch("troshkad.os.chmod")
    @patch("builtins.open", new_callable=unittest.mock.mock_open)
    @patch("troshkad.os.makedirs")
    @patch("troshkad.subprocess.Popen")
    def test_mesh_setup_peer_unreachable_does_not_fail(
        self, mock_popen, mock_makedirs, mock_open_fn, mock_chmod
    ):
        """Unreachable peer logs a warning but still returns ok."""
        mock_popen.return_value = _mock_popen(returncode=1)
        job = troshkad._create_job(
            "mesh/setup",
            {
                "project_id": "aabbccdd-1122-3344-5566-778899001122",
                "wg_private_key": "dGVzdC1rZXk=",  # pragma: allowlist secret
                "wg_address": "10.252.1.1/24",
                "wg_port": 51820,
                "peers": [
                    {
                        "public_key": "cGVlcl9rZXk=",
                        "endpoint": "10.0.0.2:51820",
                        "allowed_ips": "10.252.1.2/32",
                    }
                ],
            },
        )
        # _run_cmd for ping has check=False so returncode=1 is fine,
        # but other commands also use _run_cmd which checks by default.
        # We need a smarter mock.
        call_count = [0]

        def popen_side_effect(*args, **kwargs):
            call_count[0] += 1
            cmd = args[0] if args else kwargs.get("cmd", [])
            if isinstance(cmd, list) and "ping" in cmd:
                return _mock_popen(returncode=1)
            return _mock_popen(returncode=0)

        mock_popen.side_effect = popen_side_effect

        result = troshkad._handle_mesh_setup(job, job["params"])
        self.assertEqual(result["status"], "ok")


class TestMeshJoinNetwork(unittest.TestCase):
    """Tests for mesh/join-network handler — VXLAN+bridge on remote host."""

    @patch("troshkad.subprocess.Popen")
    def test_join_network_single_network(self, mock_popen):
        mock_popen.return_value = _mock_popen()
        job = troshkad._create_job(
            "mesh/join-network",
            {
                "project_id": "aabbccdd-1122-3344-5566-778899001122",
                "wg_local_ip": "10.252.1.2",
                "networks": [
                    {
                        "vni": 10001,
                        "bridge_name": "br-10001",
                        "wg_peer_ips": ["10.252.1.1", "10.252.1.2"],
                    }
                ],
            },
        )
        result = troshkad._handle_mesh_join_network(job, job["params"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["namespace"], "troshka-aabbccdd")

        cmds = [c[0][0] for c in mock_popen.call_args_list]
        # Should create namespace
        ns_add = [c for c in cmds if "netns" in c and "add" in c]
        self.assertGreater(len(ns_add), 0)
        # Should create VXLAN interface
        vxlan_add = [c for c in cmds if "vxlan" in c]
        self.assertGreater(len(vxlan_add), 0)
        # Should create bridge in namespace
        bridge_add = [c for c in cmds if "bridge" in c]
        self.assertGreater(len(bridge_add), 0)

    @patch("troshkad.subprocess.Popen")
    def test_join_network_fdb_skips_self(self, mock_popen):
        """FDB entries should NOT be added for the local IP."""
        mock_popen.return_value = _mock_popen()
        job = troshkad._create_job(
            "mesh/join-network",
            {
                "project_id": "aabbccdd-1122-3344-5566-778899001122",
                "wg_local_ip": "10.252.1.2",
                "networks": [
                    {
                        "vni": 10001,
                        "bridge_name": "br-10001",
                        "wg_peer_ips": ["10.252.1.1", "10.252.1.2", "10.252.1.3"],
                    }
                ],
            },
        )
        result = troshkad._handle_mesh_join_network(job, job["params"])
        self.assertEqual(result["status"], "ok")

        cmds = [c[0][0] for c in mock_popen.call_args_list]
        fdb_calls = [c for c in cmds if "fdb" in c and "append" in c]
        # Should have FDB entries for 10.252.1.1 and 10.252.1.3, but NOT for 10.252.1.2 (self)
        fdb_dsts = [c[-1] for c in fdb_calls]
        self.assertNotIn("10.252.1.2", fdb_dsts)
        self.assertIn("10.252.1.1", fdb_dsts)
        self.assertIn("10.252.1.3", fdb_dsts)

    @patch("troshkad.subprocess.Popen")
    def test_join_network_multiple_networks(self, mock_popen):
        mock_popen.return_value = _mock_popen()
        job = troshkad._create_job(
            "mesh/join-network",
            {
                "project_id": "aabbccdd-1122-3344-5566-778899001122",
                "wg_local_ip": "10.252.1.2",
                "networks": [
                    {
                        "vni": 10001,
                        "bridge_name": "br-10001",
                        "wg_peer_ips": ["10.252.1.1"],
                    },
                    {
                        "vni": 10002,
                        "bridge_name": "br-10002",
                        "wg_peer_ips": ["10.252.1.1"],
                    },
                ],
            },
        )
        result = troshkad._handle_mesh_join_network(job, job["params"])
        self.assertEqual(result["status"], "ok")

        cmds = [c[0][0] for c in mock_popen.call_args_list]
        vxlan_adds = [c for c in cmds if "vxlan" in c and "add" in c]
        self.assertEqual(len(vxlan_adds), 2)


class TestMeshTeardown(unittest.TestCase):
    """Tests for DELETE /mesh/teardown route handler."""

    @patch("troshkad.os.path.exists", return_value=True)
    @patch("troshkad.os.remove")
    @patch("troshkad.subprocess.run")
    def test_teardown_removes_interface_and_config(
        self, mock_run, mock_remove, mock_exists
    ):
        mock_run.return_value = MagicMock(returncode=0)
        handler = MagicMock()
        handler.path = "/mesh/teardown?project_id=aabbccdd-1122-3344-5566-778899001122"

        troshkad.handle_mesh_teardown(
            handler, {"project_id": "aabbccdd-1122-3344-5566-778899001122"}
        )

        # Should call ip link del
        mock_run.assert_called()
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd, ["ip", "link", "del", "wg-aabbccdd"])
        # Should remove conf file
        mock_remove.assert_called_with(
            "/var/lib/troshka/mesh/aabbccdd-1122-3344-5566-778899001122.conf"
        )
        handler._send_json.assert_called_with(200, {"status": "ok"})

    @patch("troshkad.os.path.exists", return_value=False)
    @patch("troshkad.subprocess.run")
    def test_teardown_skips_missing_conf(self, mock_run, mock_exists):
        handler = MagicMock()
        handler.path = "/mesh/teardown"
        troshkad.handle_mesh_teardown(
            handler, {"project_id": "aabbccdd-1122-3344-5566-778899001122"}
        )
        handler._send_json.assert_called_with(200, {"status": "ok"})

    def test_teardown_rejects_missing_project_id(self):
        handler = MagicMock()
        handler.path = "/mesh/teardown"
        troshkad.handle_mesh_teardown(handler, {})
        handler._send_json.assert_called_with(400, {"error": "project_id required"})


class TestMeshStatus(unittest.TestCase):
    """Tests for GET /mesh/status route handler."""

    @patch("troshkad.os.path.isdir", return_value=False)
    def test_status_no_mesh_dir(self, mock_isdir):
        handler = MagicMock()
        troshkad.handle_mesh_status(handler, {})
        handler._send_json.assert_called_with(200, {"projects": {}})

    @patch("troshkad.subprocess.check_output")
    @patch("troshkad.os.listdir")
    @patch("troshkad.os.path.isdir", return_value=True)
    def test_status_returns_peer_info(
        self, mock_isdir, mock_listdir, mock_check_output
    ):
        mock_listdir.return_value = ["aabbccdd-1122-3344-5566-778899001122.conf"]
        mock_check_output.return_value = "cGVlcl9rZXk=\t1723456789\n"
        handler = MagicMock()
        troshkad.handle_mesh_status(handler, {})

        call_args = handler._send_json.call_args[0]
        self.assertEqual(call_args[0], 200)
        projects = call_args[1]["projects"]
        pid = "aabbccdd-1122-3344-5566-778899001122"
        self.assertIn(pid, projects)
        self.assertEqual(projects[pid]["interface"], "wg-aabbccdd")
        self.assertIn("peers", projects[pid])
        self.assertEqual(projects[pid]["peers"]["cGVlcl9rZXk="], 1723456789)

    @patch(
        "troshkad.subprocess.check_output",
        side_effect=Exception("wg: interface not found"),
    )
    @patch("troshkad.os.listdir")
    @patch("troshkad.os.path.isdir", return_value=True)
    def test_status_reports_error_for_down_interface(
        self, mock_isdir, mock_listdir, mock_check_output
    ):
        mock_listdir.return_value = ["deadbeef-1111-2222-3333-444455556666.conf"]
        handler = MagicMock()
        troshkad.handle_mesh_status(handler, {})

        call_args = handler._send_json.call_args[0]
        projects = call_args[1]["projects"]
        pid = "deadbeef-1111-2222-3333-444455556666"
        self.assertIn(pid, projects)
        self.assertEqual(projects[pid]["error"], "not running")

    @patch("troshkad.os.listdir")
    @patch("troshkad.os.path.isdir", return_value=True)
    def test_status_ignores_non_conf_files(self, mock_isdir, mock_listdir):
        mock_listdir.return_value = ["readme.txt", "backup.bak"]
        handler = MagicMock()
        troshkad.handle_mesh_status(handler, {})
        handler._send_json.assert_called_with(200, {"projects": {}})


class TestContainerPull(unittest.TestCase):
    """Tests for containers/pull handler."""

    @patch("troshkad.subprocess.Popen")
    def test_pull_without_credentials(self, mock_popen):
        mock_popen.return_value = _mock_popen()
        job = troshkad._create_job(
            "containers/pull",
            {
                "image": "quay.io/test/image:latest",
            },
        )
        result = troshkad._handle_container_pull(job, job["params"])
        self.assertEqual(result["status"], "pulled")
        self.assertEqual(result["image"], "quay.io/test/image:latest")
        # Should only call podman pull (no login)
        cmds = [c[0][0] for c in mock_popen.call_args_list]
        self.assertEqual(len(cmds), 1)
        self.assertIn("podman", cmds[0])
        self.assertIn("pull", cmds[0])

    @patch("troshkad.subprocess.Popen")
    def test_pull_with_credentials(self, mock_popen):
        mock_popen.return_value = _mock_popen()
        job = troshkad._create_job(
            "containers/pull",
            {
                "image": "registry.example.com/app:v1",
                "registry": "registry.example.com",
                "username": "user",
                "password": "test-pass",  # pragma: allowlist secret
            },
        )
        result = troshkad._handle_container_pull(job, job["params"])
        self.assertEqual(result["status"], "pulled")
        # Should call login first, then pull
        cmds = [c[0][0] for c in mock_popen.call_args_list]
        self.assertEqual(len(cmds), 2)
        self.assertIn("login", cmds[0])
        self.assertIn("pull", cmds[1])


class TestContainerStart(unittest.TestCase):
    """Tests for containers/start handler."""

    @patch("troshkad.subprocess.Popen")
    def test_start_container(self, mock_popen):
        mock_popen.return_value = _mock_popen()
        job = troshkad._create_job(
            "containers/start",
            {
                "container_name": "troshka-aabbccdd-mycontainer",
            },
        )
        result = troshkad._handle_container_start(job, job["params"])
        self.assertEqual(result["status"], "started")
        self.assertEqual(result["container_name"], "troshka-aabbccdd-mycontainer")
        cmd = mock_popen.call_args[0][0]
        self.assertEqual(cmd, ["podman", "start", "troshka-aabbccdd-mycontainer"])


class TestContainerStop(unittest.TestCase):
    """Tests for containers/stop handler."""

    @patch("troshkad.subprocess.Popen")
    def test_stop_container_default_timeout(self, mock_popen):
        mock_popen.return_value = _mock_popen()
        job = troshkad._create_job(
            "containers/stop",
            {
                "container_name": "troshka-aabbccdd-mycontainer",
            },
        )
        result = troshkad._handle_container_stop(job, job["params"])
        self.assertEqual(result["status"], "stopped")
        cmd = mock_popen.call_args[0][0]
        self.assertIn("-t", cmd)
        self.assertIn("10", cmd)  # default timeout

    @patch("troshkad.subprocess.Popen")
    def test_stop_container_custom_timeout(self, mock_popen):
        mock_popen.return_value = _mock_popen()
        job = troshkad._create_job(
            "containers/stop",
            {
                "container_name": "troshka-aabbccdd-mycontainer",
                "timeout": 30,
            },
        )
        result = troshkad._handle_container_stop(job, job["params"])
        self.assertEqual(result["status"], "stopped")
        cmd = mock_popen.call_args[0][0]
        self.assertIn("30", cmd)


class TestContainerDestroy(unittest.TestCase):
    """Tests for containers/destroy handler."""

    @patch("troshkad.subprocess.Popen")
    def test_destroy_container_no_volumes(self, mock_popen):
        mock_popen.return_value = _mock_popen()
        job = troshkad._create_job(
            "containers/destroy",
            {
                "container_name": "troshka-aabbccdd-mycontainer",
            },
        )
        result = troshkad._handle_container_destroy(job, job["params"])
        self.assertEqual(result["status"], "destroyed")
        cmds = [c[0][0] for c in mock_popen.call_args_list]
        # Should stop then rm -f
        stop_calls = [c for c in cmds if "stop" in c]
        rm_calls = [c for c in cmds if "rm" in c]
        self.assertGreater(len(stop_calls), 0)
        self.assertGreater(len(rm_calls), 0)

    @patch("troshkad.os.path.ismount")
    @patch("troshkad.subprocess.Popen")
    def test_destroy_container_unmounts_volumes(self, mock_popen, mock_ismount):
        mock_popen.return_value = _mock_popen()
        mock_ismount.return_value = True
        job = troshkad._create_job(
            "containers/destroy",
            {
                "container_name": "troshka-aabbccdd-mycontainer",
                "volumes": [{"mount_dir": "/var/lib/troshka/vms/proj/vol1"}],
            },
        )
        result = troshkad._handle_container_destroy(job, job["params"])
        self.assertEqual(result["status"], "destroyed")
        cmds = [c[0][0] for c in mock_popen.call_args_list]
        umount_calls = [c for c in cmds if "umount" in c]
        self.assertGreater(len(umount_calls), 0)

    @patch("troshkad.os.path.ismount")
    @patch("troshkad.subprocess.Popen")
    def test_destroy_skips_unmounted_volumes(self, mock_popen, mock_ismount):
        mock_popen.return_value = _mock_popen()
        mock_ismount.return_value = False
        job = troshkad._create_job(
            "containers/destroy",
            {
                "container_name": "troshka-aabbccdd-mycontainer",
                "volumes": [{"mount_dir": "/var/lib/troshka/vms/proj/vol1"}],
            },
        )
        result = troshkad._handle_container_destroy(job, job["params"])
        self.assertEqual(result["status"], "destroyed")
        cmds = [c[0][0] for c in mock_popen.call_args_list]
        umount_calls = [c for c in cmds if "umount" in c]
        self.assertEqual(len(umount_calls), 0)


class TestContainerLogs(unittest.TestCase):
    """Tests for containers/logs handler."""

    @patch("troshkad.subprocess.run")
    def test_logs_default_tail(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="line1\nline2\nline3\n", stderr=""
        )
        job = troshkad._create_job(
            "containers/logs",
            {
                "container_name": "troshka-aabbccdd-mycontainer",
            },
        )
        result = troshkad._handle_container_logs(job, job["params"])
        self.assertEqual(result["container_name"], "troshka-aabbccdd-mycontainer")
        self.assertIn("line1", result["logs"])
        cmd = mock_run.call_args[0][0]
        self.assertIn("500", cmd)  # default tail

    @patch("troshkad.subprocess.run")
    def test_logs_custom_tail(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="log output\n", stderr=""
        )
        job = troshkad._create_job(
            "containers/logs",
            {
                "container_name": "troshka-aabbccdd-mycontainer",
                "tail": 100,
            },
        )
        _result = troshkad._handle_container_logs(job, job["params"])
        cmd = mock_run.call_args[0][0]
        self.assertIn("100", cmd)

    @patch("troshkad.subprocess.run")
    def test_logs_failure_raises(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="Error: no such container"
        )
        job = troshkad._create_job(
            "containers/logs",
            {
                "container_name": "troshka-aabbccdd-nonexistent",
            },
        )
        with self.assertRaises(RuntimeError):
            troshkad._handle_container_logs(job, job["params"])


class TestContainerExec(unittest.TestCase):
    """Tests for containers/exec handler."""

    @patch("troshkad.subprocess.run")
    def test_exec_default_command(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="output", stderr="")
        job = troshkad._create_job(
            "containers/exec",
            {
                "container_name": "troshka-aabbccdd-mycontainer",
            },
        )
        result = troshkad._handle_container_exec(job, job["params"])
        self.assertEqual(result["stdout"], "output")
        cmd = mock_run.call_args[0][0]
        self.assertEqual(
            cmd, ["podman", "exec", "troshka-aabbccdd-mycontainer", "/bin/sh"]
        )

    @patch("troshkad.subprocess.run")
    def test_exec_custom_command(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="hello", stderr="")
        job = troshkad._create_job(
            "containers/exec",
            {
                "container_name": "troshka-aabbccdd-mycontainer",
                "command": ["echo", "hello"],
            },
        )
        result = troshkad._handle_container_exec(job, job["params"])
        self.assertEqual(result["stdout"], "hello")
        cmd = mock_run.call_args[0][0]
        self.assertEqual(
            cmd, ["podman", "exec", "troshka-aabbccdd-mycontainer", "echo", "hello"]
        )

    @patch("troshkad.subprocess.run")
    def test_exec_failure_raises(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="exec failed")
        job = troshkad._create_job(
            "containers/exec",
            {
                "container_name": "troshka-aabbccdd-mycontainer",
            },
        )
        with self.assertRaises(RuntimeError):
            troshkad._handle_container_exec(job, job["params"])


class TestContainerSaveImage(unittest.TestCase):
    """Tests for containers/save-image handler."""

    @patch("troshkad.os.path.getsize", return_value=10485760)
    @patch("troshkad.os.makedirs")
    @patch("troshkad.subprocess.Popen")
    def test_save_image(self, mock_popen, mock_makedirs, mock_getsize):
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate.return_value = ("", "")
        mock_popen.return_value = proc
        job = troshkad._create_job(
            "containers/save-image",
            {
                "image": "quay.io/test/image:latest",
                "output_path": "/var/lib/troshka/cache/image.tar.gz",
            },
        )
        result = troshkad._handle_container_save_image(job, job["params"])
        self.assertEqual(result["size_bytes"], 10485760)
        self.assertEqual(result["output_path"], "/var/lib/troshka/cache/image.tar.gz")

    @patch("troshkad.os.makedirs")
    @patch("troshkad.subprocess.Popen")
    def test_save_image_failure_raises(self, mock_popen, mock_makedirs):
        proc = MagicMock()
        proc.returncode = 1
        proc.communicate.return_value = ("", "save failed")
        mock_popen.return_value = proc
        job = troshkad._create_job(
            "containers/save-image",
            {
                "image": "quay.io/test/image:latest",
                "output_path": "/var/lib/troshka/cache/image.tar.gz",
            },
        )
        with self.assertRaises(RuntimeError):
            troshkad._handle_container_save_image(job, job["params"])

    @patch("troshkad.os.makedirs")
    @patch("troshkad.subprocess.Popen")
    def test_save_image_timeout_raises(self, mock_popen, mock_makedirs):
        proc = MagicMock()
        proc.kill = MagicMock()
        # First call raises TimeoutExpired, second call (after kill) succeeds
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd="podman save", timeout=600),
            ("", ""),
        ]
        mock_popen.return_value = proc
        job = troshkad._create_job(
            "containers/save-image",
            {
                "image": "quay.io/test/image:latest",
                "output_path": "/var/lib/troshka/cache/image.tar.gz",
            },
        )
        with self.assertRaises(RuntimeError) as ctx:
            troshkad._handle_container_save_image(job, job["params"])
        self.assertIn("timed out", str(ctx.exception))


class TestContainerLoadImage(unittest.TestCase):
    """Tests for containers/load-image handler."""

    @patch("troshkad.os.path.isfile", return_value=True)
    @patch("troshkad.subprocess.Popen")
    def test_load_image(self, mock_popen, mock_isfile):
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate.return_value = ("Loaded image: quay.io/test:latest", "")
        mock_popen.return_value = proc
        job = troshkad._create_job(
            "containers/load-image",
            {
                "input_path": "/var/lib/troshka/cache/image.tar.gz",
            },
        )
        result = troshkad._handle_container_load_image(job, job["params"])
        self.assertEqual(result["status"], "loaded")

    @patch("troshkad.os.path.isfile", return_value=False)
    def test_load_image_not_found(self, mock_isfile):
        job = troshkad._create_job(
            "containers/load-image",
            {
                "input_path": "/var/lib/troshka/cache/nonexistent.tar.gz",
            },
        )
        with self.assertRaises(FileNotFoundError):
            troshkad._handle_container_load_image(job, job["params"])

    @patch("troshkad.os.path.isfile", return_value=True)
    @patch("troshkad.subprocess.Popen")
    def test_load_image_failure_raises(self, mock_popen, mock_isfile):
        proc = MagicMock()
        proc.returncode = 1
        proc.communicate.return_value = ("", "load failed")
        mock_popen.return_value = proc
        job = troshkad._create_job(
            "containers/load-image",
            {
                "input_path": "/var/lib/troshka/cache/image.tar.gz",
            },
        )
        with self.assertRaises(RuntimeError):
            troshkad._handle_container_load_image(job, job["params"])


class TestPodCreate(unittest.TestCase):
    """Tests for pods/create handler."""

    @patch("troshkad._run_cmd")
    def test_pod_create_simple(self, mock_run_cmd):
        """Create a pod with no networks, init containers, or main containers."""

        # _run_cmd for inspect returns a proc-like object whose .strip() works
        def run_cmd_side_effect(job, cmd, **kwargs):
            if "inspect" in cmd:
                return "12345"
            return ""

        mock_run_cmd.side_effect = run_cmd_side_effect

        job = troshkad._create_job(
            "pods/create",
            {
                "pod_name": "mypod",
                "project_id": "aabbccdd-1122-3344-5566-778899001122",
                "containers": [],
            },
        )
        result = troshkad._handle_pod_create(job, job["params"])
        self.assertEqual(result["status"], "created")
        self.assertEqual(result["pod_name"], "troshka-aabbccdd-mypod")

    @patch("troshkad._run_cmd")
    def test_pod_create_with_containers(self, mock_run_cmd):
        """Create a pod with init and main containers."""

        def run_cmd_side_effect(job, cmd, **kwargs):
            if "inspect" in cmd:
                return "12345"
            return ""

        mock_run_cmd.side_effect = run_cmd_side_effect

        job = troshkad._create_job(
            "pods/create",
            {
                "pod_name": "mypod",
                "project_id": "aabbccdd-1122-3344-5566-778899001122",
                "init_containers": [
                    {
                        "name": "init1",
                        "image": "busybox:latest",
                        "command": "/bin/sh -c 'echo init'",
                    },
                ],
                "containers": [
                    {"name": "app", "image": "nginx:latest", "cpus": 2, "memory": 1024},
                ],
            },
        )
        result = troshkad._handle_pod_create(job, job["params"])
        self.assertEqual(result["status"], "created")

        cmds = [c[0][1] for c in mock_run_cmd.call_args_list]
        # Should have created init container and main container
        init_create = [c for c in cmds if "create" in c and "init-init1" in " ".join(c)]
        main_create = [c for c in cmds if "create" in c and "-app" in " ".join(c)]
        self.assertGreater(
            len(init_create), 0, f"No init container created in cmds: {cmds}"
        )
        self.assertGreater(
            len(main_create), 0, f"No main container created in cmds: {cmds}"
        )

    @patch("troshkad._mount_container_volumes")
    @patch("troshkad._run_cmd")
    def test_pod_create_mounts_volumes(self, mock_run_cmd, mock_mount):
        def run_cmd_side_effect(job, cmd, **kwargs):
            if "inspect" in cmd:
                return "12345"
            return ""

        mock_run_cmd.side_effect = run_cmd_side_effect

        volumes = [
            {
                "disk_path": "/var/lib/troshka/vms/proj/disk.raw",
                "mount_dir": "/var/lib/troshka/vms/proj/mnt-disk",
                "mount_path": "/showroom",
            }
        ]
        job = troshkad._create_job(
            "pods/create",
            {
                "pod_name": "showroom",
                "project_id": "aabbccdd-1122-3344-5566-778899001122",
                "volumes": volumes,
                "containers": [],
            },
        )
        troshkad._handle_pod_create(job, job["params"])
        mock_mount.assert_called_once_with(job, volumes)

    @patch("troshkad._mount_container_volumes")
    @patch("troshkad._run_cmd")
    def test_pod_create_infers_volumes_from_mounts(self, mock_run_cmd, mock_mount):
        def run_cmd_side_effect(job, cmd, **kwargs):
            if "inspect" in cmd:
                return "12345"
            return ""

        mock_run_cmd.side_effect = run_cmd_side_effect

        with tempfile.TemporaryDirectory() as tmp:
            disk_path = os.path.join(tmp, "2db53adf-4d48b930.raw")
            mount_dir = os.path.join(tmp, "mnt-4d48b930")
            open(disk_path, "wb").close()

            job = troshkad._create_job(
                "pods/create",
                {
                    "pod_name": "showroom",
                    "project_id": "ce198e19-243c-4afd-865f-17f3f2b9504f",
                    "init_containers": [
                        {
                            "name": "git-cloner",
                            "image": "quay.io/rhpds/git-cloner:v1.1.4",
                            "mounts": [f"{mount_dir}:/showroom"],
                        }
                    ],
                    "containers": [],
                },
            )
            troshkad._handle_pod_create(job, job["params"])

        expected = [
            {
                "disk_path": disk_path,
                "mount_dir": mount_dir,
                "mount_path": "/showroom",
            }
        ]
        mock_mount.assert_called_once_with(job, expected)

    @patch("troshkad._mount_container_volumes")
    @patch("troshkad._run_cmd")
    def test_pod_create_main_container_argv_command(self, mock_run_cmd, mock_mount):
        def run_cmd_side_effect(job, cmd, **kwargs):
            if "inspect" in cmd:
                return "12345"
            return ""

        mock_run_cmd.side_effect = run_cmd_side_effect

        job = troshkad._create_job(
            "pods/create",
            {
                "pod_name": "showroom",
                "project_id": "aabbccdd-1122-3344-5566-778899001122",
                "containers": [
                    {
                        "name": "proxy",
                        "image": "quay.io/rhpds/nginx:1.25",
                        "cpus": 1,
                        "memory": 256,
                        "command": [
                            "nginx",
                            "-c",
                            "/showroom/nginx/nginx.conf",
                            "-g",
                            "daemon off;",
                        ],
                    }
                ],
            },
        )
        troshkad._handle_pod_create(job, job["params"])

        create_cmds = [
            c[0][1] for c in mock_run_cmd.call_args_list if c[0][1][1] == "create"
        ]
        proxy_cmds = [c for c in create_cmds if "troshka-aabbccdd-showroom-proxy" in c]
        self.assertEqual(len(proxy_cmds), 1)
        self.assertIn("--entrypoint", proxy_cmds[0])
        self.assertIn("nginx", proxy_cmds[0])
        self.assertIn("-c", proxy_cmds[0])


class TestPodResolvConf(unittest.TestCase):
    def test_gateway_from_ip(self):
        self.assertEqual(troshkad._gateway_from_ip("10.0.0.5"), "10.0.0.1")

    def test_pod_dns_nameserver_uses_configured_gateway(self):
        net = {"ip": "10.0.0.5", "gateway": "10.0.0.254"}
        self.assertEqual(troshkad._pod_dns_nameserver(net), "10.0.0.254")

    def test_pod_dns_nameserver_falls_back_to_subnet_dot_one(self):
        net = {"ip": "172.20.20.5"}
        self.assertEqual(troshkad._pod_dns_nameserver(net), "172.20.20.1")

    def test_append_pod_resolv_mount(self):
        cmd = ["podman", "create"]
        troshkad._append_pod_resolv_mount(cmd, "/tmp/troshka-resolv-test.conf")
        self.assertEqual(
            cmd,
            [
                "podman",
                "create",
                "-v",
                "/tmp/troshka-resolv-test.conf:/etc/resolv.conf:ro,z",
            ],
        )


class TestAppendPodmanImageCommand(unittest.TestCase):
    """Tests for podman argv command handling."""

    def test_argv_entrypoint(self):
        cmd = ["podman", "create"]
        troshkad._append_podman_image_command(
            cmd,
            "quay.io/rhpds/nginx:1.25",
            ["nginx", "-c", "/showroom/nginx/nginx.conf", "-g", "daemon off;"],
        )
        self.assertEqual(
            cmd,
            [
                "podman",
                "create",
                "--entrypoint",
                "nginx",
                "quay.io/rhpds/nginx:1.25",
                "-c",
                "/showroom/nginx/nginx.conf",
                "-g",
                "daemon off;",
            ],
        )

    def test_argv_flags_only(self):
        cmd = ["podman", "create"]
        troshkad._append_podman_image_command(
            cmd,
            "quay.io/rhpds/wetty:latest",
            ["--base=/wetty_aap/", "--port=8001"],
        )
        self.assertEqual(
            cmd,
            [
                "podman",
                "create",
                "quay.io/rhpds/wetty:latest",
                "--base=/wetty_aap/",
                "--port=8001",
            ],
        )

    def test_shell_entrypoint_string(self):
        cmd = ["podman", "create"]
        troshkad._append_podman_image_command(
            cmd, "busybox:1.36", "/bin/sh -ec 'echo hi'"
        )
        self.assertEqual(
            cmd,
            [
                "podman",
                "create",
                "--entrypoint",
                "/bin/sh",
                "busybox:1.36",
                "-ec",
                "echo hi",
            ],
        )

    def test_shell_script_string(self):
        cmd = ["podman", "create"]
        script = (
            'mkdir -p /showroom/nginx && echo "$NGINX_B64" | base64 -d '
            "> /showroom/nginx/nginx.conf"
        )
        troshkad._append_podman_image_command(cmd, "busybox:1.36", script)
        self.assertEqual(
            cmd,
            [
                "podman",
                "create",
                "--entrypoint",
                "/bin/sh",
                "busybox:1.36",
                "-c",
                script,
            ],
        )


class TestPodStart(unittest.TestCase):
    """Tests for pods/start handler."""

    @patch("troshkad._run_cmd")
    def test_pod_start_no_init_containers(self, mock_run_cmd):
        def run_cmd_side_effect(job, cmd, **kwargs):
            if "ps" in cmd:
                return ""  # no init containers
            return ""

        mock_run_cmd.side_effect = run_cmd_side_effect

        job = troshkad._create_job(
            "pods/start",
            {
                "pod_name": "troshka-aabbccdd-mypod",
            },
        )
        result = troshkad._handle_pod_start(job, job["params"])
        self.assertEqual(result["status"], "started")

    @patch("troshkad._run_cmd")
    def test_pod_start_with_init_containers(self, mock_run_cmd):
        def run_cmd_side_effect(job, cmd, **kwargs):
            if "ps" in cmd:
                return "troshka-aabbccdd-mypod-init-setup\n"
            elif "wait" in cmd:
                return "0"
            return ""

        mock_run_cmd.side_effect = run_cmd_side_effect

        job = troshkad._create_job(
            "pods/start",
            {
                "pod_name": "troshka-aabbccdd-mypod",
            },
        )
        result = troshkad._handle_pod_start(job, job["params"])
        self.assertEqual(result["status"], "started")

    @patch("troshkad._run_cmd")
    def test_pod_start_init_container_order(self, mock_run_cmd):
        started = []

        def run_cmd_side_effect(job, cmd, **kwargs):
            if "ps" in cmd:
                return (
                    "troshka-aabbccdd-showroom-init-git-cloner\n"
                    "troshka-aabbccdd-showroom-init-nginx-config\n"
                    "troshka-aabbccdd-showroom-init-antora-builder\n"
                )
            if "start" in cmd and "init-" in " ".join(cmd):
                started.append(cmd[-1])
            if "wait" in cmd:
                return "0"
            return ""

        mock_run_cmd.side_effect = run_cmd_side_effect

        job = troshkad._create_job(
            "pods/start",
            {"pod_name": "troshka-aabbccdd-showroom"},
        )
        troshkad._handle_pod_start(job, job["params"])
        self.assertEqual(
            started,
            [
                "troshka-aabbccdd-showroom-init-git-cloner",
                "troshka-aabbccdd-showroom-init-nginx-config",
                "troshka-aabbccdd-showroom-init-antora-builder",
            ],
        )

    @patch("troshkad._run_cmd")
    def test_pod_start_starts_main_containers_only(self, mock_run_cmd):
        started = []

        def run_cmd_side_effect(job, cmd, **kwargs):
            if cmd[:3] == ["podman", "ps", "-a"] and "init-" in " ".join(cmd):
                return "troshka-aabbccdd-showroom-init-git-cloner\n"
            if cmd[:3] == ["podman", "ps", "-a"] and "pod=" in " ".join(cmd):
                return (
                    "troshka-aabbccdd-showroom-infra\n"
                    "troshka-aabbccdd-showroom-init-git-cloner\n"
                    "troshka-aabbccdd-showroom-proxy\n"
                )
            if cmd[:2] == ["podman", "start"]:
                started.append(cmd[-1])
            if "wait" in cmd:
                return "0"
            return ""

        mock_run_cmd.side_effect = run_cmd_side_effect

        job = troshkad._create_job(
            "pods/start",
            {"pod_name": "troshka-aabbccdd-showroom"},
        )
        troshkad._handle_pod_start(job, job["params"])
        self.assertEqual(
            started,
            [
                "troshka-aabbccdd-showroom-init-git-cloner",
                "troshka-aabbccdd-showroom-proxy",
            ],
        )
        pod_start_cmds = [
            c[0][1]
            for c in mock_run_cmd.call_args_list
            if c[0][1][:3] == ["podman", "pod", "start"]
        ]
        self.assertEqual(pod_start_cmds, [])

    @patch("troshkad._run_cmd")
    def test_pod_start_init_container_failure(self, mock_run_cmd):
        def run_cmd_side_effect(job, cmd, **kwargs):
            if "ps" in cmd:
                return "troshka-aabbccdd-mypod-init-setup\n"
            elif "wait" in cmd:
                return "1"
            elif "logs" in cmd:
                return "init failed: missing config"
            return ""

        mock_run_cmd.side_effect = run_cmd_side_effect

        job = troshkad._create_job(
            "pods/start",
            {
                "pod_name": "troshka-aabbccdd-mypod",
            },
        )
        with self.assertRaises(RuntimeError) as ctx:
            troshkad._handle_pod_start(job, job["params"])
        self.assertIn("exit code 1", str(ctx.exception))


class TestPodDestroy(unittest.TestCase):
    """Tests for pods/destroy handler."""

    @patch("troshkad.os.path.exists", return_value=True)
    @patch("troshkad.os.unlink")
    @patch("troshkad.subprocess.Popen")
    def test_pod_destroy_cleans_up(self, mock_popen, mock_unlink, mock_exists):
        mock_popen.return_value = _mock_popen()
        job = troshkad._create_job(
            "pods/destroy",
            {
                "pod_name": "troshka-aabbccdd-mypod",
            },
        )
        result = troshkad._handle_pod_destroy(job, job["params"])
        self.assertEqual(result["status"], "destroyed")
        # Should remove netns symlink
        mock_unlink.assert_called_with("/var/run/netns/ctr-dd-mypod")
        # Should call podman pod rm -f
        cmds = [c[0][0] for c in mock_popen.call_args_list]
        pod_rm = [c for c in cmds if "pod" in c and "rm" in c]
        self.assertGreater(len(pod_rm), 0)

    @patch("troshkad.os.path.ismount", return_value=True)
    @patch("troshkad.os.path.exists", return_value=False)
    @patch("troshkad.subprocess.Popen")
    def test_pod_destroy_unmounts_volumes(self, mock_popen, mock_exists, mock_ismount):
        mock_popen.return_value = _mock_popen()
        job = troshkad._create_job(
            "pods/destroy",
            {
                "pod_name": "troshka-aabbccdd-mypod",
                "volumes": [{"mount_dir": "/var/lib/troshka/vms/proj/vol"}],
            },
        )
        result = troshkad._handle_pod_destroy(job, job["params"])
        self.assertEqual(result["status"], "destroyed")
        cmds = [c[0][0] for c in mock_popen.call_args_list]
        umount_calls = [c for c in cmds if "umount" in c]
        self.assertGreater(len(umount_calls), 0)

    @patch("troshkad.os.path.exists", return_value=False)
    @patch("troshkad.subprocess.Popen")
    def test_pod_destroy_no_netns_symlink(self, mock_popen, mock_exists):
        """Should not fail if netns symlink doesn't exist."""
        mock_popen.return_value = _mock_popen()
        job = troshkad._create_job(
            "pods/destroy",
            {
                "pod_name": "troshka-aabbccdd-mypod",
            },
        )
        result = troshkad._handle_pod_destroy(job, job["params"])
        self.assertEqual(result["status"], "destroyed")


class TestNfsHealthRecovery(unittest.TestCase):
    """Tests for NFS health check with recovery path."""

    def setUp(self):
        self._orig_config = troshkad._config.copy()
        self._orig_healthy = troshkad._nfs_healthy
        self._orig_stale_since = troshkad._nfs_stale_since

    def tearDown(self):
        troshkad._config.update(self._orig_config)
        troshkad._nfs_healthy = self._orig_healthy
        troshkad._nfs_stale_since = self._orig_stale_since

    def test_local_mode_always_healthy(self):
        troshkad._config["storage_mode"] = "local"
        result = troshkad._check_nfs_health()
        self.assertTrue(result)
        self.assertTrue(troshkad._nfs_healthy)

    @patch("troshkad.os.path.ismount", return_value=False)
    def test_shared_mode_not_mounted_unhealthy(self, mock_ismount):
        troshkad._config["storage_mode"] = "shared"
        troshkad._nfs_healthy = True
        result = troshkad._check_nfs_health()
        self.assertFalse(result)
        self.assertFalse(troshkad._nfs_healthy)
        self.assertGreater(troshkad._nfs_stale_since, 0)

    @patch("troshkad.os.statvfs")
    @patch("troshkad.os.path.ismount", return_value=True)
    def test_shared_mode_healthy_probe_passes(self, mock_ismount, mock_statvfs):
        troshkad._config["storage_mode"] = "shared"
        troshkad._nfs_healthy = False  # was unhealthy, should recover
        result = troshkad._check_nfs_health()
        self.assertTrue(result)
        self.assertTrue(troshkad._nfs_healthy)
        self.assertEqual(troshkad._nfs_stale_since, 0.0)

    def test_default_mode_treated_as_local(self):
        troshkad._config.pop("storage_mode", None)
        result = troshkad._check_nfs_health()
        self.assertTrue(result)


class TestNfsRecovery(unittest.TestCase):
    """Tests for _try_nfs_recovery."""

    def setUp(self):
        self._orig_config = troshkad._config.copy()

    def tearDown(self):
        troshkad._config.update(self._orig_config)

    @patch("builtins.open", side_effect=OSError("no fstab"))
    def test_recovery_fails_no_fstab(self, mock_open_fn):
        troshkad._config["shared_mount"] = "/var/lib/troshka/shared"
        result = troshkad._try_nfs_recovery()
        self.assertFalse(result)

    @patch(
        "builtins.open",
        new_callable=unittest.mock.mock_open,
        read_data="# empty fstab\n",
    )
    def test_recovery_fails_no_entry(self, mock_open_fn):
        troshkad._config["shared_mount"] = "/var/lib/troshka/shared"
        result = troshkad._try_nfs_recovery()
        self.assertFalse(result)

    @patch("troshkad.subprocess.run")
    @patch(
        "builtins.open",
        new_callable=unittest.mock.mock_open,
        read_data="nfs-server:/export /var/lib/troshka/shared nfs soft,timeo=50 0 0\n",
    )
    def test_recovery_succeeds(self, mock_open_fn, mock_run):
        troshkad._config["shared_mount"] = "/var/lib/troshka/shared"
        mock_run.side_effect = [
            MagicMock(returncode=0),  # umount -l
            MagicMock(returncode=0, stderr=""),  # mount
        ]
        result = troshkad._try_nfs_recovery()
        self.assertTrue(result)

    @patch("troshkad.subprocess.run")
    @patch(
        "builtins.open",
        new_callable=unittest.mock.mock_open,
        read_data="nfs-server:/export /var/lib/troshka/shared nfs soft,timeo=50 0 0\n",
    )
    def test_recovery_fails_mount_error(self, mock_open_fn, mock_run):
        troshkad._config["shared_mount"] = "/var/lib/troshka/shared"
        mock_run.side_effect = [
            MagicMock(returncode=0),  # umount -l
            MagicMock(returncode=1, stderr="mount: mount failed"),  # mount
        ]
        result = troshkad._try_nfs_recovery()
        self.assertFalse(result)


class TestRateLimitingEdgeCases(unittest.TestCase):
    """Additional rate-limiting tests for edge cases not covered by helpers."""

    def setUp(self):
        troshkad._fail_tracker.clear()
        troshkad._banned_ips.clear()
        troshkad._permabanned_ips.clear()
        troshkad._ban_history.clear()

    def tearDown(self):
        troshkad._fail_tracker.clear()
        troshkad._banned_ips.clear()
        troshkad._permabanned_ips.clear()
        troshkad._ban_history.clear()

    def test_failures_from_different_ips_are_independent(self):
        for _ in range(troshkad._BAN_THRESHOLD - 1):
            troshkad._record_auth_failure("10.0.0.1")
            troshkad._record_auth_failure("10.0.0.2")
        self.assertFalse(troshkad._is_banned("10.0.0.1"))
        self.assertFalse(troshkad._is_banned("10.0.0.2"))

    def test_temp_ban_cleared_by_is_banned_after_expiry(self):
        """_is_banned should remove the ban entry when it's expired."""
        troshkad._banned_ips["10.0.0.99"] = time.monotonic() - 1
        self.assertFalse(troshkad._is_banned("10.0.0.99"))
        self.assertNotIn("10.0.0.99", troshkad._banned_ips)

    def test_cleanup_preserves_permabanned(self):
        """_cleanup_rate_limit should not clear permabanned IPs."""
        troshkad._permabanned_ips.add("10.0.0.50")
        troshkad._cleanup_rate_limit()
        self.assertIn("10.0.0.50", troshkad._permabanned_ips)

    def test_ban_history_cleanup_by_window(self):
        """Old ban history entries should be cleaned by _cleanup_rate_limit."""
        troshkad._ban_history["10.0.0.60"] = [time.monotonic() - 7200]
        troshkad._cleanup_rate_limit()
        self.assertNotIn("10.0.0.60", troshkad._ban_history)

    def test_fail_tracker_window_sliding(self):
        """Old failures outside the window should be pruned on next failure."""
        ip = "10.0.0.70"
        # Add old failures
        old_time = time.monotonic() - troshkad._BAN_WINDOW - 10
        troshkad._fail_tracker[ip] = [old_time] * (troshkad._BAN_THRESHOLD - 1)
        # Add one new failure -- old ones should be pruned, so no ban
        troshkad._record_auth_failure(ip)
        self.assertFalse(troshkad._is_banned(ip))
        self.assertEqual(len(troshkad._fail_tracker[ip]), 1)


class TestShowroomInfraForward(unittest.TestCase):
    """Showroom pod -> lab-bridge forwarding + SNAT rule generation."""

    def _run_and_capture(self):
        """Run _allow_infra_veth_forward with a mocked _run_cmd, return calls."""
        bridge_listing = "\n".join(
            [
                "2: br-1929: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500",
                "9: br-bmc-6fcf0e3e: <BROADCAST,MULTICAST,UP> mtu 1500",
            ]
        )

        def fake_run_cmd(job, cmd, **kwargs):
            if "link" in cmd and "show" in cmd and "bridge" in cmd:
                return bridge_listing
            return ""

        with patch("troshkad._run_cmd", side_effect=fake_run_cmd) as mock_run:
            troshkad._allow_infra_veth_forward(
                {"log": []}, "troshka-6fcf0e3e", "vishowroomh"
            )
        return [call.args[1] for call in mock_run.call_args_list]

    def test_masquerade_added_per_bridge(self):
        """A NAT postrouting masquerade must be added for each lab bridge."""
        cmds = self._run_and_capture()
        for bridge in ("br-1929", "br-bmc-6fcf0e3e"):
            masq = [
                "ip",
                "netns",
                "exec",
                "troshka-6fcf0e3e",
                "nft",
                "add",
                "rule",
                "inet",
                "nat",
                "postrouting",
                "oifname",
                bridge,
                "masquerade",
            ]
            self.assertIn(masq, cmds, f"missing masquerade rule for {bridge}")

    def test_forward_accept_still_added(self):
        """Forward accept rules (both directions) remain for each bridge."""
        cmds = self._run_and_capture()
        fwd_out = [
            "ip",
            "netns",
            "exec",
            "troshka-6fcf0e3e",
            "nft",
            "add",
            "rule",
            "inet",
            "filter",
            "forward",
            "iifname",
            "vishowroomh",
            "oifname",
            "br-1929",
            "accept",
        ]
        self.assertIn(fwd_out, cmds)


if __name__ == "__main__":
    unittest.main()
