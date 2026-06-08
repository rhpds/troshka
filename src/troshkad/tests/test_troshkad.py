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

    def test_job_dispatch_and_poll(self):
        """Test job dispatch, polling until completion, and result retrieval."""
        # Register a test handler
        def test_echo_handler(job, params):
            time.sleep(0.2)
            return {"echo": params.get("msg")}
        troshkad.COMMAND_HANDLERS["_test/echo"] = test_echo_handler

        try:
            # Dispatch job
            status, body = _make_request("/commands/_test/echo", method="POST", body={"msg": "hello"})
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
            status1, body1 = _make_request("/commands/_test/slow", method="POST", body={})
            self.assertEqual(status1, 202)
            status2, body2 = _make_request("/commands/_test/slow", method="POST", body={})
            self.assertEqual(status2, 202)

            # Third should return 503
            status3, body3 = _make_request("/commands/_test/slow", method="POST", body={})
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
            status, body = _make_request("/commands/_test/drain", method="POST", body={})
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
            with unittest.mock.patch.object(troshkad, "_do_update_restart", mock_restart):
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
        """Test that /host/disk-usage returns disk statistics."""
        status, body = _make_request("/host/disk-usage")
        self.assertEqual(status, 200)
        self.assertIn("free_bytes", body)
        self.assertIn("total_bytes", body)
        self.assertIn("used_pct", body)
        # On macOS dev machine, /var/lib/troshka won't exist so it should return used_pct=100
        self.assertIsInstance(body["free_bytes"], (int, float))
        self.assertIsInstance(body["total_bytes"], (int, float))
        self.assertIsInstance(body["used_pct"], (int, float))
        self.assertGreaterEqual(body["used_pct"], 0)
        self.assertLessEqual(body["used_pct"], 100)


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
        job = troshkad._create_job("vms/create", {
            "domain_name": "troshka-aabbccdd-11223344",
            "vcpus": 2,
            "ram_mb": 4096,
            "disks": [{"path": "/var/lib/troshka/vms/proj/aabb-1122.qcow2", "bus": "virtio"}],
            "networks": [{"bridge": "br-troshka-abc", "model": "virtio"}],
            "seed_iso": "/var/lib/troshka/vms/proj/aabb-seed.iso",
        })
        result = troshkad._handle_vm_create(job, job["params"])
        self.assertTrue(mock_popen.called)
        cmd = mock_popen.call_args[0][0]
        self.assertEqual(cmd[0], "virt-install")
        self.assertIn("--name", cmd)
        self.assertIn("troshka-aabbccdd-11223344", cmd)

    @patch("troshkad.subprocess.Popen")
    def test_vm_destroy_calls_virsh(self, mock_popen):
        mock_popen.return_value = _mock_popen()
        job = troshkad._create_job("vms/destroy", {"domain_name": "troshka-aabb1122-11223344"})
        troshkad._handle_vm_destroy(job, job["params"])
        calls = [c[0][0] for c in mock_popen.call_args_list]
        self.assertTrue(any("destroy" in c for c in calls))
        self.assertTrue(any("undefine" in c for c in calls))

    @patch("troshkad.subprocess.Popen")
    def test_vm_start(self, mock_popen):
        mock_popen.return_value = _mock_popen(stdout="Domain started")
        job = troshkad._create_job("vms/start", {"domain_name": "troshka-aabb1122-11223344"})
        troshkad._handle_vm_start(job, job["params"])
        cmd = mock_popen.call_args[0][0]
        self.assertEqual(cmd[:2], ["virsh", "start"])

    @patch("troshkad.subprocess.Popen")
    def test_vm_stop(self, mock_popen):
        mock_popen.return_value = _mock_popen(stdout="Domain stopped")
        job = troshkad._create_job("vms/stop", {"domain_name": "troshka-aabb1122-11223344"})
        troshkad._handle_vm_stop(job, job["params"])
        cmd = mock_popen.call_args[0][0]
        self.assertEqual(cmd[:2], ["virsh", "shutdown"])

    def test_vm_create_rejects_invalid_domain(self):
        """Domain name must match troshka-{hex}-{hex} pattern."""
        job = troshkad._create_job("vms/create", {
            "domain_name": "evil; rm -rf /",
            "vcpus": 2, "ram_mb": 4096, "disks": [], "networks": [],
        })
        with self.assertRaises(ValueError):
            troshkad._handle_vm_create(job, job["params"])


class TestStorageHandlers(unittest.TestCase):

    @patch("troshkad.os.makedirs")
    @patch("troshkad.subprocess.Popen")
    def test_disk_create_qcow2(self, mock_popen, mock_makedirs):
        mock_popen.return_value = _mock_popen()
        job = troshkad._create_job("disks/create", {
            "path": "/var/lib/troshka/vms/proj-id/aabb-1122.qcow2",
            "size_gb": 20,
            "format": "qcow2",
        })
        result = troshkad._handle_disk_create(job, job["params"])
        cmd = mock_popen.call_args[0][0]
        self.assertEqual(cmd[0], "qemu-img")
        self.assertIn("create", cmd)
        self.assertEqual(result["status"], "created")

    @patch("troshkad.os.makedirs")
    @patch("troshkad.subprocess.Popen")
    def test_disk_create_with_backing(self, mock_popen, mock_makedirs):
        mock_popen.return_value = _mock_popen()
        job = troshkad._create_job("disks/create", {
            "path": "/var/lib/troshka/vms/proj-id/aabb-1122.qcow2",
            "size_gb": 20,
            "format": "qcow2",
            "backing_file": "/var/lib/troshka/images/base.qcow2",
        })
        troshkad._handle_disk_create(job, job["params"])
        cmd = mock_popen.call_args[0][0]
        self.assertIn("-b", cmd)

    @patch("troshkad.subprocess.Popen")
    def test_disk_resize(self, mock_popen):
        mock_popen.return_value = _mock_popen()
        job = troshkad._create_job("disks/resize", {
            "path": "/var/lib/troshka/vms/proj-id/aabb-1122.qcow2",
            "new_size_gb": 40,
        })
        troshkad._handle_disk_resize(job, job["params"])
        cmd = mock_popen.call_args[0][0]
        self.assertEqual(cmd[:2], ["qemu-img", "resize"])

    @patch("troshkad.os.makedirs")
    @patch("troshkad.subprocess.Popen")
    def test_seed_create(self, mock_popen, mock_makedirs):
        mock_popen.return_value = _mock_popen()
        with patch("tempfile.TemporaryDirectory") as mock_tempdir:
            mock_tempdir.return_value.__enter__.return_value = "/tmp/test-tmpdir"
            job = troshkad._create_job("seeds/create", {
                "path": "/var/lib/troshka/vms/proj-id/aabb-seed.iso",
                "meta_data": "instance-id: test",
                "user_data": "#cloud-config\npassword: test",
            })
            with patch("builtins.open", unittest.mock.mock_open()):
                troshkad._handle_seed_create(job, job["params"])
            cmd = mock_popen.call_args[0][0]
            self.assertEqual(cmd[0], "xorriso")

    def test_disk_create_rejects_bad_path(self):
        job = troshkad._create_job("disks/create", {
            "path": "/etc/passwd",
            "size_gb": 20, "format": "qcow2",
        })
        with self.assertRaises(ValueError):
            troshkad._handle_disk_create(job, job["params"])


class TestNetworkHandlers(unittest.TestCase):

    @patch("troshkad.subprocess.Popen")
    def test_network_setup(self, mock_popen):
        mock_popen.return_value = _mock_popen()
        job = troshkad._create_job("networks/setup", {
            "network_name": "troshka-net-aabb",
            "cidr": "192.168.100.0/24",
            "vni": 10001,
            "bridge_name": "br-troshka-aabb",
            "project_id": "aabbccdd-1122-3344-5566-778899001122",
        })
        result = troshkad._handle_network_setup(job, job["params"])
        self.assertEqual(result["status"], "configured")

    @patch("troshkad.subprocess.Popen")
    def test_network_teardown(self, mock_popen):
        mock_popen.return_value = _mock_popen()
        job = troshkad._create_job("networks/teardown", {
            "network_name": "troshka-net-aabb",
            "project_id": "aabbccdd-1122-3344-5566-778899001122",
        })
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
        job = troshkad._create_job("snapshots/create", {
            "domain_name": "troshka-aabbccdd-11223344",
            "output_path": "/var/lib/troshka/tmp/snapshot.qcow2",
        })
        result = troshkad._handle_snapshot_create(job, job["params"])
        self.assertEqual(result["status"], "created")


class TestHostEndpoints(unittest.TestCase):
    """Unit tests for host management command handlers."""

    @patch("troshkad.subprocess.Popen")
    def test_resize_storage(self, mock_popen):
        """Test that resize-storage runs xfs_growfs."""
        mock_popen.return_value = _mock_popen(stdout="Done")
        job = troshkad._create_job("host/resize-storage", {})
        result = troshkad._handle_resize_storage(job, job["params"])
        self.assertTrue(mock_popen.called)
        cmd = mock_popen.call_args[0][0]
        self.assertEqual(cmd, ["xfs_growfs", "/var/lib/troshka"])
        self.assertEqual(result["status"], "resized")

    @patch("troshkad.os.remove")
    def test_files_remove(self, mock_remove):
        """Test that files/remove removes valid paths."""
        job = troshkad._create_job("files/remove", {
            "paths": ["/var/lib/troshka/vms/test/disk.qcow2"]
        })
        result = troshkad._handle_files_remove(job, job["params"])
        mock_remove.assert_called_once_with("/var/lib/troshka/vms/test/disk.qcow2")
        self.assertEqual(result["removed"], 1)

    def test_files_remove_rejects_bad_path(self):
        """Test that files/remove rejects paths outside /var/lib/troshka."""
        job = troshkad._create_job("files/remove", {
            "paths": ["/etc/passwd"]
        })
        with self.assertRaises(ValueError) as ctx:
            troshkad._handle_files_remove(job, job["params"])
        self.assertIn("/var/lib/troshka", str(ctx.exception))


class TestGcEndpoints(unittest.TestCase):
    """Unit tests for garbage collection endpoints."""

    @patch("troshkad.subprocess.run")
    @patch("troshkad.os.listdir")
    @patch("troshkad.os.path.exists")
    @patch("troshkad.os.path.isdir")
    def test_gc_discover_finds_orphans(self, mock_isdir, mock_exists, mock_listdir, mock_run):
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

        job = troshkad._create_job("gc/discover", {
            "known_project_ids": ["known-uuid"],
            "known_domains": ["troshka-aabb-1122"],
        })
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

        job = troshkad._create_job("gc/clean", {
            "orphan_dirs": ["/var/lib/troshka/vms/dead-uuid/"],
            "orphan_domains": ["troshka-dead-beef"],
            "orphan_bridges": ["br-troshka-dead"],
            "orphan_namespaces": ["troshka-deadbeef"],
            "cache_items": [],
        })
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

        job = troshkad._create_job("library/import", {
            "download_url": "https://example.com/image.qcow2",
            "cache_path": "/var/lib/troshka/images/item-123.qcow2",
        })
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
    def test_import_with_flatten(self, mock_popen, mock_makedirs, mock_getsize, mock_rename):
        """Test import with flatten=true runs qemu-img convert."""
        mock_popen.return_value = _mock_popen()
        mock_getsize.return_value = 2048

        job = troshkad._create_job("library/import", {
            "download_url": "https://example.com/image.qcow2",
            "cache_path": "/var/lib/troshka/images/item-123.qcow2",
            "flatten": True,
        })
        result = troshkad._handle_library_import(job, job["params"])

        # Check that qemu-img convert was called
        cmds = [c[0][0] for c in mock_popen.call_args_list]
        self.assertTrue(any(c[0] == "qemu-img" for c in cmds if c))

    def test_import_rejects_bad_url(self):
        """Test that import rejects non-http(s) URLs."""
        job = troshkad._create_job("library/import", {
            "download_url": "file:///etc/passwd",
            "cache_path": "/var/lib/troshka/images/item-123.qcow2",
        })
        with self.assertRaises(ValueError):
            troshkad._handle_library_import(job, job["params"])


class TestCaptureEndpoints(unittest.TestCase):
    """Unit tests for snapshot/pattern capture endpoints."""

    @patch("troshkad.shutil.copy")
    @patch("troshkad.os.path.getsize")
    @patch("troshkad.os.makedirs")
    @patch("troshkad.subprocess.run")
    @patch("troshkad.subprocess.Popen")
    def test_snapshot_capture(self, mock_popen, mock_run, mock_makedirs, mock_getsize, mock_copy):
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

            job = troshkad._create_job("snapshots/capture", {
                "domain_name": "troshka-aabbccdd-11223344",
                "disk_index": 0,
                "presigned_url": "https://s3.example.com/upload",
                "cache_path": "/var/lib/troshka/cache/snapshots/item/disk.qcow2",
            })
            result = troshkad._handle_snapshot_capture(job, job["params"])

        self.assertEqual(result["status"], "uploaded")
        self.assertEqual(result["size_bytes"], 12345)

    @patch("troshkad.shutil.copy")
    @patch("troshkad.os.path.getsize")
    @patch("troshkad.os.makedirs")
    @patch("troshkad.subprocess.run")
    @patch("troshkad.subprocess.Popen")
    def test_pattern_capture(self, mock_popen, mock_run, mock_makedirs, mock_getsize, mock_copy):
        """Test pattern capture: capture multiple disks."""
        mock_popen.return_value = _mock_popen()
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Type  Device  Target  Source\nfile  disk    vda     /var/lib/troshka/vms/proj/disk.qcow2\n",
            stderr="",
        )
        mock_getsize.return_value = 54321

        with patch("tempfile.TemporaryDirectory") as mock_tempdir:
            mock_tempdir.return_value.__enter__.return_value = "/tmp/test-tmpdir"

            job = troshkad._create_job("patterns/capture", {
                "domain_name": "troshka-aabbccdd-11223344",
                "disks": [{
                    "disk_index": 0,
                    "presigned_url": "https://s3.example.com/upload",
                    "cache_path": "/var/lib/troshka/cache/patterns/pat/disk.qcow2",
                }],
            })
            result = troshkad._handle_pattern_capture(job, job["params"])

        self.assertEqual(result["status"], "uploaded")
        self.assertEqual(len(result["disks"]), 1)
        self.assertEqual(result["disks"][0]["size_bytes"], 54321)


if __name__ == "__main__":
    unittest.main()
