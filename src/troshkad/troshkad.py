# src/troshkad/troshkad.py
"""troshkad — Troshka host agent daemon.

Single-file Python daemon managing QEMU/libvirt on the host.
Exposes a structured HTTPS REST API for the Troshka backend.
Requires only Python 3.9+ stdlib — no pip dependencies.
"""
import base64
import glob
import hashlib
import hmac
import json
import logging
import os
import shutil
import signal
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
import socketserver
import xml.etree.ElementTree as ET


class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def get_request(self):
        # Set a timeout on the raw socket BEFORE TLS handshake so a
        # stalled handshake doesn't block the accept loop forever.
        conn, addr = self.socket.accept()
        conn.settimeout(10)
        return conn, addr

    def verify_request(self, request, client_address):
        # Reject banned IPs before spawning a handler thread.
        if _is_banned(client_address[0]):
            return False
        return True


VERSION = "dev"  # stamped by backend at push time; self-hashes if unstamped


def _compute_version():
    import hashlib as _hl

    try:
        with open(__file__, "rb") as _f:
            return _hl.sha256(_f.read()).hexdigest()[:12]
    except Exception:
        return "dev"


if VERSION == "dev":
    VERSION = _compute_version()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("troshkad")

# ── Global state ──

_config = {}
_jobs = {}  # job_id -> Job dict
_jobs_lock = threading.Lock()
_draining = False
_my_pid = os.getpid()


def _safe_kill(pid, sig, expected_cmdline_substring=None):
    """Kill a PID only if it's not us and optionally matches expected command.

    Returns True if the signal was sent, False if skipped.
    """
    if pid <= 0:
        logger.warning("Refusing to kill PID %d (would signal process group)", pid)
        return False
    if pid == _my_pid or pid == os.getpid():
        logger.warning("Refusing to kill own PID %d", pid)
        return False
    if expected_cmdline_substring:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmdline = f.read().replace(b"\x00", b" ").decode(errors="replace")
            if expected_cmdline_substring not in cmdline:
                logger.warning(
                    "PID %d cmdline '%s' doesn't match expected '%s', skipping kill",
                    pid,
                    cmdline[:120],
                    expected_cmdline_substring,
                )
                return False
        except (FileNotFoundError, PermissionError):
            pass
    os.kill(pid, sig)
    return True


_vm_state_cache = {}
_vm_state_cache_lock = threading.Lock()
_vm_events = []
_vm_events_lock = threading.Lock()
_libvirt_events_available = False

# ── Rate limiting / auto-ban ──

_BAN_WINDOW = 60  # seconds to track failures
_BAN_THRESHOLD = 10  # failures within window to trigger ban
_BAN_DURATION = 300  # seconds to ban an IP
_PERMABAN_THRESHOLD = 3  # temp bans within window to trigger permaban
_PERMABAN_WINDOW = 3600  # seconds to track temp bans

_fail_tracker = {}  # ip -> [timestamp, ...]
_banned_ips = {}  # ip -> ban_expiry_timestamp
_permabanned_ips = set()  # permanently banned (until process restart)
_ban_history = {}  # ip -> [ban_timestamp, ...]
_rate_limit_lock = threading.Lock()


def _record_auth_failure(ip):
    now = time.monotonic()
    with _rate_limit_lock:
        if ip in _permabanned_ips:
            return
        times = _fail_tracker.get(ip, [])
        cutoff = now - _BAN_WINDOW
        times = [t for t in times if t > cutoff]
        times.append(now)
        _fail_tracker[ip] = times
        if len(times) >= _BAN_THRESHOLD:
            _banned_ips[ip] = now + _BAN_DURATION
            del _fail_tracker[ip]
            history = _ban_history.get(ip, [])
            history_cutoff = now - _PERMABAN_WINDOW
            history = [t for t in history if t > history_cutoff]
            history.append(now)
            _ban_history[ip] = history
            if len(history) >= _PERMABAN_THRESHOLD:
                _permabanned_ips.add(ip)
                _banned_ips.pop(ip, None)
                _ban_history.pop(ip, None)
                logger.warning(
                    "Permanently banned IP %s (%d temp bans in %ds)",
                    ip,
                    len(history),
                    _PERMABAN_WINDOW,
                )
            else:
                logger.warning(
                    "Banned IP %s for %ds (%d failures in %ds, strike %d/%d)",
                    ip,
                    _BAN_DURATION,
                    len(times),
                    _BAN_WINDOW,
                    len(history),
                    _PERMABAN_THRESHOLD,
                )


def _is_banned(ip):
    with _rate_limit_lock:
        if ip in _permabanned_ips:
            return True
        expiry = _banned_ips.get(ip)
        if expiry is None:
            return False
        if time.monotonic() > expiry:
            del _banned_ips[ip]
            return False
        return True


def _cleanup_rate_limit():
    """Periodic sweep to remove expired entries."""
    now = time.monotonic()
    with _rate_limit_lock:
        cutoff = now - _BAN_WINDOW
        stale = [
            ip for ip, times in _fail_tracker.items() if not times or times[-1] < cutoff
        ]
        for ip in stale:
            del _fail_tracker[ip]
        expired = [ip for ip, exp in _banned_ips.items() if now > exp]
        for ip in expired:
            del _banned_ips[ip]
        history_cutoff = now - _PERMABAN_WINDOW
        stale_history = [
            ip
            for ip, times in _ban_history.items()
            if not times or times[-1] < history_cutoff
        ]
        for ip in stale_history:
            del _ban_history[ip]


# ── Path / string constants ──

_TROSHKA_DIR = "/var/lib/troshka"
_VMS_DIR = "/var/lib/troshka/vms"
_TMP_DIR = "/var/lib/troshka/tmp"
_MESH_DIR = "/var/lib/troshka/mesh"
_BMC_DIR = "/var/lib/troshka/bmc"
_PXE_DIR = "/var/lib/troshka/pxe"
_CHRONY_DIR = "/var/lib/troshka/chrony"
_SHARED_DIR = "/var/lib/troshka/shared"
_LOCAL_DIR = "/var/lib/troshka/local"
_DNSMASQ_PREFIX = "/var/lib/troshka/dnsmasq"
_INET_PREFIX = "inet "

# ── SSH option constants ──

_SSH_STRICT_HOST = "StrictHostKeyChecking=no"
_SSH_KNOWN_HOSTS = "UserKnownHostsFile=/dev/null"
_SSH_LOG_LEVEL = "LogLevel=ERROR"
_SSH_TIMEOUT = "ConnectTimeout=10"

# ── Repeated string constants (S1192) ──

_NO_COMMAND = "No command specified"
_PODMAN_NAMES_FMT = "{{.Names}}"
_TROSHKA_FILTER = "name=troshka-"
_STATE_PID_FMT = "{{.State.Pid}}"
_VMS_STATE_CMD = "vms/state"
_GC_DISCOVER_CMD = "gc/discover"
_PODMAN_JSON = "--output=json"
_ISO_DATETIME_FMT = "%Y-%m-%dT%H:%M:%SZ"
_ZSTD_COMPRESSION = "compression_type=zstd"
_AWS_CLI = "/opt/troshka/venv/bin/aws"
_VENV_BIN = "/opt/troshka/venv/bin"
_VBMCD_PID = "vbmcd.pid"
_PXE_LOADER = "pxelinux.0"

# ── NFS health tracking ──

_nfs_healthy = True
_nfs_last_check = 0.0
_nfs_stale_since = 0.0


def _check_nfs_health():
    """Probe NFS mount with a timeout. Returns True if healthy."""
    global _nfs_healthy, _nfs_last_check, _nfs_stale_since
    mode = _config.get("storage_mode", "local")
    if mode != "shared":
        _nfs_healthy = True
        return True

    shared = _config.get("shared_mount", _SHARED_DIR)
    if not os.path.ismount(shared):
        if _nfs_healthy:
            logger.warning("NFS mount %s not mounted", shared)
            _nfs_stale_since = time.time()
        _nfs_healthy = False
        _nfs_last_check = time.time()
        return False

    result = [None]

    def _probe():
        try:
            os.statvfs(shared)
            result[0] = True
        except OSError:
            result[0] = False

    t = threading.Thread(target=_probe, daemon=True)
    t.start()
    t.join(timeout=5)
    if t.is_alive() or not result[0]:
        if _nfs_healthy:
            logger.warning("NFS mount %s is stale (probe timed out or failed)", shared)
            _nfs_stale_since = time.time()
        _nfs_healthy = False
        _nfs_last_check = time.time()
        return False

    if not _nfs_healthy:
        logger.info("NFS mount %s recovered", shared)
    _nfs_healthy = True
    _nfs_stale_since = 0.0
    _nfs_last_check = time.time()
    return True


def _try_nfs_recovery():
    """Attempt to recover a stale NFS mount via lazy unmount + remount."""
    shared = _config.get("shared_mount", _SHARED_DIR)
    logger.warning("Attempting NFS recovery on %s", shared)

    # Read fstab to get mount source and options
    nfs_src = nfs_opts = ""
    try:
        with open("/etc/fstab") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) >= 4 and parts[1] == shared:
                    nfs_src = parts[0]
                    nfs_opts = parts[3]
                    break
    except OSError:
        logger.error("NFS recovery: cannot read /etc/fstab")
        return False

    if not nfs_src:
        logger.error("NFS recovery: no fstab entry for %s", shared)
        return False

    # Lazy unmount (doesn't block even with open handles)
    try:
        subprocess.run(["umount", "-l", shared], capture_output=True, timeout=10)
    except Exception as e:
        logger.warning("NFS recovery: umount -l failed: %s", e)

    time.sleep(2)

    # Remount
    try:
        result = subprocess.run(
            ["mount", "-t", "nfs", "-o", nfs_opts, nfs_src, shared],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            logger.info("NFS recovery: remounted %s successfully", shared)
            return True
        else:
            logger.error("NFS recovery: mount failed: %s", result.stderr.strip())
            return False
    except subprocess.TimeoutExpired:
        logger.error("NFS recovery: mount timed out")
        return False
    except Exception as e:
        logger.exception("NFS recovery: mount error")
        return False


# ── Config ──


def load_config(path="/opt/troshka/troshkad.conf"):
    with open(path) as f:
        return json.load(f)


# ── Job tracking ──


def _create_job(command, params):
    job_id = str(uuid.uuid4())
    job = {
        "job_id": job_id,
        "command": command,
        "params": params,
        "status": "running",
        "output": [],
        "result": None,
        "started_at": time.strftime(_ISO_DATETIME_FMT, time.gmtime()),
        "_start_time": time.time(),
        "completed_at": None,
        "_process": None,
        "_cancelled": False,
    }
    with _jobs_lock:
        _jobs[job_id] = job
    return job


def _complete_job(job, status, result=None):
    job["status"] = status
    job["result"] = result or {}
    job["completed_at"] = time.strftime(_ISO_DATETIME_FMT, time.gmtime())


def _cancel_job(job_id):
    """Cancel a running job: set flag and kill subprocess."""
    job = _get_job(job_id)
    if not job:
        return None
    if job["status"] != "running":
        return job
    job["_cancelled"] = True
    proc = job.get("_process")
    if proc and proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass
    _complete_job(job, "cancelled", {"error": "cancelled by user"})
    _job_log(job, "Job cancelled")
    return job


def _get_job(job_id):
    with _jobs_lock:
        return _jobs.get(job_id)


def _running_job_count():
    with _jobs_lock:
        return sum(1 for j in _jobs.values() if j["status"] == "running")


def _cleanup_old_jobs():
    """Remove completed/failed jobs older than 1 hour."""
    cutoff = time.time() - 3600
    with _jobs_lock:
        to_remove = []
        for jid, job in _jobs.items():
            if job["status"] in ("completed", "failed") and job["completed_at"]:
                try:
                    t = time.mktime(
                        time.strptime(job["completed_at"], _ISO_DATETIME_FMT)
                    )
                    if t < cutoff:
                        to_remove.append(jid)
                except (ValueError, OverflowError):
                    to_remove.append(jid)
        for jid in to_remove:
            del _jobs[jid]
        if to_remove:
            logger.info("Cleaned up %d old jobs", len(to_remove))


def _job_cleanup_loop():
    """Background thread: prune completed jobs every 10 minutes."""
    while True:
        time.sleep(600)
        _cleanup_old_jobs()


# ── Capacity info ──


def _get_cpu_capacity():
    """Return dict with vcpus_total from os.cpu_count()."""
    try:
        return {"vcpus_total": os.cpu_count() or 0}
    except Exception:
        return {"vcpus_total": 0}


def _get_memory_capacity():
    """Return dict with ram_total_mb from /proc/meminfo."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return {"ram_total_mb": int(line.split()[1]) // 1024}
    except Exception:
        pass
    return {"ram_total_mb": 0}


def _get_storage_capacity():
    """Return dict with storage_total_gb and storage_used_gb."""
    try:
        storage_root = _TROSHKA_DIR
        if _config.get("storage_mode") == "shared":
            storage_root = _config.get("local_mount", _LOCAL_DIR)
        stat = shutil.disk_usage(storage_root)
        return {
            "storage_total_gb": stat.total // (1024**3),
            "storage_used_gb": stat.used // (1024**3),
        }
    except Exception:
        return {"storage_total_gb": 0, "storage_used_gb": 0}


def _get_vm_capacity():
    """Return dict with total_vms, running_vms, vcpus_used, ram_used_mb."""
    result_dict = {}
    try:
        result = subprocess.run(
            ["virsh", "list", "--all", "--name"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return result_dict
        domains = [d.strip() for d in result.stdout.strip().split("\n") if d.strip()]
        result_dict["total_vms"] = len(domains)
        running = subprocess.run(
            ["virsh", "list", "--name"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if running.returncode == 0:
            result_dict["running_vms"] = len(
                [d for d in running.stdout.strip().split("\n") if d.strip()]
            )
        vcpus_used = 0
        ram_used = 0
        for domain in domains:
            info = subprocess.run(
                ["virsh", "dominfo", domain],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if info.returncode == 0:
                for line in info.stdout.split("\n"):
                    if line.startswith("CPU(s):"):
                        vcpus_used += int(line.split(":")[1].strip())
                    elif line.startswith("Max memory:"):
                        ram_used += int(line.split(":")[1].strip().split()[0]) // 1024
        result_dict["vcpus_used"] = vcpus_used
        result_dict["ram_used_mb"] = ram_used
    except Exception:
        pass
    return result_dict


def _get_container_capacity():
    """Return dict with total_containers and running_containers."""
    try:
        result = subprocess.run(
            [
                "podman",
                "ps",
                "-a",
                "--filter",
                _TROSHKA_FILTER,
                "--format",
                "{{.Names}} {{.State}}",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            containers = [
                line.strip()
                for line in result.stdout.strip().split("\n")
                if line.strip()
            ]
            return {
                "total_containers": len(containers),
                "running_containers": len(
                    [c for c in containers if c.split(None, 1)[-1].lower() == "running"]
                ),
            }
    except Exception:
        pass
    return {}


def _get_capacity():
    """Read host capacity from system — best effort."""
    capacity = {
        "vcpus_total": 0,
        "vcpus_used": 0,
        "ram_total_mb": 0,
        "ram_used_mb": 0,
        "storage_total_gb": 0,
        "storage_used_gb": 0,
    }
    capacity.update(_get_cpu_capacity())
    capacity.update(_get_memory_capacity())
    capacity.update(_get_storage_capacity())
    capacity.update(_get_vm_capacity())
    capacity.update(_get_container_capacity())
    return capacity


_PSEUDO_FSTYPES = frozenset(
    {
        "proc",
        "sysfs",
        "devtmpfs",
        "tmpfs",
        "cgroup",
        "cgroup2",
        "overlay",
        "devpts",
        "mqueue",
        "hugetlbfs",
        "debugfs",
        "tracefs",
        "securityfs",
        "pstore",
        "bpf",
        "fusectl",
        "configfs",
        "autofs",
        "nfsd",
        "rpc_pipefs",
        "binfmt_misc",
        "efivarfs",
        "nsfs",
        "fuse.lxcfs",
    }
)


def _get_partitions():
    """Read all mounted partitions, filtering pseudo-filesystems and deduplicating by device."""
    partitions = []
    seen_devices = set()
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                device, mount, fstype = parts[0], parts[1], parts[2]
                if fstype in _PSEUDO_FSTYPES:
                    continue
                if device in seen_devices:
                    continue
                # Skip NFS mounts when NFS is stale to avoid D-state
                if fstype.startswith("nfs") and not _nfs_healthy:
                    continue
                seen_devices.add(device)
                try:
                    stat = shutil.disk_usage(mount)
                    partitions.append(
                        {
                            "mount": mount,
                            "total_bytes": stat.total,
                            "used_bytes": stat.used,
                            "free_bytes": stat.free,
                            "used_pct": (
                                round((stat.used / stat.total) * 100, 1)
                                if stat.total > 0
                                else 0
                            ),
                            "device": device,
                            "fstype": fstype,
                        }
                    )
                except OSError:
                    pass
    except OSError:
        pass
    return partitions


# ── Job dispatch framework ──

COMMAND_HANDLERS = {}  # command_path -> handler_func(job, params)


def _run_job_worker(job, handler):
    """Worker thread: runs handler, updates job status."""
    try:
        result = handler(job, job["params"])
        _complete_job(job, "completed", result)
    except Exception as e:
        logger.exception("Job %s failed: %s", job["job_id"], e)
        _complete_job(job, "failed", {"error": str(e)})


def _dispatch_job(command, params):
    """Dispatch a job: checks limits, creates job, spawns worker thread.

    Returns (status_code, response_body).
    """
    if _draining:
        return 503, {"status": "draining", "error": "server is draining"}

    max_jobs = _config.get("max_concurrent_jobs", max(20, os.cpu_count() or 20))
    if _running_job_count() >= max_jobs:
        return 503, {"error": f"max_concurrent_jobs ({max_jobs}) reached"}

    handler = COMMAND_HANDLERS.get(command)
    if not handler:
        return 404, {"error": f"no handler for command: {command}"}

    job = _create_job(command, params)
    worker = threading.Thread(target=_run_job_worker, args=(job, handler), daemon=True)
    worker.start()

    return 202, {
        "job_id": job["job_id"],
        "status": job["status"],
    }


# ── HTTP routing ──

ROUTES = {}  # (method, path_pattern) -> handler_func


def route(method, path):
    """Decorator to register a route handler."""

    def decorator(func):
        ROUTES[(method, path)] = func
        return func

    return decorator


def _match_path_params(parts, pattern):
    """Try to match URL path parts against a route pattern.

    Returns ``(True, params_dict)`` on match, ``(False, {})`` otherwise.
    """
    pat_parts = pattern.strip("/").split("/")
    if len(parts) != len(pat_parts):
        return False, {}
    params = {}
    for p, pp in zip(parts, pat_parts):
        if pp.startswith("{") and pp.endswith("}"):
            params[pp[1:-1]] = p
        elif p != pp:
            return False, {}
    return True, params


def _match_route(method, path):
    """Match a request to a route, supporting /jobs/{job_id} style paths."""
    # Special handling for /commands/* paths
    if path.startswith("/commands/") and method == "POST":
        handler = ROUTES.get(("POST", "/commands/{command_path}"))
        if handler:
            return handler, {"command_path": path[len("/commands/") :]}

    handler = ROUTES.get((method, path))
    if handler:
        return handler, {}
    # Try path parameter patterns
    parts = path.strip("/").split("/")
    for (m, pattern), handler in ROUTES.items():
        if m != method:
            continue
        matched, params = _match_path_params(parts, pattern)
        if matched:
            return handler, params
    return None, {}


# ── Request handler ──


class TroshkadHandler(BaseHTTPRequestHandler):
    """HTTPS request handler with auth and JSON routing."""

    timeout = 30
    protocol_version = "HTTP/1.1"

    def end_headers(self):
        self.send_header("Connection", "close")
        super().end_headers()

    def log_message(self, format, *args):
        logger.info("%s %s", self.client_address[0], format % args)

    def _check_auth(self):
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return False
        token = auth[7:]
        return hmac.compare_digest(token, _config.get("token", ""))

    def _send_json(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode())

    def _read_raw_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return b""
        return self.rfile.read(length)

    def _send_binary(self, status, data, content_type="application/octet-stream"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _stream_file(self, status, file_path, content_type="application/octet-stream"):
        size = os.path.getsize(file_path)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("X-File-Size", str(size))
        self.end_headers()
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _handle(self, method):
        ip = self.client_address[0]
        if _is_banned(ip):
            self._send_json(403, {"error": "banned"})
            return
        if not self._check_auth():
            _record_auth_failure(ip)
            self._send_json(401, {"error": "unauthorized"})
            return
        path = self.path.split("?")[0]
        handler, params = _match_route(method, path)
        if not handler:
            # Check if path exists with a different method
            path_exists = any(pattern == path for m, pattern in ROUTES.keys())
            if path_exists:
                self._send_json(405, {"error": "method not allowed"})
            else:
                self._send_json(404, {"error": "not found"})
            return
        try:
            handler(self, params)
        except Exception as e:
            logger.exception("Handler error: %s", e)
            self._send_json(500, {"error": str(e)})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_DELETE(self):
        self._handle("DELETE")


# ── Route handlers ──


@route("GET", "/health")
def handle_health(handler, params):
    status = "draining" if _draining else "ok"
    if not _nfs_healthy and _config.get("storage_mode") == "shared":
        status = "nfs_stale"
    resp = {
        "status": status,
        "version": VERSION,
        "host_id": _config.get("host_id", ""),
        "uptime_seconds": int(time.time() - _start_time),
        "running_jobs": _running_job_count(),
        "capacity": _get_capacity(),
        "partitions": _get_partitions(),
        "features": {
            "batch_vm_states": True,
            "libvirt_events": _libvirt_events_available,
        },
    }
    if not _nfs_healthy and _nfs_stale_since:
        resp["nfs_stale_seconds"] = int(time.time() - _nfs_stale_since)
    handler._send_json(200, resp)


@route("GET", "/jobs/{job_id}")
def handle_get_job(handler, params):
    job = _get_job(params["job_id"])
    if not job:
        handler._send_json(404, {"error": "job not found"})
        return
    handler._send_json(
        200,
        {
            "job_id": job["job_id"],
            "command": job["command"],
            "status": job["status"],
            "output": job["output"],
            "result": job["result"],
            "started_at": job["started_at"],
            "completed_at": job["completed_at"],
        },
    )


@route("DELETE", "/jobs/{job_id}")
def handle_cancel_job(handler, params):
    job = _cancel_job(params["job_id"])
    if not job:
        handler._send_json(404, {"error": "job not found"})
        return
    handler._send_json(
        200,
        {
            "job_id": job["job_id"],
            "status": job["status"],
        },
    )


@route("POST", "/commands/{command_path}")
def handle_dispatch_command(handler, params):
    """Dispatch a command job and return job_id + status."""
    command_path = params["command_path"]
    # Cancel any pending drain — but not for lightweight monitoring commands
    if _draining and command_path not in _SKIP_DRAIN:
        _drain_cancel.set()
    body = handler._read_body()
    status, response = _dispatch_job(command_path, body)
    handler._send_json(status, response)


# ── Param validation ──

import re

import ipaddress

_DOMAIN_RE = re.compile(r"^troshka-[a-f0-9]{8}-[a-f0-9]{8}$")
_UUID_RE = re.compile(r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")
_NET_NAME_RE = re.compile(r"^troshka-net-[a-f0-9]+$")
_BRIDGE_RE = re.compile(r"^br-(?:troshka-|bmc-)?[a-f0-9]+$")
_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
_URL_RE = re.compile(r"^https?://[a-zA-Z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+$")
_SOURCE_BRIDGE_RE = re.compile(r"source bridge='([^']+)'")
_BUS_TYPES = {"virtio", "scsi", "sata", "ide", "usb"}
_NET_MODELS = {"virtio", "e1000", "e1000e", "igb", "rtl8139"}


def _validate_domain_name(name):
    if not _DOMAIN_RE.match(name):
        raise ValueError(f"Invalid domain name: {name}")
    return name


def _validate_path(path):
    normalized = os.path.normpath(path)
    allowed_prefixes = [_TROSHKA_DIR + "/", "/opt/troshka/", "/var/log/troshka-"]
    mode = _config.get("storage_mode", "local")
    if mode == "shared":
        shared = _config.get("shared_mount", _SHARED_DIR)
        local = _config.get("local_mount", _LOCAL_DIR)
        allowed_prefixes.extend([shared + "/", local + "/", _TROSHKA_DIR + "/seeds/"])
    if not any(normalized.startswith(p) for p in allowed_prefixes):
        raise ValueError(f"Path must be under /var/lib/troshka/: {path}")
    if os.path.exists(normalized):
        real = os.path.realpath(normalized)
        if not any(real.startswith(p) for p in allowed_prefixes):
            raise ValueError(f"Path resolves outside allowed directories: {path}")
        return real
    return normalized


def _storage_path(category):
    """Resolve storage path by category based on storage mode.
    Categories: 'vms', 'images', 'cache/patterns', 'cache/snapshots', 'pxe', 'bmc', 'tmp', 'seeds'
    """
    mode = _config.get("storage_mode", "local")
    if mode == "shared":
        shared = _config.get("shared_mount", _SHARED_DIR)
        local = _config.get("local_mount", _LOCAL_DIR)
        shared_categories = {"vms", "images", "cache/snapshots"}
        local_categories = {"pxe", "bmc", "tmp", "cache/patterns"}
        if category in shared_categories:
            return os.path.join(shared, category)
        elif category in local_categories:
            return os.path.join(local, category)
        elif category == "seeds":
            return _TROSHKA_DIR + "/seeds"
        else:
            return os.path.join(shared, category)
    else:
        base = _TROSHKA_DIR
        if category == "seeds":
            return os.path.join(base, "vms")
        return os.path.join(base, category)


def _validate_url(url):
    if not _URL_RE.match(url):
        raise ValueError(f"Invalid URL: {url}")
    return url


def _validate_ip(ip_str):
    try:
        ipaddress.ip_address(ip_str)
        return ip_str
    except ValueError:
        raise ValueError(f"Invalid IP address: {ip_str}")


def _validate_cidr(cidr_str):
    try:
        ipaddress.ip_network(cidr_str, strict=False)
        return cidr_str
    except ValueError:
        raise ValueError(f"Invalid CIDR: {cidr_str}")


def _validate_mac(mac):
    if not _MAC_RE.match(mac):
        raise ValueError(f"Invalid MAC address: {mac}")
    return mac


def _validate_bus(bus):
    if bus not in _BUS_TYPES:
        raise ValueError(f"Invalid bus type: {bus}")
    return bus


def _validate_net_model(model):
    if model not in _NET_MODELS:
        raise ValueError(f"Invalid network model: {model}")
    return model


def _validate_network_name(name):
    if not _NET_NAME_RE.match(name):
        raise ValueError(f"Invalid network name: {name}")
    return name


def _validate_bridge_name(name):
    if not _BRIDGE_RE.match(name):
        raise ValueError(f"Invalid bridge name: {name}")
    return name


def _validate_project_id(pid):
    if not _UUID_RE.match(pid):
        raise ValueError(f"Invalid project ID: {pid}")
    return pid


def _job_log(job, msg):
    """Append a line to job output and log to systemd."""
    job["output"].append(msg)
    logger.info("[%s] %s", job["job_id"][:8], msg)


def _run_cmd(job, cmd, timeout=600, check=True, capture_output=True):
    """Run a subprocess command, appending output to job. Stores process handle in job for drain.

    capture_output=False must be used for commands that self-daemonize (double-fork
    and detach into the background, e.g. dnsmasq, chronyd -f, haproxy -D). A piped
    stdout/stderr is inherited by the detached grandchild, so communicate() blocks
    waiting for EOF that never arrives -- even though the process we're tracking
    already exited successfully -- until the timeout fires and kills it, turning a
    successful daemon start into a spurious "Command timed out" failure. Temp files
    avoid this: we only read them after the tracked process exits, so a lingering
    grandchild fd on them doesn't block anything.
    """
    _job_log(job, f"$ {' '.join(cmd)}")
    out_f = err_f = None
    if capture_output:
        stdout_dst, stderr_dst = subprocess.PIPE, subprocess.PIPE
    else:
        out_f = tempfile.TemporaryFile(mode="w+")
        err_f = tempfile.TemporaryFile(mode="w+")
        stdout_dst, stderr_dst = out_f, err_f
    proc = subprocess.Popen(cmd, stdout=stdout_dst, stderr=stderr_dst, text=True)
    job["_process"] = proc
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise RuntimeError(f"Command timed out after {timeout}s: {' '.join(cmd)}")
    finally:
        job["_process"] = None
        if out_f:
            out_f.seek(0)
            stdout = out_f.read()
            out_f.close()
        if err_f:
            err_f.seek(0)
            stderr = err_f.read()
            err_f.close()
    if stdout:
        for line in stdout.strip().split("\n"):
            _job_log(job, line)
    if stderr:
        for line in stderr.strip().split("\n"):
            _job_log(job, line)
    if check and proc.returncode != 0:
        raise RuntimeError(f"Command failed (exit {proc.returncode}): {' '.join(cmd)}")
    return proc


def _chown_qemu(path):
    """Set file/dir ownership to qemu:qemu so libvirt can access it."""
    import pwd

    try:
        qemu_uid = pwd.getpwnam("qemu").pw_uid
        qemu_gid = pwd.getpwnam("qemu").pw_gid
        os.chown(path, qemu_uid, qemu_gid)
    except (KeyError, OSError):
        pass


# ── VM handlers ──


def _prepare_disk_link(job, disk):
    """Create symlink or copy for a disk referencing a source file.

    Returns the validated destination path.
    """
    path = _validate_path(disk["path"])
    link_from = disk.get("symlink_from")
    if not link_from:
        return path
    link_from = _validate_path(link_from)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        if link_from.endswith(".iso"):
            os.symlink(link_from, path)
            _job_log(job, f"Symlinked {os.path.basename(path)}")
        else:
            src_size = os.path.getsize(link_from)
            _job_log(
                job,
                f"Copying {os.path.basename(link_from)} ({round(src_size / (1024**3), 1)} GB)...",
            )
            shutil.copy2(link_from, path)
            _job_log(job, f"Copied {os.path.basename(path)}")
        _chown_qemu(path)
    except FileExistsError:
        pass
    return path


def _build_disk_arg(path, disk, disk_cache):
    """Build a virt-install ``--disk`` argument string for a single disk."""
    bus = _validate_bus(disk.get("bus", "virtio"))
    device = disk.get("device", "disk")
    disk_arg = f"path={path},bus={bus}"
    rotation_rate = disk.get("rotation_rate")
    if rotation_rate and bus in ("scsi", "sata", "ide"):
        disk_arg += f",rotation_rate={int(rotation_rate)}"
    if disk_cache:
        disk_arg += f",cache={disk_cache}"
        if disk_cache == "none":
            disk_arg += ",io=native"
    if device == "cdrom":
        disk_arg += ",device=cdrom"
    return disk_arg


def _build_boot_parts(firmware, secure_boot, boot_devs):
    boot_parts = []
    if firmware == "uefi":
        if secure_boot:
            boot_parts.append("uefi")
        else:
            boot_parts.append("loader=/usr/share/edk2/ovmf/OVMF_CODE.fd")
            boot_parts.append("loader.readonly=yes")
            boot_parts.append("loader.type=pflash")
            boot_parts.append("loader.secure=no")
            boot_parts.append("nvram.template=/usr/share/edk2/ovmf/OVMF_VARS.fd")
    if boot_devs:
        boot_parts.extend(boot_devs)
    else:
        boot_parts.append("hd")
    boot_parts.append("menu=on")
    return boot_parts


def _handle_vm_create(job, params):
    domain = _validate_domain_name(params["domain_name"])
    vcpus = int(params["vcpus"])
    ram_mb = int(params["ram_mb"])
    disks = params.get("disks", [])
    networks = params.get("networks", [])
    seed_iso = params.get("seed_iso")
    firmware = params.get("firmware", "bios")
    secure_boot = params.get("secure_boot", False)
    boot_devs = params.get("boot_devs", [])
    video_model = params.get("video_model", "virtio")
    input_model = params.get("input_model", "virtio")
    domain_uuid = params.get("uuid")
    clock_offset = params.get("clock_offset")

    cmd = [
        "virt-install",
        "--name",
        domain,
        "--vcpus",
        str(vcpus),
        "--memory",
        str(ram_mb),
        "--os-variant",
        "generic",
        "--noautoconsole",
        "--noreboot",
        "--check",
        "mac_in_use=off",
    ]

    if firmware == "uefi":
        cmd.extend(["--machine", "q35"])

    _hwuuid = domain_uuid

    boot_parts = _build_boot_parts(firmware, secure_boot, boot_devs)
    cmd.extend(["--boot", ",".join(boot_parts)])
    cmd.extend(["--install", "no_install=yes"])
    disk_cache = params.get("disk_cache")
    for disk in disks:
        path = _prepare_disk_link(job, disk)
        disk_arg = _build_disk_arg(path, disk, disk_cache)
        cmd.extend(["--disk", disk_arg])
    for net in networks:
        bridge = _validate_bridge_name(net.get("bridge", "br-troshka-00000000"))
        model = _validate_net_model(net.get("model", "virtio"))
        mac = net.get("mac", "")
        net_arg = f"bridge={bridge},model={model}"
        if mac:
            net_arg += f",mac={_validate_mac(mac)}"
        cmd.extend(["--network", net_arg])
    if seed_iso:
        cmd.extend(["--disk", f"path={_validate_path(seed_iso)},device=cdrom,bus=sata"])
    if video_model in ("virtio", "vga", "qxl"):
        cmd.extend(["--video", video_model])
    # Force VNC graphics explicitly — without this, virt-install's
    # osinfo-based defaults can pick spice on some hosts, which the
    # console proxy (troshka-vncd) can't talk to.
    cmd.extend(["--graphics", "vnc,listen=127.0.0.1"])
    if input_model == "virtio":
        cmd.extend(["--input", "type=keyboard,bus=virtio"])
        cmd.extend(["--input", "type=tablet,bus=virtio"])
    cmd.extend(
        ["--channel", "unix,target.type=virtio,target.name=org.qemu.guest_agent.0"]
    )
    if clock_offset is not None:
        cmd.extend(["--clock", f"offset=variable,adjustment={int(clock_offset)}"])
    _run_cmd(job, cmd, timeout=600)

    if _hwuuid:
        import xml.etree.ElementTree as ET

        xml_str = subprocess.check_output(
            ["virsh", "dumpxml", domain], text=True, timeout=10
        )
        root = ET.fromstring(xml_str)
        uuid_elem = root.find("uuid")
        hwuuid_elem = ET.Element("hwuuid")
        hwuuid_elem.text = _hwuuid
        root.insert(list(root).index(uuid_elem) + 1, hwuuid_elem)
        tmp = f"/tmp/troshka-hwuuid-{domain}.xml"
        ET.ElementTree(root).write(tmp, xml_declaration=False)
        _run_cmd(job, ["virsh", "define", tmp], timeout=10)
        os.unlink(tmp)
        _job_log(job, f"Set hwuuid={_hwuuid} on {domain}")

    # Return the auto-generated domain UUID so the backend can store it
    dom_uuid = ""
    try:
        dom_uuid = subprocess.check_output(
            ["virsh", "domuuid", domain], text=True, timeout=10
        ).strip()
    except Exception:
        pass

    return {"domain": domain, "status": "created", "domain_uuid": dom_uuid}


COMMAND_HANDLERS["vms/create"] = _handle_vm_create


_IMAGE_CACHE_DIRS = (f"{_TROSHKA_DIR}/images/", f"{_SHARED_DIR}/images/")


def _remove_disk_file(job, path):
    """Delete a single disk file, trying qemu user first then root."""
    try:
        subprocess.run(
            ["sudo", "-u", "qemu", "rm", "-f", "--", path],
            timeout=5,
            check=True,
        )
        _job_log(job, f"Deleted disk: {path}")
    except FileNotFoundError:
        pass
    except Exception:
        try:
            os.remove(path)
            _job_log(job, f"Deleted disk (root): {path}")
        except Exception:
            _job_log(job, f"Warning: could not delete {path}")


def _delete_vm_disks(job, domain):
    """Delete disk files for a domain before undefining it.
    Files are owned by qemu:qemu, so delete as qemu user to avoid NFS root_squash issues.
    Never deletes shared library images from the image cache.
    """
    try:
        result = subprocess.run(
            ["virsh", "domblklist", domain, "--details"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        for line in result.stdout.strip().split("\n"):
            parts = line.split()
            if len(parts) < 4 or parts[1] != "disk" or not parts[3].startswith("/"):
                continue
            path = parts[3]
            if any(path.startswith(d) for d in _IMAGE_CACHE_DIRS):
                _job_log(job, f"Skipped shared image: {path}")
                continue
            _remove_disk_file(job, path)
    except Exception:
        _job_log(
            job, "Warning: could not list domain disks, undefine may leave orphan files"
        )


def _handle_vm_destroy(job, params):
    domain = _validate_domain_name(params["domain_name"])
    # Destroy (force stop) — may fail if already stopped, that's OK
    try:
        _run_cmd(job, ["virsh", "destroy", domain], timeout=30)
    except RuntimeError:
        _job_log(job, "Domain may already be stopped, continuing with undefine")
    _delete_vm_disks(job, domain)
    _run_cmd(job, ["virsh", "undefine", domain, "--nvram"], timeout=30)
    return {"domain": domain, "status": "destroyed"}


COMMAND_HANDLERS["vms/destroy"] = _handle_vm_destroy


def _handle_vm_force_off(job, params):
    domain = _validate_domain_name(params["domain_name"])
    _run_cmd(job, ["virsh", "destroy", domain], timeout=30)
    return {"domain": domain, "status": "off"}


COMMAND_HANDLERS["vms/force-off"] = _handle_vm_force_off


def _handle_vm_start(job, params):
    domain = _validate_domain_name(params["domain_name"])
    # Ensure all bridges referenced in VM XML exist in host namespace
    import re as _re

    xml_result = subprocess.run(
        ["virsh", "dumpxml", "--inactive", domain],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if xml_result.returncode == 0:
        for bridge in _SOURCE_BRIDGE_RE.findall(xml_result.stdout):
            check = subprocess.run(
                ["ip", "link", "show", bridge], capture_output=True, timeout=5
            )
            if check.returncode != 0:
                subprocess.run(
                    ["ip", "link", "add", bridge, "type", "bridge"],
                    capture_output=True,
                    timeout=5,
                )
                subprocess.run(
                    [
                        "ip",
                        "link",
                        "set",
                        bridge,
                        "type",
                        "bridge",
                        "forward_delay",
                        "99",
                        "ageing_time",
                        "0",
                    ],
                    capture_output=True,
                    timeout=5,
                )
                subprocess.run(
                    ["ip", "link", "set", bridge, "up"], capture_output=True, timeout=5
                )
                _job_log(job, f"Created missing dummy bridge {bridge}")
    _run_cmd(job, ["virsh", "start", domain], timeout=60)
    return {"domain": domain, "status": "started"}


COMMAND_HANDLERS["vms/start"] = _handle_vm_start


def _handle_vm_stop(job, params):
    domain = _validate_domain_name(params["domain_name"])
    grace = params.get("timeout", 30)
    # Graceful shutdown via ACPI
    try:
        _run_cmd(job, ["virsh", "shutdown", domain], timeout=60)
    except RuntimeError:
        pass
    # Wait for VM to stop
    import time

    for _ in range(grace):
        time.sleep(1)
        result = subprocess.run(
            ["virsh", "domstate", domain], capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0 or result.stdout.strip() in ("shut off", ""):
            return {"domain": domain, "status": "stopped", "method": "shutdown"}
    # Force destroy if graceful shutdown didn't work
    _job_log(job, f"Graceful shutdown timed out after {grace}s, forcing destroy")
    try:
        _run_cmd(job, ["virsh", "destroy", domain], timeout=30)
    except RuntimeError:
        pass
    return {"domain": domain, "status": "stopped", "method": "destroy"}


COMMAND_HANDLERS["vms/stop"] = _handle_vm_stop


def _handle_vm_reboot(job, params):
    domain = _validate_domain_name(params["domain_name"])
    _run_cmd(job, ["virsh", "reboot", domain], timeout=60)
    return {"domain": domain, "status": "rebooted"}


COMMAND_HANDLERS["vms/reboot"] = _handle_vm_reboot


def _handle_vm_state(job, params):
    """Get VM state via virsh domstate."""
    domain = _validate_domain_name(params["domain_name"])
    result = subprocess.run(
        ["virsh", "domstate", domain],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        # Domain not found or other error
        return {"domain": domain, "state": "not_found"}
    raw_state = result.stdout.strip().lower().replace(" ", "_")
    # Normalize virsh state names to match libvirt_mgr conventions
    state_map = {
        "running": "running",
        "shut_off": "shut_off",
        "paused": "paused",
        "in_shutdown": "shutting_down",
        "crashed": "crashed",
        "pmsuspended": "suspended",
        "idle": "unknown",
    }
    state = state_map.get(raw_state, raw_state)

    # Also get boot order from domain XML
    boot_devs = []
    try:
        xml_result = subprocess.run(
            ["virsh", "dumpxml", "--inactive", domain],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if xml_result.returncode == 0:
            import xml.etree.ElementTree as ET

            root = ET.fromstring(xml_result.stdout)
            for boot_el in root.findall(".//os/boot"):
                dev = boot_el.get("dev")
                if dev:
                    boot_devs.append(dev)
    except Exception:
        pass

    return {"domain": domain, "state": state, "boot_devs": boot_devs}


COMMAND_HANDLERS[_VMS_STATE_CMD] = _handle_vm_state


def _handle_vm_list(job, params):
    """List all troshka domains with their states."""
    result = subprocess.run(
        ["virsh", "list", "--all", "--name"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"virsh list failed: {result.stderr}")
    domains = []
    for name in result.stdout.strip().split("\n"):
        name = name.strip()
        if not name or not name.startswith("troshka-"):
            continue
        # Get state for each domain
        state_result = subprocess.run(
            ["virsh", "domstate", name],
            capture_output=True,
            text=True,
            timeout=5,
        )
        state = "unknown"
        if state_result.returncode == 0:
            raw = state_result.stdout.strip().lower().replace(" ", "_")
            state_map = {
                "running": "running",
                "shut_off": "shut_off",
                "paused": "paused",
                "in_shutdown": "shutting_down",
                "crashed": "crashed",
                "pmsuspended": "suspended",
            }
            state = state_map.get(raw, raw)
        domains.append({"name": name, "state": state})
    return {"domains": domains}


COMMAND_HANDLERS["vms/list"] = _handle_vm_list


def _handle_vm_vnc_port(job, params):
    """Get VNC port for a VM by parsing its XML."""
    import xml.etree.ElementTree as ET

    domain = _validate_domain_name(params["domain_name"])
    result = subprocess.run(
        ["virsh", "dumpxml", domain],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        return {"domain": domain, "vnc_port": None}
    root = ET.fromstring(result.stdout)
    graphics = root.find(".//graphics[@type='vnc']")
    vnc_port = None
    if graphics is not None:
        port = graphics.get("port")
        if port and port != "-1":
            vnc_port = int(port)
    return {"domain": domain, "vnc_port": vnc_port}


COMMAND_HANDLERS["vms/vnc-port"] = _handle_vm_vnc_port


def _collect_device_boot_entries(devices):
    """Collect per-device ``<boot order="N">`` entries from ``<devices>``.

    Returns a list of ``(order, dev_type)`` tuples.
    """
    entries = []
    if devices is None:
        return entries
    for dev_elem in devices:
        boot_child = dev_elem.find("boot")
        if boot_child is None:
            continue
        order = int(boot_child.get("order", 999))
        if dev_elem.tag == "disk":
            dev_type = "cdrom" if dev_elem.get("device") == "cdrom" else "hd"
        elif dev_elem.tag == "interface":
            dev_type = "network"
        else:
            continue
        entries.append((order, dev_type))
    return entries


def _parse_boot_devices(root):
    """Extract boot device list from a libvirt XML root element.

    Checks ``<os><boot dev="..."/>`` first.  Falls back to per-device
    ``<boot order="N">`` elements inside ``<devices>`` when the os/boot
    elements are absent.

    Returns a list of device type strings (e.g. ``["hd", "network"]``).
    """
    boot_devs = [b.get("dev") for b in root.findall(".//os/boot")]
    if boot_devs:
        return boot_devs

    dev_boots = _collect_device_boot_entries(root.find("devices"))

    if not dev_boots:
        return []

    seen = set()
    result = []
    for _, dt in sorted(dev_boots):
        if dt not in seen:
            result.append(dt)
            seen.add(dt)
    return result


def _parse_memory(root):
    """Extract RAM in MB from a libvirt XML root element.

    Reads ``<memory unit="...">`` and converts to megabytes.
    Defaults to KiB when no unit attribute is present.
    """
    mem_elem = root.find("memory")
    if mem_elem is None:
        return 0
    mem_val = int(mem_elem.text)
    if mem_elem.get("unit", "KiB") == "KiB":
        return mem_val // 1024
    return mem_val


def _handle_vm_config(job, params):
    """Get VM config from inactive XML — structured dict."""
    domain = _validate_domain_name(params["domain_name"])
    result = subprocess.run(
        ["virsh", "dumpxml", "--inactive", domain],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to get XML for {domain}: {result.stderr}")

    root = ET.fromstring(result.stdout)

    boot_devs = _parse_boot_devices(root)
    vcpus = int(root.findtext("vcpu", "0"))
    ram_mb = _parse_memory(root)

    nics = []
    for iface in root.findall(".//interface"):
        source = iface.find("source")
        mac = iface.find("mac")
        nics.append(
            {
                "bridge": source.get("bridge", "") if source is not None else "",
                "mac": mac.get("address", "") if mac is not None else "",
            }
        )

    disks = []
    cdroms = []
    for disk in root.findall(".//disk"):
        source = disk.find("source")
        path = source.get("file", "") if source is not None else ""
        if disk.get("device") == "cdrom":
            cdroms.append(path)
        else:
            disks.append(path)

    return {
        "boot_devs": boot_devs,
        "vcpus": vcpus,
        "ram_mb": ram_mb,
        "nics": nics,
        "disks": disks,
        "cdroms": cdroms,
    }


COMMAND_HANDLERS["vms/config"] = _handle_vm_config


def _hot_attach_new_disks(job, domain, disks, cur_root):
    """Hot-attach new disks to a running VM without restart.

    Returns True if hot-attach was performed, False otherwise.
    """
    cur_disk_paths = {
        d.find("source").get("file")
        for d in cur_root.find("devices").findall("disk")
        if d.get("device") != "cdrom" and d.find("source") is not None
    }
    new_disks = [d for d in disks if d["path"] not in cur_disk_paths]
    if not new_disks:
        return False
    target_letters = "bcdefghijklmnop"
    used = {
        d.find("target").get("dev")
        for d in cur_root.find("devices").findall("disk")
        if d.find("target") is not None
    }
    for d in new_disks:
        tgt = None
        for letter in target_letters:
            dev = f"vd{letter}"
            if dev not in used:
                tgt = dev
                used.add(dev)
                break
        if not tgt:
            continue
        _run_cmd(
            job,
            [
                "virsh",
                "attach-disk",
                domain,
                d["path"],
                tgt,
                "--driver",
                "qemu",
                "--subdriver",
                d.get("format", "qcow2"),
                "--targetbus",
                d.get("bus", "virtio"),
                "--persistent",
            ],
            timeout=30,
        )
        _job_log(job, f"Hot-attached {d['path']} as {tgt} to {domain}")
    return True


def _reconfigure_boot_devs(root, boot_devs):
    """Replace boot device entries in libvirt XML."""
    os_elem = root.find("os")
    for boot in os_elem.findall("boot"):
        os_elem.remove(boot)
    type_elem = os_elem.find("type")
    insert_idx = list(os_elem).index(type_elem) + 1
    for i, dev in enumerate(boot_devs):
        boot_elem = ET.Element("boot")
        boot_elem.set("dev", dev)
        os_elem.insert(insert_idx + i, boot_elem)
    # Strip per-device boot orders — can't mix with os/boot elements
    devices = root.find("devices")
    if devices is None:
        return
    for dev_elem in devices:
        boot_child = dev_elem.find("boot")
        if boot_child is not None:
            dev_elem.remove(boot_child)


def _reconfigure_nics(root, nics):
    """Replace all NIC interfaces in libvirt XML."""
    devices = root.find("devices")
    for iface in devices.findall("interface"):
        devices.remove(iface)
    for nic in nics:
        iface = ET.SubElement(devices, "interface")
        iface.set("type", "bridge")
        source = ET.SubElement(iface, "source")
        source.set("bridge", nic["bridge"])
        if nic.get("mac"):
            mac_elem = ET.SubElement(iface, "mac")
            mac_elem.set("address", nic["mac"])
        model = ET.SubElement(iface, "model")
        model.set("type", nic.get("model", "virtio"))


def _add_disk_element(job, devices, disk_info, used_targets, domain):
    import xml.etree.ElementTree as ET

    target_letters = "bcdefghijklmnop"
    target_dev = None
    for letter in target_letters:
        dev_name = f"vd{letter}"
        if dev_name not in used_targets:
            target_dev = dev_name
            used_targets.add(dev_name)
            break
    if not target_dev:
        return

    disk_elem = ET.SubElement(devices, "disk")
    disk_elem.set("type", "file")
    disk_elem.set("device", "disk")
    driver = ET.SubElement(disk_elem, "driver")
    driver.set("name", "qemu")
    driver.set("type", disk_info.get("format", "qcow2"))
    source = ET.SubElement(disk_elem, "source")
    source.set("file", disk_info["path"])
    target = ET.SubElement(disk_elem, "target")
    target.set("dev", target_dev)
    disk_bus = disk_info.get("bus", "virtio")
    target.set("bus", disk_bus)
    rr = disk_info.get("rotation_rate")
    if rr and disk_bus in ("scsi", "sata", "ide"):
        target.set("rotation_rate", str(int(rr)))
    _job_log(job, f"Added disk {disk_info['path']} as {target_dev} to {domain}")


def _collect_existing_disk_paths(existing_disks):
    """Return set of file paths from existing disk elements."""
    paths = set()
    for d in existing_disks:
        source = d.find("source")
        if source is not None and source.get("file"):
            paths.add(source.get("file"))
    return paths


def _reconfigure_disks(job, root, domain, disks):
    """Synchronize disk entries in libvirt XML with the desired list."""
    devices = root.find("devices")
    existing_disks = devices.findall("disk") if devices is not None else []
    existing_paths = _collect_existing_disk_paths(existing_disks)

    desired_paths = {d["path"] for d in disks}

    for d in existing_disks:
        if d.get("device") == "cdrom":
            continue
        source = d.find("source")
        path = source.get("file") if source is not None else None
        if path and path not in desired_paths:
            devices.remove(d)
            _job_log(job, f"Removed disk {path} from {domain}")

    used_targets = {
        d.find("target").get("dev")
        for d in devices.findall("disk")
        if d.find("target") is not None
    }
    for disk_info in disks:
        if disk_info["path"] in existing_paths:
            continue
        _add_disk_element(job, devices, disk_info, used_targets, domain)


def _add_cdrom_element(job, devices, path, cdrom_bus, dev_prefix, used_targets, domain):
    import xml.etree.ElementTree as ET

    target_letters_cd = "abcdefghijklmnop"
    target_dev = None
    for letter in target_letters_cd:
        dev_name = f"{dev_prefix}{letter}"
        if dev_name not in used_targets:
            target_dev = dev_name
            used_targets.add(dev_name)
            break
    if not target_dev:
        return
    disk_elem = ET.SubElement(devices, "disk")
    disk_elem.set("type", "file")
    disk_elem.set("device", "cdrom")
    source = ET.SubElement(disk_elem, "source")
    source.set("file", path)
    target = ET.SubElement(disk_elem, "target")
    target.set("dev", target_dev)
    target.set("bus", cdrom_bus)
    ET.SubElement(disk_elem, "readonly")
    _job_log(job, f"Updated cdrom {path} on {domain} (bus={cdrom_bus})")


def _reconfigure_cdroms(job, root, domain, cdroms):
    """Synchronize CDROM entries in libvirt XML with the desired list."""
    devices = root.find("devices")
    existing_cdroms = [
        d
        for d in (devices.findall("disk") if devices is not None else [])
        if d.get("device") == "cdrom"
    ]
    desired_set = set(cdroms)
    existing_set = set()
    cdrom_bus = "sata"
    for cd in existing_cdroms:
        src = cd.find("source")
        existing_set.add(src.get("file", "") if src is not None else "")
        tgt = cd.find("target")
        if tgt is not None and tgt.get("bus"):
            cdrom_bus = tgt.get("bus")

    if existing_set == desired_set:
        return
    for cd in existing_cdroms:
        devices.remove(cd)
    if cdrom_bus == "sata":
        dev_prefix = "sd"
    elif cdrom_bus == "ide":
        dev_prefix = "hd"
    else:
        dev_prefix = "vd"
    used_targets = {
        d.find("target").get("dev")
        for d in devices.findall("disk")
        if d.find("target") is not None
    }
    for path in cdroms:
        _add_cdrom_element(job, devices, path, cdrom_bus, dev_prefix, used_targets, domain)


def _try_hot_attach_disks(job, domain, disks, vcpus, ram_mb, nics, was_active, restart):
    import xml.etree.ElementTree as ET

    if was_active and disks is not None and not restart:
        cur_xml = subprocess.run(
            ["virsh", "dumpxml", domain], capture_output=True, text=True, timeout=10
        ).stdout
        cur_root = ET.fromstring(cur_xml)
        if not vcpus and not ram_mb and not nics:
            if _hot_attach_new_disks(job, domain, disks, cur_root):
                return {"domain": domain, "status": "reconfigured", "restarted": False}
    return None


def _configure_vnc_graphics(root, vnc_listen):
    import xml.etree.ElementTree as ET

    devices = root.find("devices")
    graphics = (
        devices.find("graphics[@type='vnc']") if devices is not None else None
    )
    if graphics is not None:
        graphics.set("listen", vnc_listen)
        graphics.set("sharePolicy", "force-shared")
        listen_elem = graphics.find("listen")
        if listen_elem is not None:
            listen_elem.set("address", vnc_listen)
    elif devices is not None:
        graphics = ET.SubElement(devices, "graphics")
        graphics.set("type", "vnc")
        graphics.set("port", "-1")
        graphics.set("autoport", "yes")
        graphics.set("listen", vnc_listen)
        graphics.set("sharePolicy", "force-shared")
        listen_sub = ET.SubElement(graphics, "listen")
        listen_sub.set("type", "address")
        listen_sub.set("address", vnc_listen)


def _apply_vcpu_ram_changes(root, vcpus, ram_mb):
    """Apply vCPU and RAM changes to libvirt XML root."""
    if vcpus is not None:
        vcpu_elem = root.find("vcpu")
        vcpu_elem.text = str(vcpus)
        vcpu_elem.set("placement", "static")
    if ram_mb is not None:
        ram_kib = ram_mb * 1024
        mem = root.find("memory")
        mem.text = str(ram_kib)
        mem.set("unit", "KiB")
        cur_mem = root.find("currentMemory")
        if cur_mem is not None:
            cur_mem.text = str(ram_kib)
            cur_mem.set("unit", "KiB")


def _handle_vm_reconfigure(job, params):
    """Reconfigure a VM: modify XML and redefine.

    Reimplements libvirt_mgr.reconfigure_vm() using virsh + XML parsing.
    """
    import xml.etree.ElementTree as ET

    domain = _validate_domain_name(params["domain_name"])
    boot_devs = params.get("boot_devs")
    vcpus = params.get("vcpus")
    ram_mb = params.get("ram_mb")
    nics = params.get("nics")
    disks = params.get("disks")
    cdroms = params.get("cdroms")
    vnc_listen = params.get("vnc_listen", "127.0.0.1")
    restart = params.get("restart", True)

    state_result = subprocess.run(
        ["virsh", "domstate", domain],
        capture_output=True,
        text=True,
        timeout=10,
    )
    was_active = (
        state_result.returncode == 0 and "running" in state_result.stdout.lower()
    )

    hot_attach_result = _try_hot_attach_disks(job, domain, disks, vcpus, ram_mb, nics, was_active, restart)
    if hot_attach_result is not None:
        return hot_attach_result

    if restart and was_active:
        _run_cmd(job, ["virsh", "destroy", domain], timeout=30)

    # Get inactive XML
    result = subprocess.run(
        ["virsh", "dumpxml", "--inactive", domain],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to get XML for {domain}: {result.stderr}")

    root = ET.fromstring(result.stdout)

    if boot_devs is not None:
        _reconfigure_boot_devs(root, boot_devs)

    _apply_vcpu_ram_changes(root, vcpus, ram_mb)

    if nics is not None:
        _reconfigure_nics(root, nics)

    if disks is not None:
        _reconfigure_disks(job, root, domain, disks)

    if cdroms is not None:
        _reconfigure_cdroms(job, root, domain, cdroms)

    if vnc_listen:
        _configure_vnc_graphics(root, vnc_listen)

    # Write new XML via virsh define
    new_xml = ET.tostring(root, encoding="unicode")
    proc = subprocess.Popen(
        ["virsh", "define", "/dev/stdin"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _, stderr = proc.communicate(input=new_xml, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(f"virsh define failed: {stderr}")
    _job_log(job, f"Redefined {domain}")

    restarted = False
    if restart and was_active:
        _run_cmd(job, ["virsh", "start", domain], timeout=60)
        restarted = True
        _job_log(job, f"Reconfigured and restarted {domain}")
    else:
        _job_log(job, f"Reconfigured {domain}")

    return {"domain": domain, "status": "reconfigured", "restarted": restarted}


COMMAND_HANDLERS["vms/reconfigure"] = _handle_vm_reconfigure


def _handle_vm_undefine(job, params):
    """Undefine a VM: force stop if running, delete disks, then undefine."""
    domain = _validate_domain_name(params["domain_name"])
    remove_storage = params.get("remove_storage", True)

    # Destroy if running (ignore errors)
    try:
        _run_cmd(job, ["virsh", "destroy", domain], timeout=30)
    except RuntimeError:
        _job_log(job, f"Domain {domain} may already be stopped")

    if remove_storage:
        _delete_vm_disks(job, domain)

    _run_cmd(job, ["virsh", "undefine", domain, "--nvram"], timeout=30)
    return {"domain": domain, "status": "undefined"}


COMMAND_HANDLERS["vms/undefine"] = _handle_vm_undefine


def _push_target_time_to_guest(job, domain, target_epoch):
    """Push a specific epoch timestamp to a running VM's guest agent.

    Tries ``virsh domtime`` first, then falls back to ``guest-exec date -s``.
    Returns True if time was successfully pushed.
    """
    try:
        ga_result = subprocess.run(
            ["virsh", "domtime", domain, "--set", "--time", str(target_epoch)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if ga_result.returncode == 0:
            _job_log(job, f"Set time via guest agent on {domain}")
            return True
    except Exception:
        pass

    try:
        exec_result = subprocess.run(
            [
                "virsh",
                "qemu-agent-command",
                domain,
                '{"execute":"guest-exec","arguments":{"path":"/usr/bin/date","arg":["-s","@'
                + str(target_epoch)
                + '"],"capture-output":true}}',
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if exec_result.returncode == 0:
            _job_log(job, f"Set time via guest-exec on {domain}")
            return True
    except Exception:
        _job_log(job, f"Could not push time to {domain} (no guest agent)")
    return False


def _push_real_time_to_guest(job, domain):
    """Push current real UTC time to a running VM.

    Returns True if time was successfully pushed.
    """
    import time

    real_epoch = int(time.time())
    try:
        subprocess.run(
            ["virsh", "domtime", domain, "--set", "--time", str(real_epoch)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        _job_log(job, f"Reset time to real UTC on {domain}")
        return True
    except Exception:
        return False


def _update_clock_element(clock_elem, offset_seconds):
    if offset_seconds is not None:
        clock_elem.set("offset", "variable")
        clock_elem.set("adjustment", str(int(offset_seconds)))
        if "basis" in clock_elem.attrib:
            del clock_elem.attrib["basis"]
    else:
        clock_elem.set("offset", "utc")
        for attr in ("adjustment", "basis"):
            if attr in clock_elem.attrib:
                del clock_elem.attrib[attr]


def _handle_vm_set_clock(job, params):
    """Update a VM's clock offset in libvirt XML and push time to guest."""
    import xml.etree.ElementTree as ET

    domain = _validate_domain_name(params["domain_name"])
    offset_seconds = params.get("offset_seconds")
    target_epoch = params.get("target_epoch")

    state_result = subprocess.run(
        ["virsh", "domstate", domain],
        capture_output=True,
        text=True,
        timeout=10,
    )
    is_running = (
        state_result.returncode == 0 and "running" in state_result.stdout.lower()
    )

    result = subprocess.run(
        ["virsh", "dumpxml", "--inactive", domain],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to get XML for {domain}: {result.stderr}")

    root = ET.fromstring(result.stdout)

    clock_elem = root.find("clock")
    if clock_elem is None:
        clock_elem = ET.SubElement(root, "clock")

    _update_clock_element(clock_elem, offset_seconds)

    new_xml = ET.tostring(root, encoding="unicode")
    proc = subprocess.Popen(
        ["virsh", "define", "/dev/stdin"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _, stderr = proc.communicate(input=new_xml, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(f"virsh define failed: {stderr}")
    _job_log(job, f"Updated clock XML for {domain}")

    pushed = False
    if is_running and target_epoch is not None:
        pushed = _push_target_time_to_guest(job, domain, target_epoch)
    elif is_running and offset_seconds is None:
        pushed = _push_real_time_to_guest(job, domain)

    return {
        "domain": domain,
        "status": "clock_updated",
        "xml_updated": True,
        "time_pushed": pushed,
    }


COMMAND_HANDLERS["vms/set-clock"] = _handle_vm_set_clock


def _build_single_guestfish_command(op):
    action = op.get("action", "")
    path = op.get("path", "")
    if action in ("rm-rf", "rm-f", "mkdir-p"):
        return f"{action} {path}"
    elif action == "write":
        content = op.get("content", "")
        return f'write {path} "{content}"'
    elif action == "upload":
        local_path = op.get("local_path", "")
        if not local_path:
            raise RuntimeError("local_path required for upload")
        return f"upload {local_path} {path}"
    elif action == "chmod":
        mode = op.get("mode", "")
        if not mode:
            raise RuntimeError("mode required for chmod")
        return f"chmod {mode} {path}"
    return ""


def _build_guestfish_commands(operations):
    """Validate operations and build guestfish command lines.

    Returns a list of guestfish command strings.
    Raises RuntimeError for invalid operations.
    """
    ALLOWED_ACTIONS = {"rm-rf", "rm-f", "mkdir-p", "write", "upload", "chmod"}
    cmds = []
    for op in operations:
        action = op.get("action", "")
        if action not in ALLOWED_ACTIONS:
            raise RuntimeError(f"unsupported action: {action}")
        path = op.get("path", "")
        if not path:
            raise RuntimeError(f"path required for action: {action}")
        cmd = _build_single_guestfish_command(op)
        if cmd:
            cmds.append(cmd)
    return cmds


def _handle_vm_modify_fs(job, params):
    """Modify a guest filesystem offline using guestfish.

    Params:
        disk: path to qcow2 disk image (must not be in use by a running VM)
        operations: list of dicts, each with 'action' and action-specific fields
            - rm-rf: remove directory recursively (path)
            - rm-f: remove file, no error if missing (path)
            - mkdir-p: create directory with parents (path)
            - write: write content to file (path, content)
            - upload: upload local file to guest (local_path, path)
            - chmod: change permissions (mode, path)
    """
    disk = params.get("disk", "")
    operations = params.get("operations", [])
    if not disk or not operations:
        raise RuntimeError("disk and operations are required")
    if not os.path.exists(disk):
        raise RuntimeError(f"disk not found: {disk}")

    guestfish_cmds = _build_guestfish_commands(operations)
    script = "\n".join(guestfish_cmds) + "\n"
    _job_log(job, f"Running guestfish on {disk} ({len(operations)} operations)")

    result = subprocess.run(
        ["guestfish", "--rw", "-a", disk, "-i"],
        input=script,
        capture_output=True,
        text=True,
        timeout=120,
    )

    results = []
    if result.returncode == 0:
        for op in operations:
            results.append(
                {"action": op["action"], "path": op.get("path", ""), "ok": True}
            )
        _job_log(job, f"All {len(operations)} operations succeeded")
    else:
        stderr = result.stderr.strip()
        _job_log(job, f"guestfish failed (rc={result.returncode}): {stderr}")
        for op in operations:
            results.append(
                {
                    "action": op["action"],
                    "path": op.get("path", ""),
                    "ok": False,
                    "error": stderr,
                }
            )

    return {"results": results}


COMMAND_HANDLERS["vms/modify-fs"] = _handle_vm_modify_fs


# ── Recert (OCP certificate regeneration) ──

RECERT_IMAGE = "quay.io/edge-infrastructure/recert:latest"
ETCD_IMAGE = "gcr.io/etcd-development/etcd:v3.6.0"
_ETCD_PORT_START = 2389
_ETCD_PORT_END = 2399

_recert_lock = threading.Lock()
_nbd_devices_in_use = set()
_nbd_module_loaded = False


def _ensure_nbd_module():
    global _nbd_module_loaded
    if _nbd_module_loaded:
        return
    result = subprocess.run(["lsmod"], capture_output=True, text=True, timeout=5)
    if "nbd" not in result.stdout:
        subprocess.run(
            ["modprobe", "nbd", "max_part=8"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    _nbd_module_loaded = True


def _allocate_nbd_device():
    with _recert_lock:
        for i in range(8):
            dev = f"/dev/nbd{i}"
            if dev in _nbd_devices_in_use:
                continue
            subprocess.run(
                ["qemu-nbd", "--disconnect", dev],
                capture_output=True,
                timeout=10,
            )
            time.sleep(0.5)
            if os.path.exists(f"{dev}p1"):
                continue
            _nbd_devices_in_use.add(dev)
            return dev
    raise RuntimeError("No free NBD devices (0-7)")


def _release_nbd_device(dev):
    with _recert_lock:
        _nbd_devices_in_use.discard(dev)


def _allocate_etcd_port():
    with _recert_lock:
        for port in range(_ETCD_PORT_START, _ETCD_PORT_END + 1):
            if not _port_in_use(port) and not _port_in_use(port + 10):
                return port
    raise RuntimeError(
        f"No free etcd ports in range {_ETCD_PORT_START}-{_ETCD_PORT_END}"
    )


def _ensure_container_image(job, image):
    result = subprocess.run(
        ["podman", "image", "exists", image],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        _job_log(job, f"Pulling {image}")
        _run_cmd(job, ["podman", "pull", image], timeout=300)


def _mount_rhcos_disk(job, nbd_dev, disk, mount_dir):
    """Connect a RHCOS qcow2 disk via NBD and mount its root partition (p4).

    Returns True on success.  Raises RuntimeError if the partition is missing.
    """
    _job_log(job, f"Connecting {os.path.basename(disk)} to {nbd_dev}")
    _run_cmd(job, ["qemu-nbd", "--connect", nbd_dev, disk], timeout=30)
    time.sleep(1)
    _run_cmd(job, ["partprobe", nbd_dev], timeout=15, check=False)
    time.sleep(1)

    partition = f"{nbd_dev}p4"
    if not os.path.exists(partition):
        raise RuntimeError(f"Partition {partition} not found — disk may not be RHCOS")

    os.makedirs(mount_dir, exist_ok=True)
    _job_log(job, f"Mounting {partition}")
    rc = _run_cmd(
        job,
        ["mount", "-o", "nouuid", partition, mount_dir],
        timeout=30,
        check=False,
    )
    if rc and rc.returncode != 0:
        _job_log(job, "Mount failed — repairing XFS log")
        _run_cmd(job, ["xfs_repair", "-L", partition], timeout=120)
        _run_cmd(job, ["mount", "-o", "nouuid", partition, mount_dir], timeout=30)
    return True


def _find_ostree_paths(mount_dir):
    """Locate RHCOS ostree deployment paths on a mounted disk.

    Returns (deploy_root, var_root, etc_k8s, etc_mcd, var_kubelet, var_etcd).
    Raises RuntimeError when the disk is not a valid RHCOS ostree layout.
    """
    deploy_dir = os.path.join(mount_dir, "ostree/deploy/rhcos/deploy")
    if not os.path.isdir(deploy_dir):
        raise RuntimeError("Not an OSTree/RHCOS disk — no ostree deploy dir")
    entries = [e for e in os.listdir(deploy_dir) if not e.endswith(".origin")]
    if not entries:
        raise RuntimeError("No OSTree deployment found")

    deploy_root = os.path.join(deploy_dir, entries[0])
    var_root = os.path.join(mount_dir, "ostree/deploy/rhcos/var")

    etc_k8s = os.path.join(deploy_root, "etc/kubernetes")
    etc_mcd = os.path.join(deploy_root, "etc/machine-config-daemon")
    var_kubelet = os.path.join(var_root, "lib/kubelet")
    var_etcd = os.path.join(var_root, "lib/etcd")

    for d in [etc_k8s, var_kubelet, var_etcd]:
        if not os.path.isdir(d):
            raise RuntimeError(f"Expected dir not found: {d}")

    return deploy_root, var_root, etc_k8s, etc_mcd, var_kubelet, var_etcd


def _save_kubeconfig(job, params, etc_k8s, force_expire):
    """Save kubeconfig from recerted disk to project dir for direct oc access.

    Returns the kubeconfig content string, or None when skipped.
    """
    if force_expire:
        return None
    project_id = params.get("project_id", "")
    vm_name = params.get("vm_name", "")
    kubeconfig_src = os.path.join(
        etc_k8s,
        "static-pod-resources/kube-apiserver-certs/secrets/"
        "node-kubeconfigs/lb-ext.kubeconfig",
    )
    if not project_id or not os.path.isfile(kubeconfig_src):
        return None

    kc_dir = os.path.join(_config.get("vm_dir", _VMS_DIR), project_id)
    os.makedirs(kc_dir, exist_ok=True)
    with open(kubeconfig_src) as f:
        kc_content = f.read()
    kc_dest = os.path.join(kc_dir, "kubeconfig")
    with open(kc_dest, "w") as f:
        f.write(kc_content)
    if vm_name:
        kc_named = os.path.join(kc_dir, f"kubeconfig-{vm_name}")
        with open(kc_named, "w") as f:
            f.write(kc_content)
    _job_log(job, f"Saved kubeconfig to {kc_dest}")
    return kc_content


def _setup_bastion_autologin(job, bastion_mount):
    """Write OCP auto-login boot script and desktop autostart entry.

    Only runs if ocp-autologin.py exists on the bastion disk.
    """
    autologin_script = os.path.join(
        bastion_mount,
        "home/cloud-user/ocp-autologin.py",
    )
    if not os.path.exists(autologin_script):
        return

    boot_script = os.path.join(
        bastion_mount,
        "home/cloud-user/ocp-autologin-boot.sh",
    )
    with open(boot_script, "w") as f:
        f.write(
            "#!/bin/bash\n"
            "# Wait for OCP oauth-server to be ready\n"
            "API=$(grep server: ~/ocp-install/auth/kubeconfig"
            " | head -1 | sed 's|.*https://api\\.||;s|:.*||')\n"
            '[ -z "$API" ] && exit 1\n'
            "CONSOLE=https://console-openshift-console.apps.$API\n"
            "for i in $(seq 1 60); do\n"
            "  curl -skL -o /dev/null -w '%{http_code}'"
            " $CONSOLE/auth/login 2>/dev/null | grep -q 200"
            " && break\n"
            "  sleep 10\n"
            "done\n"
            "export DISPLAY=:0 WAYLAND_DISPLAY=wayland-0"
            " XDG_RUNTIME_DIR=/run/user/$(id -u)"
            " MOZ_ENABLE_WAYLAND=1\n"
            "python3 ~/ocp-autologin.py $CONSOLE 2>/dev/null\n"
            "if [ $? -eq 0 ]; then\n"
            "  rm -f ~/ocp-autologin-boot.sh"
            " ~/.config/autostart/ocp-autologin.desktop\n"
            "fi\n"
        )
    os.chmod(boot_script, 0o700)
    os.chown(boot_script, 1000, 1000)
    autostart_dir = os.path.join(
        bastion_mount,
        "home/cloud-user/.config/autostart",
    )
    os.makedirs(autostart_dir, exist_ok=True)
    desktop_file = os.path.join(autostart_dir, "ocp-autologin.desktop")
    with open(desktop_file, "w") as f:
        f.write(
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=OCP Auto-Login\n"
            "Exec=/home/cloud-user/ocp-autologin-boot.sh\n"
            "X-GNOME-Autostart-enabled=true\n"
        )
    os.chown(autostart_dir, 1000, 1000)
    os.chown(desktop_file, 1000, 1000)
    _job_log(job, "Firefox auto-login scheduled for first boot")


def _perform_bastion_disk_updates(job, bastion_mount, kubeconfig_src, bastion_kubeconfig_path, common_password):
    kc_dest = os.path.join(bastion_mount, bastion_kubeconfig_path.lstrip("/"))
    os.makedirs(os.path.dirname(kc_dest), exist_ok=True)
    with open(kubeconfig_src) as f:
        kc_content = f.read()
    with open(kc_dest, "w") as f:
        f.write(kc_content)
    _job_log(job, "Bastion kubeconfig updated")

    pem = os.path.join(
        bastion_mount,
        "etc/pki/ca-trust/source/anchors/ocp-ingress.pem",
    )
    if os.path.exists(pem):
        os.unlink(pem)

    if common_password:
        pw_path = os.path.join(
            bastion_mount,
            "home/cloud-user/ocp-install/auth/kubeadmin-password",
        )
        if os.path.exists(os.path.dirname(pw_path)):
            with open(pw_path, "w") as f:
                f.write(common_password)
            _job_log(job, "Bastion kubeadmin password updated")

    ff_patterns = ["cert9.db", "key4.db", "logins.json"]
    for pattern in ff_patterns:
        for db_file in glob.glob(
            os.path.join(
                bastion_mount,
                f"home/cloud-user/.mozilla/firefox/*.default*/{pattern}",
            )
        ):
            os.unlink(db_file)

    _setup_bastion_autologin(job, bastion_mount)


def _update_bastion_disk(
    job, params, etc_k8s, common_password, bastion_kubeconfig_path, force_expire
):
    """Update bastion disk with recerted kubeconfig and clean stale data.

    Self-contained: allocates/releases its own NBD device and mount.
    Silently returns when there is no bastion disk or force_expire is set.
    """
    bastion_disk = params.get("bastion_disk")
    if not bastion_disk or force_expire:
        return
    bastion_disk = _validate_path(bastion_disk)

    kubeconfig_src = os.path.join(
        etc_k8s,
        "static-pod-resources/kube-apiserver-certs/secrets/"
        "node-kubeconfigs/lb-ext.kubeconfig",
    )
    if not os.path.isfile(kubeconfig_src):
        _job_log(
            job,
            f"No kubeconfig found at {kubeconfig_src}, skipping bastion update",
        )
        return

    _job_log(job, "Updating bastion disk")
    bastion_nbd = _allocate_nbd_device()
    bastion_mount = f"{_TMP_DIR}/recert-bastion-{job['job_id'][:8]}"
    bastion_mounted = False
    try:
        _run_cmd(
            job,
            ["qemu-nbd", "--connect", bastion_nbd, bastion_disk],
            timeout=30,
        )
        time.sleep(1)
        _run_cmd(job, ["partprobe", bastion_nbd], timeout=15, check=False)
        time.sleep(1)
        bastion_part = f"{bastion_nbd}p3"
        if not os.path.exists(bastion_part):
            bastion_part = f"{bastion_nbd}p1"
        os.makedirs(bastion_mount, exist_ok=True)
        _run_cmd(
            job,
            ["mount", "-o", "nouuid", bastion_part, bastion_mount],
            timeout=30,
        )
        bastion_mounted = True

        _perform_bastion_disk_updates(job, bastion_mount, kubeconfig_src, bastion_kubeconfig_path, common_password)

    except Exception as e:
        _job_log(job, f"Bastion update failed: {e}")
    finally:
        if bastion_mounted:
            try:
                _run_cmd(
                    job,
                    ["umount", bastion_mount],
                    timeout=30,
                    check=False,
                )
            except Exception:
                pass
        try:
            _run_cmd(
                job,
                ["qemu-nbd", "--disconnect", bastion_nbd],
                timeout=15,
                check=False,
            )
        except Exception:
            pass
        try:
            os.rmdir(bastion_mount)
        except OSError:
            pass
        _release_nbd_device(bastion_nbd)


def _wait_for_etcd_healthy(etcd_ctr, etcd_port, timeout_secs=30):
    """Poll etcd container until it reports healthy or timeout expires.

    Raises RuntimeError if etcd does not become healthy in time.
    """
    deadline = time.time() + timeout_secs
    while time.time() < deadline:
        try:
            result = subprocess.run(
                [
                    "podman",
                    "exec",
                    etcd_ctr,
                    "etcdctl",
                    f"--endpoints=http://127.0.0.1:{etcd_port}",
                    "endpoint",
                    "health",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and "healthy" in result.stdout.lower():
                return
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError(f"etcd did not become healthy within {timeout_secs}s")


def _build_recert_cmd(
    etc_k8s,
    etc_mcd,
    var_kubelet,
    etcd_port,
    force_expire,
    extend_expiration,
    cluster_rename,
    kubeadmin_password_hash,
):
    """Build the podman run command for recert."""
    cmd = [
        "podman",
        "run",
        "--rm",
        "--network",
        "host",
        "--security-opt",
        "label=disable",
        "-v",
        f"{etc_k8s}:/etc/kubernetes",
        "-v",
        f"{etc_mcd}:/etc/machine-config-daemon",
        "-v",
        f"{var_kubelet}:/var/lib/kubelet",
        RECERT_IMAGE,
        f"--etcd-endpoint=http://127.0.0.1:{etcd_port}",
        "--crypto-dir",
        "/etc/kubernetes",
        "--crypto-dir",
        "/etc/machine-config-daemon",
        "--crypto-dir",
        "/var/lib/kubelet",
        "--cluster-customization-dir",
        "/etc/kubernetes",
        "--cluster-customization-dir",
        "/var/lib/kubelet",
    ]
    if force_expire:
        cmd.append("--force-expire")
    elif extend_expiration:
        cmd.append("--extend-expiration")
    if cluster_rename:
        cmd.extend(["--cluster-rename", cluster_rename])
    if kubeadmin_password_hash:
        cmd.extend(["--kubeadmin-password-hash", kubeadmin_password_hash])
    return cmd


def _handle_vm_recert(job, params):
    """Regenerate OCP certificates on a stopped SNO's boot disk using recert.

    Params:
        disk: path to RHCOS qcow2 boot disk
        extend_expiration: bool (default True) — extend cert validity
        force_expire: bool (default False) — intentionally expire all certs (for pattern capture)
        cluster_rename: optional "name:domain" for cluster identity change
        kubeadmin_password_hash: optional new kubeadmin password hash
        bastion_disk: optional path to bastion qcow2 — injects updated kubeconfig after recert
        bastion_kubeconfig_path: guest path on bastion for kubeconfig (default /home/cloud-user/ocp-install/auth/kubeconfig)
    """
    disk = _validate_path(params.get("disk", ""))
    if not disk or not os.path.exists(disk):
        raise RuntimeError(f"disk not found: {disk}")

    extend_expiration = params.get("extend_expiration", True)
    force_expire = params.get("force_expire", False)
    cluster_rename = params.get("cluster_rename")
    kubeadmin_password_hash = params.get("kubeadmin_password_hash")
    common_password = params.get("common_password")
    bastion_disk = params.get("bastion_disk")
    if bastion_disk:
        bastion_disk = _validate_path(bastion_disk)
        if not os.path.exists(bastion_disk):
            raise RuntimeError(f"bastion disk not found: {bastion_disk}")
    bastion_kubeconfig_path = params.get(
        "bastion_kubeconfig_path", "/home/cloud-user/ocp-install/auth/kubeconfig"
    )

    _ensure_nbd_module()
    _ensure_container_image(job, ETCD_IMAGE)
    _ensure_container_image(job, RECERT_IMAGE)

    nbd_dev = _allocate_nbd_device()
    etcd_port = _allocate_etcd_port()
    etcd_peer_port = etcd_port + 10
    mount_dir = f"{_TMP_DIR}/recert-{job['job_id'][:8]}"
    etcd_ctr = f"recert-etcd-{job['job_id'][:8]}"
    mounted = False

    try:
        mounted = _mount_rhcos_disk(job, nbd_dev, disk, mount_dir)

        deploy_root, _, etc_k8s, etc_mcd, var_kubelet, var_etcd = _find_ostree_paths(
            mount_dir
        )
        _job_log(
            job,
            f"OSTree deployment: {os.path.basename(deploy_root)[:12]}...",
        )

        _job_log(job, f"Starting temp etcd on port {etcd_port}")
        _run_cmd(
            job,
            [
                "podman",
                "run",
                "-d",
                "--name",
                etcd_ctr,
                "--network",
                "host",
                "--security-opt",
                "label=disable",
                "-v",
                f"{var_etcd}:/data-dir",
                ETCD_IMAGE,
                "etcd",
                "--data-dir=/data-dir",
                "--name=recert-temp",
                f"--listen-client-urls=http://127.0.0.1:{etcd_port}",
                f"--advertise-client-urls=http://127.0.0.1:{etcd_port}",
                f"--listen-peer-urls=http://127.0.0.1:{etcd_peer_port}",
                "--force-new-cluster",
            ],
            timeout=60,
        )

        _job_log(job, "Waiting for etcd to become healthy...")
        _wait_for_etcd_healthy(etcd_ctr, etcd_port)
        _job_log(job, "etcd is healthy")

        recert_cmd = _build_recert_cmd(
            etc_k8s,
            etc_mcd,
            var_kubelet,
            etcd_port,
            force_expire,
            extend_expiration,
            cluster_rename,
            kubeadmin_password_hash,
        )
        _job_log(job, "Running recert...")
        _run_cmd(job, recert_cmd, timeout=300)
        _job_log(job, "Recert completed successfully")

        kc_content = _save_kubeconfig(job, params, etc_k8s, force_expire)
        if kc_content:
            if not isinstance(job.get("result"), dict):
                job["result"] = {}
            job["result"]["kubeconfig"] = kc_content

        _update_bastion_disk(
            job, params, etc_k8s, common_password, bastion_kubeconfig_path, force_expire
        )

        return {"status": "completed", "disk": disk}

    finally:
        try:
            _run_cmd(job, ["podman", "rm", "-f", etcd_ctr], timeout=15, check=False)
        except Exception:
            pass
        if mounted:
            try:
                _run_cmd(job, ["umount", mount_dir], timeout=30, check=False)
            except Exception:
                pass
        try:
            _run_cmd(
                job, ["qemu-nbd", "--disconnect", nbd_dev], timeout=15, check=False
            )
        except Exception:
            pass
        try:
            os.rmdir(mount_dir)
        except OSError:
            pass
        _release_nbd_device(nbd_dev)


COMMAND_HANDLERS["vms/recert"] = _handle_vm_recert


def _detect_namespace_gateway_ip(ns):
    """Return the first usable global IPv4 address inside a network namespace."""
    try:
        out = subprocess.run(
            ["ip", "netns", "exec", ns, "ip", "-4", "addr", "show"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except Exception:
        return ""
    for line in out.split("\n"):
        line = line.strip()
        if not (
            line.startswith(_INET_PREFIX)
            and "scope global" in line
            and "secondary" not in line
        ):
            continue
        addr = line.split()[1].split("/")[0]
        if (
            addr == "127.0.0.1"
            or addr.startswith("169.254")
            or addr.startswith("192.168.100")
        ):
            continue
        return addr
    return ""


def _handle_oc_exec(job, params):
    """Run an oc command inside the project's network namespace.

    Uses unshare --mount to bind a custom resolv.conf pointing at the
    project's dnsmasq gateway so oc can resolve cluster-internal domains.
    """
    project_id = _validate_project_id(params["project_id"])
    command = params.get("command", "")
    cmd_timeout = min(int(params.get("timeout", 30)), 300)
    if not command:
        raise RuntimeError("No command provided")

    ns = f"troshka-{project_id[:8]}"
    kc_path = os.path.join(
        _config.get("vm_dir", _VMS_DIR),
        project_id,
        "kubeconfig",
    )
    if not os.path.isfile(kc_path):
        raise RuntimeError(f"No kubeconfig for project {project_id[:8]}")

    gateway_ip = params.get("gateway_ip", "") or _detect_namespace_gateway_ip(ns)

    import shlex

    if gateway_ip:
        resolv_path = f"/tmp/troshka-resolv-{project_id[:8]}.conf"
        with open(resolv_path, "w") as f:
            f.write(f"nameserver {gateway_ip}\n")
        shell_cmd = (
            f"mount --bind {resolv_path} /etc/resolv.conf && "
            f"/usr/local/bin/oc --kubeconfig={kc_path} {command}"
        )
        full_cmd = [
            "ip",
            "netns",
            "exec",
            ns,
            "unshare",
            "--mount",
            "sh",
            "-c",
            shell_cmd,
        ]
    else:
        full_cmd = [
            "ip",
            "netns",
            "exec",
            ns,
            "/usr/local/bin/oc",
            f"--kubeconfig={kc_path}",
        ] + shlex.split(command)

    result = subprocess.run(
        full_cmd,
        capture_output=True,
        text=True,
        timeout=cmd_timeout,
    )
    return {
        "output": result.stdout,
        "error": result.stderr,
        "exit_code": result.returncode,
    }


COMMAND_HANDLERS["oc-exec"] = _handle_oc_exec


def _cleanup_stale_recert():
    """Clean up leftover recert artifacts from a previous agent crash."""
    import glob as _glob

    tmp_base = os.path.join(_config.get("local_mount", _LOCAL_DIR), "tmp")
    for d in _glob.glob(os.path.join(tmp_base, "recert-*")):
        if os.path.ismount(d):
            logger.warning("Cleaning stale recert mount: %s", d)
            subprocess.run(["umount", d], capture_output=True, timeout=15)
        try:
            os.rmdir(d)
        except OSError:
            pass

    for i in range(8):
        dev = f"/dev/nbd{i}"
        if os.path.exists(f"{dev}p1"):
            subprocess.run(
                ["qemu-nbd", "--disconnect", dev],
                capture_output=True,
                timeout=10,
            )

    result = subprocess.run(
        [
            "podman",
            "ps",
            "-a",
            "--filter",
            "name=recert-etcd-",
            "--format",
            _PODMAN_NAMES_FMT,
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    for name in result.stdout.strip().split("\n"):
        if name and name.startswith("recert-etcd-"):
            logger.warning("Removing stale recert container: %s", name)
            subprocess.run(
                ["podman", "rm", "-f", name],
                capture_output=True,
                timeout=15,
            )


# ── Storage handlers ──

_DISK_FORMATS = {"qcow2", "raw", "vmdk"}


def _handle_disk_create(job, params):
    path = _validate_path(params["path"])
    size_gb = int(params["size_gb"])
    fmt = params.get("format", "qcow2")
    if fmt not in _DISK_FORMATS:
        raise ValueError(f"Invalid disk format: {fmt}")
    backing = params.get("backing_file")

    os.makedirs(os.path.dirname(path), exist_ok=True)
    cmd = ["qemu-img", "create", "-f", fmt]
    if backing:
        backing = _validate_path(backing)
        _job_log(job, f"Using backing image: {os.path.basename(backing)}")
        # Ensure overlay is at least as large as backing file
        try:
            info = subprocess.run(
                ["qemu-img", "info", _PODMAN_JSON, backing],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if info.returncode == 0:
                import json as _json

                backing_vsize = _json.loads(info.stdout).get("virtual-size", 0)
                requested = size_gb * 1073741824
                if requested < backing_vsize:
                    backing_gb = (backing_vsize + 1073741823) // 1073741824
                    _job_log(
                        job,
                        f"Overlay {size_gb}G < backing {backing_gb}G, expanding to {backing_gb}G",
                    )
                    size_gb = backing_gb
        except Exception:
            pass
        cmd.extend(["-b", backing, "-F", fmt])
    cmd.extend([path, f"{size_gb}G"])
    _run_cmd(job, cmd)
    _chown_qemu(path)
    return {"path": path, "status": "created"}


COMMAND_HANDLERS["disks/create"] = _handle_disk_create


def _handle_disk_resize(job, params):
    path = _validate_path(params["path"])
    new_size_gb = int(params["new_size_gb"])
    _run_cmd(job, ["qemu-img", "resize", path, f"{new_size_gb}G"])
    return {"path": path, "status": "resized"}


COMMAND_HANDLERS["disks/resize"] = _handle_disk_resize


def _handle_disk_wipe(job, params):
    """Zero the first 1MB of a disk image (destroys boot sector/GPT)."""
    path = _validate_path(params["path"])
    if not os.path.exists(path):
        return {"error": f"Disk not found: {path}"}
    with open(path, "r+b") as f:
        f.write(b"\x00" * 1048576)
    return {"status": "wiped", "path": path}


COMMAND_HANDLERS["disks/wipe"] = _handle_disk_wipe


def _handle_seed_create(job, params):
    path = _validate_path(params["path"])
    meta_data = params.get("meta_data", "")
    user_data = params.get("user_data", "")
    network_config = params.get("network_config", "")

    import tempfile as _tf

    with _tf.TemporaryDirectory(dir=_TMP_DIR) as tmpdir:
        if meta_data:
            with open(os.path.join(tmpdir, "meta-data"), "w") as f:
                f.write(meta_data)
        if user_data:
            with open(os.path.join(tmpdir, "user-data"), "w") as f:
                f.write(user_data)
        if network_config:
            with open(os.path.join(tmpdir, "network-config"), "w") as f:
                f.write(network_config)

        os.makedirs(os.path.dirname(path), exist_ok=True)
        _run_cmd(
            job,
            [
                "xorriso",
                "-as",
                "genisoimage",
                "-output",
                path,
                "-volid",
                "cidata",
                "-joliet",
                "-rock",
                tmpdir + "/",
            ],
        )
    _chown_qemu(path)
    return {"path": path, "status": "created"}


COMMAND_HANDLERS["seeds/create"] = _handle_seed_create


import fcntl


def _handle_image_cache(job, params):
    s3_url = params.get("s3_url", "")
    url = params.get("url", "")
    dest_path = _validate_path(params["dest_path"])
    expected_size = params.get("expected_size", 0)
    aws_access_key = params.get("aws_access_key_id", "")
    aws_secret_key = params.get("aws_secret_access_key", "")
    aws_region = params.get("aws_region", "us-east-1")
    aws_endpoint_url = params.get("aws_endpoint_url", "")
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)

    lock_path = dest_path + ".lock"
    lock_fd = open(lock_path, "w")
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _job_log(
                job,
                f"Another download in progress for {os.path.basename(dest_path)}, waiting...",
            )
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            if os.path.exists(dest_path) and expected_size > 0:
                actual = os.path.getsize(dest_path)
                if actual >= expected_size - 1024:
                    _job_log(job, f"Already downloaded by another job ({actual} bytes)")
                    return {"path": dest_path, "status": "cached", "waited": True}

        if os.path.exists(dest_path) and expected_size > 0:
            actual = os.path.getsize(dest_path)
            if actual >= expected_size - 1024:
                _job_log(job, f"Already cached ({actual} bytes)")
                return {"path": dest_path, "status": "cached", "skipped": True}

        if s3_url:
            _s3_download(
                job,
                s3_url,
                dest_path,
                aws_access_key,
                aws_secret_key,
                aws_region,
                aws_endpoint_url,
            )
        else:
            _run_cmd(
                job, ["curl", "-fSL", "-o", dest_path, _validate_url(url)], timeout=3600
            )
        fmt = params.get("expected_format")
        if fmt == "qcow2":
            _run_cmd(job, ["qemu-img", "check", dest_path], timeout=60)
        return {"path": dest_path, "status": "cached"}
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()
        try:
            os.remove(lock_path)
        except FileNotFoundError:
            pass


COMMAND_HANDLERS["images/cache"] = _handle_image_cache


# Known kernel/initrd paths inside install ISOs, tried in order
_PXE_BOOT_PATHS = [
    # RHEL / CentOS / Fedora
    {"kernel": "/images/pxeboot/vmlinuz", "initrd": "/images/pxeboot/initrd.img"},
    # Ubuntu Server
    {"kernel": "/casper/vmlinuz", "initrd": "/casper/initrd"},
    # Debian
    {"kernel": "/install.amd/vmlinuz", "initrd": "/install.amd/initrd.gz"},
    # SLES / openSUSE
    {"kernel": "/boot/x86_64/loader/linux", "initrd": "/boot/x86_64/loader/initrd"},
]

# Known bootloader paths — UEFI first (preferred), then BIOS
_UEFI_BOOTLOADER_PATHS = [
    "/EFI/BOOT/BOOTX64.EFI",
    "/EFI/BOOT/grubx64.efi",
]

_BIOS_BOOTLOADER_PATHS = [
    "/isolinux/pxelinux.0",
    "/syslinux/pxelinux.0",
    "/pxelinux.0",
]


def _extract_pxe_boot_files(job, mount_point, tftp_root):
    """Copy kernel + initrd from a mounted ISO to the TFTP root."""
    import shutil

    for paths in _PXE_BOOT_PATHS:
        k_src = mount_point + paths["kernel"]
        i_src = mount_point + paths["initrd"]
        if os.path.isfile(k_src) and os.path.isfile(i_src):
            k_dest = os.path.join(tftp_root, paths["kernel"].lstrip("/"))
            i_dest = os.path.join(tftp_root, paths["initrd"].lstrip("/"))
            os.makedirs(os.path.dirname(k_dest), exist_ok=True)
            os.makedirs(os.path.dirname(i_dest), exist_ok=True)
            shutil.copy2(k_src, k_dest)
            shutil.copy2(i_src, i_dest)
            os.chmod(k_dest, 0o600)
            os.chmod(i_dest, 0o600)
            _job_log(job, f"Copied kernel to {paths['kernel']}")
            _job_log(job, f"Copied initrd to {paths['initrd']}")
            return
    # Not found — list ISO contents for debugging
    try:
        contents = os.listdir(mount_point)
        _job_log(job, f"ISO contents: {contents}")
    except OSError:
        pass
    raise RuntimeError(
        "Could not find kernel/initrd in ISO — unsupported distro layout"
    )


def _try_uefi_bootloader(job, mount_point, tftp_root):
    import shutil

    efi_boot_dir = os.path.join(mount_point, "EFI", "BOOT")
    if not os.path.isdir(efi_boot_dir):
        return None
    for fname in os.listdir(efi_boot_dir):
        src = os.path.join(efi_boot_dir, fname)
        if os.path.isfile(src):
            dest = os.path.join(tftp_root, fname)
            shutil.copy2(src, dest)
            os.chmod(dest, 0o600)
    _job_log(
        job, f"Copied EFI/BOOT/ directory ({len(os.listdir(efi_boot_dir))} files)"
    )
    for bl_path in _UEFI_BOOTLOADER_PATHS:
        bl_name = os.path.basename(bl_path)
        if os.path.isfile(os.path.join(tftp_root, bl_name)):
            return bl_name
    return None


def _try_bios_bootloader(job, mount_point, tftp_root):
    import shutil

    for bl_path in _BIOS_BOOTLOADER_PATHS:
        bl_src = mount_point + bl_path
        if os.path.isfile(bl_src):
            bl_name = os.path.basename(bl_path)
            bl_dest = os.path.join(tftp_root, bl_name)
            shutil.copy2(bl_src, bl_dest)
            os.chmod(bl_dest, 0o600)
            _job_log(job, f"Copied BIOS bootloader from {bl_path}")
            return bl_name
    return None


def _try_syslinux_bootloader(job, tftp_root):
    import shutil

    for syslinux_path in [
        "/usr/share/syslinux/pxelinux.0",
        "/usr/lib/syslinux/pxelinux.0",
    ]:
        if os.path.exists(syslinux_path):
            shutil.copy2(syslinux_path, os.path.join(tftp_root, _PXE_LOADER))
            _job_log(job, f"Copied pxelinux.0 from {syslinux_path}")
            return _PXE_LOADER
    return None


def _find_pxe_bootloader(job, mount_point, tftp_root):
    """Find and copy a PXE bootloader (UEFI or BIOS) from the mounted ISO.

    Returns the boot filename to use in dnsmasq/PXE config.
    """
    boot_filename = _try_uefi_bootloader(job, mount_point, tftp_root)
    if not boot_filename:
        boot_filename = _try_bios_bootloader(job, mount_point, tftp_root)
    if not boot_filename:
        boot_filename = _try_syslinux_bootloader(job, tftp_root)
    if not boot_filename:
        boot_filename = _PXE_LOADER
        _job_log(job, "WARNING: No bootloader found in ISO or on host")
    return boot_filename


def _patch_grub_config(job, tftp_root, install_url):
    """Patch grub.cfg to add inst.repo pointing to the HTTP install source."""
    grub_cfg_path = os.path.join(tftp_root, "grub.cfg")
    if not (install_url and os.path.isfile(grub_cfg_path)):
        return
    with open(grub_cfg_path) as f:
        grub_cfg = f.read()
    if "inst.repo" not in grub_cfg and "inst.stage2" not in grub_cfg:
        grub_cfg = grub_cfg.replace(" quiet", f" inst.repo={install_url} quiet")
        with open(grub_cfg_path, "w") as f:
            f.write(grub_cfg)
        _job_log(job, f"Patched grub.cfg with inst.repo={install_url}")
    elif "inst.stage2" in grub_cfg:
        import re

        grub_cfg = re.sub(r"inst\.stage2=\S+", f"inst.repo={install_url}", grub_cfg)
        with open(grub_cfg_path, "w") as f:
            f.write(grub_cfg)
        _job_log(job, f"Replaced inst.stage2 with inst.repo={install_url} in grub.cfg")


def _generate_pxelinux_config(job, tftp_root, install_url):
    """Generate BIOS PXE boot config (pxelinux.cfg/default)."""
    append_line = "initrd=initrd.img"
    if install_url:
        append_line += f" inst.repo={install_url}"
    pxe_cfg = (
        f"DEFAULT install\nLABEL install\n  KERNEL vmlinuz\n  APPEND {append_line}\n"
    )
    with open(os.path.join(tftp_root, "pxelinux.cfg", "default"), "w") as f:
        f.write(pxe_cfg)
    _job_log(job, "Generated pxelinux.cfg/default")


def _configure_dnsmasq_tftp(job, ns, vni, tftp_root, boot_filename):
    """Enable TFTP in dnsmasq config and restart dnsmasq."""
    dnsmasq_conf = f"/etc/dnsmasq.d/troshka-{vni}.conf"
    dnsmasq_pid = f"/run/troshka-dnsmasq-{vni}.pid"
    if not os.path.exists(dnsmasq_conf):
        return
    with open(dnsmasq_conf) as f:
        lines = f.readlines()
    filtered = [
        l
        for l in lines
        if not l.strip().startswith(("enable-tftp", "tftp-root=", "dhcp-boot="))
    ]
    filtered.append("enable-tftp\n")
    filtered.append(f"tftp-root={tftp_root}\n")
    filtered.append(f"dhcp-boot={boot_filename}\n")
    with open(dnsmasq_conf, "w") as f:
        f.writelines(filtered)
    _job_log(job, f"Configured dnsmasq TFTP with boot file {boot_filename}")
    # Always kill and restart dnsmasq in the correct namespace
    if os.path.exists(dnsmasq_pid):
        try:
            with open(dnsmasq_pid) as f:
                old_pid = int(f.read().strip())
            _safe_kill(old_pid, signal.SIGTERM)
            import time as _t2

            _t2.sleep(0.5)
        except (ValueError, ProcessLookupError, PermissionError):
            pass
    _run_cmd(
        job,
        ["ip", "netns", "exec", ns, "dnsmasq", f"--conf-file={dnsmasq_conf}"],
        timeout=10,
        capture_output=False,
    )
    _job_log(job, "Restarted dnsmasq with TFTP enabled")


def _start_pxe_http_server(job, ns, vni, mount_point, http_port):
    """Start HTTP server in namespace to serve ISO contents for PXE installs."""
    pid_file = f"/run/troshka-pxe-http-{vni}.pid"
    # Kill existing server
    if os.path.exists(pid_file):
        try:
            with open(pid_file) as f:
                old_pid = int(f.read().strip())
            _safe_kill(old_pid, signal.SIGTERM)
        except (ValueError, ProcessLookupError, PermissionError):
            pass

    http_script = f"""#!/usr/bin/env python3
import http.server
import os
import socketserver

os.chdir("{mount_point}")
socketserver.TCPServer.allow_reuse_address = True
httpd = socketserver.TCPServer(("0.0.0.0", {http_port}), http.server.SimpleHTTPRequestHandler)
httpd.serve_forever()
"""
    script_path = f"{_PXE_DIR}/{vni}/http_server.py"
    with open(script_path, "w") as f:
        f.write(http_script)

    subprocess.Popen(
        ["ip", "netns", "exec", ns, "python3", script_path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    # Write PID file (give process a moment to start)
    import time as _t

    _t.sleep(0.5)
    try:
        result = subprocess.run(
            ["pgrep", "-f", script_path],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.stdout.strip():
            pid = result.stdout.strip().split("\n")[0]
            with open(pid_file, "w") as f:
                f.write(pid)
    except (subprocess.TimeoutExpired, OSError):
        pass

    _job_log(job, f"Started HTTP install source on port {http_port}")


def _handle_pxe_setup(job, params):
    """Extract kernel/initrd from a cached ISO and set up PXE boot services.

    - Loop-mounts ISO, copies kernel + initrd + bootloader
    - Keeps ISO mounted for HTTP install source
    - Starts a Python HTTP server in the namespace
    - Generates pxelinux.cfg/default boot config
    """
    project_id = params.get("project_id")
    if not project_id:
        raise RuntimeError("project_id is required for PXE setup")
    vni = int(params["vni"])
    iso_path = _validate_path(params["iso_path"])
    gateway_ip = params.get("gateway_ip", "")
    http_port = int(params.get("http_port", 8080))
    tftp_root = params.get("tftp_root", f"{_PXE_DIR}/{vni}/tftpboot")
    mount_point = f"{_PXE_DIR}/{vni}/mnt"
    ns = f"troshka-{project_id[:8]}"

    if not os.path.exists(iso_path):
        raise RuntimeError(f"ISO not found: {iso_path}")

    # Create directories
    os.makedirs(tftp_root, exist_ok=True)
    os.makedirs(os.path.join(tftp_root, "pxelinux.cfg"), exist_ok=True)
    os.makedirs(mount_point, exist_ok=True)

    # Mount ISO first — needed for both extraction and HTTP serving
    try:
        subprocess.run(["umount", mount_point], capture_output=True, timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        pass
    _run_cmd(job, ["mount", "-o", "loop,ro", iso_path, mount_point], timeout=30)
    _job_log(job, f"Mounted ISO at {mount_point}")

    _extract_pxe_boot_files(job, mount_point, tftp_root)

    boot_filename = _find_pxe_bootloader(job, mount_point, tftp_root)

    install_url = f"http://{gateway_ip}:{http_port}/" if gateway_ip else ""
    _patch_grub_config(job, tftp_root, install_url)
    _generate_pxelinux_config(job, tftp_root, install_url)

    _configure_dnsmasq_tftp(job, ns, vni, tftp_root, boot_filename)

    _start_pxe_http_server(job, ns, vni, mount_point, http_port)
    return {
        "status": "ok",
        "tftp_root": tftp_root,
        "http_port": http_port,
        "mount_point": mount_point,
    }


COMMAND_HANDLERS["pxe/setup"] = _handle_pxe_setup


def _handle_library_import(job, params):
    """Download image, optionally flatten, optionally upload to S3."""
    download_url = params.get("download_url", "")
    s3_download_url = params.get("s3_download_url", "")
    cache_path = _validate_path(params["cache_path"])
    flatten = params.get("flatten", False)
    s3_upload_url = params.get("s3_upload_url", "")
    aws_access_key = params.get("aws_access_key_id", "")
    aws_secret_key = params.get("aws_secret_access_key", "")
    aws_region = params.get("aws_region", "us-east-1")
    aws_endpoint_url = params.get("aws_endpoint_url", "")

    temp_files = []
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        if s3_download_url:
            _job_log(job, "Downloading from S3...")
            _s3_download(
                job,
                s3_download_url,
                cache_path,
                aws_access_key,
                aws_secret_key,
                aws_region,
                aws_endpoint_url,
            )
        elif download_url:
            _job_log(job, f"Downloading from {download_url}...")
            _run_cmd(
                job,
                ["curl", "-fSL", "-o", cache_path, _validate_url(download_url)],
                timeout=7200,
            )

        if flatten:
            _job_log(job, "Flattening QCOW2 chain...")
            flat_path = cache_path + ".flat"
            temp_files.append(flat_path)
            _run_cmd(
                job,
                ["qemu-img", "convert", "-O", "qcow2", cache_path, flat_path],
                timeout=3600,
            )
            os.rename(flat_path, cache_path)
            temp_files.remove(flat_path)
            _job_log(job, "Flattening complete")

        if s3_upload_url:
            _job_log(job, "Uploading to S3...")
            _s3_upload(
                job,
                cache_path,
                s3_upload_url,
                aws_access_key,
                aws_secret_key,
                aws_region,
                aws_endpoint_url,
            )

        size_bytes = os.path.getsize(cache_path)
        return {"status": "completed", "size_bytes": size_bytes}

    finally:
        for temp_file in temp_files:
            try:
                if os.path.exists(temp_file):
                    os.remove(temp_file)
            except Exception:
                pass


COMMAND_HANDLERS["library/import"] = _handle_library_import


# ── Network handlers ──


def _handle_network_setup(job, params):
    network_name = _validate_network_name(params["network_name"])
    cidr = _validate_cidr(params["cidr"])
    _ = int(params["vni"])
    bridge_name = _validate_bridge_name(params["bridge_name"])
    project_id = _validate_project_id(params["project_id"])
    ns = f"troshka-{project_id[:8]}"

    # Create namespace
    try:
        _run_cmd(job, ["ip", "netns", "add", ns])
    except RuntimeError:
        _job_log(job, f"Namespace {ns} may already exist, continuing")

    # Create bridge in namespace
    _run_cmd(
        job,
        ["ip", "netns", "exec", ns, "ip", "link", "add", bridge_name, "type", "bridge"],
    )
    _run_cmd(
        job, ["ip", "netns", "exec", ns, "ip", "addr", "add", cidr, "dev", bridge_name]
    )
    _run_cmd(job, ["ip", "netns", "exec", ns, "ip", "link", "set", bridge_name, "up"])
    _run_cmd(job, ["ip", "netns", "exec", ns, "ip", "link", "set", "lo", "up"])

    return {"network": network_name, "namespace": ns, "status": "configured"}


COMMAND_HANDLERS["networks/setup"] = _handle_network_setup


def _handle_network_teardown(job, params):
    network_name = _validate_network_name(params["network_name"])
    project_id = _validate_project_id(params["project_id"])
    ns = f"troshka-{project_id[:8]}"

    try:
        _run_cmd(job, ["ip", "netns", "delete", ns])
    except RuntimeError:
        _job_log(job, f"Namespace {ns} may not exist, continuing")

    return {"network": network_name, "status": "removed"}


COMMAND_HANDLERS["networks/teardown"] = _handle_network_teardown


def _handle_list_bridges(job, params):
    """List all br-* bridges on the host."""
    result = subprocess.run(
        ["ip", "-o", "link", "show", "type", "bridge"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    bridges = []
    if result.returncode == 0:
        for line in result.stdout.strip().split("\n"):
            parts = line.split(":", 2)
            if len(parts) >= 2:
                name = parts[1].strip().split("@")[0]
                if name.startswith("br-"):
                    bridges.append(name)
    return {"bridges": bridges}


COMMAND_HANDLERS["networks/list-bridges"] = _handle_list_bridges


def _handle_reconnect_taps(job, params):
    """Move stale TAPs from host-namespace dummy bridges into project namespace.

    After a host restart, VMs start with TAPs on dummy bridges in the host
    namespace.  Once recover_host_services() rebuilds the real namespace
    bridges, this handler moves each running VM's TAPs into the namespace —
    same operation the qemu hook does on VM start.

    Params:
        project_id: str
        domains: list of domain names to check
    """
    project_id = _validate_project_id(params["project_id"])
    domains = params.get("domains", [])
    ns = f"troshka-{project_id[:8]}"

    ns_check = subprocess.run(
        ["ip", "netns", "list"], capture_output=True, text=True, timeout=5
    )
    if ns not in ns_check.stdout:
        return {"reconnected": 0, "error": "namespace not found"}

    reconnected = 0
    for domain in domains:
        try:
            result = subprocess.run(
                ["virsh", "dumpxml", domain],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                continue
            xml = result.stdout
            import re

            bridges = _SOURCE_BRIDGE_RE.findall(xml)
            taps = re.findall(r"target dev='((?:vnet|tap)[^']+)'", xml)
            for i, tap in enumerate(taps):
                if i >= len(bridges):
                    break
                bridge = bridges[i]
                tap_check = subprocess.run(
                    ["ip", "link", "show", tap],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if tap_check.returncode != 0:
                    continue
                _run_cmd(job, ["ip", "link", "set", tap, "netns", ns])
                _run_cmd(
                    job,
                    [
                        "ip",
                        "netns",
                        "exec",
                        ns,
                        "ip",
                        "link",
                        "set",
                        tap,
                        "master",
                        bridge,
                    ],
                )
                _run_cmd(
                    job,
                    ["ip", "netns", "exec", ns, "ip", "link", "set", tap, "up"],
                )
                reconnected += 1
                logger.info(
                    "Reconnected TAP %s -> %s/%s for %s", tap, ns, bridge, domain
                )
        except Exception as e:
            logger.warning("Failed to reconnect TAPs for %s: %s", domain, e)

    return {"reconnected": reconnected}


COMMAND_HANDLERS["networks/reconnect-taps"] = _handle_reconnect_taps


def _create_new_namespace(job, ns, veth_host, veth_ns, transit_host_ip, transit_ns_ip):
    _run_cmd(job, ["ip", "netns", "add", ns], timeout=10)
    _run_cmd(
        job,
        ["ip", "link", "add", veth_host, "type", "veth", "peer", "name", veth_ns],
        timeout=10,
    )
    _run_cmd(job, ["ip", "link", "set", veth_ns, "netns", ns], timeout=10)
    _run_cmd(
        job,
        ["ip", "addr", "add", f"{transit_host_ip}/24", "dev", veth_host],
        timeout=10,
    )
    _run_cmd(job, ["ip", "link", "set", veth_host, "up"], timeout=10)
    _run_cmd(
        job,
        [
            "ip",
            "netns",
            "exec",
            ns,
            "ip",
            "addr",
            "add",
            f"{transit_ns_ip}/24",
            "dev",
            veth_ns,
        ],
        timeout=10,
    )
    _run_cmd(
        job,
        ["ip", "netns", "exec", ns, "ip", "link", "set", veth_ns, "up"],
        timeout=10,
    )
    _run_cmd(
        job,
        ["ip", "netns", "exec", ns, "ip", "link", "set", "lo", "up"],
        timeout=10,
    )
    _run_cmd(
        job,
        [
            "ip",
            "netns",
            "exec",
            ns,
            "ip",
            "route",
            "add",
            "default",
            "via",
            transit_host_ip,
        ],
        timeout=10,
    )


def _setup_namespace_and_veth(
    job, ns, veth_host, veth_ns, transit_host_ip, transit_ns_ip, transit_cidr
):
    """Create network namespace and veth pair (idempotent)."""
    ns_exists = (
        subprocess.run(
            ["ip", "netns", "exec", ns, "true"], capture_output=True, timeout=5
        ).returncode
        == 0
    )
    if ns_exists:
        _job_log(job, f"Namespace {ns} already exists, reusing")
    else:
        _create_new_namespace(job, ns, veth_host, veth_ns, transit_host_ip, transit_ns_ip)
    try:
        _run_cmd(
            job, ["ip", "route", "add", transit_cidr, "dev", veth_host], timeout=10
        )
    except RuntimeError:
        pass

    _run_cmd(job, ["sysctl", "-w", "net.ipv4.ip_forward=1"], timeout=10)
    _job_log(job, "Namespace and veth pair configured")


def _add_vxlan_fdb_peers(job, vxlan_if, peers, host_ip):
    """Add FDB entries for VXLAN peers."""
    for peer in peers:
        if peer != host_ip:
            try:
                _validate_ip(peer)
                _run_cmd(
                    job,
                    [
                        "bridge",
                        "fdb",
                        "append",
                        "00:00:00:00:00:00",
                        "dev",
                        vxlan_if,
                        "dst",
                        peer,
                    ],
                    timeout=10,
                )
            except (ValueError, RuntimeError):
                _job_log(job, f"Warning: skipping peer {peer}")


def _attach_vxlan_to_ns_bridge(job, ns, vxlan_if, bridge):
    """Create bridge inside namespace and attach VXLAN to it."""
    try:
        _run_cmd(
            job,
            [
                "ip",
                "netns",
                "exec",
                ns,
                "ip",
                "link",
                "add",
                bridge,
                "type",
                "bridge",
            ],
            timeout=10,
        )
    except RuntimeError:
        _job_log(job, f"Bridge {bridge} already exists, reusing")
    try:
        _run_cmd(
            job,
            [
                "ip",
                "netns",
                "exec",
                ns,
                "ip",
                "link",
                "set",
                vxlan_if,
                "master",
                bridge,
            ],
            timeout=10,
        )
    except RuntimeError:
        pass
    try:
        _run_cmd(
            job,
            ["ip", "netns", "exec", ns, "ip", "link", "set", vxlan_if, "up"],
            timeout=10,
        )
    except RuntimeError:
        pass
    try:
        _run_cmd(
            job,
            ["ip", "netns", "exec", ns, "ip", "link", "set", bridge, "up"],
            timeout=10,
        )
    except RuntimeError:
        pass


def _ensure_host_dummy_bridge(job, bridge):
    """Create dummy bridge in host namespace for libvirt validation."""
    try:
        subprocess.run(
            ["ip", "link", "show", bridge],
            capture_output=True,
            check=True,
            timeout=5,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        _run_cmd(job, ["ip", "link", "add", bridge, "type", "bridge"], timeout=10)
        subprocess.run(
            [
                "ip",
                "link",
                "set",
                bridge,
                "type",
                "bridge",
                "forward_delay",
                "99",
                "ageing_time",
                "0",
            ],
            capture_output=True,
            timeout=5,
        )
    _run_cmd(job, ["ip", "link", "set", bridge, "up"], timeout=10)


def _assign_bridge_gateway_ip(job, ns, net, bridge, cidr):
    """Assign bridge IP if DHCP/DNS is enabled."""
    if not (net.get("dhcp_enabled") or net.get("dns_enabled")):
        return
    dhcp_cfg = net.get("dhcp_config", {})
    gateway_ip = dhcp_cfg.get("gateway", "")
    if gateway_ip and cidr:
        prefix = cidr.split("/")[1] if "/" in cidr else "24"
        try:
            _run_cmd(
                job,
                [
                    "ip",
                    "netns",
                    "exec",
                    ns,
                    "ip",
                    "addr",
                    "add",
                    f"{gateway_ip}/{prefix}",
                    "dev",
                    bridge,
                ],
                timeout=10,
            )
        except RuntimeError:
            pass


def _setup_vxlan_bridge(job, ns, host_ip, net, _pid):
    """Set up a single VXLAN + bridge pair inside the namespace."""
    vni = int(net["vni"])
    bridge = net["bridge_name"]
    vxlan_if = net["vxlan_name"]
    cidr = net.get("cidr", "")
    peers = net.get("peers", [])

    _validate_bridge_name(bridge)

    # Clean up existing
    try:
        _run_cmd(job, ["ip", "link", "del", vxlan_if], timeout=10)
    except RuntimeError:
        pass
    try:
        _run_cmd(
            job,
            ["ip", "netns", "exec", ns, "ip", "link", "del", vxlan_if],
            timeout=10,
        )
    except RuntimeError:
        pass

    # Create VXLAN in host namespace (may already exist from a previous deploy)
    try:
        _run_cmd(
            job,
            [
                "ip",
                "link",
                "add",
                vxlan_if,
                "type",
                "vxlan",
                "id",
                str(vni),
                "local",
                host_ip,
                "dstport",
                "4789",
                "nolearning",
            ],
            timeout=10,
        )
    except RuntimeError:
        _job_log(job, f"VXLAN {vxlan_if} already exists, reusing")

    _add_vxlan_fdb_peers(job, vxlan_if, peers, host_ip)

    # Move VXLAN into namespace (may already be there)
    try:
        _run_cmd(job, ["ip", "link", "set", vxlan_if, "netns", ns], timeout=10)
    except RuntimeError:
        _job_log(job, f"VXLAN {vxlan_if} already in namespace, reusing")

    _attach_vxlan_to_ns_bridge(job, ns, vxlan_if, bridge)

    _ensure_host_dummy_bridge(job, bridge)

    _assign_bridge_gateway_ip(job, ns, net, bridge, cidr)

    _job_log(job, f"VXLAN {vxlan_if} (VNI {vni}) + bridge {bridge} configured")


def _build_dnsmasq_config_lines(project_id, net, dnsmasq_pid, dnsmasq_lease, range_start, range_end, lease_time, bridge):
    pid = project_id[:8]
    bmc_bridge = f"br-bmc-{pid}"
    conf_lines = [
        f"interface={bridge}",
        "bind-interfaces",
        "except-interface=lo",
        f"no-dhcp-interface={bmc_bridge}",
        "no-resolv",
        "server=8.8.8.8",
        "server=1.1.1.1",
        "no-hosts",
        f"pid-file={dnsmasq_pid}",
        f"dhcp-leasefile={dnsmasq_lease}",
        f"dhcp-range={range_start},{range_end},{lease_time}",
    ]
    for dh in net.get("dhcp_hosts", []):
        safe_name = (dh.get("name") or "").replace(" ", "-").replace("_", "-")
        hostname_part = f",{safe_name}" if safe_name else ""
        conf_lines.append(f"dhcp-host={dh['mac']},{dh['ip']}{hostname_part}")
    if net.get("dns_enabled") and net.get("dns_domain"):
        conf_lines.append(f"domain={net['dns_domain']}")
    for dns_rec in net.get("dns_records", []):
        rec_name = dns_rec.get("name", "")
        rec_ip = dns_rec.get("ip", "")
        if rec_name and rec_ip:
            conf_lines.append(f"address=/{rec_name}/{rec_ip}")
    _append_pxe_dnsmasq_config(conf_lines, net)
    return conf_lines


def _setup_dnsmasq_for_network(job, ns, project_id, net):
    """Configure and start dnsmasq for a single network (DHCP/DNS/PXE)."""
    if not net.get("dhcp_enabled"):
        return
    vni = int(net["vni"])
    bridge = net["bridge_name"]
    dhcp_cfg = net.get("dhcp_config", {})
    range_start = dhcp_cfg.get("range_start", "")
    range_end = dhcp_cfg.get("range_end", "")
    lease_time = dhcp_cfg.get("lease_time", "24h")
    if not (range_start and range_end):
        return

    pid_short = project_id[:8]
    dnsmasq_conf = f"/etc/dnsmasq.d/troshka-{pid_short}-{vni}.conf"
    dnsmasq_pid = f"/run/troshka-dnsmasq-{pid_short}-{vni}.pid"
    dnsmasq_lease = f"{_DNSMASQ_PREFIX}-{pid_short}-{vni}.leases"

    conf_lines = _build_dnsmasq_config_lines(project_id, net, dnsmasq_pid, dnsmasq_lease, range_start, range_end, lease_time, bridge)

    os.makedirs("/etc/dnsmasq.d", exist_ok=True)
    with open(dnsmasq_conf, "w") as f:
        f.write("\n".join(conf_lines) + "\n")

    _kill_and_restart_dnsmasq(job, ns, dnsmasq_conf, dnsmasq_pid, vni, bridge)


def _append_builtin_pxe_config(conf_lines, pxe):
    """Append builtin TFTP/PXE boot lines to dnsmasq config."""
    tftp_r = pxe["tftp_root"]
    conf_lines.append("enable-tftp")
    conf_lines.append(f"tftp-root={tftp_r}")
    boot_file = _PXE_LOADER
    for candidate in ["BOOTX64.EFI", "grubx64.efi", _PXE_LOADER]:
        if os.path.isfile(os.path.join(tftp_r, candidate)):
            boot_file = candidate
            break
    conf_lines.append(f"dhcp-boot={boot_file}")


def _append_pxe_dnsmasq_config(conf_lines, net):
    """Append PXE boot configuration lines to a dnsmasq config."""
    pxe = net.get("pxe_config")
    if not pxe:
        return
    if pxe.get("server_mode") == "builtin" and pxe.get("tftp_root"):
        _append_builtin_pxe_config(conf_lines, pxe)
    else:
        method = pxe.get("method", "legacy")
        if method == "legacy" and pxe.get("next_server") and pxe.get("boot_file"):
            conf_lines.append(
                f"dhcp-boot={pxe['boot_file']},{pxe['next_server']},{pxe['next_server']}"
            )
        elif method == "ipxe" and pxe.get("ipxe_script_url"):
            conf_lines.append(f"dhcp-boot={pxe['ipxe_script_url']}")
        elif method == "uefi-http" and pxe.get("uefi_boot_url"):
            conf_lines.append(f"dhcp-boot={pxe['uefi_boot_url']}")


def _kill_existing_dnsmasq(dnsmasq_conf, dnsmasq_pid):
    if os.path.exists(dnsmasq_pid):
        try:
            with open(dnsmasq_pid) as f:
                old_pid = int(f.read().strip())
            _safe_kill(old_pid, signal.SIGTERM)
            for _ in range(20):
                try:
                    os.kill(old_pid, 0)
                    time.sleep(0.25)
                except ProcessLookupError:
                    break
            else:
                _safe_kill(old_pid, signal.SIGKILL)
        except (ValueError, ProcessLookupError, PermissionError):
            pass
        try:
            os.remove(dnsmasq_pid)
        except FileNotFoundError:
            pass
    subprocess.run(["pkill", "-f", dnsmasq_conf], capture_output=True, timeout=5)
    time.sleep(0.3)


def _kill_and_restart_dnsmasq(job, ns, dnsmasq_conf, dnsmasq_pid, vni, bridge):
    """Kill existing dnsmasq for a VNI and restart it in the namespace."""
    _kill_existing_dnsmasq(dnsmasq_conf, dnsmasq_pid)

    _run_cmd(
        job,
        ["ip", "netns", "exec", ns, "dnsmasq", f"--conf-file={dnsmasq_conf}"],
        timeout=10,
        capture_output=False,
    )
    try:
        with open(dnsmasq_pid) as _pf:
            _dpid = _pf.read().strip()
        subprocess.run(
            [
                "auditctl",
                "-a",
                "exit,always",
                "-F",
                "arch=b64",
                "-S",
                "kill",
                "-F",
                f"a0={_dpid}",
                "-k",
                "dnsmasq-kill",
            ],
            capture_output=True,
            timeout=5,
        )
    except Exception:
        pass
    _job_log(job, f"dnsmasq started for VNI {vni} on {bridge}")


def _kill_existing_chronyd(chrony_pid):
    """Kill existing chronyd by PID file and clean up."""
    if not os.path.exists(chrony_pid):
        return
    try:
        with open(chrony_pid) as f:
            old_pid = int(f.read().strip())
        _safe_kill(old_pid, signal.SIGTERM)
        for _ in range(10):
            try:
                os.kill(old_pid, 0)
                time.sleep(0.25)
            except ProcessLookupError:
                break
        else:
            _safe_kill(old_pid, signal.SIGKILL)
    except (ValueError, ProcessLookupError, PermissionError):
        pass
    try:
        os.remove(chrony_pid)
    except FileNotFoundError:
        pass


def _setup_chrony_ntp(job, ns, pid, networks):
    """Start chronyd NTP server in the project namespace."""
    chrony_dir = _CHRONY_DIR
    os.makedirs(chrony_dir, exist_ok=True)
    chrony_conf = f"{chrony_dir}/{pid}.conf"
    chrony_pid = f"/run/troshka-chronyd-{pid}.pid"
    chrony_drift = f"{chrony_dir}/{pid}.drift"

    chrony_bind_ip = None
    for net in networks:
        dhcp_cfg = net.get("dhcp_config", {})
        gw_ip = dhcp_cfg.get("gateway", "")
        if gw_ip:
            chrony_bind_ip = gw_ip
            break

    if not chrony_bind_ip:
        return

    conf_content = (
        f"local stratum 3\n"
        f"allow 0.0.0.0/0\n"
        f"driftfile {chrony_drift}\n"
        f"pidfile {chrony_pid}\n"
        f"bindaddress {chrony_bind_ip}\n"
        f"port 123\n"
    )
    with open(chrony_conf, "w") as f:
        f.write(conf_content)

    _kill_existing_chronyd(chrony_pid)

    try:
        _run_cmd(
            job,
            ["ip", "netns", "exec", ns, "chronyd", "-f", chrony_conf],
            timeout=10,
            capture_output=False,
        )
        _job_log(job, f"chronyd started on {chrony_bind_ip} in namespace {ns}")
    except RuntimeError:
        _job_log(job, "chronyd not available, skipping NTP server")


def _setup_ns_nftables_base(job, ns, veth_ns):
    """Create base nftables tables, chains, and masquerade rule in namespace."""
    for tbl in ["filter", "nat"]:
        subprocess.run(
            ["ip", "netns", "exec", ns, "nft", "flush", "table", "inet", tbl],
            capture_output=True,
            timeout=10,
        )
        subprocess.run(
            ["ip", "netns", "exec", ns, "nft", "delete", "table", "inet", tbl],
            capture_output=True,
            timeout=10,
        )
    _run_cmd(
        job,
        ["ip", "netns", "exec", ns, "nft", "add", "table", "inet", "filter"],
        timeout=10,
    )
    _run_cmd(
        job,
        [
            "ip",
            "netns",
            "exec",
            ns,
            "nft",
            "add",
            "chain",
            "inet",
            "filter",
            "forward",
            "{ type filter hook forward priority 0; policy drop; }",
        ],
        timeout=10,
    )
    _run_cmd(
        job,
        ["ip", "netns", "exec", ns, "nft", "add", "table", "inet", "nat"],
        timeout=10,
    )
    _run_cmd(
        job,
        [
            "ip",
            "netns",
            "exec",
            ns,
            "nft",
            "add",
            "chain",
            "inet",
            "nat",
            "postrouting",
            "{ type nat hook postrouting priority 100; }",
        ],
        timeout=10,
    )
    _run_cmd(
        job,
        [
            "ip",
            "netns",
            "exec",
            ns,
            "nft",
            "add",
            "chain",
            "inet",
            "nat",
            "prerouting",
            "{ type nat hook prerouting priority -100; }",
        ],
        timeout=10,
    )
    # Masquerade outbound traffic from bridges
    _run_cmd(
        job,
        [
            "ip",
            "netns",
            "exec",
            ns,
            "nft",
            "add",
            "rule",
            "inet",
            "nat",
            "postrouting",
            "oifname",
            veth_ns,
            "masquerade",
        ],
        timeout=10,
    )


def _setup_ns_nftables_forwarding(job, ns, networks, routers, pid):
    """Add intra-bridge, inter-bridge, and established/related forwarding rules."""
    # Intra-bridge forwarding (cluster + BMC bridges)
    for net in networks:
        bridge = net["bridge_name"]
        _run_cmd(
            job,
            [
                "ip",
                "netns",
                "exec",
                ns,
                "nft",
                "add",
                "rule",
                "inet",
                "filter",
                "forward",
                "iifname",
                bridge,
                "oifname",
                bridge,
                "accept",
            ],
            timeout=10,
        )
    bmc_bridge = f"br-bmc-{pid}"
    try:
        _run_cmd(
            job,
            [
                "ip",
                "netns",
                "exec",
                ns,
                "nft",
                "add",
                "rule",
                "inet",
                "filter",
                "forward",
                "iifname",
                bmc_bridge,
                "oifname",
                bmc_bridge,
                "accept",
            ],
            timeout=10,
        )
    except RuntimeError:
        pass

    # Router: inter-bridge forwarding
    for router in routers:
        vnis = router.get("connected_vnis", [])
        for i, vni_a in enumerate(vnis):
            for vni_b in vnis[i + 1 :]:
                br_a = f"br-{vni_a}"
                br_b = f"br-{vni_b}"
                _run_cmd(
                    job,
                    [
                        "ip",
                        "netns",
                        "exec",
                        ns,
                        "nft",
                        "add",
                        "rule",
                        "inet",
                        "filter",
                        "forward",
                        "iifname",
                        br_a,
                        "oifname",
                        br_b,
                        "accept",
                    ],
                    timeout=10,
                )
                _run_cmd(
                    job,
                    [
                        "ip",
                        "netns",
                        "exec",
                        ns,
                        "nft",
                        "add",
                        "rule",
                        "inet",
                        "filter",
                        "forward",
                        "iifname",
                        br_b,
                        "oifname",
                        br_a,
                        "accept",
                    ],
                    timeout=10,
                )

    # Allow established/related
    _run_cmd(
        job,
        [
            "ip",
            "netns",
            "exec",
            ns,
            "nft",
            "add",
            "rule",
            "inet",
            "filter",
            "forward",
            "ct",
            "state",
            "established,related",
            "accept",
        ],
        timeout=10,
    )


def _add_outbound_port_rule(job, ns, bridge, veth_ns, entry):
    """Add a single outbound port/protocol nftables rule."""
    if entry == "icmp" or entry.startswith("icmp/"):
        icmp_type = entry.split("/", 1)[1] if "/" in entry else None
        cmd = [
            "ip",
            "netns",
            "exec",
            ns,
            "nft",
            "add",
            "rule",
            "inet",
            "filter",
            "forward",
            "iifname",
            bridge,
            "oifname",
            veth_ns,
            "ip",
            "protocol",
            "icmp",
        ]
        if icmp_type:
            cmd.extend(["icmp", "type", icmp_type])
        cmd.append("accept")
        _run_cmd(job, cmd, timeout=10)
    elif "/" in entry:
        port, proto = entry.split("/", 1)
        _run_cmd(
            job,
            [
                "ip",
                "netns",
                "exec",
                ns,
                "nft",
                "add",
                "rule",
                "inet",
                "filter",
                "forward",
                "iifname",
                bridge,
                "oifname",
                veth_ns,
                proto,
                "dport",
                port,
                "accept",
            ],
            timeout=10,
        )
    else:
        for proto in ("tcp", "udp"):
            _run_cmd(
                job,
                [
                    "ip",
                    "netns",
                    "exec",
                    ns,
                    "nft",
                    "add",
                    "rule",
                    "inet",
                    "filter",
                    "forward",
                    "iifname",
                    bridge,
                    "oifname",
                    veth_ns,
                    proto,
                    "dport",
                    entry,
                    "accept",
                ],
                timeout=10,
            )


def _setup_ns_outbound_rules(job, ns, veth_ns, networks, gateway):
    """Configure outbound traffic rules (allow-all or restricted ports)."""
    outbound_policy = (
        gateway.get("outbound_policy", "allow-all") if gateway else "allow-all"
    )
    outbound_ports = gateway.get("outbound_ports", "") if gateway else ""
    if outbound_policy == "restrict" and outbound_ports:
        allowed = [
            str(p).strip() for p in str(outbound_ports).split(",") if str(p).strip()
        ]
        _job_log(job, f"Outbound restricted to ports: {', '.join(allowed)}")
        for net in networks:
            bridge = net["bridge_name"]
            for entry in allowed:
                _add_outbound_port_rule(job, ns, bridge, veth_ns, entry)
    else:
        for net in networks:
            bridge = net["bridge_name"]
            _run_cmd(
                job,
                [
                    "ip",
                    "netns",
                    "exec",
                    ns,
                    "nft",
                    "add",
                    "rule",
                    "inet",
                    "filter",
                    "forward",
                    "iifname",
                    bridge,
                    "oifname",
                    veth_ns,
                    "accept",
                ],
                timeout=10,
            )


def _setup_ns_port_forward_dnat(job, ns, veth_ns, gateway, transit_octet3):
    """Set up DNAT rules for port forwarding inside the namespace.

    Returns a dict mapping pf_idx -> transit IP for use by host nftables.
    """
    pf_transit_ips = {}
    if not (gateway and gateway.get("mode") == "nat-portforward"):
        return pf_transit_ips
    for pf_idx, pf in enumerate(gateway.get("port_forwards", [])):
        ext_port = pf.get("extPort", "")
        int_ip = pf.get("intIp", "")
        int_port = pf.get("intPort", "")
        transit_port = pf.get("_transit_port")
        effective_port = str(transit_port) if transit_port else str(ext_port)
        if not (ext_port and int_ip and int_port):
            continue
        pf_transit_ip = f"172.30.{transit_octet3}.{10 + pf_idx}"
        pf_transit_ips[pf_idx] = pf_transit_ip
        try:
            _run_cmd(
                job,
                [
                    "ip",
                    "netns",
                    "exec",
                    ns,
                    "ip",
                    "addr",
                    "add",
                    f"{pf_transit_ip}/24",
                    "dev",
                    veth_ns,
                ],
                timeout=10,
            )
        except RuntimeError:
            pass  # May already exist
        _run_cmd(
            job,
            [
                "ip",
                "netns",
                "exec",
                ns,
                "nft",
                "add",
                "rule",
                "inet",
                "nat",
                "prerouting",
                "ip",
                "daddr",
                pf_transit_ip,
                "tcp",
                "dport",
                effective_port,
                "dnat",
                "ip",
                "to",
                f"{int_ip}:{int_port}",
            ],
            timeout=10,
        )
        _run_cmd(
            job,
            [
                "ip",
                "netns",
                "exec",
                ns,
                "nft",
                "add",
                "rule",
                "inet",
                "filter",
                "forward",
                "iifname",
                veth_ns,
                "tcp",
                "dport",
                str(int_port),
                "accept",
            ],
            timeout=10,
        )
    return pf_transit_ips


def _nft_try(job, cmd):
    """Run an nft command, ignoring errors (for idempotent setup)."""
    try:
        _run_cmd(job, cmd, timeout=10)
    except RuntimeError:
        pass


def _setup_host_nftables(
    job, pid, veth_host, transit_cidr, gateway, pf_transit_ips, transit_ns_ip
):
    """Set up nftables rules in the HOST namespace for NAT/port-forwarding."""
    if not (gateway and gateway.get("mode") in ("nat", "nat-portforward")):
        return

    fwd_chain = f"troshka-fwd-{pid}"
    post_chain = f"troshka-post-{pid}"
    pre_chain = f"troshka-pre-{pid}"

    _nft_try(job, ["nft", "add", "table", "inet", "filter"])
    _nft_try(
        job,
        [
            "nft",
            "add",
            "chain",
            "inet",
            "filter",
            "forward",
            "{ type filter hook forward priority 0; policy accept; }",
        ],
    )
    _nft_try(job, ["nft", "add", "table", "inet", "nat"])
    _nft_try(
        job,
        [
            "nft",
            "add",
            "chain",
            "inet",
            "nat",
            "postrouting",
            "{ type nat hook postrouting priority 100; }",
        ],
    )
    _nft_try(
        job,
        [
            "nft",
            "add",
            "chain",
            "inet",
            "nat",
            "prerouting",
            "{ type nat hook prerouting priority -100; }",
        ],
    )
    _nft_try(job, ["nft", "add", "chain", "inet", "filter", fwd_chain])
    _nft_try(job, ["nft", "flush", "chain", "inet", "filter", fwd_chain])
    _nft_try(job, ["nft", "add", "chain", "inet", "nat", post_chain])
    _nft_try(job, ["nft", "flush", "chain", "inet", "nat", post_chain])
    _nft_try(job, ["nft", "add", "chain", "inet", "nat", pre_chain])
    _nft_try(job, ["nft", "flush", "chain", "inet", "nat", pre_chain])

    # Check if jump rules exist, add if not
    for table, chain, jump_chain in [
        ("filter", "forward", fwd_chain),
        ("nat", "postrouting", post_chain),
        ("nat", "prerouting", pre_chain),
    ]:
        check = subprocess.run(
            ["nft", "list", "chain", "inet", table, chain],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if f"jump {jump_chain}" not in check.stdout:
            _nft_try(
                job,
                ["nft", "add", "rule", "inet", table, chain, "jump", jump_chain],
            )

    # Forward traffic through veth
    _run_cmd(
        job,
        [
            "nft",
            "add",
            "rule",
            "inet",
            "filter",
            fwd_chain,
            "iifname",
            veth_host,
            "accept",
        ],
        timeout=10,
    )
    _run_cmd(
        job,
        [
            "nft",
            "add",
            "rule",
            "inet",
            "filter",
            fwd_chain,
            "oifname",
            veth_host,
            "accept",
        ],
        timeout=10,
    )
    # Masquerade transit traffic
    _run_cmd(
        job,
        [
            "nft",
            "add",
            "rule",
            "inet",
            "nat",
            post_chain,
            "ip",
            "saddr",
            transit_cidr,
            "masquerade",
        ],
        timeout=10,
    )

    # EIP port forward DNAT in host namespace
    _setup_host_port_forward_dnat(
        job, gateway, pf_transit_ips, transit_ns_ip, pre_chain
    )

    _job_log(job, "Host nftables configured")


def _setup_host_port_forward_dnat(
    job, gateway, pf_transit_ips, transit_ns_ip, pre_chain
):
    """Set up EIP port forward DNAT rules in the host namespace."""
    if gateway.get("mode") != "nat-portforward":
        return
    for pf_idx, pf in enumerate(gateway.get("port_forwards", [])):
        ext_port = pf.get("extPort", "")
        int_ip = pf.get("intIp", "")
        int_port = pf.get("intPort", "")
        priv_ip = pf.get("_private_ip", "")
        transit_port = pf.get("_transit_port")
        effective_port = str(transit_port) if transit_port else str(ext_port)
        pf_transit_ip = pf_transit_ips.get(pf_idx, transit_ns_ip)
        if not (ext_port and int_ip and int_port):
            continue
        if priv_ip:
            _run_cmd(
                job,
                [
                    "nft",
                    "add",
                    "rule",
                    "inet",
                    "nat",
                    pre_chain,
                    "ip",
                    "daddr",
                    priv_ip,
                    "tcp",
                    "dport",
                    str(ext_port),
                    "dnat",
                    "ip",
                    "to",
                    f"{pf_transit_ip}:{ext_port}",
                ],
                timeout=10,
            )
        elif transit_port:
            _run_cmd(
                job,
                [
                    "nft",
                    "add",
                    "rule",
                    "inet",
                    "nat",
                    pre_chain,
                    "tcp",
                    "dport",
                    str(transit_port),
                    "dnat",
                    "ip",
                    "to",
                    f"{pf_transit_ip}:{effective_port}",
                ],
                timeout=10,
            )
        else:
            _job_log(
                job,
                f"Skipping port forward :{ext_port} — no EIP private IP or transit port",
            )


def _handle_network_full_setup(job, params):
    """Full VXLAN mesh network setup: namespace, veth, VXLAN, bridge, DHCP, nftables.

    Replaces the generate_setup_script() bash script with structured handler.
    Params:
        project_id: str
        host_ip: str  — this host's IP for VXLAN local binding
        networks: list of {vni, bridge_name, vxlan_name, cidr,
                           dhcp_enabled, dhcp_config, dns_enabled, dns_domain,
                           dhcp_hosts, peers, pxe_config}
        gateway: optional {mode, port_forwards, eip_private_ips, transit_ns_ip,
                           outbound_policy, outbound_ports}
        routers: list of {connected_vnis}
    """
    project_id = _validate_project_id(params["project_id"])
    host_ip = _validate_ip(params["host_ip"])
    networks = params.get("networks", [])
    gateway = params.get("gateway")
    routers = params.get("routers", [])

    pid = project_id[:8]
    ns = f"troshka-{pid}"
    veth_host = f"ve{pid}h"
    veth_ns = f"ve{pid}n"

    # Derive transit subnet from first VNI
    all_vnis = [int(net["vni"]) for net in networks]
    first_vni = all_vnis[0] if all_vnis else 1000
    transit_octet3 = first_vni & 0xFF
    transit_host_ip = f"172.30.{transit_octet3}.1"
    transit_ns_ip = f"172.30.{transit_octet3}.2"
    transit_cidr = f"172.30.{transit_octet3}.0/24"

    # qemu hook is installed by the agent install script — not managed here

    _setup_namespace_and_veth(
        job, ns, veth_host, veth_ns, transit_host_ip, transit_ns_ip, transit_cidr
    )

    # ── VXLAN + Bridge setup (inside namespace) ──
    for net in networks:
        _setup_vxlan_bridge(job, ns, host_ip, net, pid)

    # ── DHCP (dnsmasq inside namespace) ──
    for net in networks:
        _setup_dnsmasq_for_network(job, ns, project_id, net)

    _setup_chrony_ntp(job, ns, pid, networks)

    _setup_ns_nftables_base(job, ns, veth_ns)

    _setup_ns_nftables_forwarding(job, ns, networks, routers, pid)

    _setup_ns_outbound_rules(job, ns, veth_ns, networks, gateway)

    pf_transit_ips = _setup_ns_port_forward_dnat(
        job, ns, veth_ns, gateway, transit_octet3
    )
    _job_log(job, "Namespace nftables configured")

    _setup_host_nftables(
        job, pid, veth_host, transit_cidr, gateway, pf_transit_ips, transit_ns_ip
    )

    return {
        "project_id": project_id,
        "namespace": ns,
        "networks": len(networks),
        "status": "configured",
    }


COMMAND_HANDLERS["networks/full-setup"] = _handle_network_full_setup


def _assign_lb_ip_to_bridge(job, ns, lb_ip):
    """Assign an IP to the first non-BMC bridge in the namespace."""
    bridges = subprocess.run(
        ["ip", "netns", "exec", ns, "ip", "-o", "link", "show", "type", "bridge"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    bridge_name = ""
    for line in bridges.stdout.strip().split("\n"):
        if line and "br-bmc" not in line:
            bridge_name = line.split(":")[1].strip().split("@")[0]
            break
    if bridge_name:
        try:
            _run_cmd(
                job,
                [
                    "ip",
                    "netns",
                    "exec",
                    ns,
                    "ip",
                    "addr",
                    "add",
                    f"{lb_ip}/24",
                    "dev",
                    bridge_name,
                ],
                timeout=10,
            )
        except RuntimeError:
            pass


def _build_haproxy_config(haproxy_pid, frontends, backends, bind_addr):
    """Generate HAProxy configuration content."""
    lines = [
        "global",
        "    daemon",
        "    maxconn 4096",
        f"    pidfile {haproxy_pid}",
        "",
        "defaults",
        "    mode tcp",
        "    timeout connect 5s",
        "    timeout client 30s",
        "    timeout server 30s",
        "    option tcplog",
        "",
    ]
    for fe in frontends:
        fe_name = fe["name"].replace(" ", "-").lower()
        be_name = f"{fe_name}-servers"
        lines.append(f"frontend {fe_name}")
        lines.append(f"    bind {bind_addr}:{fe['bindPort']}")
        lines.append(f"    default_backend {be_name}")
        lines.append("")
        lines.append(f"backend {be_name}")
        lines.append("    balance roundrobin")
        for be in backends:
            lines.append(
                f"    server {be['name']} {be['ip']}:{fe['backendPort']} check"
            )
        lines.append("")
    return "\n".join(lines)


def _handle_lb_setup(job, params):
    """Set up HAProxy load balancer inside project namespace."""
    ns = params["ns"]
    project_id = _validate_project_id(params["project_id"])
    pid = project_id[:8]
    frontends = params.get("frontends", [])
    backends = params.get("backends", [])
    lb_ip = params.get("lb_ip", "")
    bind_addr = lb_ip if lb_ip else "*"

    if lb_ip:
        _assign_lb_ip_to_bridge(job, ns, lb_ip)

    haproxy_conf = f"/etc/haproxy/troshka-{pid}.cfg"
    haproxy_pid = f"/run/troshka-haproxy-{pid}.pid"

    config_content = _build_haproxy_config(haproxy_pid, frontends, backends, bind_addr)
    _job_log(job, f"Writing HAProxy config to {haproxy_conf}")

    os.makedirs("/etc/haproxy", exist_ok=True)
    with open(haproxy_conf, "w") as f:
        f.write(config_content)

    # Kill old HAProxy for this project
    if os.path.exists(haproxy_pid):
        try:
            with open(haproxy_pid) as f:
                old_pid = f.read().strip()
            if old_pid:
                _run_cmd(job, ["kill", "-9", old_pid], timeout=5, check=False)
        except Exception:
            pass

    # Start HAProxy in namespace
    _run_cmd(
        job,
        [
            "ip",
            "netns",
            "exec",
            ns,
            "haproxy",
            "-f",
            haproxy_conf,
            "-D",
            "-p",
            haproxy_pid,
        ],
        timeout=10,
        capture_output=False,
    )
    _job_log(job, f"HAProxy started in namespace {ns}")
    return {"status": "started", "config": haproxy_conf}


COMMAND_HANDLERS["lb/setup"] = _handle_lb_setup


def _handle_lb_teardown(job, params):
    """Tear down HAProxy for a project."""
    project_id = _validate_project_id(params["project_id"])
    pid = project_id[:8]

    haproxy_conf = f"/etc/haproxy/troshka-{pid}.cfg"
    haproxy_pid = f"/run/troshka-haproxy-{pid}.pid"

    if os.path.exists(haproxy_pid):
        try:
            with open(haproxy_pid) as f:
                old_pid = f.read().strip()
            if old_pid:
                _run_cmd(job, ["kill", "-9", old_pid], timeout=5, check=False)
        except Exception:
            pass

    for f_path in [haproxy_conf, haproxy_pid]:
        try:
            os.remove(f_path)
        except FileNotFoundError:
            pass

    _job_log(job, f"HAProxy teardown complete for project {pid}")
    return {"status": "torn_down"}


COMMAND_HANDLERS["lb/teardown"] = _handle_lb_teardown


def _teardown_haproxy(job, pid):
    """Kill HAProxy and remove config/pid files for a project."""
    haproxy_pid_file = f"/run/troshka-haproxy-{pid}.pid"
    if os.path.exists(haproxy_pid_file):
        try:
            with open(haproxy_pid_file) as f:
                hp_pid = f.read().strip()
            if hp_pid:
                _run_cmd(job, ["kill", "-9", hp_pid], timeout=5, check=False)
        except Exception:
            pass
    for hp_path in [f"/etc/haproxy/troshka-{pid}.cfg", haproxy_pid_file]:
        try:
            os.remove(hp_path)
        except FileNotFoundError:
            pass


def _teardown_dnsmasq(job, pid_short):
    """Kill dnsmasq processes and clean config/lease files for a project."""
    for pidfile in glob.glob(f"/run/troshka-dnsmasq-{pid_short}-*.pid"):
        try:
            with open(pidfile) as f:
                dnsmasq_pid = int(f.read().strip())
            _safe_kill(dnsmasq_pid, 9)
            _job_log(job, f"Killed dnsmasq PID {dnsmasq_pid}")
        except (ValueError, OSError):
            pass
        try:
            os.remove(pidfile)
        except FileNotFoundError:
            pass
    for pat in [
        f"/etc/dnsmasq.d/troshka-{pid_short}-*.conf",
        f"{_DNSMASQ_PREFIX}-{pid_short}-*.leases",
    ]:
        for f in glob.glob(pat):
            try:
                os.remove(f)
            except FileNotFoundError:
                pass


def _teardown_chronyd(job, pid):
    """Kill chronyd and clean config/drift files for a project."""
    chrony_pid_file = f"/run/troshka-chronyd-{pid}.pid"
    if os.path.exists(chrony_pid_file):
        try:
            with open(chrony_pid_file) as f:
                chrony_pid = int(f.read().strip())
            _safe_kill(chrony_pid, 9)
            _job_log(job, f"Killed chronyd PID {chrony_pid}")
        except (ValueError, OSError):
            pass
        try:
            os.remove(chrony_pid_file)
        except FileNotFoundError:
            pass
    for chrony_path in [
        f"{_CHRONY_DIR}/{pid}.conf",
        f"{_CHRONY_DIR}/{pid}.drift",
    ]:
        try:
            os.remove(chrony_path)
        except FileNotFoundError:
            pass


def _teardown_metadata_service(job, pid):
    """Kill metadata service and remove script + log for a project."""
    try:
        _run_cmd(
            job, ["pkill", "-9", "-f", f"metadata-{pid}.py"], timeout=5, check=False
        )
    except RuntimeError:
        pass
    for meta_path in [
        f"/opt/troshka/metadata-{pid}.py",
        f"/var/log/troshka-metadata-{pid}.log",
    ]:
        try:
            os.remove(meta_path)
            _job_log(job, f"Removed: {meta_path}")
        except FileNotFoundError:
            pass


def _remove_nft_jump_rules(table, main_chain, proj_chain):
    try:
        result = subprocess.run(
            ["nft", "-a", "list", "chain", "inet", table, main_chain],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in result.stdout.split("\n"):
            if f"jump {proj_chain}" in line:
                handle = line.strip().split("# handle ")[-1]
                subprocess.run(
                    [
                        "nft",
                        "delete",
                        "rule",
                        "inet",
                        table,
                        main_chain,
                        "handle",
                        handle,
                    ],
                    capture_output=True,
                    timeout=5,
                )
    except Exception:
        pass


def _teardown_host_nftables(job, pid):
    """Remove jump rules from main chains, then delete project chains."""
    fwd_chain = f"troshka-fwd-{pid}"
    post_chain = f"troshka-post-{pid}"
    pre_chain = f"troshka-pre-{pid}"
    for table, main_chain, proj_chain in [
        ("filter", "forward", fwd_chain),
        ("nat", "postrouting", post_chain),
        ("nat", "prerouting", pre_chain),
    ]:
        _remove_nft_jump_rules(table, main_chain, proj_chain)
        try:
            _run_cmd(
                job, ["nft", "flush", "chain", "inet", table, proj_chain], timeout=10
            )
            _run_cmd(
                job, ["nft", "delete", "chain", "inet", table, proj_chain], timeout=10
            )
        except RuntimeError:
            pass


def _teardown_single_pxe_vni(vni):
    """Tear down PXE services for a single VNI."""
    import shutil

    pid_file = f"/run/troshka-pxe-http-{vni}.pid"
    if os.path.exists(pid_file):
        try:
            with open(pid_file) as f:
                http_pid = int(f.read().strip())
            _safe_kill(http_pid, signal.SIGTERM)
        except (ValueError, ProcessLookupError, PermissionError):
            pass
        try:
            os.remove(pid_file)
        except FileNotFoundError:
            pass
    mount_point = f"{_PXE_DIR}/{vni}/mnt"
    try:
        subprocess.run(["umount", mount_point], capture_output=True, timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        pass
    pxe_dir = f"{_PXE_DIR}/{vni}"
    if os.path.isdir(pxe_dir):
        try:
            shutil.rmtree(pxe_dir)
        except OSError:
            pass


def _teardown_pxe_services(_job, vni_list):
    """Kill PXE HTTP servers, unmount ISOs, and remove PXE directories."""
    for vni in vni_list:
        _teardown_single_pxe_vni(vni)


def _teardown_vxlan_interfaces(job, ns, vni_list):
    """Delete VXLAN interfaces from namespace and host before namespace deletion."""
    for vni in vni_list:
        vxlan_if = f"vxlan-{vni}"
        try:
            _run_cmd(
                job,
                ["ip", "netns", "exec", ns, "ip", "link", "del", vxlan_if],
                timeout=10,
            )
        except RuntimeError:
            pass
        try:
            _run_cmd(job, ["ip", "link", "del", vxlan_if], timeout=10)
        except RuntimeError:
            pass


def _handle_network_full_teardown(job, params):
    """Tear down project networking: destroy VMs, delete namespace, clean up files.

    Replaces generate_destroy_script() for the network/cleanup portion.
    Params:
        project_id: str
        vni_list: list of VNI ints — for dnsmasq file cleanup
    """
    project_id = _validate_project_id(params["project_id"])
    vni_list = params.get("vni_list", [])

    pid = project_id[:8]
    ns = f"troshka-{pid}"
    veth_host = f"ve{pid}h"

    _teardown_vxlan_interfaces(job, ns, vni_list)

    _teardown_haproxy(job, pid)

    pid_short = project_id[:8] if project_id else ns.replace("troshka-", "")
    _teardown_dnsmasq(job, pid_short)

    _teardown_chronyd(job, pid)

    _teardown_metadata_service(job, pid)

    # Delete namespace
    try:
        _run_cmd(job, ["ip", "netns", "del", ns], timeout=10)
    except RuntimeError:
        _job_log(job, f"Namespace {ns} may not exist")

    # Delete host-side veth
    try:
        _run_cmd(job, ["ip", "link", "del", veth_host], timeout=10)
    except RuntimeError:
        pass

    _teardown_host_nftables(job, pid)

    # Clean up dnsmasq files
    for vni in vni_list:
        for path in [
            f"/run/troshka-dnsmasq-{vni}.pid",
            f"/etc/dnsmasq.d/troshka-{vni}.conf",
            f"{_DNSMASQ_PREFIX}-{vni}.leases",
        ]:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass

    _teardown_pxe_services(job, vni_list)

    # Delete bridges in host namespace (safe since VNIs are never recycled)
    for vni in vni_list:
        bridge = f"br-{vni}"
        try:
            _run_cmd(job, ["ip", "link", "delete", bridge], timeout=10)
            _job_log(job, f"Removed bridge: {bridge}")
        except RuntimeError:
            pass

    return {"project_id": project_id, "status": "torn_down"}


COMMAND_HANDLERS["networks/full-teardown"] = _handle_network_full_teardown


def _handle_mesh_setup(job, params):
    """Set up WireGuard interface for a project mesh.
    Params:
        project_id: str
        wg_private_key: str (base64)
        wg_address: str (e.g. "10.252.1.1/24")
        wg_port: int
        peers: list of {public_key, endpoint, allowed_ips}
    """
    project_id = params["project_id"]
    pid = project_id[:8]
    wg_iface = f"wg-{pid}"

    os.makedirs(_MESH_DIR, exist_ok=True)
    conf_path = f"{_MESH_DIR}/{project_id}.conf"

    conf_lines = [
        "[Interface]",
        f"PrivateKey = {params['wg_private_key']}",
        f"ListenPort = {params['wg_port']}",
        "",
    ]
    for peer in params["peers"]:
        conf_lines.extend(
            [
                "[Peer]",
                f"PublicKey = {peer['public_key']}",
                f"Endpoint = {peer['endpoint']}",
                f"AllowedIPs = {peer['allowed_ips']}",
                "PersistentKeepalive = 25",
                "",
            ]
        )

    with open(conf_path, "w") as f:
        f.write("\n".join(conf_lines))
    os.chmod(conf_path, 0o600)

    _run_cmd(job, ["ip", "link", "del", wg_iface], check=False)
    _run_cmd(job, ["ip", "link", "add", wg_iface, "type", "wireguard"])
    _run_cmd(job, ["wg", "setconf", wg_iface, conf_path])
    _run_cmd(job, ["ip", "addr", "add", params["wg_address"], "dev", wg_iface])
    _run_cmd(job, ["ip", "link", "set", wg_iface, "up"])

    for peer in params["peers"]:
        peer_ip = peer["allowed_ips"].split("/")[0]
        proc = _run_cmd(job, ["ping", "-c", "3", "-W", "2", peer_ip], check=False)
        if proc.returncode != 0:
            logger.warning(
                "Mesh peer %s not yet reachable (may connect later)", peer_ip
            )

    return {"status": "ok", "interface": wg_iface}


COMMAND_HANDLERS["mesh/setup"] = _handle_mesh_setup


def _handle_mesh_join_network(job, params):
    """Set up VXLAN + bridge on a remote (non-network) host.
    Params:
        project_id: str
        wg_local_ip: str -- this host's WireGuard tunnel IP (e.g. "10.252.1.2")
        networks: list of {vni, bridge_name, wg_peer_ips}
    """
    project_id = params["project_id"]
    pid = project_id[:8]
    ns = f"troshka-{pid}"
    wg_local_ip = params["wg_local_ip"]

    _run_cmd(job, ["ip", "netns", "add", ns], check=False)
    _run_cmd(job, ["ip", "netns", "exec", ns, "ip", "link", "set", "lo", "up"])

    for net in params["networks"]:
        vni = net["vni"]
        bridge = net["bridge_name"]
        vxlan_if = f"vxlan-{vni}"
        peers = net["wg_peer_ips"]

        _run_cmd(
            job,
            [
                "ip",
                "link",
                "add",
                vxlan_if,
                "type",
                "vxlan",
                "id",
                str(vni),
                "local",
                wg_local_ip,
                "dstport",
                "4789",
                "nolearning",
            ],
        )

        _run_cmd(job, ["ip", "link", "set", vxlan_if, "netns", ns])

        for peer_ip in peers:
            if peer_ip != wg_local_ip:
                _run_cmd(
                    job,
                    [
                        "ip",
                        "netns",
                        "exec",
                        ns,
                        "bridge",
                        "fdb",
                        "append",
                        "00:00:00:00:00:00",
                        "dev",
                        vxlan_if,
                        "dst",
                        peer_ip,
                    ],
                )

        _run_cmd(
            job,
            ["ip", "netns", "exec", ns, "ip", "link", "add", bridge, "type", "bridge"],
        )
        _run_cmd(
            job,
            [
                "ip",
                "netns",
                "exec",
                ns,
                "ip",
                "link",
                "set",
                vxlan_if,
                "master",
                bridge,
            ],
        )
        _run_cmd(job, ["ip", "netns", "exec", ns, "ip", "link", "set", vxlan_if, "up"])
        _run_cmd(job, ["ip", "netns", "exec", ns, "ip", "link", "set", bridge, "up"])

        _run_cmd(job, ["ip", "link", "add", bridge, "type", "bridge"], check=False)
        _run_cmd(
            job,
            [
                "ip",
                "link",
                "set",
                bridge,
                "type",
                "bridge",
                "forward_delay",
                "99",
                "ageing_time",
                "0",
            ],
            check=False,
        )
        _run_cmd(job, ["ip", "link", "set", bridge, "up"], check=False)

    return {"status": "ok", "namespace": ns}


COMMAND_HANDLERS["mesh/join-network"] = _handle_mesh_join_network


@route("DELETE", "/mesh/teardown")
def handle_mesh_teardown(handler, params):
    from urllib.parse import parse_qs, urlparse

    qs = parse_qs(urlparse(handler.path).query)
    project_id = params.get("project_id") or (qs.get("project_id", [None])[0])
    if not project_id:
        handler._send_json(400, {"error": "project_id required"})
        return

    pid = project_id[:8]
    wg_iface = f"wg-{pid}"
    conf_path = f"{_MESH_DIR}/{project_id}.conf"

    subprocess.run(["ip", "link", "del", wg_iface], capture_output=True, check=False)

    if os.path.exists(conf_path):
        os.remove(conf_path)

    handler._send_json(200, {"status": "ok"})


_CONF_EXT = ".conf"


@route("GET", "/mesh/status")
def handle_mesh_status(handler, params):
    result = {}
    mesh_dir = _MESH_DIR
    if not os.path.isdir(mesh_dir):
        handler._send_json(200, {"projects": {}})
        return

    for fname in os.listdir(mesh_dir):
        if not fname.endswith(_CONF_EXT):
            continue
        project_id = fname[: -len(_CONF_EXT)]
        pid = project_id[:8]
        wg_iface = f"wg-{pid}"

        try:
            out = subprocess.check_output(
                ["wg", "show", wg_iface, "latest-handshakes"],
                text=True,
                timeout=5,
            )
            peers = {}
            for line in out.strip().split("\n"):
                if "\t" in line:
                    pubkey, ts = line.split("\t", 1)
                    peers[pubkey] = int(ts)
            result[project_id] = {"interface": wg_iface, "peers": peers}
        except Exception:
            result[project_id] = {"interface": wg_iface, "error": "not running"}

    handler._send_json(200, {"projects": result})


def _handle_network_add_dnat(job, params):
    """Add two-hop nftables DNAT for external access to a VM inside a project namespace.

    Hop 1 (host level): transit_port → transit_ip:transit_port
    Hop 2 (namespace level): transit_ip:transit_port → dst_ip:dst_port

    This matches how EIP port forwards work — traffic enters via the host,
    gets DNATed to a secondary IP on the namespace veth, then the namespace
    DNATs to the actual VM.
    """
    ns = params["namespace"]
    pid = ns.replace("troshka-", "")
    transit_port = int(params["transit_port"])
    dst_ip = params["dst_ip"]
    dst_port = int(params["dst_port"])
    pre_chain = f"troshka-pre-{pid}"

    # Find the transit veth namespace IP (172.30.x.2)
    result = subprocess.run(
        ["ip", "netns", "exec", ns, "ip", "-4", "addr", "show"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    transit_ns_ip = ""
    for line in result.stdout.splitlines():
        line = line.strip()
        if "inet 172.30." in line and "/24" in line:
            ip = line.split()[1].split("/")[0]
            if ip.endswith(".2"):
                transit_ns_ip = ip
                break
    if not transit_ns_ip:
        raise RuntimeError(f"Cannot find transit namespace IP in {ns}")

    # Add secondary IP for this port forward
    octet3 = transit_ns_ip.split(".")[2]
    # Find next free secondary IP
    existing_ips = set()
    for line in result.stdout.splitlines():
        line = line.strip()
        if f"inet 172.30.{octet3}." in line:
            ip = line.split()[1].split("/")[0]
            existing_ips.add(ip)
    pf_ip_idx = 10
    while f"172.30.{octet3}.{pf_ip_idx}" in existing_ips:
        pf_ip_idx += 1
    pf_transit_ip = f"172.30.{octet3}.{pf_ip_idx}"

    # Add secondary IP to namespace veth
    ns_veth = f"ve{pid}n"
    _run_cmd(
        job,
        [
            "ip",
            "netns",
            "exec",
            ns,
            "ip",
            "addr",
            "add",
            f"{pf_transit_ip}/24",
            "dev",
            ns_veth,
        ],
        timeout=5,
        check=False,
    )

    # Hop 1: host-level DNAT — transit_port → pf_transit_ip:transit_port
    _run_cmd(
        job,
        [
            "nft",
            "add",
            "rule",
            "inet",
            "nat",
            pre_chain,
            "tcp",
            "dport",
            str(transit_port),
            "dnat",
            "ip",
            "to",
            f"{pf_transit_ip}:{transit_port}",
        ],
        timeout=10,
    )

    # Hop 2: namespace-level DNAT — pf_transit_ip:transit_port → dst_ip:dst_port
    _run_cmd(
        job,
        [
            "ip",
            "netns",
            "exec",
            ns,
            "nft",
            "add",
            "rule",
            "inet",
            "nat",
            "prerouting",
            "ip",
            "daddr",
            pf_transit_ip,
            "tcp",
            "dport",
            str(transit_port),
            "dnat",
            "ip",
            "to",
            f"{dst_ip}:{dst_port}",
        ],
        timeout=10,
    )

    _job_log(
        job,
        f"DNAT: :{transit_port} → {pf_transit_ip}:{transit_port} → {dst_ip}:{dst_port}",
    )
    return {
        "namespace": ns,
        "transit_port": transit_port,
        "transit_ip": pf_transit_ip,
        "dst": f"{dst_ip}:{dst_port}",
    }


COMMAND_HANDLERS["networks/add-dnat"] = _handle_network_add_dnat


def _handle_seed_create_batch(job, params):
    """Create multiple seed ISOs in one job call.

    Params:
        seeds: list of {path, meta_data, user_data, network_config}
    """
    seeds = params.get("seeds", [])
    if not seeds:
        raise ValueError("Missing required parameter: seeds")

    import tempfile as _tf

    created = 0
    for seed in seeds:
        path = _validate_path(seed["path"])
        meta_data = seed.get("meta_data", "")
        user_data = seed.get("user_data", "")
        network_config = seed.get("network_config", "")

        with _tf.TemporaryDirectory(dir=_TMP_DIR) as tmpdir:
            if meta_data:
                with open(os.path.join(tmpdir, "meta-data"), "w") as f:
                    f.write(meta_data)
            if user_data:
                with open(os.path.join(tmpdir, "user-data"), "w") as f:
                    f.write(user_data)
            if network_config:
                with open(os.path.join(tmpdir, "network-config"), "w") as f:
                    f.write(network_config)

            os.makedirs(os.path.dirname(path), exist_ok=True)
            _run_cmd(
                job,
                [
                    "xorriso",
                    "-as",
                    "genisoimage",
                    "-output",
                    path,
                    "-volid",
                    "cidata",
                    "-joliet",
                    "-rock",
                    tmpdir + "/",
                ],
            )
        _chown_qemu(path)
        created += 1
        _job_log(job, f"Seed ISO created: {path}")

    return {"created": created, "status": "completed"}


COMMAND_HANDLERS["seeds/create-batch"] = _handle_seed_create_batch


_METADATA_SCRIPT_TEMPLATE = """
import http.server
import json
import subprocess
import socketserver

CONFIGS = {configs_json}

def get_mac_for_ip(ip):
    try:
        result = subprocess.run(["ip", "neigh", "show", ip], capture_output=True, text=True)
        for line in result.stdout.strip().split("\\n"):
            parts = line.split()
            if len(parts) >= 5 and parts[0] == ip:
                return parts[4].lower()
    except Exception:
        pass
    return None

class MetadataHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        client_ip = self.client_address[0]
        mac = get_mac_for_ip(client_ip)
        config = CONFIGS.get(mac, {{}})
        meta = json.loads(config.get("metadata", "{{}}"))
        vm_name = config.get("vm_name", "troshka-vm")

        if self.path in ("/latest/user-data", "/latest/user-data/"):
            self.send_response(200)
            self.send_header("Content-Type", "text/yaml")
            self.end_headers()
            self.wfile.write(config.get("userdata", "").encode())
        elif self.path in ("/latest/meta-data/", "/latest/meta-data"):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ami-id\\ninstance-id\\nlocal-hostname\\nhostname\\ninstance-type\\n")
        elif self.path == "/latest/meta-data/instance-id":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(meta.get("instance-id", vm_name).encode())
        elif self.path in ("/latest/meta-data/local-hostname", "/latest/meta-data/hostname"):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(meta.get("local-hostname", vm_name).encode())
        elif self.path == "/latest/meta-data/ami-id":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"troshka-image")
        elif self.path == "/latest/meta-data/instance-type":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"troshka.nested")
        elif self.path in ("/", "/latest", "/latest/"):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"latest\\n")
        else:
            self.send_response(200)
            self.end_headers()

socketserver.TCPServer.allow_reuse_address = True
server = http.server.HTTPServer(("169.254.169.254", 80), MetadataHandler)
server.serve_forever()
"""


def _handle_metadata_deploy(job, params):
    """Deploy the cloud-init metadata service inside a network namespace."""
    project_id = _validate_project_id(params["project_id"])
    bridges = params.get("bridges", [])
    vm_configs = params.get("vm_configs", {})
    namespace = params.get("namespace", f"troshka-{project_id[:8]}")

    # Validate bridges
    for bridge in bridges:
        _validate_bridge_name(bridge)

    # Step 1: Kill existing metadata service for this project
    try:
        _run_cmd(job, ["pkill", "-9", "-f", f"metadata-{project_id[:8]}.py"], timeout=5)
        _job_log(job, "Killed existing metadata service (if any)")
    except RuntimeError:
        _job_log(job, "No existing metadata service to kill")

    # Step 2: Add metadata IP to each bridge inside namespace
    for bridge in bridges:
        try:
            _run_cmd(
                job,
                [
                    "ip",
                    "netns",
                    "exec",
                    namespace,
                    "ip",
                    "addr",
                    "add",
                    "169.254.169.254/32",
                    "dev",
                    bridge,
                ],
                timeout=10,
            )
            _job_log(job, f"Added metadata IP to {bridge} in {namespace}")
        except RuntimeError as e:
            if "File exists" in str(e) or "RTNETLINK answers: File exists" in str(e):
                _job_log(job, f"Metadata IP already exists on {bridge}, continuing")
            else:
                raise

    # Step 3: Write metadata service script
    script_path = f"/opt/troshka/metadata-{project_id[:8]}.py"
    configs_json = json.dumps(vm_configs)
    script_content = _METADATA_SCRIPT_TEMPLATE.format(configs_json=configs_json)

    os.makedirs("/opt/troshka", exist_ok=True)
    with open(script_path, "w") as f:
        f.write(script_content)
    _job_log(job, f"Wrote metadata service script to {script_path}")

    # Step 4: Start metadata service in namespace
    log_file = f"/var/log/troshka-metadata-{project_id[:8]}.log"
    proc = subprocess.Popen(
        ["ip", "netns", "exec", namespace, "nohup", "python3", script_path],
        stdout=open(log_file, "w"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    time.sleep(0.5)  # Let process start

    # Check if process is still running
    if proc.poll() is not None:
        _job_log(
            job,
            f"Warning: metadata service may have failed to start (check {log_file})",
        )
        return {
            "status": "started",
            "pid": None,
            "warning": "Process exited immediately",
        }

    pid = proc.pid
    _job_log(
        job, f"Started metadata service in {namespace} (PID {pid}, log: {log_file})"
    )

    return {"status": "started", "pid": pid}


COMMAND_HANDLERS["metadata/deploy"] = _handle_metadata_deploy


def _handle_eip_configure(job, params):
    project_id = _validate_project_id(params["project_id"])
    eip_mappings = params.get("eip_mappings", [])
    ns = f"troshka-{project_id[:8]}"

    for mapping in eip_mappings:
        _validate_ip(mapping["public_ip"])
        private_ip = _validate_ip(mapping["private_ip"])
        _run_cmd(
            job,
            [
                "ip",
                "netns",
                "exec",
                ns,
                "nft",
                "add",
                "rule",
                "ip",
                "nat",
                "postrouting",
                "ip",
                "saddr",
                private_ip,
                "counter",
                "masquerade",
            ],
        )

    return {"project_id": project_id, "status": "configured"}


COMMAND_HANDLERS["eips/configure"] = _handle_eip_configure


# ── Operations handlers ──


def _discover_orphan_dirs(job, known_project_ids):
    """Scan VM dirs for orphan project dirs (local + shared storage)."""
    orphan_dirs = []
    for vms_dir in [
        _VMS_DIR,
        f"{_SHARED_DIR}/vms",
        f"{_LOCAL_DIR}/vms",
    ]:
        if not os.path.exists(vms_dir):
            continue
        try:
            for entry in os.listdir(vms_dir):
                if entry not in known_project_ids:
                    full_path = os.path.join(vms_dir, entry)
                    if os.path.isdir(full_path):
                        orphan_dirs.append(full_path + "/")
                        _job_log(job, f"Orphan dir: {full_path}/")
        except Exception as e:
            _job_log(job, f"Failed to scan {vms_dir}: {e}")
    return orphan_dirs


def _discover_orphan_containers(job, known_project_ids):
    """List orphan containers (podman) starting with troshka-."""
    orphan_containers = []
    try:
        result = subprocess.run(
            [
                "podman",
                "ps",
                "-a",
                "--filter",
                _TROSHKA_FILTER,
                "--format",
                _PODMAN_NAMES_FMT,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            for ctr_name in result.stdout.strip().split("\n"):
                ctr_name = ctr_name.strip()
                if not ctr_name or not ctr_name.startswith("troshka-"):
                    continue
                # Extract project prefix: troshka-{project_id[:8]}-{container_id[:8]}
                parts = ctr_name.split("-", 2)
                if len(parts) >= 2:
                    proj_prefix = parts[1]
                    if not any(
                        pid.startswith(proj_prefix) for pid in known_project_ids
                    ):
                        orphan_containers.append(ctr_name)
                        _job_log(job, f"Orphan container: {ctr_name}")
    except Exception as e:
        _job_log(job, f"Failed to list podman containers: {e}")
    return orphan_containers


def _discover_orphan_domains(job, known_domains):
    """List all virsh domains starting with troshka- that don't belong to known projects."""
    orphan_domains = []
    known_domain_prefixes = set(known_domains)
    try:
        result = subprocess.run(
            ["virsh", "list", "--all", "--name"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            for domain in result.stdout.strip().split("\n"):
                domain = domain.strip()
                if not domain.startswith("troshka-"):
                    continue
                if not any(
                    domain.startswith(prefix) for prefix in known_domain_prefixes
                ):
                    orphan_domains.append(domain)
                    _job_log(job, f"Orphan domain: {domain}")
    except Exception as e:
        _job_log(job, f"Failed to list virsh domains: {e}")
    return orphan_domains


def _collect_vm_bridges():
    all_vm_bridges = set()
    vm_list = subprocess.run(
        ["virsh", "list", "--all", "--name"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    for vm_name in vm_list.stdout.strip().split("\n"):
        if not vm_name.strip():
            continue
        xml = subprocess.run(
            ["virsh", "dumpxml", vm_name.strip()],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if xml.returncode == 0:
            all_vm_bridges.update(_SOURCE_BRIDGE_RE.findall(xml.stdout))
    return all_vm_bridges


def _check_bridge_is_orphan(bridge_name, all_vm_bridges):
    """Return True if the bridge is an orphan (br-* prefix, not used by VMs, exists in host namespace)."""
    if not bridge_name.startswith("br-") or bridge_name in all_vm_bridges:
        return False
    ns_check = subprocess.run(
        ["ip", "link", "show", bridge_name],
        capture_output=True,
        timeout=5,
    )
    return ns_check.returncode == 0


def _find_orphan_host_bridges(job, all_vm_bridges):
    orphan_bridges = []
    result = subprocess.run(
        ["ip", "-o", "link", "show", "type", "bridge"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode == 0:
        for line in result.stdout.strip().split("\n"):
            parts = line.split(":", 2)
            if len(parts) >= 2:
                bridge_name = parts[1].strip().split("@")[0]
                if _check_bridge_is_orphan(bridge_name, all_vm_bridges):
                    orphan_bridges.append(bridge_name)
                    _job_log(job, f"Orphan bridge: {bridge_name}")
    return orphan_bridges


def _discover_orphan_bridges(job):
    """List orphan bridges — both br-troshka-* and dummy br-{vni} bridges not referenced by any defined VM."""
    orphan_bridges = []
    try:
        all_vm_bridges = _collect_vm_bridges()
        orphan_bridges = _find_orphan_host_bridges(job, all_vm_bridges)
    except Exception as e:
        _job_log(job, f"Failed to list bridges: {e}")
    return orphan_bridges


def _discover_orphan_namespaces(job, known_project_ids):
    """List namespaces matching troshka-* that don't belong to known projects."""
    orphan_namespaces = []
    known_ns_prefixes = {f"troshka-{pid[:8]}" for pid in known_project_ids}
    try:
        result = subprocess.run(
            ["ip", "netns", "list"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                if line.startswith("troshka-"):
                    ns_name = line.split()[0]
                    if ns_name not in known_ns_prefixes:
                        orphan_namespaces.append(ns_name)
                        _job_log(job, f"Orphan namespace: {ns_name}")
    except Exception as e:
        _job_log(job, f"Failed to list namespaces: {e}")
    return orphan_namespaces


def _discover_cache_items(job):
    """Scan cache dirs for staleness (report all items, backend will decide eviction)."""
    cache_items = []
    local = _config.get("local_mount", _LOCAL_DIR)
    cache_dirs = [
        (f"{local}/cache/patterns", "pattern"),
        (f"{_TROSHKA_DIR}/cache/patterns", "pattern"),
        (f"{_TROSHKA_DIR}/cache/snapshots", "snapshot"),
        (f"{_TROSHKA_DIR}/images", "image"),
        (f"{_LOCAL_DIR}/cache/patterns", "pattern"),
        (f"{_LOCAL_DIR}/cache/snapshots", "snapshot"),
        (f"{_SHARED_DIR}/images", "image"),
        (f"{_SHARED_DIR}/cache/patterns", "pattern"),
        (f"{_SHARED_DIR}/cache/snapshots", "snapshot"),
    ]
    for cache_dir, item_type in cache_dirs:
        if os.path.exists(cache_dir):
            try:
                for entry in os.listdir(cache_dir):
                    full_path = os.path.join(cache_dir, entry)
                    try:
                        stat = os.stat(full_path)
                        age_hours = (time.time() - stat.st_atime) / 3600
                        cache_items.append(
                            {
                                "path": full_path,
                                "type": item_type,
                                "age_hours": int(age_hours),
                            }
                        )
                    except Exception:
                        pass
            except Exception as e:
                _job_log(job, f"Failed to scan {cache_dir}: {e}")
    return cache_items


def _discover_orphan_leases(job, known_project_ids):
    """Clean orphan dnsmasq lease files (stale files from deleted projects)."""
    known_prefixes = {pid[:8] for pid in known_project_ids}
    for lf in glob.glob(f"{_DNSMASQ_PREFIX}-*.leases"):
        prefix = os.path.basename(lf).replace("dnsmasq-", "").split("-")[0]
        if prefix not in known_prefixes:
            try:
                os.remove(lf)
                _job_log(job, f"Cleaned orphan lease: {os.path.basename(lf)}")
            except OSError:
                pass


def _discover_orphan_bmc(job, known_bmc_project_ids):
    """Discover orphaned BMC directories."""
    orphaned_bmc = []
    bmc_base = _BMC_DIR
    known_bmc = set(known_bmc_project_ids)
    if os.path.isdir(bmc_base):
        for entry in os.listdir(bmc_base):
            full = os.path.join(bmc_base, entry)
            if os.path.isdir(full) and entry not in known_bmc:
                orphaned_bmc.append(entry)
                _job_log(job, f"Orphaned BMC dir: {entry}")
    return orphaned_bmc


def _get_active_tmpdirs():
    """Collect all active temp directories from running jobs."""
    active_tmpdirs = set()
    with _jobs_lock:
        for j in _jobs.values():
            if j["status"] == "running":
                for td in j.get("_tmpdirs", []):
                    active_tmpdirs.add(os.path.realpath(td))
    return active_tmpdirs


def _discover_stale_temps(job):
    """Scan temp dir — cross-reference against running jobs' _tmpdirs."""
    stale_temps = []
    _s3_tmpdir = os.path.join(_config.get("local_mount", _LOCAL_DIR), "tmp")
    if os.path.exists(_s3_tmpdir):
        active_tmpdirs = _get_active_tmpdirs()
        try:
            for entry in os.listdir(_s3_tmpdir):
                full_path = os.path.realpath(os.path.join(_s3_tmpdir, entry))
                if full_path not in active_tmpdirs:
                    stale_temps.append(full_path)
                    _job_log(job, f"Orphaned temp: {entry}")
        except OSError as e:
            _job_log(job, f"Failed to scan temp dir: {e}")
    return stale_temps


def _discover_orphan_metadata(job, known_project_ids):
    """Discover orphaned metadata scripts."""
    orphaned_metadata_ids = []
    known_prefixes = {pid[:8] for pid in known_project_ids}
    for entry in glob.glob("/opt/troshka/metadata-*.py"):
        prefix = os.path.basename(entry).replace("metadata-", "").replace(".py", "")
        if prefix not in known_prefixes:
            orphaned_metadata_ids.append(prefix)
            _job_log(job, f"Orphaned metadata: {entry}")
    return orphaned_metadata_ids


def _handle_gc_discover(job, params):
    """Scan host for orphaned resources (dirs, domains, bridges, namespaces, cache items)."""
    known_project_ids = params.get("known_project_ids", [])
    known_domains = params.get("known_domains", [])
    known_bmc_project_ids = params.get("known_bmc_project_ids", [])

    orphan_dirs = _discover_orphan_dirs(job, known_project_ids)
    orphan_containers = _discover_orphan_containers(job, known_project_ids)
    orphan_domains = _discover_orphan_domains(job, known_domains)
    orphan_bridges = _discover_orphan_bridges(job)
    orphan_namespaces = _discover_orphan_namespaces(job, known_project_ids)
    cache_items = _discover_cache_items(job)
    _discover_orphan_leases(job, known_project_ids)
    orphaned_bmc = _discover_orphan_bmc(job, known_bmc_project_ids)
    stale_temps = _discover_stale_temps(job)
    orphaned_metadata_ids = _discover_orphan_metadata(job, known_project_ids)

    return {
        "orphan_dirs": orphan_dirs,
        "orphan_domains": orphan_domains,
        "orphan_containers": orphan_containers,
        "orphan_bridges": orphan_bridges,
        "orphan_namespaces": orphan_namespaces,
        "cache_items": cache_items,
        "orphaned_bmc_project_ids": orphaned_bmc,
        "orphaned_metadata_ids": orphaned_metadata_ids,
        "stale_temps": stale_temps,
    }


COMMAND_HANDLERS[_GC_DISCOVER_CMD] = _handle_gc_discover


def _clean_orphan_dirs(job, orphan_dirs):
    """Remove orphan directories with NFS retry logic. Returns count removed."""
    removed = 0
    for path in orphan_dirs:
        try:
            validated = _validate_path(path)
            if os.path.isdir(validated):
                shutil.rmtree(validated)
                _job_log(job, f"Removed dir: {validated}")
                removed += 1
        except OSError:
            # rmtree may fail on NFS (busy .nfs* files) — retry: delete visible files, then rmdir
            try:
                validated = _validate_path(path)
                for entry in os.listdir(validated):
                    fp = os.path.join(validated, entry)
                    try:
                        os.remove(fp)
                    except OSError:
                        pass
                os.rmdir(validated)
                _job_log(job, f"Removed dir (retry): {validated}")
                removed += 1
            except OSError as e2:
                _job_log(job, f"Failed to remove {path}: {e2}")
    return removed


def _clean_orphan_domains(job, orphan_domains):
    """Remove orphan domains via virsh destroy + undefine. Returns count removed."""
    removed = 0
    for domain in orphan_domains:
        try:
            _validate_domain_name(domain)
            # Try to destroy (force stop) — may fail if already stopped
            try:
                _run_cmd(job, ["virsh", "destroy", domain], timeout=30)
            except RuntimeError:
                _job_log(job, f"Domain {domain} may already be stopped")
            _run_cmd(job, ["virsh", "undefine", domain, "--nvram"], timeout=30)
            _job_log(job, f"Removed domain: {domain}")
            removed += 1
        except Exception as e:
            _job_log(job, f"Failed to remove domain {domain}: {e}")
    return removed


def _clean_orphan_containers(job, orphan_containers):
    """Remove orphan containers via podman stop + rm. Returns count removed."""
    removed = 0
    for ctr in orphan_containers:
        try:
            if not ctr.startswith("troshka-"):
                raise ValueError(f"Invalid container name: {ctr}")
            try:
                _run_cmd(job, ["podman", "stop", "-t", "5", ctr], timeout=15)
            except RuntimeError:
                pass
            _run_cmd(job, ["podman", "rm", "-f", ctr], timeout=15)
            _job_log(job, f"Removed container: {ctr}")
            removed += 1
        except Exception as e:
            _job_log(job, f"Failed to remove container {ctr}: {e}")
    return removed


def _kill_bmc_processes(job, bmc_dir):
    """Kill all BMC processes in a directory."""
    for fname in os.listdir(bmc_dir):
        if fname.endswith(".pid"):
            pid_path = os.path.join(bmc_dir, fname)
            try:
                with open(pid_path) as f:
                    p = int(f.read().strip())
                _safe_kill(p, signal.SIGTERM)
                _job_log(job, f"Killed BMC process PID {p} ({fname})")
            except (
                ValueError,
                ProcessLookupError,
                PermissionError,
                FileNotFoundError,
            ):
                pass


def _remove_bmc_bridge(job, project_id):
    """Remove BMC bridge in namespace and host."""
    pid_short = project_id[:8]
    bridge = f"br-bmc-{pid_short}"
    ns = f"troshka-{pid_short}"
    try:
        _run_cmd(
            job,
            ["ip", "netns", "exec", ns, "ip", "link", "del", bridge],
            timeout=10,
        )
    except RuntimeError:
        pass
    try:
        _run_cmd(job, ["ip", "link", "del", bridge], timeout=10)
    except RuntimeError:
        pass


def _clean_orphan_bmc(job, orphan_bmc_ids):
    """Clean up orphaned BMC resources (PIDs, pools, dirs, bridges). Returns count removed."""
    removed = 0
    for project_id in orphan_bmc_ids:
        bmc_dir = f"{_BMC_DIR}/{project_id}"
        if os.path.isdir(bmc_dir):
            _kill_bmc_processes(job, bmc_dir)
            pool_name = f"troshka-vmedia-{project_id[:8]}"
            subprocess.run(
                ["virsh", "pool-destroy", pool_name], capture_output=True, timeout=10
            )
            subprocess.run(
                ["virsh", "pool-undefine", pool_name], capture_output=True, timeout=10
            )
            shutil.rmtree(bmc_dir, ignore_errors=True)
            _job_log(job, f"Removed BMC dir + pool: {bmc_dir}")
            removed += 1

        _remove_bmc_bridge(job, project_id)
    return removed


def _clean_stale_temps(job, stale_temps):
    """Remove stale temp files with containment check. Returns count removed."""
    removed = 0
    _s3_tmpdir = os.path.join(_config.get("local_mount", _LOCAL_DIR), "tmp")
    real_tmpdir = os.path.realpath(_s3_tmpdir)
    for path in stale_temps:
        try:
            real_path = os.path.realpath(path)
            if not real_path.startswith(real_tmpdir + os.sep):
                _job_log(job, f"Rejected path outside temp dir: {path}")
                continue
            if os.path.isdir(real_path):
                shutil.rmtree(real_path)
            else:
                os.remove(real_path)
            _job_log(job, f"Removed stale temp: {real_path}")
            removed += 1
        except OSError as e:
            _job_log(job, f"Failed to remove {path}: {e}")
    return removed


def _clean_orphan_bridges(job, orphan_bridges):
    """Remove orphan bridges. Returns count removed."""
    removed = 0
    for bridge in orphan_bridges:
        try:
            _validate_bridge_name(bridge)
            _run_cmd(job, ["ip", "link", "delete", bridge], timeout=10)
            _job_log(job, f"Removed bridge: {bridge}")
            removed += 1
        except Exception as e:
            _job_log(job, f"Failed to remove bridge {bridge}: {e}")
    return removed


def _clean_orphan_namespaces(job, orphan_namespaces):
    """Remove orphan namespaces. Returns count removed."""
    removed = 0
    for ns in orphan_namespaces:
        try:
            if not ns.startswith("troshka-"):
                raise ValueError(f"Invalid namespace name: {ns}")
            _run_cmd(job, ["ip", "netns", "delete", ns], timeout=10)
            _job_log(job, f"Removed namespace: {ns}")
            removed += 1
        except Exception as e:
            _job_log(job, f"Failed to remove namespace {ns}: {e}")
    return removed


def _clean_cache_items(job, cache_items):
    """Remove cache items. Returns count removed."""
    removed = 0
    for path in cache_items:
        try:
            validated = _validate_path(path)
            if os.path.isdir(validated):
                shutil.rmtree(validated)
                _job_log(job, f"Removed cache dir: {validated}")
            else:
                os.remove(validated)
                _job_log(job, f"Removed cache file: {validated}")
            removed += 1
        except FileNotFoundError:
            _job_log(job, f"Cache item not found (skipped): {path}")
        except Exception as e:
            _job_log(job, f"Failed to remove cache item {path}: {e}")
    return removed


def _clean_orphan_metadata(job, orphan_metadata_ids):
    """Remove orphan metadata scripts and logs. Returns count removed."""
    removed = 0
    for project_id in orphan_metadata_ids:
        pid_short = project_id[:8]
        try:
            _run_cmd(
                job,
                ["pkill", "-9", "-f", f"metadata-{pid_short}.py"],
                timeout=5,
                check=False,
            )
        except RuntimeError:
            pass
        for meta_path in [
            f"/opt/troshka/metadata-{pid_short}.py",
            f"/var/log/troshka-metadata-{pid_short}.log",
        ]:
            try:
                os.remove(meta_path)
                _job_log(job, f"Removed orphan: {meta_path}")
                removed += 1
            except FileNotFoundError:
                pass
    return removed


def _handle_gc_clean(job, params):
    """Remove specific orphaned resources provided by the backend."""
    removed_dirs = _clean_orphan_dirs(job, params.get("orphan_dirs", []))
    removed_domains = _clean_orphan_domains(job, params.get("orphan_domains", []))
    removed_containers = _clean_orphan_containers(
        job, params.get("orphan_containers", [])
    )
    removed_bridges = _clean_orphan_bridges(job, params.get("orphan_bridges", []))
    removed_namespaces = _clean_orphan_namespaces(
        job, params.get("orphan_namespaces", [])
    )
    removed_cache = _clean_cache_items(job, params.get("cache_items", []))
    removed_bmc = _clean_orphan_bmc(job, params.get("orphan_bmc_project_ids", []))
    removed_metadata = _clean_orphan_metadata(
        job, params.get("orphan_metadata_ids", [])
    )
    removed_temps = _clean_stale_temps(job, params.get("stale_temps", []))

    return {
        "removed_dirs": removed_dirs,
        "removed_domains": removed_domains,
        "removed_containers": removed_containers,
        "removed_bridges": removed_bridges,
        "removed_namespaces": removed_namespaces,
        "removed_cache": removed_cache,
        "removed_bmc": removed_bmc,
        "removed_metadata": removed_metadata,
        "removed_temps": removed_temps,
    }


COMMAND_HANDLERS["gc/clean"] = _handle_gc_clean


def _handle_snapshot_create(job, params):
    domain = _validate_domain_name(params["domain_name"])
    output_path = _validate_path(params["output_path"])

    # Shut down VM first for consistent snapshot
    try:
        _run_cmd(job, ["virsh", "shutdown", domain], timeout=60)
        # Wait for VM to stop (up to 60s)
        for _ in range(60):
            result = subprocess.run(
                ["virsh", "domstate", domain],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if "shut off" in result.stdout:
                break
            time.sleep(1)
    except RuntimeError:
        _job_log(job, "VM may already be stopped")

    # Get disk path from domain XML
    result = subprocess.run(
        ["virsh", "domblklist", domain, "--details"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    disk_path = None
    if result.returncode == 0:
        for line in result.stdout.strip().split("\n"):
            parts = line.split()
            if len(parts) >= 4 and parts[1] == "disk":
                disk_path = parts[3]
                break

    if not disk_path:
        raise RuntimeError(f"Could not find disk for domain {domain}")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    _run_cmd(
        job,
        ["qemu-img", "convert", "-O", "qcow2", disk_path, output_path],
        timeout=3600,
    )

    return {"domain": domain, "output_path": output_path, "status": "created"}


COMMAND_HANDLERS["snapshots/create"] = _handle_snapshot_create


def _get_disk_path_by_index(domain, disk_index):
    """Get disk path from virsh domblklist by index."""
    result = subprocess.run(
        ["virsh", "domblklist", domain, "--details"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to get disk list for domain {domain}")

    disk_count = 0
    for line in result.stdout.strip().split("\n"):
        parts = line.split()
        if len(parts) >= 4 and parts[1] == "disk":
            if disk_count == disk_index:
                return parts[3]
            disk_count += 1

    raise RuntimeError(
        f"Disk index {disk_index} not found for domain {domain} (found {disk_count} disks)"
    )


def _build_s3_env(
    aws_access_key="", aws_secret_key="", aws_region="us-east-1", aws_endpoint_url=""
):
    """Build environment dict for AWS CLI S3 operations."""
    env = os.environ.copy()
    _s3_tmpdir = os.path.join(_config.get("local_mount", _LOCAL_DIR), "tmp")
    os.makedirs(_s3_tmpdir, exist_ok=True)
    env["TMPDIR"] = _s3_tmpdir
    if aws_access_key:
        env["AWS_ACCESS_KEY_ID"] = aws_access_key
        env["AWS_SECRET_ACCESS_KEY"] = aws_secret_key
        env["AWS_DEFAULT_REGION"] = aws_region
    if aws_endpoint_url:
        env["AWS_ENDPOINT_URL"] = aws_endpoint_url
    return env


def _read_proc_upload_bytes(pid):
    """Read bytes uploaded by a process from /proc/{pid}/io. Returns -1 on failure."""
    try:
        with open(f"/proc/{pid}/io") as f:
            for line in f:
                if line.startswith("read_bytes:"):
                    return int(line.split(":")[1].strip())
    except OSError:
        pass
    return -1


def _s3_upload(
    job,
    local_path,
    s3_url,
    aws_access_key="",
    aws_secret_key="",
    aws_region="us-east-1",
    aws_endpoint_url="",
):
    """Upload a file to S3 using aws cli with file-size progress monitoring."""
    total_bytes = os.path.getsize(local_path)
    total_gb = round(total_bytes / (1024**3), 1)
    env = _build_s3_env(aws_access_key, aws_secret_key, aws_region, aws_endpoint_url)
    aws_bin = _AWS_CLI
    if not os.path.exists(aws_bin):
        aws_bin = "aws"
    proc = subprocess.Popen(
        [aws_bin, "s3", "cp", local_path, s3_url],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    job["_process"] = proc
    try:
        while proc.poll() is None:
            if job.get("_cancelled"):
                proc.kill()
                proc.wait()
                raise RuntimeError("S3 upload cancelled")
            read_bytes = _read_proc_upload_bytes(proc.pid)
            if read_bytes >= 0:
                cur_gb = round(read_bytes / (1024**3), 1)
                pct = (
                    min(100, int(read_bytes * 100 / total_bytes))
                    if total_bytes > 0
                    else 0
                )
                _job_log(job, f"Uploading: {cur_gb} of {total_gb} GB ({pct}%)")
            time.sleep(5)
        if proc.returncode != 0:
            raise RuntimeError(f"S3 upload failed (exit {proc.returncode})")
    finally:
        job["_process"] = None


def _format_upload_progress(pid, total_bytes, total_gb):
    """Format upload progress string from /proc/pid/io. Returns string or None."""
    read_bytes = _read_proc_upload_bytes(pid)
    if read_bytes < 0:
        return None
    cur_gb = round(read_bytes / (1024**3), 1)
    pct = min(100, int(read_bytes * 100 / total_bytes)) if total_bytes > 0 else 0
    return f"Uploading: {cur_gb} of {total_gb} GB ({pct}%)"


def _format_cache_progress(cache_path, total_bytes, total_gb):
    """Format cache copy progress string. Returns string or None."""
    try:
        if not os.path.exists(cache_path):
            return None
        cached = os.path.getsize(cache_path)
        cache_pct = min(100, int(cached * 100 / total_bytes)) if total_bytes > 0 else 0
        if cache_pct < 100:
            cached_gb = round(cached / (1024**3), 1)
            return f"Caching: {cached_gb} of {total_gb} GB ({cache_pct}%)"
    except OSError:
        pass
    return None


def _s3_upload_with_cache(
    job,
    local_path,
    total_bytes,
    s3_url,
    cache_path,
    aws_access_key="",
    aws_secret_key="",
    aws_region="us-east-1",
    aws_endpoint_url="",
):
    """Upload to S3 while reporting combined upload + cache progress."""
    total_gb = round(total_bytes / (1024**3), 1)
    env = _build_s3_env(aws_access_key, aws_secret_key, aws_region, aws_endpoint_url)
    aws_bin = _AWS_CLI
    if not os.path.exists(aws_bin):
        aws_bin = "aws"
    proc = subprocess.Popen(
        [aws_bin, "s3", "cp", local_path, s3_url],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    job["_process"] = proc
    try:
        while proc.poll() is None:
            if job.get("_cancelled"):
                proc.kill()
                proc.wait()
                raise RuntimeError("S3 upload cancelled")
            parts = []
            upload_msg = _format_upload_progress(proc.pid, total_bytes, total_gb)
            if upload_msg:
                parts.append(upload_msg)
            cache_msg = _format_cache_progress(cache_path, total_bytes, total_gb)
            if cache_msg:
                parts.append(cache_msg)
            if parts:
                _job_log(job, " / ".join(parts))
            time.sleep(5)
        if proc.returncode != 0:
            raise RuntimeError(f"S3 upload failed (exit {proc.returncode})")
    finally:
        job["_process"] = None


def _s3_download(
    job,
    s3_url,
    local_path,
    aws_access_key="",
    aws_secret_key="",
    aws_region="us-east-1",
    aws_endpoint_url="",
):
    """Download a file from S3 using aws cli with file-size progress monitoring."""
    env = os.environ.copy()
    _s3_tmpdir = os.path.join(_config.get("local_mount", _LOCAL_DIR), "tmp")
    os.makedirs(_s3_tmpdir, exist_ok=True)
    env["TMPDIR"] = _s3_tmpdir
    if aws_access_key:
        env["AWS_ACCESS_KEY_ID"] = aws_access_key
        env["AWS_SECRET_ACCESS_KEY"] = aws_secret_key
        env["AWS_DEFAULT_REGION"] = aws_region
    if aws_endpoint_url:
        env["AWS_ENDPOINT_URL"] = aws_endpoint_url
    aws_bin = _AWS_CLI
    if not os.path.exists(aws_bin):
        aws_bin = "aws"
    proc = subprocess.Popen(
        [aws_bin, "s3", "cp", s3_url, local_path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    while proc.poll() is None:
        try:
            cur = os.path.getsize(local_path) if os.path.exists(local_path) else 0
            if cur > 0:
                cur_gb = round(cur / (1024**3), 1)
                _job_log(job, f"Downloading: {cur_gb} GB")
        except OSError:
            pass
        time.sleep(5)
    stderr_output = (proc.stderr.read() or "").strip() if proc.stderr else ""
    if proc.returncode != 0:
        for line in stderr_output.splitlines():
            _job_log(job, line)
        detail = stderr_output.splitlines()[-1] if stderr_output else ""
        raise RuntimeError(
            f"S3 download failed (exit {proc.returncode})"
            + (f": {detail}" if detail else "")
        )


def _handle_snapshot_capture(job, params):
    """Capture a disk snapshot: flatten, upload to S3, cache locally."""
    domain = _validate_domain_name(params["domain_name"])
    disk_index = int(params["disk_index"])
    s3_url = params.get("s3_url", "")
    cache_path = _validate_path(params["cache_path"])
    aws_access_key = params.get("aws_access_key_id", "")
    aws_secret_key = params.get("aws_secret_access_key", "")
    aws_region = params.get("aws_region", "us-east-1")
    aws_endpoint_url = params.get("aws_endpoint_url", "")

    import tempfile as _tf

    running = _is_domain_running(domain)
    snapshotted = False
    if running:
        snapshotted = _snapshot_domain(job, domain)

    disk_path = _get_disk_path_by_index(domain, disk_index)
    if snapshotted:
        backing = subprocess.run(
            ["qemu-img", "info", _PODMAN_JSON, disk_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if backing.returncode == 0:
            import json as _json

            bfn = _json.loads(backing.stdout).get("full-backing-filename", "")
            if bfn and os.path.exists(bfn):
                disk_path = bfn
    _job_log(job, f"Disk {disk_index} path: {disk_path}")

    _local_tmp = os.path.join(_config.get("local_mount", _LOCAL_DIR), "tmp")
    os.makedirs(_local_tmp, exist_ok=True)
    try:
        with _tf.TemporaryDirectory(dir=_local_tmp) as tmpdir:
            job.setdefault("_tmpdirs", []).append(tmpdir)
            tmp_flat = os.path.join(tmpdir, "flat.qcow2")
            _job_log(job, "Flattening disk...")
            cmd = [
                "qemu-img",
                "convert",
                "-c",
                "-o",
                _ZSTD_COMPRESSION,
                "-O",
                "qcow2",
            ]
            if running and not snapshotted:
                cmd.insert(2, "-U")
            cmd.extend([disk_path, tmp_flat])
            _run_cmd(job, cmd, timeout=3600)

            _job_log(job, "Uploading to S3...")
            _s3_upload(
                job,
                tmp_flat,
                s3_url,
                aws_access_key,
                aws_secret_key,
                aws_region,
                aws_endpoint_url,
            )

            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            _job_log(job, f"Caching to {cache_path}...")
            shutil.copy(tmp_flat, cache_path)
    finally:
        if snapshotted:
            _commit_snapshot(job, domain)

    size_bytes = os.path.getsize(cache_path)
    return {"status": "uploaded", "size_bytes": size_bytes}


COMMAND_HANDLERS["snapshots/capture"] = _handle_snapshot_capture


def _is_domain_running(domain):
    """Check if a libvirt domain is currently running."""
    try:
        result = subprocess.run(
            ["virsh", "domstate", domain], capture_output=True, text=True, timeout=5
        )
        return result.returncode == 0 and "running" in result.stdout
    except Exception:
        return False


def _abort_and_commit_overlay(job, domain, target, overlay, timeout=300):
    """Abort any active block job, then commit+pivot an overlay back to base.
    Returns True on success, False on failure."""
    # Abort any active block job first
    subprocess.run(
        ["virsh", "blockjob", domain, target, "--abort"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    # Wait for abort to complete
    for _ in range(60):
        info = subprocess.run(
            ["virsh", "blockjob", domain, target, "--info"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if info.returncode != 0 or "No current block job" in info.stderr:
            break
        time.sleep(1)
    # Commit and pivot
    r = subprocess.run(
        [
            "virsh",
            "blockcommit",
            domain,
            target,
            "--active",
            "--pivot",
            "--wait",
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if r.returncode == 0:
        try:
            os.remove(overlay)
        except OSError:
            pass
        return True
    return False


def _cleanup_stale_snapshots(job, domain):
    """Clean up any leftover .troshka-capture overlays from previous captures.
    Must be called BEFORE creating a new snapshot."""
    result = subprocess.run(
        ["virsh", "domblklist", domain, "--details"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        return
    for line in result.stdout.strip().split("\n"):
        parts = line.split()
        if len(parts) >= 4 and parts[1] == "disk" and ".troshka-capture" in parts[3]:
            target = parts[2]
            overlay = parts[3]
            _job_log(job, f"Found stale overlay on {target}, cleaning up...")
            if _abort_and_commit_overlay(job, domain, target, overlay):
                _job_log(job, f"Cleaned stale overlay for {target}")
            else:
                _job_log(
                    job,
                    f"Could not clean stale overlay for {target}",
                )


def _snapshot_domain(job, domain):
    """Fstrim → clean stale overlays → freeze → snapshot → thaw.
    Total freeze time < 1 second.
    Returns True if snapshot created, False if failed (use -U fallback)."""
    try:
        r = subprocess.run(
            ["virsh", "domfstrim", domain], capture_output=True, text=True, timeout=30
        )
        if r.returncode == 0:
            _job_log(job, f"Trimmed free blocks: {domain}")
    except Exception:
        pass

    _cleanup_stale_snapshots(job, domain)

    frozen = False
    try:
        r = subprocess.run(
            ["virsh", "domfsfreeze", domain], capture_output=True, text=True, timeout=10
        )
        if r.returncode == 0:
            frozen = True
    except Exception:
        pass

    try:
        result = subprocess.run(
            [
                "virsh",
                "snapshot-create-as",
                domain,
                "--name",
                "troshka-capture",
                "--disk-only",
                "--atomic",
                "--no-metadata",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip())
    except Exception as e:
        _job_log(job, f"Snapshot failed ({e}), using crash-consistent mode")
        if frozen:
            subprocess.run(
                ["virsh", "domfsthaw", domain],
                capture_output=True,
                text=True,
                timeout=10,
            )
        return False
    finally:
        if frozen:
            subprocess.run(
                ["virsh", "domfsthaw", domain],
                capture_output=True,
                text=True,
                timeout=10,
            )

    _job_log(job, "Snapshot created, VM running on overlay (freeze < 1s)")
    return True


def _commit_overlay_with_retry(job, domain, target, overlay):
    """Commit an overlay back to base with one retry on failure."""
    r = subprocess.run(
        [
            "virsh",
            "blockcommit",
            domain,
            target,
            "--active",
            "--pivot",
            "--wait",
            "--verbose",
        ],
        capture_output=True,
        text=True,
        timeout=3600,
    )
    if r.returncode == 0:
        try:
            os.remove(overlay)
        except OSError:
            pass
        _job_log(job, f"Overlay committed and removed for {target}")
        return
    _job_log(job, f"Block-commit failed for {target}: {r.stderr.strip()}")
    # Don't leave a broken state — abort and try once more
    if _abort_and_commit_overlay(job, domain, target, overlay):
        _job_log(job, f"Overlay committed on retry for {target}")
    else:
        _job_log(
            job,
            f"WARNING: overlay stuck for {target}, needs manual cleanup",
        )


def _commit_snapshot(job, domain):
    """Block-commit overlays back to base, wait, pivot, delete overlay."""
    result = subprocess.run(
        ["virsh", "domblklist", domain, "--details"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        return
    for line in result.stdout.strip().split("\n"):
        parts = line.split()
        if len(parts) >= 4 and parts[1] == "disk" and ".troshka-capture" in parts[3]:
            target = parts[2]
            overlay = parts[3]
            _job_log(job, f"Committing overlay {target} back to base...")
            _commit_overlay_with_retry(job, domain, target, overlay)


def _flatten_disk_with_progress(job, disk_path, tmp_flat, running, snapshotted):
    """Flatten a disk to tmp_flat with progress monitoring thread. Returns flat file size."""
    src_size = 0
    try:
        info_out = subprocess.check_output(
            ["qemu-img", "info", _PODMAN_JSON, disk_path],
            timeout=30,
        )
        import json as _json_info

        src_size = _json_info.loads(info_out).get("virtual-size", 0)
    except Exception:
        src_size = os.path.getsize(disk_path)
    src_size_gb = round(src_size / (1024**3), 1)
    _job_log(
        job,
        f"Flattening {os.path.basename(disk_path)} ({src_size_gb} GB)...",
    )

    flatten_done = threading.Event()

    def _monitor_flatten(
        flatten_done=flatten_done,
        tmp_flat=tmp_flat,
        src_size=src_size,
        src_size_gb=src_size_gb,
    ):
        while not flatten_done.is_set():
            try:
                if os.path.exists(tmp_flat):
                    cur = os.path.getsize(tmp_flat)
                    cur_gb = round(cur / (1024**3), 1)
                    pct = min(100, int(cur * 100 / src_size)) if src_size > 0 else 0
                    _job_log(
                        job,
                        f"Flattening: {cur_gb} of {src_size_gb} GB ({pct}%)",
                    )
            except OSError:
                pass
            flatten_done.wait(10)
        return None

    mon = threading.Thread(target=_monitor_flatten, daemon=True)
    mon.start()

    cmd = [
        "qemu-img",
        "convert",
        "-c",
        "-o",
        _ZSTD_COMPRESSION,
        "-O",
        "qcow2",
    ]
    if running and not snapshotted:
        cmd.insert(2, "-U")
    cmd.extend([disk_path, tmp_flat])
    _run_cmd(job, cmd, timeout=3600)
    flatten_done.set()

    return os.path.getsize(tmp_flat)


def _upload_and_cache_disk(
    job,
    tmp_flat,
    flat_size,
    s3_url,
    cache_path,
    aws_access_key,
    aws_secret_key,
    aws_region,
    aws_endpoint_url,
):
    """Upload flattened disk to S3 and cache locally in parallel."""
    flat_size_gb = round(flat_size / (1024**3), 1)

    cache_error = [None]

    def _do_cache(cache_path=cache_path, tmp_flat=tmp_flat, cache_error=cache_error):
        try:
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            shutil.copy(tmp_flat, cache_path)
        except Exception as e:
            cache_error[0] = e
        return None

    cache_thread = threading.Thread(target=_do_cache, daemon=True)
    cache_thread.start()

    _job_log(job, f"Saving {flat_size_gb} GB...")
    _s3_upload_with_cache(
        job,
        tmp_flat,
        flat_size,
        s3_url,
        cache_path,
        aws_access_key,
        aws_secret_key,
        aws_region,
        aws_endpoint_url,
    )

    _job_log(job, "Upload complete, waiting for cache...")
    while cache_thread.is_alive():
        msg = _format_cache_progress(cache_path, flat_size, flat_size_gb)
        if msg:
            _job_log(job, msg)
        cache_thread.join(timeout=5)

    if cache_error[0]:
        _job_log(job, f"Cache copy failed: {cache_error[0]}")


def _process_single_disk_capture(
    job,
    disk_info,
    domain_name,
    running,
    snapshotted,
    aws_access_key,
    aws_secret_key,
    aws_region,
    aws_endpoint_url,
):
    """Process a single disk for pattern capture."""
    disk_path = _validate_path(disk_info["disk_path"])
    s3_url = disk_info["s3_url"]
    cache_path = _validate_path(disk_info["cache_path"])

    if not os.path.exists(disk_path):
        raise RuntimeError(f"Disk not found: {disk_path}")

    import tempfile as _tf

    _local_tmp = os.path.join(_config.get("local_mount", _LOCAL_DIR), "tmp")
    os.makedirs(_local_tmp, exist_ok=True)
    with _tf.TemporaryDirectory(dir=_local_tmp) as tmpdir:
        job.setdefault("_tmpdirs", []).append(tmpdir)
        tmp_flat = os.path.join(tmpdir, "flat.qcow2")

        flat_size = _flatten_disk_with_progress(
            job, disk_path, tmp_flat, running, snapshotted
        )

        if job.get("_cancelled"):
            raise RuntimeError("Cancelled")

        flat_size_gb = round(flat_size / (1024**3), 1)
        _job_log(job, f"Flattened: {flat_size_gb} GB (compressed)")

        commit_thread = None
        snapshot_committed = False
        if snapshotted:

            def _do_commit():
                _commit_snapshot(job, domain_name)
                return None

            commit_thread = threading.Thread(target=_do_commit, daemon=True)
            commit_thread.start()
            snapshot_committed = True

        if job.get("_cancelled"):
            raise RuntimeError("Cancelled")

        _upload_and_cache_disk(
            job,
            tmp_flat,
            flat_size,
            s3_url,
            cache_path,
            aws_access_key,
            aws_secret_key,
            aws_region,
            aws_endpoint_url,
        )

        if job.get("_cancelled"):
            raise RuntimeError("Cancelled")

        if commit_thread:
            commit_thread.join(timeout=600)

    size_bytes = os.path.getsize(cache_path)
    return {"size_bytes": size_bytes}, snapshot_committed


def _handle_pattern_capture_direct(job, params):
    """Capture disks by path — uses external snapshot for running VMs.

    Running VM flow:
      1. freeze → snapshot → thaw (sub-second) — VM writes go to overlay
      2. Flatten the now read-only base disk (minutes, VM unaffected)
      3. Upload to S3
      4. Block-commit overlay back to base

    If snapshot fails: skip freeze entirely, use -U for crash-consistent capture.
    NEVER hold freeze during flatten.
    """
    disks = params.get("disks", [])
    domain_name = params.get("domain_name", "")
    aws_access_key = params.get("aws_access_key_id", "")
    aws_secret_key = params.get("aws_secret_access_key", "")
    aws_region = params.get("aws_region", "us-east-1")
    aws_endpoint_url = params.get("aws_endpoint_url", "")

    running = False
    snapshotted = False
    if domain_name:
        running = _is_domain_running(domain_name)
        if running:
            snapshotted = _snapshot_domain(job, domain_name)

    result_disks = []
    try:
        for disk_info in disks:
            disk_result, committed = _process_single_disk_capture(
                job,
                disk_info,
                domain_name,
                running,
                snapshotted,
                aws_access_key,
                aws_secret_key,
                aws_region,
                aws_endpoint_url,
            )
            result_disks.append(disk_result)
            if committed:
                snapshotted = False
    finally:
        if snapshotted:
            _commit_snapshot(job, domain_name)

    return {"status": "uploaded", "disks": result_disks}


COMMAND_HANDLERS["patterns/capture-direct"] = _handle_pattern_capture_direct


def _handle_pattern_export(job, params):
    domain = _validate_domain_name(params["domain_name"])
    output_path = _validate_path(params["output_path"])

    # Same as snapshot but flatten the qcow2 chain
    result = subprocess.run(
        ["virsh", "domblklist", domain, "--details"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    disk_path = None
    if result.returncode == 0:
        for line in result.stdout.strip().split("\n"):
            parts = line.split()
            if len(parts) >= 4 and parts[1] == "disk":
                disk_path = parts[3]
                break

    if not disk_path:
        raise RuntimeError(f"Could not find disk for domain {domain}")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    _run_cmd(
        job,
        ["qemu-img", "convert", "-O", "qcow2", disk_path, output_path],
        timeout=3600,
    )

    return {"domain": domain, "output_path": output_path, "status": "exported"}


COMMAND_HANDLERS["patterns/export"] = _handle_pattern_export


# ── Host handlers ──


@route("GET", "/host/disk-usage")
def handle_disk_usage(handler, params):
    """Return disk usage stats for all mounted partitions."""
    handler._send_json(200, {"partitions": _get_partitions()})


def _get_domains_from_cache():
    """Get domain states from event cache."""
    with _vm_state_cache_lock:
        domains = {
            name: {"state": info["state"]} for name, info in _vm_state_cache.items()
        }
    return domains


def _get_domains_via_virsh():
    """Get domain states via virsh commands."""
    domains = {}
    try:
        result = subprocess.run(
            ["virsh", "list", "--all", "--name"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        for name in result.stdout.strip().split("\n"):
            name = name.strip()
            if not name or not name.startswith("troshka-"):
                continue
            st = subprocess.run(
                ["virsh", "domstate", name], capture_output=True, text=True, timeout=5
            )
            if st.returncode == 0:
                raw = st.stdout.strip().lower().replace(" ", "_")
                state_map = {
                    "running": "running",
                    "shut_off": "shut_off",
                    "paused": "paused",
                    "in_shutdown": "shutting_down",
                    "crashed": "crashed",
                    "pmsuspended": "suspended",
                    "idle": "unknown",
                }
                domains[name] = {"state": state_map.get(raw, raw)}
    except Exception as e:
        logger.warning("Failed to list VM states: %s", e)
    return domains


@route("GET", "/vms/states")
def handle_vm_states(handler, params):
    """Return all troshka-* domain states in one call."""
    global _libvirt_events_available
    if _libvirt_events_available:
        domains = _get_domains_from_cache()
        if domains:
            handler._send_json(200, {"domains": domains, "source": "events"})
            return
    domains = _get_domains_via_virsh()
    handler._send_json(200, {"domains": domains, "source": "virsh"})


@route("GET", "/vms/events")
def handle_vm_events(handler, params):
    """Return queued VM state change events."""
    import urllib.parse

    qs = urllib.parse.parse_qs(urllib.parse.urlparse(handler.path).query)
    since = float(qs.get("since", [0])[0])
    if not _libvirt_events_available:
        handler._send_json(200, {"events": [], "available": False})
        return
    with _vm_events_lock:
        filtered = [e for e in _vm_events if e["timestamp"] > since]
    handler._send_json(200, {"events": filtered, "available": True})


def _append_vm_event(event_dict):
    """Thread-safe append to _vm_events with max 500 cap."""
    with _vm_events_lock:
        _vm_events.append(event_dict)
        while len(_vm_events) > 500:
            _vm_events.pop(0)


def _seed_libvirt_cache(conn, _lv):
    """Populate VM state cache with current states on startup."""
    try:
        state_map = {
            _lv.VIR_DOMAIN_RUNNING: "running",
            _lv.VIR_DOMAIN_PAUSED: "paused",
            _lv.VIR_DOMAIN_SHUTDOWN: "shutting_down",
            _lv.VIR_DOMAIN_SHUTOFF: "shut_off",
            _lv.VIR_DOMAIN_CRASHED: "crashed",
            _lv.VIR_DOMAIN_PMSUSPENDED: "suspended",
        }
        for dom in conn.listAllDomains():
            name = dom.name()
            if not name.startswith("troshka-"):
                continue
            info = dom.info()
            with _vm_state_cache_lock:
                _vm_state_cache[name] = {
                    "state": state_map.get(info[0], "unknown"),
                    "since": time.time(),
                }
    except Exception as e:
        logger.warning("Failed to seed VM state cache: %s", e)


def _rearm_block_threshold(dom, dev, threshold):
    """Re-arm block threshold at next increment (80% -> 90%)."""
    try:
        info = dom.blockInfo(dev)
        if info:
            capacity = info[0]
            new_threshold = int(capacity * 0.9)
            if new_threshold > threshold:
                dom.setBlockThreshold(dev, new_threshold)
    except Exception:
        pass


def _start_libvirt_event_loop():
    """Start libvirt event loop for domain lifecycle events."""
    global _libvirt_events_available
    try:
        import libvirt as _lv
    except ImportError:
        logger.info("python3-libvirt not available, VM state events disabled")
        return

    _EVENT_STATE_MAP = {
        _lv.VIR_DOMAIN_EVENT_STARTED: "running",
        _lv.VIR_DOMAIN_EVENT_STOPPED: "shut_off",
        _lv.VIR_DOMAIN_EVENT_SHUTDOWN: "shutting_down",
        _lv.VIR_DOMAIN_EVENT_SUSPENDED: "paused",
        _lv.VIR_DOMAIN_EVENT_RESUMED: "running",
        _lv.VIR_DOMAIN_EVENT_CRASHED: "crashed",
        _lv.VIR_DOMAIN_EVENT_PMSUSPENDED: "suspended",
    }

    def _lifecycle_cb(conn, dom, event, detail, opaque):
        name = dom.name()
        if not name.startswith("troshka-"):
            return
        state = _EVENT_STATE_MAP.get(event)
        if not state:
            return
        now = time.time()
        with _vm_state_cache_lock:
            _vm_state_cache[name] = {"state": state, "since": now}
        _append_vm_event({"domain": name, "state": state, "timestamp": now})

    def _block_threshold_cb(conn, dom, dev, path, threshold, opaque):
        name = dom.name()
        if not name.startswith("troshka-"):
            return
        now = time.time()
        _append_vm_event(
            {
                "type": "block_threshold",
                "domain": name,
                "disk": dev,
                "threshold_bytes": threshold,
                "timestamp": now,
            }
        )
        logger.warning("Block threshold exceeded: %s disk %s", name, dev)
        _rearm_block_threshold(dom, dev, threshold)

    def _event_loop():
        while True:
            try:
                _lv.virEventRunDefaultImpl()
            except Exception:
                time.sleep(1)

    try:
        _lv.virEventRegisterDefaultImpl()
        conn = _lv.open("qemu:///system")
        if conn is None:
            logger.warning("Failed to open libvirt connection for events")
            return
        conn.domainEventRegisterAny(
            None, _lv.VIR_DOMAIN_EVENT_ID_LIFECYCLE, _lifecycle_cb, None
        )
        conn.domainEventRegisterAny(
            None, _lv.VIR_DOMAIN_EVENT_ID_BLOCK_THRESHOLD, _block_threshold_cb, None
        )
        conn.setKeepAlive(5, 3)
        _seed_libvirt_cache(conn, _lv)
        threading.Thread(target=_event_loop, daemon=True, name="libvirt-events").start()
        _libvirt_events_available = True
        logger.info(
            "Libvirt event loop started (%d domains cached)", len(_vm_state_cache)
        )
    except Exception as e:
        logger.warning("Failed to start libvirt event loop: %s", e)


def _find_mount_device(mount_path):
    """Find the block device backing a mount point from /proc/mounts."""
    with open("/proc/mounts") as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == mount_path:
                return parts[0]
    return None


def _wait_for_block_device_growth(job, sys_size, dev_name, fs_bytes):
    """Poll sysfs until block device is larger than filesystem (max 60s)."""
    import time as _time

    for _ in range(60):
        with open(sys_size) as f:
            blk_bytes = int(f.read().strip()) * 512
        if blk_bytes > fs_bytes:
            _job_log(
                job,
                f"Block device {dev_name}: {blk_bytes // (1024**3)} GB (fs: {fs_bytes // (1024**3)} GB)",
            )
            return
        _time.sleep(1)
    _job_log(
        job,
        f"Block device did not grow after 60s (still {blk_bytes // (1024**3)} GB)",
    )


def _handle_resize_storage(job, params):
    """Resize /var/lib/troshka filesystem using xfs_growfs.

    After an EBS modify_volume call, the kernel block device may take a few
    seconds to reflect the new size.  Poll until it grows (or 60s timeout),
    then run xfs_growfs.
    """
    mount = _TROSHKA_DIR
    dev = _find_mount_device(mount)
    if not dev:
        raise RuntimeError(f"Cannot find block device for {mount}")

    st = os.statvfs(mount)
    fs_bytes = st.f_blocks * st.f_frsize

    dev_name = os.path.basename(os.path.realpath(dev))
    sys_size = f"/sys/block/{dev_name}/size"
    if not os.path.exists(sys_size):
        _job_log(job, f"No sysfs entry {sys_size}, running xfs_growfs directly")
        _run_cmd(job, ["xfs_growfs", mount], timeout=120)
        return {"status": "resized"}

    _wait_for_block_device_growth(job, sys_size, dev_name, fs_bytes)

    _run_cmd(job, ["xfs_growfs", mount], timeout=120)
    return {"status": "resized"}


COMMAND_HANDLERS["host/resize-storage"] = _handle_resize_storage


def _remove_path_with_fallback(job, validated_path):
    """Remove a file or directory, falling back to qemu user on PermissionError.
    Returns True if removed, False if not found."""
    try:
        if os.path.isdir(validated_path):
            shutil.rmtree(validated_path)
            _job_log(job, f"Removed directory: {validated_path}")
        else:
            os.remove(validated_path)
            _job_log(job, f"Removed file: {validated_path}")
        return True
    except FileNotFoundError:
        _job_log(job, f"Skipped (not found): {validated_path}")
        return False
    except PermissionError:
        subprocess.run(
            ["sudo", "-u", "qemu", "rm", "-rf", "--", validated_path],
            timeout=10,
            check=True,
        )
        _job_log(job, f"Removed as qemu: {validated_path}")
        return True


def _handle_files_remove(job, params):
    """Remove files or directories under /var/lib/troshka, /opt/troshka, or /var/log."""
    paths = params.get("paths", [])
    if not paths:
        raise ValueError("Missing required parameter: paths")

    kill_pattern = params.get("kill_pattern", "")
    if kill_pattern:
        try:
            subprocess.run(
                ["pkill", "-9", "-f", kill_pattern],
                timeout=5,
                capture_output=True,
            )
            _job_log(job, f"Killed processes matching: {kill_pattern}")
        except Exception:
            pass

    removed = 0
    for path in paths:
        validated_path = _validate_path(path)
        try:
            if _remove_path_with_fallback(job, validated_path):
                removed += 1
        except Exception as e:
            _job_log(job, f"Failed to remove {validated_path}: {e}")
            raise

    return {"removed": removed}


COMMAND_HANDLERS["files/remove"] = _handle_files_remove


def _handle_files_stat(job, params):
    path = _validate_path(params["path"])
    exists = os.path.exists(path)
    size = os.path.getsize(path) if exists else 0
    return {"exists": exists, "size": size}


COMMAND_HANDLERS["files/stat"] = _handle_files_stat


# ── Update mechanism ──


def _do_update_restart(script_path, new_path):
    """Move new script into place and exit (systemd will restart)."""
    os.rename(new_path, script_path)
    logger.info("Update installed, exiting for systemd restart")
    os._exit(0)


_drain_cancel = threading.Event()
_SKIP_DRAIN = {
    _VMS_STATE_CMD,
    "vms/states",
    "host/disk-usage",
    _GC_DISCOVER_CMD,
    "vm/ssh-exec",
    "vm/guest-exec",
    "containers/states",
    "mesh/setup",
    "mesh/join-network",
}


def _get_blocking_jobs():
    """Return list of running jobs that are not in _SKIP_DRAIN."""
    with _jobs_lock:
        return [
            j
            for j in _jobs.values()
            if j["status"] == "running" and j.get("command", "") not in _SKIP_DRAIN
        ]


def _terminate_blocking_jobs():
    """Terminate any running jobs that have a subprocess."""
    with _jobs_lock:
        for job in _jobs.values():
            if job["status"] == "running" and job.get("_process"):
                try:
                    job["_process"].terminate()
                    logger.warning(
                        "Terminated job %s for update (%s)",
                        job["job_id"][:8],
                        job.get("command", "?"),
                    )
                except Exception:
                    pass


def _drain_and_update(script_path, new_path, force):
    """Background thread: drain jobs, then update and restart."""
    global _draining
    _draining = True
    _drain_cancel.clear()
    logger.info("Update drain started, force=%s", force)
    drain_timeout = 5 if force else 120

    start = time.time()
    while time.time() - start < drain_timeout:
        blocking = _get_blocking_jobs()
        if not blocking:
            break
        if _drain_cancel.is_set():
            logger.info("Update drain cancelled — new work arrived")
            _draining = False
            try:
                os.remove(new_path)
            except OSError:
                pass
            return
        logger.info(
            "Update drain waiting on %d job(s): %s",
            len(blocking),
            ", ".join(f"{j['job_id'][:8]}({j.get('command', '?')})" for j in blocking),
        )
        time.sleep(2)

    if _drain_cancel.is_set():
        logger.info("Update drain cancelled before restart")
        _draining = False
        return

    _terminate_blocking_jobs()
    _do_update_restart(script_path, new_path)


@route("GET", "/host/diag")
def handle_diag(handler, params):
    """Diagnostic endpoint — returns nftables, routes, interfaces, namespaces."""
    diag = {}
    for name, cmd in [
        ("nftables", ["nft", "list", "ruleset"]),
        ("routes", ["ip", "route", "show"]),
        ("interfaces", ["ip", "-o", "link", "show"]),
        ("namespaces", ["ip", "netns", "list"]),
        ("vxlan", ["ip", "-d", "link", "show", "type", "vxlan"]),
    ]:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            diag[name] = proc.stdout.strip()
        except Exception as e:
            diag[name] = f"error: {e}"
    handler._send_json(200, diag)


def _handle_nft_reset(job, params):
    """Flush all troshka nftables chains and delete them. Nuclear reset."""
    flushed = 0
    proc = subprocess.run(
        ["nft", "list", "ruleset"], capture_output=True, text=True, timeout=5
    )
    if proc.returncode != 0:
        return {"flushed_chains": 0, "error": "nft list failed"}

    # First pass: flush base chains (removes jump rules to troshka chains)
    for table_chain in [
        ("filter", "forward"),
        ("nat", "postrouting"),
        ("nat", "prerouting"),
    ]:
        try:
            _run_cmd(
                job,
                ["nft", "flush", "chain", "inet", table_chain[0], table_chain[1]],
                timeout=5,
            )
        except RuntimeError:
            pass

    # Second pass: find and delete all troshka-* chains
    for line in proc.stdout.split("\n"):
        line = line.strip()
        if line.startswith("chain troshka-"):
            chain_name = line.split()[1]
            # Determine which table this chain is in
            table_type = (
                "nat" if ("post" in chain_name or "pre" in chain_name) else "filter"
            )
            try:
                _run_cmd(
                    job,
                    ["nft", "flush", "chain", "inet", table_type, chain_name],
                    timeout=5,
                )
                _run_cmd(
                    job,
                    ["nft", "delete", "chain", "inet", table_type, chain_name],
                    timeout=5,
                )
                flushed += 1
                _job_log(job, f"Deleted chain {table_type}/{chain_name}")
            except RuntimeError:
                pass
    return {"flushed_chains": flushed}


COMMAND_HANDLERS["host/nft-reset"] = _handle_nft_reset


@route("POST", "/admin/update")
def handle_update(handler, params):
    """Accept a new script, validate syntax, drain, and restart."""
    import base64

    body = handler._read_body()
    if "script" not in body:
        handler._send_json(400, {"error": "missing 'script' field"})
        return

    # Decode script
    try:
        script_bytes = base64.b64decode(body["script"])
        script_text = script_bytes.decode("utf-8")
    except Exception as e:
        handler._send_json(400, {"error": f"invalid base64: {e}"})
        return

    # Syntax check
    try:
        compile(script_text, "<upload>", "exec")
    except SyntaxError as e:
        handler._send_json(400, {"error": f"syntax error: {e}"})
        return

    # Write to temp file
    script_path = os.path.abspath(__file__)
    new_path = script_path + ".new"
    try:
        with open(new_path, "w") as f:
            f.write(script_text)
    except Exception as e:
        handler._send_json(500, {"error": f"failed to write script: {e}"})
        return

    # Check for force mode
    force = "force=true" in handler.path

    # Send success response before starting drain
    version = body.get("version", "unknown")
    handler._send_json(200, {"status": "restarting", "version": version})

    # Spawn drain thread
    drain_thread = threading.Thread(
        target=_drain_and_update,
        args=(script_path, new_path, force),
        daemon=True,
    )
    drain_thread.start()


def _set_vncd_tls_mode(no_tls):
    """Rewrite the troshka-vncd systemd unit's ExecStart to add/remove --no-tls.

    Mirrors the install-time logic in agent_deployer.py so the lightweight
    update-vncd push path can toggle TLS mode without a full reinstall.
    """
    import re

    unit_path = "/etc/systemd/system/troshka-vncd.service"
    base_exec = "/opt/troshka/venv/bin/python3 /opt/troshka/troshka-vncd.py"
    new_exec_line = f"ExecStart={base_exec} --no-tls" if no_tls else f"ExecStart={base_exec}"

    with open(unit_path) as f:
        content = f.read()
    content = re.sub(
        r"^ExecStart=.*troshka-vncd\.py.*$",
        new_exec_line,
        content,
        flags=re.MULTILINE,
    )
    with open(unit_path, "w") as f:
        f.write(content)

    if no_tls:
        try:
            subprocess.run(
                ["firewall-cmd", "--add-port=8080/tcp", "--permanent"], timeout=10
            )
            subprocess.run(["firewall-cmd", "--reload"], timeout=10)
        except Exception:
            pass

    subprocess.run(["systemctl", "daemon-reload"], timeout=10, check=True)


@route("POST", "/admin/update-vncd")
def handle_update_vncd(handler, params):
    """Update troshka-vncd.py, optionally flip its TLS mode, and restart it."""
    import base64

    body = handler._read_body()
    if "script" not in body:
        handler._send_json(400, {"error": "missing 'script' field"})
        return

    # Decode script
    try:
        script_bytes = base64.b64decode(body["script"])
    except Exception as e:
        handler._send_json(400, {"error": f"invalid base64: {e}"})
        return

    # Write to file
    vncd_path = "/opt/troshka/troshka-vncd.py"
    try:
        with open(vncd_path + ".new", "wb") as f:
            f.write(script_bytes)
        os.rename(vncd_path + ".new", vncd_path)
        os.chmod(vncd_path, 0o700)
    except Exception as e:
        handler._send_json(500, {"error": f"failed to write vncd: {e}"})
        return

    # Optionally flip TLS mode (systemd unit ExecStart) before restarting.
    # Absent "no_tls" means "leave the current mode alone" — keeps callers
    # that only push the script (no mode info) from silently flipping mode.
    if "no_tls" in body:
        try:
            _set_vncd_tls_mode(bool(body["no_tls"]))
        except Exception as e:
            handler._send_json(500, {"error": f"failed to update vncd unit: {e}"})
            return

    # Restart service
    try:
        subprocess.run(["systemctl", "restart", "troshka-vncd"], timeout=10, check=True)
    except Exception as e:
        handler._send_json(500, {"error": f"failed to restart vncd: {e}"})
        return

    handler._send_json(200, {"status": "updated"})


# ── Server factory ──

_start_time = time.time()


def create_server(config):
    """Create and return an HTTPS server (does not start serving)."""
    server = ThreadingHTTPServer(("0.0.0.0", config["port"]), TroshkadHandler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(config["tls_cert"], config["tls_key"])
    client_ca = config.get("client_ca", "")
    if client_ca and os.path.isfile(client_ca):
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.load_verify_locations(client_ca)
        logger.info("mTLS enabled — requiring client certs signed by %s", client_ca)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    return server


# ── Main ──


def _drain_running_jobs(timeout=120):
    """Wait for running jobs to finish, kill any remaining after timeout."""
    _SKIP_DRAIN = {_VMS_STATE_CMD, "vms/states", "host/disk-usage", _GC_DISCOVER_CMD}
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _jobs_lock:
            running = [
                j
                for j in _jobs.values()
                if j["status"] == "running" and j.get("command", "") not in _SKIP_DRAIN
            ]
        if not running:
            break
        names = [f"{j['job_id'][:8]}({j.get('command', '?')})" for j in running]
        logger.info(
            "Draining: %d job(s) still running: %s", len(running), ", ".join(names)
        )
        time.sleep(2)

    with _jobs_lock:
        for job in _jobs.values():
            if job["status"] == "running" and job.get("_process"):
                try:
                    job["_process"].kill()
                    logger.warning(
                        "Killed job %s subprocess (drain timeout)",
                        job["job_id"][:8],
                    )
                except Exception:
                    pass


def main():
    global _config, _start_time
    conf_path = sys.argv[1] if len(sys.argv) > 1 else "/opt/troshka/troshkad.conf"
    _config = load_config(conf_path)
    _start_time = time.time()

    # Global exception handler — log unhandled exceptions instead of silent exit
    def _unhandled_exception(exc_type, exc_value, exc_tb):
        if exc_type is KeyboardInterrupt:
            return
        logger.critical(
            "Unhandled exception in main thread",
            exc_info=(exc_type, exc_value, exc_tb),
        )

    sys.excepthook = _unhandled_exception

    def _thread_exception(args):
        if args.exc_type is SystemExit:
            return
        logger.error(
            "Unhandled exception in thread %s: %s",
            args.thread.name if args.thread else "unknown",
            args.exc_value,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    threading.excepthook = _thread_exception

    # Install signal handler EARLY — before any restore code that might
    # accidentally SIGTERM us (stale PID file with recycled PID).
    _server_ref = [None]

    def shutdown(signum, frame):
        global _draining
        logger.info("Received signal %d, requesting shutdown", signum)
        _draining = True
        if _server_ref[0]:
            threading.Thread(target=_server_ref[0].shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    cleanup_thread = threading.Thread(target=_job_cleanup_loop, daemon=True)
    cleanup_thread.start()

    _cleanup_nbd_ports()
    _cleanup_stale_recert()
    threading.Thread(target=_nbd_reaper_loop, daemon=True).start()

    # Restore services from previous deploy
    _restore_bmc_services()
    _restore_dnsmasq()

    # Watchdog: check dnsmasq + system services every 30s, restart if dead
    watchdog = threading.Thread(target=_watchdog_loop, daemon=True)
    watchdog.start()

    _start_libvirt_event_loop()

    server = create_server(_config)
    _server_ref[0] = server
    logger.info("troshkad %s listening on port %d", str(VERSION), int(_config["port"]))

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("Server stopped accepting requests, draining jobs...")
        _drain_running_jobs(timeout=120)
        server.server_close()
        logger.info("troshkad stopped")


def _check_dnsmasq_project_alive(pidfile, conf_path, project_prefix):
    """Check if the project that owns this dnsmasq still exists (domains + namespace).

    Returns True if the project is alive (or cannot be determined), False if orphaned.
    Cleans up orphan files when returning False.
    """
    if not project_prefix:
        return True
    domain_check = subprocess.run(
        ["virsh", "list", "--all", "--name"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    has_domains = any(
        f"troshka-{project_prefix}-" in line for line in domain_check.stdout.split("\n")
    )
    ns_check = subprocess.run(
        ["ip", "netns", "list"], capture_output=True, text=True, timeout=5
    )
    has_namespace = f"troshka-{project_prefix}" in ns_check.stdout
    if has_domains or has_namespace:
        return True
    # Project is gone — clean up orphan files
    try:
        os.remove(pidfile)
        os.remove(conf_path)
        for lf in glob.glob(f"{_DNSMASQ_PREFIX}-{project_prefix}-*.leases"):
            os.remove(lf)
        logger.info(
            "Cleaned orphan dnsmasq files for deleted project %s",
            project_prefix,
        )
    except OSError:
        pass
    return False


def _log_dead_dnsmasq_info(pidfile):
    """Log information about dead dnsmasq process."""
    try:
        with open(pidfile) as f:
            dead_pid = int(f.read().strip())
        logger.warning(
            "dnsmasq PID %d from %s is dead — restarting",
            dead_pid,
            os.path.basename(pidfile),
        )
    except (FileNotFoundError, ValueError):
        logger.warning(
            "dnsmasq PID file %s missing or corrupt — restarting",
            os.path.basename(pidfile),
        )


def _find_namespace_from_conf(conf_path):
    """Extract namespace name from dnsmasq config file."""
    ns_name = None
    try:
        with open(conf_path) as f:
            for line in f:
                if line.startswith("no-dhcp-interface=br-bmc-"):
                    pid_short = line.strip().split("br-bmc-")[1]
                    ns_name = f"troshka-{pid_short}"
                    break
    except Exception:
        pass
    return ns_name


def _restart_dead_dnsmasq(pidfile, conf_path, conf_name):
    """Restart a dead dnsmasq process from its config. Returns True if restarted."""
    _log_dead_dnsmasq_info(pidfile)
    ns_name = _find_namespace_from_conf(conf_path)
    if not ns_name:
        return False
    ns_check = subprocess.run(
        ["ip", "netns", "list"], capture_output=True, text=True, timeout=5
    )
    if ns_name not in ns_check.stdout:
        return False
    try:
        # dnsmasq self-daemonizes (double-fork); capture_output=True here would pipe
        # stdout/stderr, which the detached grandchild inherits, so subprocess.run()
        # would block waiting for EOF that never arrives until the timeout kills an
        # already-successfully-started process. Use DEVNULL since we don't need the
        # output (failures are reported via the caught exception below).
        subprocess.run(
            [
                "ip",
                "netns",
                "exec",
                ns_name,
                "dnsmasq",
                f"--conf-file={conf_path}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        try:
            with open(
                pidfile.replace(_CONF_EXT, ".pid").replace("/etc/dnsmasq.d/", "/run/")
            ) as pf:
                new_pid = pf.read().strip()
            subprocess.run(
                [
                    "auditctl",
                    "-a",
                    "exit,always",
                    "-F",
                    "arch=b64",
                    "-S",
                    "kill",
                    "-F",
                    f"a0={new_pid}",
                    "-k",
                    "dnsmasq-kill",
                ],
                capture_output=True,
                timeout=5,
            )
        except Exception:
            pass
        logger.info("dnsmasq restored for %s", conf_name)
        return True
    except Exception as e:
        logger.warning("Failed to restart dnsmasq %s: %s", conf_name, e)
        return False


def _get_conf_from_pidfile(pidfile):
    """Convert pidfile name to config file name and path."""
    conf_name = (
        os.path.basename(pidfile)
        .replace("troshka-dnsmasq-", "troshka-")
        .replace(".pid", _CONF_EXT)
    )
    conf_path = f"/etc/dnsmasq.d/{conf_name}"
    return conf_name, conf_path


def _get_project_prefix_from_pidfile(pidfile):
    """Extract project prefix from pidfile name."""
    parts = (
        os.path.basename(pidfile)
        .replace("troshka-dnsmasq-", "")
        .replace(".pid", "")
        .split("-")
    )
    return parts[0] if parts else ""


def _is_process_alive(pidfile):
    """Check if process from pidfile is alive."""
    try:
        with open(pidfile) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return True
    except (ValueError, OSError):
        return False


def _process_single_dnsmasq_pidfile(pidfile):
    """Process a single dnsmasq pidfile, restart if needed. Returns True if restarted."""
    conf_name, conf_path = _get_conf_from_pidfile(pidfile)
    if not os.path.exists(conf_path):
        try:
            os.remove(pidfile)
        except OSError:
            pass
        return False
    project_prefix = _get_project_prefix_from_pidfile(pidfile)
    if not _check_dnsmasq_project_alive(pidfile, conf_path, project_prefix):
        return False
    if not _is_process_alive(pidfile):
        return _restart_dead_dnsmasq(pidfile, conf_path, conf_name)
    return False


def _check_and_restart_dnsmasq():
    """Check all dnsmasq PID files — restart any that died."""
    restarted = 0
    for pidfile in glob.glob("/run/troshka-dnsmasq-*.pid"):
        if _process_single_dnsmasq_pidfile(pidfile):
            restarted += 1
    return restarted


def _restore_dnsmasq():
    """Restore dnsmasq for all active namespaces on troshkad startup."""
    restarted = _check_and_restart_dnsmasq()
    if restarted:
        logger.info("dnsmasq restore: restarted %d instance(s)", restarted)


# Services that must be running for troshkad to function.
# Each entry: (unit_name, restart_unit, is_socket)
_REQUIRED_SERVICES = [
    ("virtqemud", "virtqemud.socket", True),
    ("virtstoraged", "virtstoraged.socket", True),
    ("virtnetworkd", "virtnetworkd.socket", True),
    ("nftables", "nftables.service", False),
]


_watchdog_http_failures = 0


def _watchdog_check_http():
    """Self-health check: verify our HTTP server is responsive."""
    global _watchdog_http_failures
    try:
        import socket as _sock

        s = _sock.create_connection(("127.0.0.1", _config["port"]), timeout=5)
        s.close()
        _watchdog_http_failures = 0
    except Exception:
        _watchdog_http_failures += 1
        logger.warning(
            "watchdog: HTTP server self-check failed (%d/6)",
            _watchdog_http_failures,
        )
        if _watchdog_http_failures >= 6:
            logger.error(
                "watchdog: HTTP server unresponsive for %d checks, forcing restart",
                _watchdog_http_failures,
            )
            os._exit(1)


def _watchdog_check_services():
    """Check systemd services and restart any that are not active."""
    for service_name, unit, is_socket in _REQUIRED_SERVICES:
        try:
            result = subprocess.run(
                ["systemctl", "is-active", unit],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.stdout.strip() == "active":
                continue
            if is_socket:
                check_svc = subprocess.run(
                    ["systemctl", "is-active", f"{service_name}.service"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if check_svc.stdout.strip() == "active":
                    continue
            logger.warning("watchdog: %s is not active, restarting", unit)
            subprocess.run(
                ["systemctl", "start", unit],
                capture_output=True,
                timeout=10,
            )
        except Exception as e:
            logger.warning("watchdog: %s check error: %s", service_name, e)


def _watchdog_check_nfs():
    """NFS health check with auto-recovery after sustained staleness."""
    if _config.get("storage_mode") != "shared":
        return
    try:
        healthy = _check_nfs_health()
        if not healthy and _nfs_stale_since:
            stale_secs = int(time.time() - _nfs_stale_since)
            if stale_secs >= 60:
                logger.warning(
                    "watchdog: NFS stale for %ds, attempting recovery",
                    stale_secs,
                )
                if _try_nfs_recovery():
                    _check_nfs_health()
    except Exception as e:
        logger.warning("watchdog: NFS check error: %s", e)


def _watchdog_loop():
    """Periodically check dnsmasq instances + system services, restart if dead."""
    time.sleep(10)
    while True:
        if _draining:
            time.sleep(30)
            continue
        _watchdog_check_http()
        try:
            _check_and_restart_dnsmasq()
        except Exception as e:
            logger.warning("watchdog: dnsmasq check error: %s", e)
        _watchdog_check_services()
        _cleanup_rate_limit()
        _watchdog_check_nfs()
        time.sleep(30)


def _restore_sushy_emulators(bmc_dir, ns, venv_bin):
    """Restart all sushy-emulator processes from config files in a BMC directory."""
    for fname in os.listdir(bmc_dir):
        if not (fname.startswith("sushy-") and fname.endswith(_CONF_EXT)):
            continue
        conf_path = os.path.join(bmc_dir, fname)
        pid_path = conf_path.replace(_CONF_EXT, ".pid")
        # Kill stale process if any
        if os.path.exists(pid_path):
            try:
                with open(pid_path) as f:
                    old_pid = int(f.read().strip())
                _safe_kill(old_pid, signal.SIGTERM, "sushy-emulator")
            except (ValueError, ProcessLookupError, PermissionError):
                pass
        proc = subprocess.Popen(
            [
                "ip",
                "netns",
                "exec",
                ns,
                f"{venv_bin}/sushy-emulator",
                "--config",
                conf_path,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        with open(pid_path, "w") as f:
            f.write(str(proc.pid))
        logger.info(
            "BMC restore: sushy-emulator started for %s (PID %d)",
            fname,
            proc.pid,
        )


def _kill_stale_vbmcd(vbmcd_pid_path):
    """Kill stale vbmcd process by PID file and clean up."""
    if not os.path.exists(vbmcd_pid_path):
        return
    try:
        with open(vbmcd_pid_path) as f:
            old_pid = int(f.read().strip())
        if _safe_kill(old_pid, signal.SIGTERM, "vbmcd"):
            for _ in range(10):
                time.sleep(0.5)
                try:
                    os.kill(old_pid, 0)
                except ProcessLookupError:
                    break
    except (ValueError, ProcessLookupError, PermissionError):
        pass
    try:
        os.remove(vbmcd_pid_path)
    except FileNotFoundError:
        pass


def _register_vbmc_entries(bmc_dir, ns, venv_bin, env):
    """Re-register vbmc entries from the config dir."""
    vbmcd_conf_dir = os.path.join(bmc_dir, "vbmcd")
    if not os.path.isdir(vbmcd_conf_dir):
        return
    for entry in os.listdir(vbmcd_conf_dir):
        entry_path = os.path.join(vbmcd_conf_dir, entry)
        if os.path.isdir(entry_path) and entry.startswith("troshka-"):
            try:
                subprocess.run(
                    [
                        "ip",
                        "netns",
                        "exec",
                        ns,
                        f"{venv_bin}/vbmc",
                        "start",
                        entry,
                    ],
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=10,
                )
                logger.info("BMC restore: vbmc started %s", entry)
            except Exception:
                logger.warning("BMC restore: failed to start vbmc %s", entry)


def _restore_vbmcd(bmc_dir, ns, venv_bin, project_short):
    """Restart vbmcd and re-register vbmc entries from a BMC directory."""
    vbmcd_conf = os.path.join(bmc_dir, "virtualbmc.conf")
    vbmcd_pid_path = os.path.join(bmc_dir, _VBMCD_PID)
    if not os.path.exists(vbmcd_conf):
        return

    _kill_stale_vbmcd(vbmcd_pid_path)

    env = os.environ.copy()
    env["VIRTUALBMC_CONFIG"] = vbmcd_conf
    subprocess.Popen(
        ["ip", "netns", "exec", ns, f"{venv_bin}/vbmcd"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )
    for _ in range(20):
        time.sleep(0.5)
        if os.path.exists(vbmcd_pid_path):
            break
    logger.info("BMC restore: vbmcd started for %s", project_short)

    _register_vbmc_entries(bmc_dir, ns, venv_bin, env)


def _restore_bmc_services():
    """Restart BMC services (sushy-emulator, vbmcd) from existing configs on troshkad startup."""
    bmc_base = _BMC_DIR
    venv_bin = _VENV_BIN
    if not os.path.isdir(bmc_base):
        return

    for project_dir in os.listdir(bmc_base):
        bmc_dir = os.path.join(bmc_base, project_dir)
        if not os.path.isdir(bmc_dir):
            continue

        pid = project_dir[:8]
        ns = f"troshka-{pid}"

        # Check namespace exists
        try:
            subprocess.run(
                ["ip", "netns", "exec", ns, "true"],
                capture_output=True,
                timeout=5,
                check=True,
            )
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            FileNotFoundError,
        ):
            logger.info(
                "BMC restore: namespace %s not found, skipping %s", ns, project_dir[:8]
            )
            continue

        _restore_sushy_emulators(bmc_dir, ns, venv_bin)
        _restore_vbmcd(bmc_dir, ns, venv_bin, project_dir[:8])

    logger.info("BMC restore complete")


def _generate_self_signed_cert(cert_path, key_path, cn, ip):
    """Generate a self-signed TLS certificate with the given CN and IP SAN."""
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
            "3650",
            "-subj",
            f"/CN={cn}",
            "-addext",
            f"subjectAltName=IP:{ip}",
            "-keyout",
            key_path,
            "-out",
            cert_path,
        ],
        capture_output=True,
        timeout=10,
        check=True,
    )
    os.chmod(key_path, 0o600)


def _kill_pid_file(pid_path):
    """Read a PID file and kill the process. Silently ignores missing/dead processes."""
    if not os.path.exists(pid_path):
        return
    try:
        with open(pid_path) as f:
            old_pid = int(f.read().strip())
        _safe_kill(old_pid, signal.SIGTERM)
    except (ValueError, ProcessLookupError, PermissionError):
        pass


def _bmc_start_dnsmasq(job, ns, pid, bridge, bmc_cidr, dhcp_hosts):
    """Start dnsmasq for DHCP on BMC bridge with static reservations."""
    net_base = bmc_cidr.rsplit(".", 1)[0]
    dnsmasq_conf = f"/etc/dnsmasq.d/troshka-bmc-{pid}.conf"
    dnsmasq_pid_file = f"/run/troshka-dnsmasq-bmc-{pid}.pid"
    dnsmasq_lease = f"{_DNSMASQ_PREFIX}-bmc-{pid}.leases"
    conf_lines = [
        f"interface={bridge}",
        "bind-interfaces",
        "except-interface=lo",
        "no-resolv",
        "no-hosts",
        f"pid-file={dnsmasq_pid_file}",
        f"dhcp-leasefile={dnsmasq_lease}",
        f"dhcp-range={net_base}.100,{net_base}.199,24h",
    ]
    for dh in dhcp_hosts:
        safe_name = (dh.get("name") or "").replace(" ", "-").replace("_", "-")
        hostname_part = f",{safe_name}" if safe_name else ""
        conf_lines.append(f"dhcp-host={dh['mac']},{dh['ip']}{hostname_part}")
    with open(dnsmasq_conf, "w") as f:
        f.write("\n".join(conf_lines) + "\n")

    _kill_pid_file(dnsmasq_pid_file)

    subprocess.Popen(
        ["ip", "netns", "exec", ns, "dnsmasq", f"--conf-file={dnsmasq_conf}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    _job_log(job, f"BMC dnsmasq started with {len(dhcp_hosts)} DHCP reservations")


def _bmc_write_sushy_conf(
    conf_path,
    bmc_ip,
    port,
    pool_name,
    htpasswd_path,
    dom_uuid,
    ssl_cert=None,
    ssl_key=None,
):
    """Write a sushy-emulator config file."""
    with open(conf_path, "w") as f:
        f.write(f"SUSHY_EMULATOR_LISTEN_IP = '{bmc_ip}'\n")
        f.write(f"SUSHY_EMULATOR_LISTEN_PORT = {port}\n")
        f.write("SUSHY_EMULATOR_LIBVIRT_URI = 'qemu:///system'\n")
        f.write("SUSHY_EMULATOR_FEATURE_SET = 'vmedia'\n")
        f.write("SUSHY_EMULATOR_IGNORE_BOOT_DEVICE = False\n")
        f.write("SUSHY_EMULATOR_VMEDIA_VERIFY_SSL = False\n")
        f.write(f"SUSHY_EMULATOR_STORAGE_POOL = '{pool_name}'\n")
        f.write(f"SUSHY_EMULATOR_AUTH_FILE = '{htpasswd_path}'\n")
        if ssl_cert:
            f.write(f"SUSHY_EMULATOR_SSL_CERT = '{ssl_cert}'\n")
            f.write(f"SUSHY_EMULATOR_SSL_KEY = '{ssl_key}'\n")
        if dom_uuid:
            f.write(f"SUSHY_EMULATOR_ALLOWED_INSTANCES = ['{dom_uuid}']\n")


def _bmc_start_sushy_instance(
    job, ns, venv_bin, conf_path, pid_path, bmc_ip, port, domain_name
):
    """Start a single sushy-emulator process and record its PID."""
    _kill_pid_file(pid_path)
    proc = subprocess.Popen(
        [
            "ip",
            "netns",
            "exec",
            ns,
            f"{venv_bin}/sushy-emulator",
            "--config",
            conf_path,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    with open(pid_path, "w") as f:
        f.write(str(proc.pid))
    _job_log(
        job,
        f"sushy-emulator started for {domain_name} at {bmc_ip}:{port} (PID {proc.pid})",
    )


def _bmc_start_sushy_for_vm(job, ns, bmc_dir, venv_bin, vm, pool_name, htpasswd_path):
    """Start HTTP and SSL sushy-emulator instances for one VM."""
    domain_name = _validate_domain_name(vm["domain_name"])
    bmc_ip = _validate_ip(vm["bmc_ip"])
    vm_short = domain_name.split("-")[-1] if "-" in domain_name else domain_name[:8]

    # Get libvirt UUID for ALLOWED_INSTANCES (sushy uses UUIDs, not names)
    dom_uuid = ""
    try:
        result = subprocess.run(
            ["virsh", "domuuid", domain_name],
            capture_output=True,
            text=True,
            timeout=5,
        )
        dom_uuid = result.stdout.strip()
    except Exception:
        pass

    # HTTP on port 8000
    conf_path = os.path.join(bmc_dir, f"sushy-{vm_short}.conf")
    _bmc_write_sushy_conf(conf_path, bmc_ip, 8000, pool_name, htpasswd_path, dom_uuid)
    pid_path = os.path.join(bmc_dir, f"sushy-{vm_short}.pid")
    _bmc_start_sushy_instance(
        job, ns, venv_bin, conf_path, pid_path, bmc_ip, 8000, domain_name
    )

    # SSL on port 8443
    cert_path = os.path.join(bmc_dir, f"sushy-{vm_short}.crt")
    key_path = os.path.join(bmc_dir, f"sushy-{vm_short}.key")
    _generate_self_signed_cert(cert_path, key_path, f"sushy-{domain_name}", bmc_ip)
    ssl_conf_path = os.path.join(bmc_dir, f"sushy-{vm_short}-ssl.conf")
    _bmc_write_sushy_conf(
        ssl_conf_path,
        bmc_ip,
        8443,
        pool_name,
        htpasswd_path,
        dom_uuid,
        ssl_cert=cert_path,
        ssl_key=key_path,
    )
    ssl_pid_path = os.path.join(bmc_dir, f"sushy-{vm_short}-ssl.pid")
    _bmc_start_sushy_instance(
        job, ns, venv_bin, ssl_conf_path, ssl_pid_path, bmc_ip, 8443, domain_name
    )


def _bmc_start_vbmcd(job, ns, bmc_dir, venv_bin, vms, bmc_username, bmc_password):
    """Start vbmcd daemon and register VMs for IPMI access."""
    vbmcd_conf_dir = os.path.join(bmc_dir, "vbmcd")
    if os.path.isdir(vbmcd_conf_dir):
        shutil.rmtree(vbmcd_conf_dir)
    os.makedirs(vbmcd_conf_dir, exist_ok=True)

    vbmcd_conf_path = os.path.join(bmc_dir, "virtualbmc.conf")
    with open(vbmcd_conf_path, "w") as f:
        f.write("[default]\n")
        f.write(f"config_dir = {vbmcd_conf_dir}\n")
        f.write(f"pid_file = {bmc_dir}/vbmcd.pid\n")
        f.write("[log]\n")
        f.write(f"logfile = {bmc_dir}/vbmcd.log\n")

    vbmcd_pid_path = os.path.join(bmc_dir, _VBMCD_PID)
    _bmc_stop_vbmcd(vbmcd_pid_path)

    env = os.environ.copy()
    env["VIRTUALBMC_CONFIG"] = vbmcd_conf_path
    proc = subprocess.Popen(
        ["ip", "netns", "exec", ns, f"{venv_bin}/vbmcd"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )
    # Don't write PID file -- vbmcd manages its own via pid_file in config.
    # Wait for vbmcd to be ready (writes its PID file and opens ZMQ port).
    for _ in range(20):
        time.sleep(0.5)
        if os.path.exists(vbmcd_pid_path):
            break

    _job_log(job, f"vbmcd started (wrapper PID {proc.pid})")

    for vm in vms:
        domain_name = _validate_domain_name(vm["domain_name"])
        bmc_ip = _validate_ip(vm["bmc_ip"])

        _run_cmd(
            job,
            [
                "ip",
                "netns",
                "exec",
                ns,
                f"{venv_bin}/vbmc",
                "add",
                domain_name,
                "--port",
                "623",
                "--address",
                bmc_ip,
                "--username",
                bmc_username,
                "--password",
                bmc_password,
                "--libvirt-uri",
                "qemu:///system",
            ],
            timeout=30,
        )
        _run_cmd(
            job,
            ["ip", "netns", "exec", ns, f"{venv_bin}/vbmc", "start", domain_name],
            timeout=30,
        )
        _job_log(job, f"vbmc registered {domain_name} at {bmc_ip}:623")


def _bmc_stop_vbmcd(vbmcd_pid_path):
    """Stop an existing vbmcd process, waiting for it to exit cleanly."""
    if not os.path.exists(vbmcd_pid_path):
        return
    try:
        with open(vbmcd_pid_path) as f:
            old_pid = int(f.read().strip())
        _safe_kill(old_pid, signal.SIGTERM)
        for _ in range(10):
            time.sleep(0.5)
            try:
                os.kill(old_pid, 0)
            except ProcessLookupError:
                break
    except (ValueError, ProcessLookupError, PermissionError):
        pass
    # Only remove PID file if process is confirmed dead
    try:
        with open(vbmcd_pid_path) as f:
            check_pid = int(f.read().strip())
        os.kill(check_pid, 0)
    except (ValueError, ProcessLookupError, PermissionError, FileNotFoundError):
        try:
            os.remove(vbmcd_pid_path)
        except FileNotFoundError:
            pass


def _handle_bmc_setup(job, params):
    """Set up virtual BMC endpoints for a project's VMs.

    Creates a BMC bridge inside the project namespace, starts sushy-emulator
    (Redfish) and vbmcd/vbmc (IPMI) for each BMC-enabled VM.
    """
    project_id = _validate_project_id(params["project_id"])
    bmc_cidr = params["bmc_cidr"]
    bmc_gateway_ip = params["bmc_gateway_ip"]
    bmc_username = params.get("bmc_username", "admin")
    bmc_password = params.get("bmc_password", "password")
    vms = params.get("vms", [])

    if not vms:
        _job_log(job, "No BMC-enabled VMs, skipping")
        return {"status": "skipped"}

    pid = project_id[:8]
    ns = f"troshka-{pid}"
    bridge = f"br-bmc-{pid}"
    prefix = bmc_cidr.split("/")[1] if "/" in bmc_cidr else "24"
    bmc_dir = f"{_BMC_DIR}/{project_id}"
    venv_bin = _VENV_BIN

    os.makedirs(bmc_dir, exist_ok=True)

    # 1. Create BMC bridge inside namespace
    try:
        _run_cmd(
            job, ["ip", "netns", "exec", ns, "ip", "link", "del", bridge], timeout=10
        )
    except RuntimeError:
        pass
    _run_cmd(
        job,
        ["ip", "netns", "exec", ns, "ip", "link", "add", bridge, "type", "bridge"],
        timeout=10,
    )
    _run_cmd(
        job,
        [
            "ip",
            "netns",
            "exec",
            ns,
            "ip",
            "addr",
            "add",
            f"{bmc_gateway_ip}/{prefix}",
            "dev",
            bridge,
        ],
        timeout=10,
    )
    _run_cmd(
        job, ["ip", "netns", "exec", ns, "ip", "link", "set", bridge, "up"], timeout=10
    )

    # Dummy bridge in host namespace for libvirt validation
    try:
        subprocess.run(
            ["ip", "link", "show", bridge], capture_output=True, check=True, timeout=5
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        _run_cmd(job, ["ip", "link", "add", bridge, "type", "bridge"], timeout=10)
        subprocess.run(
            ["nmcli", "dev", "set", bridge, "managed", "no"],
            capture_output=True,
            timeout=5,
        )
    _run_cmd(job, ["ip", "link", "set", bridge, "up"], timeout=10)

    _job_log(job, f"BMC bridge {bridge} created in namespace {ns}")

    # 2. Assign BMC IPs to the bridge
    for vm in vms:
        bmc_ip = _validate_ip(vm["bmc_ip"])
        _run_cmd(
            job,
            [
                "ip",
                "netns",
                "exec",
                ns,
                "ip",
                "addr",
                "add",
                f"{bmc_ip}/{prefix}",
                "dev",
                bridge,
            ],
            timeout=10,
        )

    # 2b. Start dnsmasq for DHCP on BMC bridge (static reservations)
    dhcp_hosts = params.get("dhcp_hosts", [])
    if dhcp_hosts:
        _bmc_start_dnsmasq(job, ns, pid, bridge, bmc_cidr, dhcp_hosts)

    # 3. Create htpasswd file for sushy basic auth (bcrypt format required by sushy-tools)
    htpasswd_path = os.path.join(bmc_dir, "htpasswd")
    bcrypt_hash = subprocess.run(
        [
            f"{venv_bin}/python3",
            "-c",
            f"import bcrypt; print(bcrypt.hashpw({bmc_password!r}.encode(), bcrypt.gensalt()).decode())",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    with open(htpasswd_path, "w") as f:
        f.write(f"{bmc_username}:{bcrypt_hash}\n")

    # 4. Create per-project libvirt storage pool for virtual media
    vmedia_dir = os.path.join(bmc_dir, "vmedia")
    os.makedirs(vmedia_dir, exist_ok=True)
    pool_name = f"troshka-vmedia-{pid}"
    # Remove existing pool if any
    subprocess.run(
        ["virsh", "pool-destroy", pool_name], capture_output=True, timeout=10
    )
    subprocess.run(
        ["virsh", "pool-undefine", pool_name], capture_output=True, timeout=10
    )
    subprocess.run(
        ["virsh", "pool-define-as", pool_name, "dir", "--target", vmedia_dir],
        capture_output=True,
        timeout=10,
    )
    subprocess.run(["virsh", "pool-start", pool_name], capture_output=True, timeout=10)
    subprocess.run(
        ["virsh", "pool-autostart", pool_name], capture_output=True, timeout=10
    )
    _job_log(job, f"Storage pool {pool_name} created at {vmedia_dir}")

    # 5. Start sushy-emulator per VM (HTTP + SSL)
    for vm in vms:
        _bmc_start_sushy_for_vm(
            job, ns, bmc_dir, venv_bin, vm, pool_name, htpasswd_path
        )

    # 6. Start vbmcd and register VMs for IPMI
    _bmc_start_vbmcd(job, ns, bmc_dir, venv_bin, vms, bmc_username, bmc_password)

    return {
        "status": "ok",
        "bmc_bridge": bridge,
        "vm_count": len(vms),
    }


COMMAND_HANDLERS["bmc/setup"] = _handle_bmc_setup


def _handle_bmc_create_bridge(job, params):
    """Create BMC bridge only (no services). Called before VM creation so libvirt can validate the bridge name."""
    project_id = _validate_project_id(params["project_id"])
    bmc_cidr = params["bmc_cidr"]
    bmc_gateway_ip = params["bmc_gateway_ip"]

    pid = project_id[:8]
    ns = f"troshka-{pid}"
    bridge = f"br-bmc-{pid}"
    prefix = bmc_cidr.split("/")[1] if "/" in bmc_cidr else "24"

    # Create bridge inside namespace
    try:
        _run_cmd(
            job, ["ip", "netns", "exec", ns, "ip", "link", "del", bridge], timeout=10
        )
    except RuntimeError:
        pass
    _run_cmd(
        job,
        ["ip", "netns", "exec", ns, "ip", "link", "add", bridge, "type", "bridge"],
        timeout=10,
    )
    _run_cmd(
        job,
        [
            "ip",
            "netns",
            "exec",
            ns,
            "ip",
            "addr",
            "add",
            f"{bmc_gateway_ip}/{prefix}",
            "dev",
            bridge,
        ],
        timeout=10,
    )
    _run_cmd(
        job, ["ip", "netns", "exec", ns, "ip", "link", "set", bridge, "up"], timeout=10
    )

    # Dummy bridge in host namespace for libvirt validation
    try:
        subprocess.run(
            ["ip", "link", "show", bridge], capture_output=True, check=True, timeout=5
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        _run_cmd(job, ["ip", "link", "add", bridge, "type", "bridge"], timeout=10)
        subprocess.run(
            ["nmcli", "dev", "set", bridge, "managed", "no"],
            capture_output=True,
            timeout=5,
        )
    _run_cmd(job, ["ip", "link", "set", bridge, "up"], timeout=10)

    # Assign BMC IPs to the bridge
    for vm in params.get("vms", []):
        bmc_ip = _validate_ip(vm["bmc_ip"])
        _run_cmd(
            job,
            [
                "ip",
                "netns",
                "exec",
                ns,
                "ip",
                "addr",
                "add",
                f"{bmc_ip}/{prefix}",
                "dev",
                bridge,
            ],
            timeout=10,
        )

    _job_log(job, f"BMC bridge {bridge} created (services not started)")
    return {"status": "ok", "bridge": bridge}


COMMAND_HANDLERS["bmc/create-bridge"] = _handle_bmc_create_bridge


def _kill_bmc_processes(job, bmc_dir):
    """Kill all sushy-emulator and vbmcd processes for a BMC directory. Returns kill count."""
    killed = 0
    if os.path.isdir(bmc_dir):
        for fname in os.listdir(bmc_dir):
            if fname.startswith("sushy-") and fname.endswith(".pid"):
                pid_path = os.path.join(bmc_dir, fname)
                try:
                    with open(pid_path) as f:
                        p = int(f.read().strip())
                    _safe_kill(p, signal.SIGTERM)
                    killed += 1
                    _job_log(job, f"Killed sushy-emulator PID {p}")
                except (ValueError, ProcessLookupError, PermissionError):
                    pass

    # Kill vbmcd directly — all vbmc entries die with it, no need for graceful stop
    vbmcd_pid_path = os.path.join(bmc_dir, _VBMCD_PID)
    if os.path.exists(vbmcd_pid_path):
        try:
            with open(vbmcd_pid_path) as f:
                p = int(f.read().strip())
            _safe_kill(p, signal.SIGTERM)
            killed += 1
            _job_log(job, f"Killed vbmcd PID {p}")
        except (ValueError, ProcessLookupError, PermissionError):
            pass
    return killed


def _teardown_bmc_dnsmasq(job, pid):
    """Kill BMC dnsmasq and clean up its config/PID/lease files. Returns 1 if killed, 0 otherwise."""
    dnsmasq_pid_file = f"/run/troshka-dnsmasq-bmc-{pid}.pid"
    killed = 0
    if os.path.exists(dnsmasq_pid_file):
        try:
            with open(dnsmasq_pid_file) as f:
                p = int(f.read().strip())
            _safe_kill(p, signal.SIGTERM)
            killed = 1
            _job_log(job, f"Killed BMC dnsmasq PID {p}")
        except (ValueError, ProcessLookupError, PermissionError):
            pass
    for f_path in [
        f"/etc/dnsmasq.d/troshka-bmc-{pid}.conf",
        dnsmasq_pid_file,
        f"{_DNSMASQ_PREFIX}-bmc-{pid}.leases",
    ]:
        try:
            os.remove(f_path)
        except FileNotFoundError:
            pass
    return killed


def _handle_bmc_teardown(job, params):
    """Tear down all BMC endpoints for a project."""
    project_id = _validate_project_id(params["project_id"])
    pid = project_id[:8]
    ns = f"troshka-{pid}"
    bridge = f"br-bmc-{pid}"
    bmc_dir = f"{_BMC_DIR}/{project_id}"

    killed = _kill_bmc_processes(job, bmc_dir)

    try:
        _run_cmd(
            job, ["ip", "netns", "exec", ns, "ip", "link", "del", bridge], timeout=10
        )
        _job_log(job, f"Removed BMC bridge {bridge} from namespace")
    except RuntimeError:
        pass

    try:
        _run_cmd(job, ["ip", "link", "del", bridge], timeout=10)
    except RuntimeError:
        pass

    killed += _teardown_bmc_dnsmasq(job, pid)

    # Destroy libvirt storage pool for virtual media
    pool_name = f"troshka-vmedia-{pid}"
    subprocess.run(
        ["virsh", "pool-destroy", pool_name], capture_output=True, timeout=10
    )
    subprocess.run(
        ["virsh", "pool-undefine", pool_name], capture_output=True, timeout=10
    )
    _job_log(job, f"Removed storage pool {pool_name}")

    if os.path.isdir(bmc_dir):
        shutil.rmtree(bmc_dir, ignore_errors=True)
        _job_log(job, f"Removed BMC config dir: {bmc_dir}")

    return {"status": "ok", "killed": killed}


COMMAND_HANDLERS["bmc/teardown"] = _handle_bmc_teardown


def _handle_bmc_status(job, params):
    """Check status of BMC processes for a project."""
    project_id = _validate_project_id(params["project_id"])
    bmc_dir = f"{_BMC_DIR}/{project_id}"

    result = {"sushy_processes": [], "vbmcd_running": False}

    if not os.path.isdir(bmc_dir):
        return result

    for fname in os.listdir(bmc_dir):
        if fname.startswith("sushy-") and fname.endswith(".pid"):
            pid_path = os.path.join(bmc_dir, fname)
            try:
                with open(pid_path) as f:
                    p = int(f.read().strip())
                os.kill(p, 0)
                result["sushy_processes"].append(
                    {"pid": p, "file": fname, "alive": True}
                )
            except (ValueError, ProcessLookupError, PermissionError, FileNotFoundError):
                result["sushy_processes"].append({"file": fname, "alive": False})

    vbmcd_pid_path = os.path.join(bmc_dir, _VBMCD_PID)
    if os.path.exists(vbmcd_pid_path):
        try:
            with open(vbmcd_pid_path) as f:
                p = int(f.read().strip())
            os.kill(p, 0)
            result["vbmcd_running"] = True
        except (ValueError, ProcessLookupError, PermissionError):
            pass

    return result


COMMAND_HANDLERS["bmc/status"] = _handle_bmc_status


def _handle_vm_migrate(job, params):
    """Migrate a VM to another host. Uses --live if running, offline if stopped."""
    domain = _validate_domain_name(params["domain"])
    target_host = _validate_ip(params["target_host"])

    # Check domain state
    state_proc = subprocess.run(
        ["virsh", "domstate", domain], capture_output=True, text=True, timeout=10
    )
    state = state_proc.stdout.strip()
    _job_log(job, f"VM state: {state}")

    cmd = [
        "virsh",
        "migrate",
        "--verbose",
        "--persistent",
        "--undefinesource",
        domain,
        f"qemu+tls://{target_host}/system",
    ]
    if state == "running":
        cmd.insert(2, "--live")

    _run_cmd(job, cmd, timeout=600)

    return {
        "domain": domain,
        "target_host": target_host,
        "status": "migrated",
    }


COMMAND_HANDLERS["vm/migrate"] = _handle_vm_migrate


def _handle_tls_update_certs(job, params):
    """Update libvirt TLS certificates (for auto-renewal)."""
    import base64 as _b64

    ca_cert = _b64.b64decode(params["ca_cert_b64"]).decode()
    host_cert = _b64.b64decode(params["host_cert_b64"]).decode()
    host_key = _b64.b64decode(params["host_key_b64"]).decode()

    os.makedirs("/etc/pki/CA", exist_ok=True)
    os.makedirs("/etc/pki/libvirt/private", exist_ok=True)

    with open("/etc/pki/CA/cacert.pem", "w") as f:
        f.write(ca_cert)
    with open("/etc/pki/libvirt/servercert.pem", "w") as f:
        f.write(host_cert)
    with open("/etc/pki/libvirt/private/serverkey.pem", "w") as f:
        f.write(host_key)
    os.chmod("/etc/pki/libvirt/private/serverkey.pem", 0o600)
    with open("/etc/pki/libvirt/clientcert.pem", "w") as f:
        f.write(host_cert)
    with open("/etc/pki/libvirt/private/clientkey.pem", "w") as f:
        f.write(host_key)
    os.chmod("/etc/pki/libvirt/private/clientkey.pem", 0o600)

    _run_cmd(job, ["systemctl", "restart", "virtqemud"], timeout=30)
    return {"status": "updated"}


COMMAND_HANDLERS["tls/update-certs"] = _handle_tls_update_certs


def _serial_open_pty(domain):
    """Find and open the serial console PTY for a domain. Returns the PTY path."""
    import re

    result = subprocess.run(
        ["virsh", "dumpxml", domain],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Cannot get XML for {domain}: {result.stderr}")
    pty_match = re.search(r"source path='(/dev/pts/\d+)'", result.stdout)
    if not pty_match:
        raise RuntimeError(f"No serial console PTY found for {domain}")
    return pty_match.group(1)


def _serial_clean_output(raw, outf, marker):
    """Strip ANSI escapes, echoed commands, and prompts from serial output."""
    import re

    raw = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07", "", raw)
    raw = raw.replace("\r\n", "\n").replace("\r", "")
    out_lines = []
    for line in raw.split("\n"):
        clean = line.strip()
        if not clean:
            continue
        # Skip echoed command line and marker artifacts
        if (
            "__a=" in clean
            or "__b=" in clean
            or f"cat {outf}" in clean
            or marker in clean
        ):
            continue
        # Strip custom prompt prefix if present
        if re.match(r"^\S+[>#\$%]\s", clean):
            clean = re.sub(r"^\S+[>#\$%]\s+", "", clean).strip()
        if clean:
            out_lines.append(clean)
    return "\n".join(out_lines)


def _serial_poke_and_login(child, username, password, domain, any_prompt, shell_prompt):
    """Poke the serial console and handle login if needed. Returns error dict or None."""
    child.send("stty echo 2>/dev/null\r")
    time.sleep(0.3)

    child.send("\x03\r")
    time.sleep(0.5)

    def _login():
        if not password:
            raise RuntimeError("VM is at login prompt but no password provided")
        child.send(username + "\r")
        child.expect("[Pp]assword:", timeout=5)
        child.send(password + "\r")
        idx = child.expect([shell_prompt, "Last login", "incorrect", any_prompt[-1]], timeout=10)
        if idx == 1:
            child.expect(shell_prompt, timeout=5)
        elif idx != 0:
            raise RuntimeError("Login failed")

    idx = child.expect(any_prompt, timeout=3)
    if idx == 0:
        _login()
    elif idx == 3:
        child.send("\r")
        idx2 = child.expect(any_prompt, timeout=3)
        if idx2 == 0:
            _login()
        elif idx2 == 3:
            return {
                "domain": domain,
                "output": "",
                "error": "Console not responding",
            }
    return None


def _handle_vm_serial_exec(job, params):
    """Execute a command on a VM via serial console using pexpect fdspawn on the raw PTY."""
    domain = _validate_domain_name(params["domain_name"])
    username = params.get("username", "root")
    password = params.get("password", "")
    command = params.get("command", "")
    timeout_secs = min(params.get("timeout", 10), 60)

    if not command:
        raise RuntimeError(_NO_COMMAND)

    pty_path = _serial_open_pty(domain)

    for sp in [
        "/opt/troshka/venv/lib/python3.12/site-packages",
        "/opt/troshka/venv/lib/python3.13/site-packages",
    ]:
        if sp not in sys.path and os.path.isdir(sp):
            sys.path.insert(0, sp)
    from pexpect import fdpexpect, TIMEOUT, EOF

    fd = os.open(pty_path, os.O_RDWR)
    child = fdpexpect.fdspawn(fd, encoding="utf-8", timeout=timeout_secs)

    SHELL = r"[#\$] "
    ANY_PROMPT = ["login:", SHELL, r"[>%] ", TIMEOUT]

    try:
        err = _serial_poke_and_login(child, username, password, domain, ANY_PROMPT, SHELL)
        if err is not None:
            return err

        import random

        rid = random.randint(10000, 99999)
        outf = f"/tmp/.t{rid}"
        marker = f"XDONE{rid}X"
        child.send(
            f"__a=XDONE; __b={rid}X; ({command}) > {outf} 2>&1; cat {outf}; rm -f {outf}; echo $__a$__b; unset __a __b\r"
        )
        child.expect(marker, timeout=timeout_secs)
        raw = child.before or ""

        output = _serial_clean_output(raw, outf, marker)
        return {"domain": domain, "output": output}
    except TIMEOUT:
        return {"domain": domain, "output": "", "error": "Command timed out"}
    except EOF:
        return {"domain": domain, "output": "", "error": "Console connection closed"}
    except RuntimeError as e:
        return {"domain": domain, "output": "", "error": str(e)}
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


COMMAND_HANDLERS["vm/serial-exec"] = _handle_vm_serial_exec


def _handle_vm_ssh_exec(job, params):
    """Execute a command on a VM via SSH through the network namespace."""
    project_id = params.get("project_id", "")
    vm_ip = params.get("vm_ip", "")
    username = params.get("username", "cloud-user")
    password = params.get("password", "")
    private_key = params.get("private_key", "")
    command = params.get("command", "")
    timeout_secs = min(params.get("timeout", 10), 3600)

    if not command:
        raise RuntimeError(_NO_COMMAND)
    if not vm_ip:
        raise RuntimeError("No VM IP specified")
    if not password and not private_key:
        raise RuntimeError("No password or private_key specified")

    ns = f"troshka-{project_id[:8]}" if project_id else ""
    ns_prefix = ["ip", "netns", "exec", ns] if ns else []

    import tempfile as _tf

    key_file = None
    ssh_cmd = ns_prefix[:]
    try:
        if private_key:
            key_file = _tf.NamedTemporaryFile(mode="w", suffix=".pem", delete=False)
            key_file.write(private_key)
            key_file.close()
            os.chmod(key_file.name, 0o600)
            ssh_cmd += [
                "ssh",
                "-i",
                key_file.name,
                "-o",
                _SSH_STRICT_HOST,
                "-o",
                _SSH_KNOWN_HOSTS,
                "-o",
                _SSH_LOG_LEVEL,
                "-o",
                f"ConnectTimeout={min(timeout_secs, 10)}",
                f"{username}@{vm_ip}",
                command,
            ]
        else:
            ssh_cmd += [
                "sshpass",
                "-p",
                password,
                "ssh",
                "-o",
                _SSH_STRICT_HOST,
                "-o",
                _SSH_KNOWN_HOSTS,
                "-o",
                _SSH_LOG_LEVEL,
                "-o",
                f"ConnectTimeout={min(timeout_secs, 10)}",
                f"{username}@{vm_ip}",
                command,
            ]

        result = subprocess.run(
            ssh_cmd,
            capture_output=True,
            text=True,
            timeout=timeout_secs + 5,
        )
        return {
            "output": result.stdout,
            "error": result.stderr,
            "exit_code": result.returncode,
        }
    finally:
        if key_file and os.path.exists(key_file.name):
            os.unlink(key_file.name)


COMMAND_HANDLERS["vm/ssh-exec"] = _handle_vm_ssh_exec


def _guest_exec_poll(domain, pid, timeout_secs, job):
    """Poll qemu-guest-agent for guest-exec completion. Returns result dict or raises."""
    import json as _json
    import base64

    status_cmd = _json.dumps(
        {
            "execute": "guest-exec-status",
            "arguments": {"pid": pid},
        }
    )
    deadline = time.time() + timeout_secs
    while time.time() < deadline:
        if job.get("_cancelled"):
            raise RuntimeError("Job cancelled")
        sr = subprocess.run(
            ["virsh", "qemu-agent-command", domain, status_cmd, "--timeout", "10"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if sr.returncode != 0:
            raise RuntimeError(f"guest-exec-status failed: {sr.stderr.strip()}")
        status = _json.loads(sr.stdout).get("return", {})
        if status.get("exited"):
            stdout = ""
            stderr = ""
            if status.get("out-data"):
                stdout = base64.b64decode(status["out-data"]).decode(
                    "utf-8", errors="replace"
                )
            if status.get("err-data"):
                stderr = base64.b64decode(status["err-data"]).decode(
                    "utf-8", errors="replace"
                )
            return {
                "output": stdout,
                "error": stderr,
                "exit_code": status.get("exitcode", -1),
            }
        time.sleep(0.5)
    raise RuntimeError(f"guest-exec timed out after {timeout_secs}s (pid={pid})")


def _handle_vm_guest_exec(job, params):
    """Execute a command on a VM via qemu-guest-agent."""
    domain = _validate_domain_name(params["domain_name"])
    command = params.get("command", "")
    timeout_secs = min(params.get("timeout", 600), 3600)

    if not command:
        raise RuntimeError(_NO_COMMAND)

    # Check guest agent is available (10s timeout to avoid blocking on frozen VMs)
    try:
        check = subprocess.run(
            [
                "virsh",
                "qemu-agent-command",
                domain,
                '{"execute":"guest-info"}',
                "--timeout",
                "10",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if check.returncode != 0:
            raise RuntimeError(
                f"Guest agent not available on {domain}: {check.stderr.strip()}"
            )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Guest agent not responding on {domain}")

    import json as _json

    # Verify guest-exec is enabled (some distros block it by default)
    try:
        info = _json.loads(check.stdout)
        cmds = info.get("return", {}).get("supported_commands", [])
        exec_cmd = next((c for c in cmds if c.get("name") == "guest-exec"), None)
        if exec_cmd and not exec_cmd.get("enabled", False):
            raise RuntimeError(
                f"guest-exec is disabled on {domain} (blocked by guest agent config)"
            )
    except (_json.JSONDecodeError, StopIteration):
        pass

    # Execute command via guest-exec
    exec_cmd = _json.dumps(
        {
            "execute": "guest-exec",
            "arguments": {
                "path": "/bin/sh",
                "arg": ["-c", command],
                "capture-output": True,
            },
        }
    )
    result = subprocess.run(
        ["virsh", "qemu-agent-command", domain, exec_cmd, "--timeout", "10"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(f"guest-exec failed: {result.stderr.strip()}")

    resp = _json.loads(result.stdout)
    pid = resp.get("return", {}).get("pid")
    if pid is None:
        raise RuntimeError(f"No PID in guest-exec response: {result.stdout}")

    return _guest_exec_poll(domain, pid, timeout_secs, job)


COMMAND_HANDLERS["vm/guest-exec"] = _handle_vm_guest_exec


# ── Console exec (VNC send-key + screenshot + OCR) ──

_CHAR_TO_KEYS = {}
for _c in "abcdefghijklmnopqrstuvwxyz":
    _CHAR_TO_KEYS[_c] = [f"KEY_{_c.upper()}"]
    _CHAR_TO_KEYS[_c.upper()] = ["KEY_LEFTSHIFT", f"KEY_{_c.upper()}"]
for _i, _c in enumerate("1234567890"):
    _CHAR_TO_KEYS[_c] = [f"KEY_{_c}"]
_SHIFT_DIGITS = {
    "!": "KEY_1",
    "@": "KEY_2",
    "#": "KEY_3",
    "$": "KEY_4",
    "%": "KEY_5",
    "^": "KEY_6",
    "&": "KEY_7",
    "*": "KEY_8",
    "(": "KEY_9",
    ")": "KEY_0",
}
for _c, _k in _SHIFT_DIGITS.items():
    _CHAR_TO_KEYS[_c] = ["KEY_LEFTSHIFT", _k]
_SYMBOL_KEYS = {
    " ": ["KEY_SPACE"],
    "\n": ["KEY_ENTER"],
    "\t": ["KEY_TAB"],
    "-": ["KEY_MINUS"],
    "=": ["KEY_EQUAL"],
    "[": ["KEY_LEFTBRACE"],
    "]": ["KEY_RIGHTBRACE"],
    "\\": ["KEY_BACKSLASH"],
    ";": ["KEY_SEMICOLON"],
    "'": ["KEY_APOSTROPHE"],
    "`": ["KEY_GRAVE"],
    ",": ["KEY_COMMA"],
    ".": ["KEY_DOT"],
    "/": ["KEY_SLASH"],
    "_": ["KEY_LEFTSHIFT", "KEY_MINUS"],
    "+": ["KEY_LEFTSHIFT", "KEY_EQUAL"],
    "{": ["KEY_LEFTSHIFT", "KEY_LEFTBRACE"],
    "}": ["KEY_LEFTSHIFT", "KEY_RIGHTBRACE"],
    "|": ["KEY_LEFTSHIFT", "KEY_BACKSLASH"],
    ":": ["KEY_LEFTSHIFT", "KEY_SEMICOLON"],
    '"': ["KEY_LEFTSHIFT", "KEY_APOSTROPHE"],
    "~": ["KEY_LEFTSHIFT", "KEY_GRAVE"],
    "<": ["KEY_LEFTSHIFT", "KEY_COMMA"],
    ">": ["KEY_LEFTSHIFT", "KEY_DOT"],
    "?": ["KEY_LEFTSHIFT", "KEY_SLASH"],
}
_CHAR_TO_KEYS.update(_SYMBOL_KEYS)


def _console_screenshot_ocr(domain):
    """Take a VNC screenshot and OCR it to text."""
    tmp_img = f"/tmp/troshka-screen-{domain}.ppm"
    tmp_txt = f"/tmp/troshka-ocr-{domain}"
    try:
        result = subprocess.run(
            ["virsh", "screenshot", domain, tmp_img],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return ""
        result = subprocess.run(
            ["tesseract", tmp_img, tmp_txt],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return ""
        with open(tmp_txt + ".txt") as f:
            return f.read()
    except (subprocess.TimeoutExpired, OSError):
        return ""
    finally:
        for p in [tmp_img, tmp_txt + ".txt"]:
            try:
                os.remove(p)
            except OSError:
                pass


def _console_send_text(domain, text):
    """Type text into a VM via virsh send-key."""
    for ch in text:
        keys = _CHAR_TO_KEYS.get(ch)
        if not keys:
            continue
        subprocess.run(
            ["virsh", "send-key", domain] + keys,
            capture_output=True,
            timeout=5,
        )


def _console_send_keys(domain, *keys):
    """Send raw key names to a VM."""
    subprocess.run(
        ["virsh", "send-key", domain] + list(keys),
        capture_output=True,
        timeout=5,
    )


def _console_detect_state(ocr_text):
    """Detect console state from OCR text."""
    import re

    text = ocr_text.strip()
    if not text or len(text) < 3:
        return "unknown"
    last_lines = "\n".join(text.split("\n")[-5:])
    if re.search(r"login\s*:?\s*$", last_lines, re.IGNORECASE | re.MULTILINE):
        return "login"
    if re.search(r"[Pp]ass[wvu]ord\s*:?\s*$", last_lines, re.MULTILINE):
        return "password"
    if re.search(r"[\]$#~]\s*$", last_lines, re.MULTILINE):
        return "shell"
    return "unknown"


def _console_login(job, domain, username, password):
    """Log into the console if needed. Returns True if shell prompt reached."""
    for attempt in range(4):
        ocr = _console_screenshot_ocr(domain)
        state = _console_detect_state(ocr)
        last_line = ocr.strip().split("\n")[-1] if ocr.strip() else "(empty)"
        _job_log(
            job,
            f"Console state: {state} (attempt {attempt + 1}, last: {last_line[:80]})",
        )

        if state == "shell":
            return True

        if state == "unknown":
            _console_send_keys(domain, "KEY_LEFTCTRL", "KEY_LEFTALT", "KEY_F3")
            time.sleep(2)
            _console_send_keys(domain, "KEY_ENTER")
            time.sleep(1)
            continue

        if state == "login":
            _console_send_text(domain, username + "\n")
            time.sleep(2)
            continue

        if state == "password":
            _console_send_text(domain, password + "\n")
            time.sleep(3)

    return False


def _console_extract_output(ocr_text):
    """Extract command output between markers."""
    import re

    m = re.search(r"TROSHKA_BEGIN\s*\n(.*?)TROSHKA_EXIT\s*(\d+)?", ocr_text, re.DOTALL)
    if m:
        output = m.group(1).strip()
        exit_code = int(m.group(2)) if m.group(2) else None
        return output, exit_code
    return ocr_text.strip(), None


def _handle_vm_console_exec(job, params):
    """Execute a command via VNC console: send-key + screenshot + OCR."""
    domain = _validate_domain_name(params["domain_name"])
    command = params.get("command", "")
    username = params.get("username", "root")
    password = params.get("password", "")
    timeout = min(int(params.get("timeout", 10)), 60)
    force_tty = params.get("force_tty", False)

    if not command:
        raise RuntimeError("No command provided")
    if not password:
        raise RuntimeError("Password required for console exec")

    # Verify tesseract is available
    if subprocess.run(["which", "tesseract"], capture_output=True).returncode != 0:
        raise RuntimeError("tesseract not installed on host")

    # Verify domain is running
    state_result = subprocess.run(
        ["virsh", "domstate", domain],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if "running" not in state_result.stdout.lower():
        raise RuntimeError(f"{domain} is not running")

    # Switch to TTY3 if requested (TTY1-2 used by Wayland/GNOME)
    if force_tty:
        _console_send_keys(domain, "KEY_LEFTCTRL", "KEY_LEFTALT", "KEY_F3")
        time.sleep(2)
        _job_log(job, "Switched to TTY3")

    # Login if needed
    if not _console_login(job, domain, username, password):
        return {
            "domain": domain,
            "output": "",
            "exit_code": None,
            "error": "Could not reach shell prompt",
            "method": "console",
        }
    _job_log(job, "Shell prompt reached")

    # Clear screen, then send command with output markers
    _console_send_text(domain, "clear\n")
    time.sleep(0.5)
    wrapped = f"echo TROSHKA_BEGIN; {command} 2>&1; echo TROSHKA_EXIT $?"
    _console_send_text(domain, wrapped + "\n")
    _job_log(job, "Command sent, waiting for output")

    # Poll for markers in OCR output (up to timeout)
    ocr = ""
    deadline = time.time() + min(timeout, 60)
    while time.time() < deadline:
        time.sleep(2)
        ocr = _console_screenshot_ocr(domain)
        if "TROSHKA_EXIT" in ocr:
            break
    output, exit_code = _console_extract_output(ocr)
    _job_log(job, f"Output captured ({len(output)} chars)")

    # Switch back to TTY1 if we switched away
    if force_tty:
        _console_send_keys(domain, "KEY_LEFTCTRL", "KEY_LEFTALT", "KEY_F1")

    return {
        "domain": domain,
        "output": output,
        "exit_code": exit_code,
        "error": "",
        "method": "console",
    }


COMMAND_HANDLERS["vm/console-exec"] = _handle_vm_console_exec


# ── VM file transfer ──


def _scp_common_args(password="", key_file=""):
    """SSH/SCP args shared by file transfer operations."""
    if key_file:
        return [
            "scp",
            "-i",
            key_file,
            "-o",
            _SSH_STRICT_HOST,
            "-o",
            _SSH_KNOWN_HOSTS,
            "-o",
            _SSH_LOG_LEVEL,
            "-o",
            _SSH_TIMEOUT,
        ]
    return [
        "sshpass",
        "-p",
        password,
        "scp",
        "-o",
        _SSH_STRICT_HOST,
        "-o",
        _SSH_KNOWN_HOSTS,
        "-o",
        _SSH_LOG_LEVEL,
        "-o",
        _SSH_TIMEOUT,
    ]


def _ssh_common_args(password="", key_file=""):
    """SSH args shared by file transfer operations."""
    if key_file:
        return [
            "ssh",
            "-i",
            key_file,
            "-o",
            _SSH_STRICT_HOST,
            "-o",
            _SSH_KNOWN_HOSTS,
            "-o",
            _SSH_LOG_LEVEL,
            "-o",
            _SSH_TIMEOUT,
        ]
    return [
        "sshpass",
        "-p",
        password,
        "ssh",
        "-o",
        _SSH_STRICT_HOST,
        "-o",
        _SSH_KNOWN_HOSTS,
        "-o",
        _SSH_LOG_LEVEL,
        "-o",
        _SSH_TIMEOUT,
    ]


def _handle_vm_file_push_job(job, params):
    """SCP a local temp file to a VM (job-based for large files)."""
    project_id = params["project_id"]
    vm_ip = params["vm_ip"]
    username = params["username"]
    password = params["password"]
    remote_path = params["remote_path"]
    mode = params.get("mode", "")
    local_path = params["local_path"]
    file_size = os.path.getsize(local_path)

    ns = f"troshka-{project_id[:8]}" if project_id else ""
    ns_prefix = ["ip", "netns", "exec", ns] if ns else []

    try:
        _job_log(job, f"Uploading {file_size} bytes to {remote_path}")
        proc = subprocess.Popen(
            ns_prefix
            + _scp_common_args(password)
            + [
                local_path,
                f"{username}@{vm_ip}:{remote_path}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        job["_process"] = proc
        _, stderr = proc.communicate(timeout=3600)
        job["_process"] = None
        if proc.returncode != 0:
            raise RuntimeError(
                f"SCP failed (exit {proc.returncode}): {stderr.decode().strip()}"
            )

        if mode:
            subprocess.run(
                ns_prefix
                + _ssh_common_args(password)
                + [
                    f"{username}@{vm_ip}",
                    f"chmod {mode} {remote_path}",
                ],
                capture_output=True,
                timeout=30,
            )

        _job_log(job, f"Upload complete: {file_size} bytes")
        return {"size": file_size, "remote_path": remote_path}
    finally:
        try:
            os.unlink(local_path)
        except OSError:
            pass


COMMAND_HANDLERS["vm/file-push-job"] = _handle_vm_file_push_job

LARGE_FILE_THRESHOLD = 10 * 1024 * 1024  # 10 MB


def _scp_push_small_file(
    handler,
    ns_prefix,
    password,
    key_file,
    tmp_path,
    username,
    vm_ip,
    remote_path,
    mode,
    file_size,
):
    """SCP a small file to a VM synchronously and send the HTTP response."""
    try:
        result = subprocess.run(
            ns_prefix
            + _scp_common_args(password=password, key_file=key_file)
            + [
                tmp_path,
                f"{username}@{vm_ip}:{remote_path}",
            ],
            capture_output=True,
            timeout=120,
        )
        if result.returncode != 0:
            handler._send_json(
                502, {"error": f"SCP failed: {result.stderr.decode().strip()}"}
            )
            return

        if mode:
            subprocess.run(
                ns_prefix
                + _ssh_common_args(password=password, key_file=key_file)
                + [
                    f"{username}@{vm_ip}",
                    f"chmod {mode} {remote_path}",
                ],
                capture_output=True,
                timeout=30,
            )

        handler._send_json(200, {"size": file_size, "remote_path": remote_path})
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        if key_file:
            try:
                os.unlink(key_file)
            except OSError:
                pass


def _prepare_ssh_key_file(private_key):
    """Write private key to a temporary file and return the path."""
    if not private_key:
        return ""
    key_file = f"/tmp/troshka-scp-key-{uuid.uuid4()}"
    fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(private_key)
    return key_file


def _validate_file_push_data(handler, data, key_file):
    """Validate file data and return temp path, or send error and return None."""
    if not data:
        if key_file:
            os.unlink(key_file)
        handler._send_json(400, {"error": "Empty file body"})
        return None

    tmp_path = f"/tmp/troshka-xfer-{uuid.uuid4()}"
    with open(tmp_path, "wb") as f:
        f.write(data)
    return tmp_path


@route("POST", "/vm/file-push")
def handle_vm_file_push(handler, params):
    """Push a file to a VM via SCP. Binary body, metadata in query params."""
    import urllib.parse

    qs = urllib.parse.parse_qs(urllib.parse.urlparse(handler.path).query)

    project_id = qs.get("project_id", [""])[0]
    vm_ip = qs.get("vm_ip", [""])[0]
    username = qs.get("username", ["cloud-user"])[0]
    password = qs.get("password", [""])[0]
    private_key = qs.get("private_key", [""])[0]
    remote_path = qs.get("remote_path", [""])[0]
    mode = qs.get("mode", [""])[0]

    if not all([project_id, vm_ip, remote_path]) or not (password or private_key):
        handler._send_json(
            400,
            {
                "error": "Missing required params: project_id, vm_ip, remote_path, and password or private_key"
            },
        )
        return

    key_file = _prepare_ssh_key_file(private_key)

    data = handler._read_raw_body()
    tmp_path = _validate_file_push_data(handler, data, key_file)
    if tmp_path is None:
        return

    file_size = len(data)

    if file_size > LARGE_FILE_THRESHOLD:
        status, body = _dispatch_job(
            "vm/file-push-job",
            {
                "project_id": project_id,
                "vm_ip": vm_ip,
                "username": username,
                "password": password,
                "private_key": private_key,
                "remote_path": remote_path,
                "mode": mode,
                "local_path": tmp_path,
            },
        )
        if "job_id" in body:
            body["size"] = file_size
        handler._send_json(status, body)
        return

    ns = f"troshka-{project_id[:8]}" if project_id else ""
    ns_prefix = ["ip", "netns", "exec", ns] if ns else []

    _scp_push_small_file(
        handler,
        ns_prefix,
        password,
        key_file,
        tmp_path,
        username,
        vm_ip,
        remote_path,
        mode,
        file_size,
    )


@route("POST", "/vm/file-pull")
def handle_vm_file_pull(handler, params):
    """Pull a file from a VM via SCP. Returns binary response."""
    body = handler._read_body()
    project_id = body.get("project_id", "")
    vm_ip = body.get("vm_ip", "")
    username = body.get("username", "cloud-user")
    password = body.get("password", "")
    remote_path = body.get("remote_path", "")

    if not all([project_id, vm_ip, password, remote_path]):
        handler._send_json(
            400,
            {
                "error": "Missing required fields: project_id, vm_ip, password, remote_path"
            },
        )
        return

    ns = f"troshka-{project_id[:8]}" if project_id else ""
    ns_prefix = ["ip", "netns", "exec", ns] if ns else []

    tmp_path = f"/tmp/troshka-xfer-{uuid.uuid4()}"
    try:
        result = subprocess.run(
            ns_prefix
            + _scp_common_args(password)
            + [
                f"{username}@{vm_ip}:{remote_path}",
                tmp_path,
            ],
            capture_output=True,
            timeout=3600,
        )
        if result.returncode != 0:
            handler._send_json(
                502, {"error": f"SCP failed: {result.stderr.decode().strip()}"}
            )
            return

        handler._stream_file(200, tmp_path)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ── NBD export for pattern buffer ──

_nbd_ports_lock = threading.Lock()
_nbd_ports = (
    {}
)  # port -> {"pid": int, "domain": str, "disk_path": str, "snapshotted": bool}

NBD_PORT_START = 10809
NBD_PORT_END = 10829


NBD_MAX_AGE = 3600  # kill qemu-nbd exports older than 1 hour


def _cleanup_nbd_ports():
    """Kill qemu-nbd processes on NBD ports not tracked in _nbd_ports."""
    for port in range(NBD_PORT_START, NBD_PORT_END + 1):
        with _nbd_ports_lock:
            if port in _nbd_ports:
                continue
        subprocess.run(["fuser", "-k", f"{port}/tcp"], capture_output=True, timeout=5)


def _reap_stale_nbd_exports():
    """Find and kill NBD exports older than NBD_MAX_AGE."""
    now = time.time()
    stale = []
    with _nbd_ports_lock:
        for port, info in _nbd_ports.items():
            if now - info.get("started", now) > NBD_MAX_AGE:
                stale.append(port)
    for port in stale:
        logger.warning("Reaping stale NBD export on port %d", port)
        with _nbd_ports_lock:
            info = _nbd_ports.pop(port, None)
        if info and info.get("pid"):
            try:
                os.kill(info["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass
        subprocess.run(
            ["fuser", "-k", f"{port}/tcp"], capture_output=True, timeout=5
        )


def _nbd_reaper_loop():
    """Periodically clean up stale NBD exports."""
    while True:
        time.sleep(300)
        _reap_stale_nbd_exports()
        _cleanup_nbd_ports()


def _port_in_use(port):
    """Check if a TCP port is actually in use."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def _allocate_nbd_port():
    """Find the next free port in the NBD range."""
    with _nbd_ports_lock:
        for port in range(NBD_PORT_START, NBD_PORT_END + 1):
            if port not in _nbd_ports and not _port_in_use(port):
                return port
    raise RuntimeError("No free NBD ports available")


def _get_disk_actual_size(disk_path):
    """Query qemu-img for the actual on-disk size of a disk image. Returns 0 on failure."""
    try:
        info = subprocess.run(
            ["qemu-img", "info", _PODMAN_JSON, disk_path],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if info.returncode == 0:
            import json as _json

            return _json.loads(info.stdout).get("actual-size", 0)
    except Exception:
        pass
    return 0


def _get_nbd_process_pid(port):
    """Get the PID of the qemu-nbd process listening on the given port."""
    try:
        ps = subprocess.run(
            ["fuser", f"{port}/tcp"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if ps.stdout.strip():
            return int(ps.stdout.strip().split()[-1])
    except Exception:
        pass
    return None


def _handle_nbd_export(job, params):
    """Snapshot a VM disk and serve it read-only over TLS-secured NBD."""
    domain_name = params.get("domain_name", "")
    disk_path = _validate_path(params.get("disk_path", ""))

    if not domain_name:
        raise RuntimeError("domain_name is required")
    if not os.path.exists(disk_path):
        raise RuntimeError(f"Disk not found: {disk_path}")

    running = _is_domain_running(domain_name)
    snapshotted = False
    if running:
        snapshotted = _snapshot_domain(job, domain_name)

    port = _allocate_nbd_port()

    subprocess.run(["fuser", "-k", f"{port}/tcp"], capture_output=True, timeout=5)

    cmd = [
        "qemu-nbd",
        "--read-only",
        "--port",
        str(port),
        "--export-name",
        "disk",
        "--persistent",
        "--fork",
    ]

    cmd.append(disk_path)

    _job_log(job, f"Starting NBD export on port {port}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        if snapshotted:
            _commit_snapshot(job, domain_name)
        raise RuntimeError(f"qemu-nbd failed: {result.stderr.strip()}")

    pid = _get_nbd_process_pid(port)

    with _nbd_ports_lock:
        _nbd_ports[port] = {
            "pid": pid,
            "domain": domain_name,
            "disk_path": disk_path,
            "snapshotted": snapshotted,
            "started": time.time(),
        }

    _job_log(job, f"NBD export active on port {port} (PID {pid})")
    return {
        "port": port,
        "export_name": "disk",
        "snapshotted": snapshotted,
        "disk_size_bytes": _get_disk_actual_size(disk_path),
    }


COMMAND_HANDLERS["nbd/export"] = _handle_nbd_export


def _handle_nbd_stop(job, params):
    """Stop NBD export and commit snapshot overlay."""
    domain_name = params.get("domain_name", "")
    port = int(params.get("port", 0))

    if not port:
        raise RuntimeError("port is required")

    with _nbd_ports_lock:
        info = _nbd_ports.pop(port, None)

    if info and info.get("pid"):
        try:
            os.kill(info["pid"], signal.SIGTERM)
            _job_log(job, f"Killed qemu-nbd PID {info['pid']} on port {port}")
        except ProcessLookupError:
            _job_log(job, f"qemu-nbd PID {info['pid']} already exited")
    else:
        subprocess.run(["fuser", "-k", f"{port}/tcp"], capture_output=True, timeout=10)
        _job_log(job, f"Killed process on port {port} via fuser")

    if domain_name and info and info.get("snapshotted"):
        _job_log(job, "Committing snapshot overlay...")
        _commit_snapshot(job, domain_name)

    return {"port": port, "stopped": True}


COMMAND_HANDLERS["nbd/stop"] = _handle_nbd_stop


def _log_flatten_progress(job, output_path, total_bytes, total_gb, prev_bytes, prev_time):
    """Compute and log flatten transfer progress."""
    if not os.path.exists(output_path):
        return
    cur = os.path.getsize(output_path)
    cur_gb = round(cur / (1024**3), 1)
    now = time.time()
    dt = now - prev_time[0]
    rate_mbps = (
        round((cur - prev_bytes[0]) / (1024**2) / dt) if dt > 0 else 0
    )
    prev_bytes[0] = cur
    prev_time[0] = now
    rate_str = f" ({rate_mbps} MB/s)" if rate_mbps > 0 else ""
    if total_bytes:
        pct = min(100, int(cur * 100 / total_bytes))
        _job_log(
            job,
            f"Flattening: {cur_gb} of {total_gb} GB ({pct}%){rate_str}",
        )
    else:
        _job_log(job, f"Flattening: {cur_gb} GB written{rate_str}")


def _flatten_progress_monitor(job, output_path, total_bytes, flatten_done):
    """Monitor flatten progress, logging transfer rate and percentage."""
    total_gb = round(total_bytes / (1024**3), 1) if total_bytes else 0
    prev_bytes = [0]
    prev_time = [time.time()]
    while not flatten_done.is_set():
        try:
            _log_flatten_progress(job, output_path, total_bytes, total_gb, prev_bytes, prev_time)
        except OSError:
            pass
        flatten_done.wait(10)


def _handle_nbd_pull_flatten(job, params):
    """Connect to remote NBD export, flatten+compress to local disk."""
    nbd_host = params.get("nbd_host", "")
    nbd_port = int(params.get("nbd_port", 0))
    export_name = params.get("export_name", "disk")
    output_path = _validate_path(params.get("output_path", ""))

    if not nbd_host or not nbd_port:
        raise RuntimeError("nbd_host and nbd_port are required")
    if not output_path:
        raise RuntimeError("output_path is required")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    nbd_src = f"nbd://{nbd_host}:{nbd_port}/{export_name}"
    total_bytes = params.get("total_bytes", 0)

    cmd = ["qemu-img", "convert", "-c", "-o", _ZSTD_COMPRESSION, "-O", "qcow2"]
    cmd.append(nbd_src)
    cmd.append(output_path)

    total_gb = round(total_bytes / (1024**3), 1) if total_bytes else 0
    _job_log(job, f"Pulling from {nbd_host}:{nbd_port}, flattening {total_gb} GB...")

    flatten_done = threading.Event()

    mon = threading.Thread(
        target=_flatten_progress_monitor,
        args=(job, output_path, total_bytes, flatten_done),
        daemon=True,
    )
    mon.start()

    try:
        _run_cmd(job, cmd, timeout=3600)
    finally:
        flatten_done.set()

    size_bytes = os.path.getsize(output_path)
    size_gb = round(size_bytes / (1024**3), 1)
    _job_log(job, f"Flatten complete: {size_gb} GB")
    return {"size_bytes": size_bytes, "output_path": output_path}


COMMAND_HANDLERS["nbd/pull-flatten"] = _handle_nbd_pull_flatten


def _handle_upload_and_cache(job, params):
    """Upload a local file to S3 and copy to cache path."""
    local_path = _validate_path(params["local_path"])
    s3_url = params["s3_url"]
    cache_path = _validate_path(params["cache_path"])
    aws_access_key = params.get("aws_access_key_id", "")
    aws_secret_key = params.get("aws_secret_access_key", "")
    aws_region = params.get("aws_region", "us-east-1")
    aws_endpoint_url = params.get("aws_endpoint_url", "")

    if not os.path.exists(local_path):
        raise RuntimeError(f"File not found: {local_path}")

    file_size = os.path.getsize(local_path)

    cache_error = [None]

    def _do_cache():
        try:
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            shutil.copy(local_path, cache_path)
        except Exception as e:
            cache_error[0] = e

    cache_thread = threading.Thread(target=_do_cache, daemon=True)
    cache_thread.start()

    _job_log(job, f"Uploading {round(file_size / (1024**3), 1)} GB to S3...")
    _s3_upload_with_cache(
        job,
        local_path,
        file_size,
        s3_url,
        cache_path,
        aws_access_key,
        aws_secret_key,
        aws_region,
        aws_endpoint_url,
    )

    _job_log(job, "Upload complete, waiting for cache...")
    while cache_thread.is_alive():
        try:
            if os.path.exists(cache_path):
                cached = os.path.getsize(cache_path)
                cached_gb = round(cached / (1024**3), 1)
                total_gb = round(file_size / (1024**3), 1)
                cache_pct = (
                    min(100, int(cached * 100 / file_size)) if file_size > 0 else 0
                )
                _job_log(job, f"Caching: {cached_gb} of {total_gb} GB ({cache_pct}%)")
        except OSError:
            pass
        cache_thread.join(timeout=5)

    if cache_error[0]:
        _job_log(job, f"Cache copy failed: {cache_error[0]}")

    try:
        os.unlink(local_path)
    except OSError:
        pass

    return {"size_bytes": file_size, "cached": cache_error[0] is None}


COMMAND_HANDLERS["patterns/upload-and-cache"] = _handle_upload_and_cache


# ── Container handlers ──


def _handle_container_pull(job, params):
    image = params["image"]
    registry = params.get("registry")
    username = params.get("username")
    password = params.get("password")

    # Login if credentials provided
    if registry and username and password:
        _job_log(job, f"Logging in to {registry}...")
        _run_cmd(
            job,
            ["podman", "login", registry, "-u", username, "-p", password],
            timeout=30,
        )

    _job_log(job, f"Pulling {image}...")
    _run_cmd(job, ["podman", "pull", image], timeout=600)
    return {"image": image, "status": "pulled"}


COMMAND_HANDLERS["containers/pull"] = _handle_container_pull


def _mount_container_volumes(job, volumes):
    """Format (if needed) and loop-mount raw disk volumes for a container."""
    mount_dirs = []
    for vol in volumes:
        disk_path = _validate_path(vol["disk_path"])
        mount_dir = _validate_path(vol["mount_dir"])
        os.makedirs(mount_dir, exist_ok=True)

        # Format if not already formatted (only works on raw disk images)
        try:
            blkid = subprocess.run(
                ["blkid", disk_path], capture_output=True, text=True, timeout=5
            )
            if blkid.returncode != 0:
                # Verify it's a raw image before formatting
                file_check = subprocess.run(
                    ["file", disk_path], capture_output=True, text=True, timeout=5
                )
                if "QEMU" in file_check.stdout:
                    raise RuntimeError(
                        f"Disk {os.path.basename(disk_path)} is qcow2 — container volumes must be raw format"
                    )
                _job_log(job, f"Formatting {os.path.basename(disk_path)} as ext4...")
                _run_cmd(job, ["mkfs.ext4", "-q", "-F", disk_path], timeout=30)
        except subprocess.TimeoutExpired:
            pass

        _job_log(job, f"Mounting {os.path.basename(disk_path)} at {mount_dir}")
        _run_cmd(job, ["mount", "-o", "loop", disk_path, mount_dir], timeout=10)
        mount_dirs.append(mount_dir)
    return mount_dirs


def _parse_command_override(command):
    """Parse a container command override into a list of argv tokens.

    Accepts a plain shell-style string (`--foo bar`), a pasted JSON array
    string (`["--foo", "bar"]` — the Kubernetes/Docker `command:` style users
    sometimes paste in by habit), or an already-parsed list. Returns None for
    empty input so callers can treat it as "no override".
    """
    if not command:
        return None
    if isinstance(command, list):
        return [str(c) for c in command] or None
    command = command.strip()
    if command.startswith("["):
        try:
            parsed = json.loads(command)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, list):
            return [str(c) for c in parsed] or None
    import shlex

    return shlex.split(command) or None


def _build_container_cmd(
    name,
    image,
    cpus,
    memory_mb,
    env_vars,
    ports,
    networks,
    volumes,
    command,
    restart_policy,
    privileged,
):
    """Build the podman create command list for a container."""
    cmd = ["podman", "create", "--name", name]
    cmd.extend(["--cpus", str(cpus)])
    cmd.extend(["--memory", f"{memory_mb}m"])
    cmd.extend(["--restart", restart_policy])

    if privileged:
        cmd.append("--privileged")

    for ev in env_vars:
        cmd.extend(["-e", f"{ev['key']}={ev['value']}"])

    # Port mappings only work with podman-managed networks, not --network none.
    # With veth bridge attachment, services are accessed via the container's bridge IP directly.
    if not networks:
        for p in ports:
            port_str = f"{p['containerPort']}"
            if p.get("hostPort"):
                port_str = f"{p['hostPort']}:{p['containerPort']}"
            if p.get("protocol", "tcp") == "udp":
                port_str += "/udp"
            cmd.extend(["-p", port_str])

    # Network: start with --network none, attach to bridges after creation via veth
    if networks:
        cmd.extend(["--network", "none"])

    for vol in volumes:
        mount_dir = _validate_path(vol["mount_dir"])
        mount_path = vol["mount_path"]
        cmd.extend(["-v", f"{mount_dir}:{mount_path}"])

    cmd.append(image)
    tokens = _parse_command_override(command)
    if tokens:
        cmd.extend(tokens)
    return cmd


def _setup_container_veth_pair(job, name, idx, veth_host, veth_ctr, mac, netns_name, bridge):
    """Create and configure a veth pair for container networking."""
    proj_ns = "troshka-" + name.split("-")[1]

    _job_log(job, f"Attaching to {bridge} (eth{idx})")
    try:
        _run_cmd(
            job,
            [
                "ip",
                "link",
                "add",
                veth_host,
                "type",
                "veth",
                "peer",
                "name",
                veth_ctr,
            ],
            timeout=10,
        )
    except RuntimeError:
        _job_log(job, f"Veth {veth_host} already exists, reusing")

    if mac:
        _run_cmd(
            job,
            ["ip", "link", "set", veth_ctr, "address", mac],
            timeout=5,
        )

    _run_cmd(
        job,
        ["ip", "link", "set", veth_ctr, "netns", netns_name],
        timeout=10,
    )

    _run_cmd(
        job,
        ["ip", "link", "set", veth_host, "netns", proj_ns],
        timeout=10,
    )
    _run_cmd(
        job,
        [
            "ip",
            "netns",
            "exec",
            proj_ns,
            "ip",
            "link",
            "set",
            veth_host,
            "master",
            bridge,
        ],
        timeout=10,
    )
    _run_cmd(
        job,
        ["ip", "netns", "exec", proj_ns, "ip", "link", "set", veth_host, "up"],
        timeout=5,
    )

    _run_cmd(
        job,
        [
            "ip",
            "netns",
            "exec",
            netns_name,
            "ip",
            "link",
            "set",
            veth_ctr,
            "name",
            f"eth{idx}",
        ],
        timeout=5,
    )
    _run_cmd(
        job,
        [
            "ip",
            "netns",
            "exec",
            netns_name,
            "ip",
            "link",
            "set",
            f"eth{idx}",
            "up",
        ],
        timeout=5,
    )


def _configure_container_interface_ip(job, idx, ip, cidr, netns_name):
    """Configure IP address and default route for container interface."""
    prefix = cidr.split("/")[1] if "/" in cidr else "24"
    _run_cmd(
        job,
        [
            "ip",
            "netns",
            "exec",
            netns_name,
            "ip",
            "addr",
            "add",
            f"{ip}/{prefix}",
            "dev",
            f"eth{idx}",
        ],
        timeout=5,
    )
    if idx == 0:
        gw = ip.rsplit(".", 1)[0] + ".1"
        try:
            _run_cmd(
                job,
                [
                    "ip",
                    "netns",
                    "exec",
                    netns_name,
                    "ip",
                    "route",
                    "add",
                    "default",
                    "via",
                    gw,
                ],
                timeout=5,
            )
        except RuntimeError:
            pass


def _attach_container_to_bridges(job, name, networks):
    """Attach a container to VXLAN bridges via veth pairs."""
    _run_cmd(job, ["podman", "start", name], timeout=30)

    inspect = subprocess.run(
        ["podman", "inspect", "--format", _STATE_PID_FMT, name],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if inspect.returncode != 0 or not inspect.stdout.strip():
        raise RuntimeError(f"Failed to get container PID: {inspect.stderr}")
    ctr_pid = inspect.stdout.strip()

    netns_path = f"/var/run/netns/ctr-{name[-8:]}"
    os.makedirs("/var/run/netns", exist_ok=True)
    try:
        os.symlink(f"/proc/{ctr_pid}/ns/net", netns_path)
    except FileExistsError:
        os.remove(netns_path)
        os.symlink(f"/proc/{ctr_pid}/ns/net", netns_path)
    netns_name = f"ctr-{name[-8:]}"

    for idx, net in enumerate(networks):
        bridge = _validate_bridge_name(net["bridge"])
        mac = net.get("mac", "")
        ip = net.get("ip", "")
        cidr = net.get("cidr", "10.0.0.0/24")

        veth_host = f"vc{name[-8:]}{idx}h"[:15]
        veth_ctr = f"vc{name[-8:]}{idx}n"[:15]

        _setup_container_veth_pair(job, name, idx, veth_host, veth_ctr, mac, netns_name, bridge)

        if ip:
            _configure_container_interface_ip(job, idx, ip, cidr, netns_name)

    try:
        os.remove(netns_path)
    except FileNotFoundError:
        pass


def _handle_container_create(job, params):
    name = params["container_name"]
    image = params["image"]
    cpus = params.get("cpus", 1)
    memory_mb = params.get("memory_mb", 512)
    env_vars = params.get("env_vars", [])
    ports = params.get("ports", [])
    networks = params.get("networks", [])
    volumes = params.get("volumes", [])
    command = params.get("command")
    restart_policy = params.get("restart_policy", "always")
    privileged = params.get("privileged", False)

    _mount_container_volumes(job, volumes)

    cmd = _build_container_cmd(
        name,
        image,
        cpus,
        memory_mb,
        env_vars,
        ports,
        networks,
        volumes,
        command,
        restart_policy,
        privileged,
    )

    _job_log(job, f"Creating container {name}...")
    _run_cmd(job, cmd, timeout=60)

    if networks:
        _attach_container_to_bridges(job, name, networks)

    return {"container_name": name, "status": "created"}


COMMAND_HANDLERS["containers/create"] = _handle_container_create


def _handle_container_start(job, params):
    name = params["container_name"]
    _job_log(job, f"Starting container {name}...")
    _run_cmd(job, ["podman", "start", name], timeout=30)
    return {"container_name": name, "status": "started"}


COMMAND_HANDLERS["containers/start"] = _handle_container_start


def _handle_container_stop(job, params):
    name = params["container_name"]
    timeout = params.get("timeout", 10)
    _job_log(job, f"Stopping container {name}...")
    _run_cmd(job, ["podman", "stop", "-t", str(timeout), name], timeout=timeout + 10)
    return {"container_name": name, "status": "stopped"}


COMMAND_HANDLERS["containers/stop"] = _handle_container_stop


def _handle_container_destroy(job, params):
    name = params["container_name"]
    _ = params.get("project_id", "")
    volumes = params.get("volumes", [])

    # Stop container (ignore errors if already stopped)
    _job_log(job, f"Stopping container {name}...")
    try:
        _run_cmd(job, ["podman", "stop", "-t", "5", name], timeout=15)
    except RuntimeError:
        pass

    # Remove container
    _job_log(job, f"Removing container {name}...")
    try:
        _run_cmd(job, ["podman", "rm", "-f", name], timeout=15)
    except RuntimeError:
        pass

    # Unmount loop devices
    for vol in volumes:
        mount_dir = vol.get("mount_dir", "")
        if mount_dir and os.path.ismount(mount_dir):
            _job_log(job, f"Unmounting {mount_dir}")
            try:
                _run_cmd(job, ["umount", mount_dir], timeout=10)
            except RuntimeError:
                _run_cmd(job, ["umount", "-l", mount_dir], timeout=10)

    return {"container_name": name, "status": "destroyed"}


COMMAND_HANDLERS["containers/destroy"] = _handle_container_destroy


def _handle_container_save_image(job, params):
    image = params["image"]
    output_path = _validate_path(params["output_path"])

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    _job_log(job, f"Saving container image {image}...")

    # podman save | gzip > output.tar.gz (streaming, no intermediate file)
    cmd = f"podman save {image} | gzip > {output_path}"
    proc = subprocess.Popen(
        cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    job["_process"] = proc
    try:
        _, stderr = proc.communicate(timeout=600)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise RuntimeError(f"Image save timed out: {image}")
    finally:
        job["_process"] = None

    if proc.returncode != 0:
        raise RuntimeError(f"podman save failed: {stderr}")

    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    _job_log(job, f"Saved {image} ({size_mb:.1f} MB)")
    return {"output_path": output_path, "size_bytes": os.path.getsize(output_path)}


COMMAND_HANDLERS["containers/save-image"] = _handle_container_save_image


def _handle_container_load_image(job, params):
    input_path = _validate_path(params["input_path"])

    if not os.path.isfile(input_path):
        raise FileNotFoundError(f"Image file not found: {input_path}")

    _job_log(job, f"Loading container image from {os.path.basename(input_path)}...")
    cmd = f"gunzip -c {input_path} | podman load"
    proc = subprocess.Popen(
        cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    job["_process"] = proc
    try:
        stdout, stderr = proc.communicate(timeout=600)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise RuntimeError(f"Image load timed out: {input_path}")
    finally:
        job["_process"] = None

    if proc.returncode != 0:
        raise RuntimeError(f"podman load failed: {stderr}")

    if stdout:
        _job_log(job, stdout.strip())
    return {"input_path": input_path, "status": "loaded"}


COMMAND_HANDLERS["containers/load-image"] = _handle_container_load_image


def _handle_container_logs(job, params):
    name = params["container_name"]
    tail = params.get("tail", 500)

    _job_log(job, f"Fetching logs for {name}...")
    result = subprocess.run(
        ["podman", "logs", "--tail", str(tail), name],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to get logs: {result.stderr}")

    return {"logs": result.stdout, "container_name": name}


COMMAND_HANDLERS["containers/logs"] = _handle_container_logs


def _handle_container_exec(job, params):
    name = params["container_name"]
    command = params.get("command", ["/bin/sh"])

    _job_log(job, f"Executing command in {name}...")
    cmd = ["podman", "exec", name] + command
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Exec failed: {result.stderr}")

    return {"stdout": result.stdout, "stderr": result.stderr, "container_name": name}


COMMAND_HANDLERS["containers/exec"] = _handle_container_exec


def _setup_pod_veth_pair(job, _full_pod_name, idx, veth_host, veth_ctr, mac, netns_name, proj_ns, bridge):
    """Create and configure a veth pair for pod networking."""
    _run_cmd(
        job,
        [
            "ip",
            "link",
            "add",
            veth_host,
            "type",
            "veth",
            "peer",
            "name",
            veth_ctr,
        ],
    )

    if mac:
        _run_cmd(job, ["ip", "link", "set", veth_ctr, "address", mac])

    _run_cmd(job, ["ip", "link", "set", veth_ctr, "netns", netns_name])

    _run_cmd(job, ["ip", "link", "set", veth_host, "netns", proj_ns])
    _run_cmd(
        job,
        [
            "ip",
            "netns",
            "exec",
            proj_ns,
            "ip",
            "link",
            "set",
            veth_host,
            "master",
            bridge,
        ],
    )
    _run_cmd(
        job,
        ["ip", "netns", "exec", proj_ns, "ip", "link", "set", veth_host, "up"],
    )

    _run_cmd(
        job,
        [
            "ip",
            "netns",
            "exec",
            netns_name,
            "ip",
            "link",
            "set",
            veth_ctr,
            "name",
            f"eth{idx}",
        ],
    )
    _run_cmd(
        job,
        [
            "ip",
            "netns",
            "exec",
            netns_name,
            "ip",
            "link",
            "set",
            f"eth{idx}",
            "up",
        ],
    )


def _configure_pod_interface_ip(job, idx, ip_addr, cidr, netns_name):
    """Configure IP address and default route for pod interface."""
    prefix = cidr.split("/")[1] if "/" in cidr else "24"
    _run_cmd(
        job,
        [
            "ip",
            "netns",
            "exec",
            netns_name,
            "ip",
            "addr",
            "add",
            f"{ip_addr}/{prefix}",
            "dev",
            f"eth{idx}",
        ],
    )

    if idx == 0:
        parts = ip_addr.split(".")
        gw = f"{parts[0]}.{parts[1]}.{parts[2]}.1"
        _run_cmd(
            job,
            [
                "ip",
                "netns",
                "exec",
                netns_name,
                "ip",
                "route",
                "add",
                "default",
                "via",
                gw,
            ],
            check=False,
        )


def _attach_pod_to_bridges(job, full_pod_name, infra_pid, networks, project_id):
    """Attach a pod's infra container to VXLAN bridges via veth pairs."""
    netns_name = f"ctr-{full_pod_name[-8:]}"
    os.makedirs("/var/run/netns", exist_ok=True)
    ns_path = f"/var/run/netns/{netns_name}"
    proc_ns = f"/proc/{infra_pid}/ns/net"
    if os.path.exists(ns_path):
        os.unlink(ns_path)
    os.symlink(proc_ns, ns_path)

    proj_ns = f"troshka-{project_id[:8]}"
    for idx, net in enumerate(networks):
        bridge = _validate_bridge_name(net["bridge"])
        mac = net.get("mac", "")
        ip_addr = net.get("ip", "")
        cidr = net.get("cidr", "")

        veth_host = f"vp{full_pod_name[-8:]}{idx}h"[:15]
        veth_ctr = f"vp{full_pod_name[-8:]}{idx}n"[:15]

        _setup_pod_veth_pair(job, full_pod_name, idx, veth_host, veth_ctr, mac, netns_name, proj_ns, bridge)

        if ip_addr and cidr:
            _configure_pod_interface_ip(job, idx, ip_addr, cidr, netns_name)


def _create_init_container(job, full_pod_name, ic):
    """Create a single init container in a pod."""
    ic_name = f"{full_pod_name}-init-{ic['name']}"
    cmd = ["podman", "create", "--pod", full_pod_name, "--name", ic_name]
    for k, v in (ic.get("env") or {}).items():
        cmd.extend(["-e", f"{k}={v}"])
    for vol in ic.get("mounts") or []:
        cmd.extend(["-v", vol])
    tokens = _parse_command_override(ic.get("command"))
    if tokens:
        cmd.extend(["--entrypoint", json.dumps(tokens)])
    cmd.append(ic["image"])
    _run_cmd(job, cmd)
    _job_log(job, f"Init container created: {ic['name']}")


def _create_main_container(job, full_pod_name, ctr, restart_policy, privileged):
    """Create a single main container in a pod."""
    ctr_name = f"{full_pod_name}-{ctr['name']}"
    cmd = [
        "podman",
        "create",
        "--pod",
        full_pod_name,
        "--name",
        ctr_name,
        "--restart",
        restart_policy,
    ]
    if ctr.get("cpus"):
        cmd.extend(["--cpus", str(ctr["cpus"])])
    if ctr.get("memory"):
        cmd.extend(["--memory", f"{ctr['memory']}m"])
    for k, v in (ctr.get("env") or {}).items():
        cmd.extend(["-e", f"{k}={v}"])
    for vol in ctr.get("mounts") or []:
        cmd.extend(["-v", vol])
    if privileged:
        cmd.append("--privileged")
    tokens = _parse_command_override(ctr.get("command"))
    if tokens:
        cmd.extend(["--entrypoint", json.dumps(tokens)])
    cmd.append(ctr["image"])
    _run_cmd(job, cmd)
    _job_log(job, f"Main container created: {ctr['name']}")


def _create_pod_containers(
    job, full_pod_name, init_containers, containers, restart_policy, privileged
):
    """Create init containers and main containers inside a pod."""
    for ic in init_containers:
        _create_init_container(job, full_pod_name, ic)

    for ctr in containers:
        _create_main_container(job, full_pod_name, ctr, restart_policy, privileged)


def _handle_pod_create(job, params):
    pod_name = params["pod_name"]
    project_id = params.get("project_id", "")
    networks = params.get("networks", [])
    init_containers = params.get("init_containers", [])
    containers = params.get("containers", [])
    restart_policy = params.get("restart_policy", "always")
    privileged = params.get("privileged", False)

    full_pod_name = f"troshka-{project_id[:8]}-{pod_name}"

    cmd = [
        "podman",
        "pod",
        "create",
        "--name",
        full_pod_name,
        "--network",
        "none",
        "--infra-name",
        f"{full_pod_name}-infra",
    ]
    _run_cmd(job, cmd)
    _job_log(job, f"Pod created: {full_pod_name}")

    infra_name = f"{full_pod_name}-infra"
    out = _run_cmd(job, ["podman", "inspect", "--format", _STATE_PID_FMT, infra_name])
    infra_pid = int(out.strip())

    if infra_pid == 0:
        _run_cmd(job, ["podman", "start", infra_name])
        out = _run_cmd(
            job, ["podman", "inspect", "--format", _STATE_PID_FMT, infra_name]
        )
        infra_pid = int(out.strip())

    if networks:
        _attach_pod_to_bridges(job, full_pod_name, infra_pid, networks, project_id)

    _create_pod_containers(
        job, full_pod_name, init_containers, containers, restart_policy, privileged
    )

    return {"pod_name": full_pod_name, "status": "created"}


COMMAND_HANDLERS["pods/create"] = _handle_pod_create


def _handle_pod_start(job, params):
    pod_name = params["pod_name"]

    out = _run_cmd(
        job,
        [
            "podman",
            "ps",
            "-a",
            "--filter",
            f"name={pod_name}-init-",
            "--format",
            _PODMAN_NAMES_FMT,
        ],
        check=False,
    )
    init_names = [n.strip() for n in out.strip().split("\n") if n.strip()]

    for ic_name in sorted(init_names):
        _job_log(job, f"Starting init container: {ic_name}")
        _run_cmd(job, ["podman", "start", ic_name])
        out = _run_cmd(job, ["podman", "wait", ic_name])
        exit_code = int(out.strip())
        if exit_code != 0:
            logs = _run_cmd(
                job, ["podman", "logs", "--tail", "50", ic_name], check=False
            )
            raise RuntimeError(
                f"Init container {ic_name} failed with exit code {exit_code}: {logs}"
            )
        _job_log(job, f"Init container {ic_name} completed (exit 0)")

    _run_cmd(job, ["podman", "pod", "start", pod_name])
    _job_log(job, f"Pod started: {pod_name}")
    return {"pod_name": pod_name, "status": "started"}


COMMAND_HANDLERS["pods/start"] = _handle_pod_start


def _handle_pod_destroy(job, params):
    pod_name = params["pod_name"]
    _ = params.get("project_id", "")
    volumes = params.get("volumes", [])

    netns_name = f"ctr-{pod_name[-8:]}"
    ns_path = f"/var/run/netns/{netns_name}"
    if os.path.exists(ns_path):
        os.unlink(ns_path)

    _run_cmd(job, ["podman", "pod", "rm", "-f", pod_name], check=False)
    _job_log(job, f"Pod destroyed: {pod_name}")

    for vol in volumes:
        mount_dir = vol.get("mount_dir", "")
        if mount_dir and os.path.ismount(mount_dir):
            _run_cmd(job, ["umount", mount_dir], check=False)

    return {"pod_name": pod_name, "status": "destroyed"}


COMMAND_HANDLERS["pods/destroy"] = _handle_pod_destroy


def _get_container_states():
    """Query podman for all troshka-* container states."""
    containers = {}
    result = subprocess.run(
        [
            "podman",
            "ps",
            "-a",
            "--filter",
            _TROSHKA_FILTER,
            "--format",
            "{{.Names}} {{.State}}",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    state_map = {
        "running": "running",
        "created": "created",
        "exited": "stopped",
        "paused": "paused",
        "dead": "stopped",
    }
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        parts = line.strip().split(None, 1)
        if len(parts) == 2:
            name, state = parts
            containers[name] = {"state": state_map.get(state.lower(), state.lower())}
    return containers


def _get_container_namespace_ips(name):
    """Get IP addresses for a running container via its network namespace."""
    try:
        pid_result = subprocess.run(
            ["podman", "inspect", "--format", _STATE_PID_FMT, name],
            capture_output=True,
            text=True,
            timeout=5,
        )
        pid = pid_result.stdout.strip()
        if not pid or pid == "0":
            return []

        ip_result = subprocess.run(
            ["nsenter", "-t", pid, "-n", "ip", "-4", "-o", "addr", "show"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        ips = []
        for ip_line in ip_result.stdout.strip().split("\n"):
            if "scope global" in ip_line:
                addr_part = (
                    ip_line.split(_INET_PREFIX)[1].split("/")[0]
                    if _INET_PREFIX in ip_line
                    else ""
                )
                if addr_part:
                    ips.append(addr_part)
        return ips
    except Exception:
        return []


def _enrich_container_ips(containers):
    """Query IPs for running containers via their network namespace."""
    for name, info in containers.items():
        if info["state"] != "running":
            continue
        ips = _get_container_namespace_ips(name)
        if ips:
            info["ips"] = ips


def _get_pod_states():
    """Query podman for all troshka-* pod states."""
    pods = {}
    pod_out = subprocess.run(
        [
            "podman",
            "pod",
            "ps",
            "--filter",
            _TROSHKA_FILTER,
            "--format",
            "{{.Name}} {{.Status}}",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    for line in pod_out.stdout.strip().split("\n"):
        if not line.strip():
            continue
        parts = line.strip().split(None, 1)
        if len(parts) < 2:
            continue
        name, status = parts[0], parts[1].lower()
        if "running" in status:
            state = "running"
        elif "degraded" in status:
            state = "running"
        else:
            state = "stopped"
        pods[name] = {"state": state}
    return pods


@route("GET", "/containers/states")
def handle_container_states(handler, params):
    """Return all troshka-* container states in one call, including IPs."""
    containers = {}
    try:
        containers = _get_container_states()
        _enrich_container_ips(containers)
    except Exception as e:
        logger.warning("Failed to list container states: %s", e)

    pods = {}
    try:
        pods = _get_pod_states()
    except Exception:
        pass

    handler._send_json(
        200, {"containers": containers, "pods": pods, "source": "podman"}
    )


if __name__ == "__main__":
    main()
