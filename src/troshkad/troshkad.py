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
import threading
import time
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
import socketserver


class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True

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
_jobs = {}       # job_id -> Job dict
_jobs_lock = threading.Lock()
_draining = False

_vm_state_cache = {}
_vm_state_cache_lock = threading.Lock()
_vm_events = []
_vm_events_lock = threading.Lock()
_libvirt_events_available = False


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
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "_start_time": time.time(),
        "completed_at": None,
        "_process": None,
    }
    with _jobs_lock:
        _jobs[job_id] = job
    return job


def _complete_job(job, status, result=None):
    job["status"] = status
    job["result"] = result or {}
    job["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


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
                    t = time.mktime(time.strptime(job["completed_at"], "%Y-%m-%dT%H:%M:%SZ"))
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

def _get_capacity():
    """Read host capacity from system — best effort."""
    capacity = {
        "vcpus_total": 0, "vcpus_used": 0,
        "ram_total_mb": 0, "ram_used_mb": 0,
        "storage_total_gb": 0, "storage_used_gb": 0,
    }
    try:
        capacity["vcpus_total"] = os.cpu_count() or 0
    except Exception:
        pass
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    capacity["ram_total_mb"] = int(line.split()[1]) // 1024
                    break
    except Exception:
        pass
    try:
        stat = shutil.disk_usage("/var/lib/troshka")
        capacity["storage_total_gb"] = stat.total // (1024**3)
        capacity["storage_used_gb"] = stat.used // (1024**3)
    except Exception:
        pass
    try:
        result = subprocess.run(
            ["virsh", "list", "--all", "--name"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            domains = [d.strip() for d in result.stdout.strip().split("\n") if d.strip()]
            vcpus_used = 0
            ram_used = 0
            for domain in domains:
                info = subprocess.run(
                    ["virsh", "dominfo", domain],
                    capture_output=True, text=True, timeout=5,
                )
                if info.returncode == 0:
                    for line in info.stdout.split("\n"):
                        if line.startswith("CPU(s):"):
                            vcpus_used += int(line.split(":")[1].strip())
                        elif line.startswith("Max memory:"):
                            ram_used += int(line.split(":")[1].strip().split()[0]) // 1024
            capacity["vcpus_used"] = vcpus_used
            capacity["ram_used_mb"] = ram_used
    except Exception:
        pass
    return capacity


_PSEUDO_FSTYPES = frozenset({
    "proc", "sysfs", "devtmpfs", "tmpfs", "cgroup", "cgroup2", "overlay",
    "devpts", "mqueue", "hugetlbfs", "debugfs", "tracefs", "securityfs",
    "pstore", "bpf", "fusectl", "configfs", "autofs", "nfsd",
    "rpc_pipefs", "binfmt_misc", "efivarfs", "nsfs", "fuse.lxcfs",
})


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
                seen_devices.add(device)
                try:
                    stat = shutil.disk_usage(mount)
                    partitions.append({
                        "mount": mount,
                        "total_bytes": stat.total,
                        "used_bytes": stat.used,
                        "free_bytes": stat.free,
                        "used_pct": round((stat.used / stat.total) * 100, 1) if stat.total > 0 else 0,
                        "device": device,
                        "fstype": fstype,
                    })
                except (OSError, PermissionError):
                    pass
    except (OSError, FileNotFoundError):
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


def _match_route(method, path):
    """Match a request to a route, supporting /jobs/{job_id} style paths."""
    # Special handling for /commands/* paths
    if path.startswith("/commands/") and method == "POST":
        handler = ROUTES.get(("POST", "/commands/{command_path}"))
        if handler:
            return handler, {"command_path": path[len("/commands/"):]}

    handler = ROUTES.get((method, path))
    if handler:
        return handler, {}
    # Try path parameter patterns
    parts = path.strip("/").split("/")
    for (m, pattern), handler in ROUTES.items():
        if m != method:
            continue
        pat_parts = pattern.strip("/").split("/")
        if len(parts) != len(pat_parts):
            continue
        params = {}
        match = True
        for p, pp in zip(parts, pat_parts):
            if pp.startswith("{") and pp.endswith("}"):
                params[pp[1:-1]] = p
            elif p != pp:
                match = False
                break
        if match:
            return handler, params
    return None, {}


# ── Request handler ──

class TroshkadHandler(BaseHTTPRequestHandler):
    """HTTPS request handler with auth and JSON routing."""

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

    def _handle(self, method):
        if not self._check_auth():
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


# ── Route handlers ──

@route("GET", "/health")
def handle_health(handler, params):
    handler._send_json(200, {
        "status": "draining" if _draining else "ok",
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
    })


@route("GET", "/jobs/{job_id}")
def handle_get_job(handler, params):
    job = _get_job(params["job_id"])
    if not job:
        handler._send_json(404, {"error": "job not found"})
        return
    handler._send_json(200, {
        "job_id": job["job_id"],
        "command": job["command"],
        "status": job["status"],
        "output": job["output"],
        "result": job["result"],
        "started_at": job["started_at"],
        "completed_at": job["completed_at"],
    })


@route("POST", "/commands/{command_path}")
def handle_dispatch_command(handler, params):
    """Dispatch a command job and return job_id + status."""
    # Cancel any pending drain — new work means we shouldn't restart
    if _draining:
        _drain_cancel.set()
    command_path = params["command_path"]
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
_BUS_TYPES = {"virtio", "scsi", "sata", "ide", "usb"}
_NET_MODELS = {"virtio", "e1000", "rtl8139"}


def _validate_domain_name(name):
    if not _DOMAIN_RE.match(name):
        raise ValueError(f"Invalid domain name: {name}")
    return name


def _validate_path(path):
    normalized = os.path.normpath(path)
    allowed_prefixes = ["/var/lib/troshka/"]
    mode = _config.get("storage_mode", "local")
    if mode == "shared":
        shared = _config.get("shared_mount", "/var/lib/troshka/shared")
        local = _config.get("local_mount", "/var/lib/troshka/local")
        allowed_prefixes.extend([shared + "/", local + "/", "/var/lib/troshka/seeds/"])
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
        shared = _config.get("shared_mount", "/var/lib/troshka/shared")
        local = _config.get("local_mount", "/var/lib/troshka/local")
        shared_categories = {"vms", "images", "cache/snapshots"}
        local_categories = {"pxe", "bmc", "tmp", "cache/patterns"}
        if category in shared_categories:
            return os.path.join(shared, category)
        elif category in local_categories:
            return os.path.join(local, category)
        elif category == "seeds":
            return "/var/lib/troshka/seeds"
        else:
            return os.path.join(shared, category)
    else:
        base = "/var/lib/troshka"
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


def _run_cmd(job, cmd, timeout=600):
    """Run a subprocess command, appending output to job. Stores process handle in job for drain."""
    _job_log(job, f"$ {' '.join(cmd)}")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    job["_process"] = proc
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise RuntimeError(f"Command timed out after {timeout}s: {' '.join(cmd)}")
    finally:
        job["_process"] = None
    if stdout:
        for line in stdout.strip().split("\n"):
            _job_log(job, line)
    if stderr:
        for line in stderr.strip().split("\n"):
            _job_log(job, line)
    if proc.returncode != 0:
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

    cmd = [
        "virt-install",
        "--name", domain,
        "--vcpus", str(vcpus),
        "--memory", str(ram_mb),
        "--os-variant", "generic",
        "--noautoconsole",
        "--noreboot",
        "--check", "mac_in_use=off",
    ]

    # Build --boot flag: firmware + boot device order
    boot_parts = []
    if firmware == "uefi":
        boot_parts.append("uefi")
        if secure_boot:
            boot_parts.append("firmware.feature0.name=secure-boot")
            boot_parts.append("firmware.feature0.enabled=yes")
    if boot_devs:
        boot_parts.extend(boot_devs)
    else:
        boot_parts.append("hd")
    boot_parts.append("menu=on")
    cmd.extend(["--boot", ",".join(boot_parts)])
    cmd.extend(["--install", "no_install=yes"])
    for disk in disks:
        path = _validate_path(disk["path"])
        bus = _validate_bus(disk.get("bus", "virtio"))
        device = disk.get("device", "disk")
        # Hard-link shared cached files into VM dir (preserves permissions, survives --remove-all-storage)
        link_from = disk.get("symlink_from")
        if link_from:
            link_from = _validate_path(link_from)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            try:
                if link_from.endswith(".iso"):
                    os.link(link_from, path)
                    _job_log(job, f"Linked {os.path.basename(path)}")
                else:
                    src_size = os.path.getsize(link_from)
                    _job_log(job, f"Copying {os.path.basename(link_from)} ({round(src_size / (1024**3), 1)} GB)...")
                    shutil.copy2(link_from, path)
                    _job_log(job, f"Copied {os.path.basename(path)}")
                _chown_qemu(path)
            except FileExistsError:
                pass
        disk_cache = params.get("disk_cache")
        disk_arg = f"path={path},bus={bus}"
        if disk_cache:
            disk_arg += f",cache={disk_cache}"
            if disk_cache == "none":
                disk_arg += ",io=native"
        if device == "cdrom":
            disk_arg += ",device=cdrom"
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
    if input_model == "virtio":
        cmd.extend(["--input", "type=keyboard,bus=virtio"])
        cmd.extend(["--input", "type=tablet,bus=virtio"])
    cmd.extend(["--channel", "unix,target.type=virtio,target.name=org.qemu.guest_agent.0"])
    _run_cmd(job, cmd, timeout=600)

    return {"domain": domain, "status": "created"}

COMMAND_HANDLERS["vms/create"] = _handle_vm_create


def _delete_vm_disks(job, domain):
    """Delete disk files for a domain before undefining it.
    Files are owned by qemu:qemu, so delete as qemu user to avoid NFS root_squash issues."""
    try:
        result = subprocess.run(
            ["virsh", "domblklist", domain, "--details"],
            capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.strip().split("\n"):
            parts = line.split()
            if len(parts) >= 4 and parts[1] == "disk" and parts[3].startswith("/"):
                path = parts[3]
                try:
                    subprocess.run(["sudo", "-u", "qemu", "rm", "-f", "--", path], timeout=5, check=True)
                    _job_log(job,f"Deleted disk: {path}")
                except FileNotFoundError:
                    pass
                except Exception:
                    try:
                        os.remove(path)
                        _job_log(job,f"Deleted disk (root): {path}")
                    except Exception:
                        _job_log(job,f"Warning: could not delete {path}")
    except Exception:
        _job_log(job,"Warning: could not list domain disks, undefine may leave orphan files")


def _handle_vm_destroy(job, params):
    domain = _validate_domain_name(params["domain_name"])
    # Destroy (force stop) — may fail if already stopped, that's OK
    try:
        _run_cmd(job, ["virsh", "destroy", domain], timeout=30)
    except RuntimeError:
        _job_log(job,"Domain may already be stopped, continuing with undefine")
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
        capture_output=True, text=True, timeout=10,
    )
    if xml_result.returncode == 0:
        for bridge in _re.findall(r"source bridge='([^']+)'", xml_result.stdout):
            check = subprocess.run(["ip", "link", "show", bridge], capture_output=True, timeout=5)
            if check.returncode != 0:
                subprocess.run(["ip", "link", "add", bridge, "type", "bridge"], capture_output=True, timeout=5)
                subprocess.run(["ip", "link", "set", bridge, "type", "bridge",
                                "forward_delay", "99", "ageing_time", "0"], capture_output=True, timeout=5)
                subprocess.run(["ip", "link", "set", bridge, "up"], capture_output=True, timeout=5)
                _job_log(job,f"Created missing dummy bridge {bridge}")
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
        result = subprocess.run(["virsh", "domstate", domain], capture_output=True, text=True, timeout=5)
        if result.returncode != 0 or result.stdout.strip() in ("shut off", ""):
            return {"domain": domain, "status": "stopped", "method": "shutdown"}
    # Force destroy if graceful shutdown didn't work
    _job_log(job,f"Graceful shutdown timed out after {grace}s, forcing destroy")
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
        capture_output=True, text=True, timeout=10,
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
            capture_output=True, text=True, timeout=5,
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

COMMAND_HANDLERS["vms/state"] = _handle_vm_state


def _handle_vm_list(job, params):
    """List all troshka domains with their states."""
    result = subprocess.run(
        ["virsh", "list", "--all", "--name"],
        capture_output=True, text=True, timeout=10,
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
            capture_output=True, text=True, timeout=5,
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
        capture_output=True, text=True, timeout=10,
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


def _handle_vm_config(job, params):
    """Get VM config from inactive XML — structured dict."""
    import xml.etree.ElementTree as ET

    domain = _validate_domain_name(params["domain_name"])
    result = subprocess.run(
        ["virsh", "dumpxml", "--inactive", domain],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to get XML for {domain}: {result.stderr}")

    root = ET.fromstring(result.stdout)

    boot_devs = [b.get("dev") for b in root.findall(".//os/boot")]
    vcpus = int(root.findtext("vcpu", "0"))
    mem_elem = root.find("memory")
    mem_kib = int(mem_elem.text) if mem_elem is not None else 0
    if mem_elem is not None and mem_elem.get("unit", "KiB") == "KiB":
        ram_mb = mem_kib // 1024
    else:
        ram_mb = mem_kib

    nics = []
    for iface in root.findall(".//interface"):
        source = iface.find("source")
        mac = iface.find("mac")
        nics.append({
            "bridge": source.get("bridge", "") if source is not None else "",
            "mac": mac.get("address", "") if mac is not None else "",
        })

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

    # Check if domain is running
    state_result = subprocess.run(
        ["virsh", "domstate", domain],
        capture_output=True, text=True, timeout=10,
    )
    was_active = state_result.returncode == 0 and "running" in state_result.stdout.lower()

    if restart and was_active:
        _run_cmd(job, ["virsh", "destroy", domain], timeout=30)

    # Get inactive XML
    result = subprocess.run(
        ["virsh", "dumpxml", "--inactive", domain],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to get XML for {domain}: {result.stderr}")

    root = ET.fromstring(result.stdout)

    # ── Boot devices ──
    if boot_devs is not None:
        os_elem = root.find("os")
        for boot in os_elem.findall("boot"):
            os_elem.remove(boot)
        type_elem = os_elem.find("type")
        insert_idx = list(os_elem).index(type_elem) + 1
        for i, dev in enumerate(boot_devs):
            boot_elem = ET.Element("boot")
            boot_elem.set("dev", dev)
            os_elem.insert(insert_idx + i, boot_elem)

    # ── vCPUs ──
    if vcpus is not None:
        vcpu_elem = root.find("vcpu")
        vcpu_elem.text = str(vcpus)
        vcpu_elem.set("placement", "static")

    # ── RAM ──
    if ram_mb is not None:
        ram_kib = ram_mb * 1024
        mem = root.find("memory")
        mem.text = str(ram_kib)
        mem.set("unit", "KiB")
        cur_mem = root.find("currentMemory")
        if cur_mem is not None:
            cur_mem.text = str(ram_kib)
            cur_mem.set("unit", "KiB")

    # ── NICs ──
    if nics is not None:
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

    # ── Disks ──
    if disks is not None:
        devices = root.find("devices")
        existing_disks = devices.findall("disk") if devices is not None else []
        existing_paths = set()
        for d in existing_disks:
            source = d.find("source")
            if source is not None and source.get("file"):
                existing_paths.add(source.get("file"))

        desired_paths = {d["path"] for d in disks}

        # Remove disks no longer in topology (skip cdroms)
        for d in existing_disks:
            if d.get("device") == "cdrom":
                continue
            source = d.find("source")
            path = source.get("file") if source is not None else None
            if path and path not in desired_paths:
                devices.remove(d)
                _job_log(job,f"Removed disk {path} from {domain}")

        # Add new disks
        target_letters = "bcdefghijklmnop"
        used_targets = {d.find("target").get("dev") for d in devices.findall("disk") if d.find("target") is not None}
        for disk_info in disks:
            if disk_info["path"] in existing_paths:
                continue
            target_dev = None
            for letter in target_letters:
                dev_name = f"vd{letter}"
                if dev_name not in used_targets:
                    target_dev = dev_name
                    used_targets.add(dev_name)
                    break
            if not target_dev:
                continue

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
            target.set("bus", disk_info.get("bus", "virtio"))
            _job_log(job,f"Added disk {disk_info['path']} as {target_dev} to {domain}")

    # ── CDROMs ──
    if cdroms is not None:
        devices = root.find("devices")
        existing_cdroms = [d for d in (devices.findall("disk") if devices is not None else []) if d.get("device") == "cdrom"]
        desired_set = set(cdroms)
        existing_set = set()
        cdrom_bus = "sata"
        for cd in existing_cdroms:
            src = cd.find("source")
            existing_set.add(src.get("file", "") if src is not None else "")
            tgt = cd.find("target")
            if tgt is not None and tgt.get("bus"):
                cdrom_bus = tgt.get("bus")

        if existing_set != desired_set:
            for cd in existing_cdroms:
                devices.remove(cd)
            dev_prefix = "sd" if cdrom_bus == "sata" else "hd" if cdrom_bus == "ide" else "vd"
            target_letters_cd = "abcdefghijklmnop"
            used_targets = {d.find("target").get("dev") for d in devices.findall("disk") if d.find("target") is not None}
            for path in cdroms:
                target_dev = None
                for letter in target_letters_cd:
                    dev_name = f"{dev_prefix}{letter}"
                    if dev_name not in used_targets:
                        target_dev = dev_name
                        used_targets.add(dev_name)
                        break
                if not target_dev:
                    continue
                disk_elem = ET.SubElement(devices, "disk")
                disk_elem.set("type", "file")
                disk_elem.set("device", "cdrom")
                source = ET.SubElement(disk_elem, "source")
                source.set("file", path)
                target = ET.SubElement(disk_elem, "target")
                target.set("dev", target_dev)
                target.set("bus", cdrom_bus)
                ET.SubElement(disk_elem, "readonly")
                _job_log(job,f"Updated cdrom {path} on {domain} (bus={cdrom_bus})")

    # ── VNC ──
    if vnc_listen:
        devices = root.find("devices")
        graphics = devices.find("graphics[@type='vnc']") if devices is not None else None
        if graphics is not None:
            graphics.set("listen", vnc_listen)
            listen_elem = graphics.find("listen")
            if listen_elem is not None:
                listen_elem.set("address", vnc_listen)
        elif devices is not None:
            graphics = ET.SubElement(devices, "graphics")
            graphics.set("type", "vnc")
            graphics.set("port", "-1")
            graphics.set("autoport", "yes")
            graphics.set("listen", vnc_listen)
            listen_sub = ET.SubElement(graphics, "listen")
            listen_sub.set("type", "address")
            listen_sub.set("address", vnc_listen)

    # Write new XML via virsh define
    new_xml = ET.tostring(root, encoding="unicode")
    proc = subprocess.Popen(
        ["virsh", "define", "/dev/stdin"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    stdout, stderr = proc.communicate(input=new_xml, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(f"virsh define failed: {stderr}")
    _job_log(job,f"Redefined {domain}")

    restarted = False
    if restart and was_active:
        _run_cmd(job, ["virsh", "start", domain], timeout=60)
        restarted = True
        _job_log(job,f"Reconfigured and restarted {domain}")
    else:
        _job_log(job,f"Reconfigured {domain}")

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
        _job_log(job,f"Domain {domain} may already be stopped")

    if remove_storage:
        _delete_vm_disks(job, domain)

    _run_cmd(job, ["virsh", "undefine", domain, "--nvram"], timeout=30)
    return {"domain": domain, "status": "undefined"}

COMMAND_HANDLERS["vms/undefine"] = _handle_vm_undefine


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


def _handle_seed_create(job, params):
    path = _validate_path(params["path"])
    meta_data = params.get("meta_data", "")
    user_data = params.get("user_data", "")
    network_config = params.get("network_config", "")

    import tempfile as _tf
    with _tf.TemporaryDirectory(dir="/var/lib/troshka/tmp") as tmpdir:
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
        _run_cmd(job, [
            "xorriso", "-as", "genisoimage",
            "-output", path,
            "-volid", "cidata",
            "-joliet", "-rock",
            tmpdir + "/",
        ])
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
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)

    lock_path = dest_path + ".lock"
    lock_fd = open(lock_path, "w")
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _job_log(job,f"Another download in progress for {os.path.basename(dest_path)}, waiting...")
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            if os.path.exists(dest_path) and expected_size > 0:
                actual = os.path.getsize(dest_path)
                if actual >= expected_size - 1024:
                    _job_log(job,f"Already downloaded by another job ({actual} bytes)")
                    return {"path": dest_path, "status": "cached", "waited": True}

        if os.path.exists(dest_path) and expected_size > 0:
            actual = os.path.getsize(dest_path)
            if actual >= expected_size - 1024:
                _job_log(job,f"Already cached ({actual} bytes)")
                return {"path": dest_path, "status": "cached", "skipped": True}

        if s3_url:
            _s3_download(job, s3_url, dest_path, aws_access_key, aws_secret_key, aws_region)
        else:
            _run_cmd(job, ["curl", "-fSL", "-o", dest_path, _validate_url(url)], timeout=3600)
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
    tftp_root = params.get("tftp_root", f"/var/lib/troshka/pxe/{vni}/tftpboot")
    mount_point = f"/var/lib/troshka/pxe/{vni}/mnt"
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
    _job_log(job,f"Mounted ISO at {mount_point}")

    # Copy kernel + initrd preserving directory structure so GRUB finds them
    found = False
    for paths in _PXE_BOOT_PATHS:
        k_src = mount_point + paths["kernel"]
        i_src = mount_point + paths["initrd"]
        if os.path.isfile(k_src) and os.path.isfile(i_src):
            import shutil
            k_dest = os.path.join(tftp_root, paths["kernel"].lstrip("/"))
            i_dest = os.path.join(tftp_root, paths["initrd"].lstrip("/"))
            os.makedirs(os.path.dirname(k_dest), exist_ok=True)
            os.makedirs(os.path.dirname(i_dest), exist_ok=True)
            shutil.copy2(k_src, k_dest)
            shutil.copy2(i_src, i_dest)
            os.chmod(k_dest, 0o644)
            os.chmod(i_dest, 0o644)
            _job_log(job,f"Copied kernel to {paths['kernel']}")
            _job_log(job,f"Copied initrd to {paths['initrd']}")
            found = True
            break
    if not found:
        # List top-level dirs to help debug
        try:
            contents = os.listdir(mount_point)
            _job_log(job,f"ISO contents: {contents}")
        except OSError:
            pass
        raise RuntimeError("Could not find kernel/initrd in ISO — unsupported distro layout")

    # Copy bootloader — try UEFI first, then BIOS
    import shutil
    boot_filename = None
    efi_boot_dir = os.path.join(mount_point, "EFI", "BOOT")
    if os.path.isdir(efi_boot_dir):
        for fname in os.listdir(efi_boot_dir):
            src = os.path.join(efi_boot_dir, fname)
            if os.path.isfile(src):
                dest = os.path.join(tftp_root, fname)
                shutil.copy2(src, dest)
                os.chmod(dest, 0o644)
        _job_log(job,f"Copied EFI/BOOT/ directory ({len(os.listdir(efi_boot_dir))} files)")
        for bl_path in _UEFI_BOOTLOADER_PATHS:
            bl_name = os.path.basename(bl_path)
            if os.path.isfile(os.path.join(tftp_root, bl_name)):
                boot_filename = bl_name
                break
    if not boot_filename:
        for bl_path in _BIOS_BOOTLOADER_PATHS:
            bl_src = mount_point + bl_path
            if os.path.isfile(bl_src):
                bl_name = os.path.basename(bl_path)
                bl_dest = os.path.join(tftp_root, bl_name)
                shutil.copy2(bl_src, bl_dest)
                os.chmod(bl_dest, 0o644)
                _job_log(job,f"Copied BIOS bootloader from {bl_path}")
                boot_filename = bl_name
                break
    if not boot_filename:
        for syslinux_path in ["/usr/share/syslinux/pxelinux.0", "/usr/lib/syslinux/pxelinux.0"]:
            if os.path.exists(syslinux_path):
                shutil.copy2(syslinux_path, os.path.join(tftp_root, "pxelinux.0"))
                boot_filename = "pxelinux.0"
                _job_log(job,f"Copied pxelinux.0 from {syslinux_path}")
                break
    if not boot_filename:
        boot_filename = "pxelinux.0"
        _job_log(job,"WARNING: No bootloader found in ISO or on host")

    # Patch GRUB config to add inst.repo pointing to our HTTP server
    install_url = f"http://{gateway_ip}:{http_port}/" if gateway_ip else ""
    grub_cfg_path = os.path.join(tftp_root, "grub.cfg")
    if install_url and os.path.isfile(grub_cfg_path):
        with open(grub_cfg_path) as f:
            grub_cfg = f.read()
        if "inst.repo" not in grub_cfg and "inst.stage2" not in grub_cfg:
            grub_cfg = grub_cfg.replace(" quiet", f" inst.repo={install_url} quiet")
            with open(grub_cfg_path, "w") as f:
                f.write(grub_cfg)
            _job_log(job,f"Patched grub.cfg with inst.repo={install_url}")
        elif "inst.stage2" in grub_cfg:
            import re
            grub_cfg = re.sub(r'inst\.stage2=\S+', f'inst.repo={install_url}', grub_cfg)
            with open(grub_cfg_path, "w") as f:
                f.write(grub_cfg)
            _job_log(job,f"Replaced inst.stage2 with inst.repo={install_url} in grub.cfg")

    # Generate BIOS PXE boot config (pxelinux.cfg/default)
    append_line = "initrd=initrd.img"
    if install_url:
        append_line += f" inst.repo={install_url}"
    pxe_cfg = f"DEFAULT install\nLABEL install\n  KERNEL vmlinuz\n  APPEND {append_line}\n"
    with open(os.path.join(tftp_root, "pxelinux.cfg", "default"), "w") as f:
        f.write(pxe_cfg)
    _job_log(job,"Generated pxelinux.cfg/default")

    # Ensure dnsmasq config has TFTP enabled and restart it
    dnsmasq_conf = f"/etc/dnsmasq.d/troshka-{vni}.conf"
    dnsmasq_pid = f"/run/troshka-dnsmasq-{vni}.pid"
    if os.path.exists(dnsmasq_conf):
        with open(dnsmasq_conf) as f:
            lines = f.readlines()
        filtered = [l for l in lines if not l.strip().startswith(("enable-tftp", "tftp-root=", "dhcp-boot="))]
        filtered.append(f"enable-tftp\n")
        filtered.append(f"tftp-root={tftp_root}\n")
        filtered.append(f"dhcp-boot={boot_filename}\n")
        with open(dnsmasq_conf, "w") as f:
            f.writelines(filtered)
        _job_log(job,f"Configured dnsmasq TFTP with boot file {boot_filename}")
        # Always kill and restart dnsmasq in the correct namespace
        if os.path.exists(dnsmasq_pid):
            try:
                with open(dnsmasq_pid) as f:
                    old_pid = int(f.read().strip())
                os.kill(old_pid, signal.SIGTERM)
                import time as _t2
                _t2.sleep(0.5)
            except (ValueError, ProcessLookupError, PermissionError):
                pass
        _run_cmd(job, ["ip", "netns", "exec", ns, "dnsmasq", f"--conf-file={dnsmasq_conf}"], timeout=10)
        _job_log(job,"Restarted dnsmasq with TFTP enabled")

    # Start HTTP server in namespace to serve ISO contents (ISO already mounted above)
    pid_file = f"/run/troshka-pxe-http-{vni}.pid"
    # Kill existing server
    if os.path.exists(pid_file):
        try:
            with open(pid_file) as f:
                old_pid = int(f.read().strip())
            os.kill(old_pid, signal.SIGTERM)
        except (ValueError, ProcessLookupError, PermissionError):
            pass

    http_script = f'''#!/usr/bin/env python3
import http.server
import os
import socketserver

os.chdir("{mount_point}")
socketserver.TCPServer.allow_reuse_address = True
httpd = socketserver.TCPServer(("0.0.0.0", {http_port}), http.server.SimpleHTTPRequestHandler)
httpd.serve_forever()
'''
    script_path = f"/var/lib/troshka/pxe/{vni}/http_server.py"
    with open(script_path, "w") as f:
        f.write(http_script)

    subprocess.Popen(
        ["ip", "netns", "exec", ns, "python3", script_path],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    # Write PID file (give process a moment to start)
    import time as _t
    _t.sleep(0.5)
    try:
        result = subprocess.run(
            ["pgrep", "-f", script_path],
            capture_output=True, text=True, timeout=5,
        )
        if result.stdout.strip():
            pid = result.stdout.strip().split("\n")[0]
            with open(pid_file, "w") as f:
                f.write(pid)
    except (subprocess.TimeoutExpired, OSError):
        pass

    _job_log(job,f"Started HTTP install source on port {http_port}")
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

    temp_files = []
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        if s3_download_url:
            _job_log(job,f"Downloading from S3...")
            _s3_download(job, s3_download_url, cache_path, aws_access_key, aws_secret_key, aws_region)
        elif download_url:
            _job_log(job,f"Downloading from {download_url}...")
            _run_cmd(job, ["curl", "-fSL", "-o", cache_path, _validate_url(download_url)], timeout=7200)

        if flatten:
            _job_log(job,"Flattening QCOW2 chain...")
            flat_path = cache_path + ".flat"
            temp_files.append(flat_path)
            _run_cmd(job, ["qemu-img", "convert", "-O", "qcow2", cache_path, flat_path], timeout=3600)
            os.rename(flat_path, cache_path)
            temp_files.remove(flat_path)
            _job_log(job,"Flattening complete")

        if s3_upload_url:
            _job_log(job,"Uploading to S3...")
            _s3_upload(job, cache_path, s3_upload_url, aws_access_key, aws_secret_key, aws_region)

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
    vni = int(params["vni"])
    bridge_name = _validate_bridge_name(params["bridge_name"])
    project_id = _validate_project_id(params["project_id"])
    ns = f"troshka-{project_id[:8]}"

    # Create namespace
    try:
        _run_cmd(job, ["ip", "netns", "add", ns])
    except RuntimeError:
        _job_log(job,f"Namespace {ns} may already exist, continuing")

    # Create bridge in namespace
    _run_cmd(job, ["ip", "netns", "exec", ns, "ip", "link", "add", bridge_name, "type", "bridge"])
    _run_cmd(job, ["ip", "netns", "exec", ns, "ip", "addr", "add", cidr, "dev", bridge_name])
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
        _job_log(job,f"Namespace {ns} may not exist, continuing")

    return {"network": network_name, "status": "removed"}

COMMAND_HANDLERS["networks/teardown"] = _handle_network_teardown


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

    # ── Namespace + veth setup (idempotent — reuse if already exists) ──
    ns_exists = subprocess.run(["ip", "netns", "exec", ns, "true"], capture_output=True, timeout=5).returncode == 0
    if ns_exists:
        _job_log(job,f"Namespace {ns} already exists, reusing")
    else:
        _run_cmd(job, ["ip", "netns", "add", ns], timeout=10)
        _run_cmd(job, ["ip", "link", "add", veth_host, "type", "veth", "peer", "name", veth_ns], timeout=10)
        _run_cmd(job, ["ip", "link", "set", veth_ns, "netns", ns], timeout=10)
        _run_cmd(job, ["ip", "addr", "add", f"{transit_host_ip}/24", "dev", veth_host], timeout=10)
        _run_cmd(job, ["ip", "link", "set", veth_host, "up"], timeout=10)
        _run_cmd(job, ["ip", "netns", "exec", ns, "ip", "addr", "add", f"{transit_ns_ip}/24", "dev", veth_ns], timeout=10)
        _run_cmd(job, ["ip", "netns", "exec", ns, "ip", "link", "set", veth_ns, "up"], timeout=10)
        _run_cmd(job, ["ip", "netns", "exec", ns, "ip", "link", "set", "lo", "up"], timeout=10)
        _run_cmd(job, ["ip", "netns", "exec", ns, "ip", "route", "add", "default", "via", transit_host_ip], timeout=10)
    try:
        _run_cmd(job, ["ip", "route", "add", transit_cidr, "dev", veth_host], timeout=10)
    except RuntimeError:
        pass

    _run_cmd(job, ["sysctl", "-w", "net.ipv4.ip_forward=1"], timeout=10)
    _job_log(job,"Namespace and veth pair configured")

    # ── VXLAN + Bridge setup (inside namespace) ──
    for net in networks:
        vni = int(net["vni"])
        bridge = net["bridge_name"]
        vxlan_if = net["vxlan_name"]
        cidr = net.get("cidr", "")
        peers = net.get("peers", [])

        # Validate names
        _validate_bridge_name(bridge)

        # Clean up existing
        try:
            _run_cmd(job, ["ip", "link", "del", vxlan_if], timeout=10)
        except RuntimeError:
            pass
        try:
            _run_cmd(job, ["ip", "netns", "exec", ns, "ip", "link", "del", vxlan_if], timeout=10)
        except RuntimeError:
            pass

        # Create VXLAN in host namespace (may already exist from a previous deploy)
        try:
            _run_cmd(job, ["ip", "link", "add", vxlan_if, "type", "vxlan",
                            "id", str(vni), "local", host_ip, "dstport", "4789", "nolearning"], timeout=10)
        except RuntimeError:
            _job_log(job,f"VXLAN {vxlan_if} already exists, reusing")

        # Add peers
        for peer in peers:
            if peer != host_ip:
                try:
                    _validate_ip(peer)
                    _run_cmd(job, ["bridge", "fdb", "append", "00:00:00:00:00:00",
                                    "dev", vxlan_if, "dst", peer], timeout=10)
                except (ValueError, RuntimeError):
                    _job_log(job,f"Warning: skipping peer {peer}")

        # Move VXLAN into namespace (may already be there)
        try:
            _run_cmd(job, ["ip", "link", "set", vxlan_if, "netns", ns], timeout=10)
        except RuntimeError:
            _job_log(job,f"VXLAN {vxlan_if} already in namespace, reusing")

        # Create bridge inside namespace (may already exist)
        try:
            _run_cmd(job, ["ip", "netns", "exec", ns, "ip", "link", "add", bridge, "type", "bridge"], timeout=10)
        except RuntimeError:
            _job_log(job,f"Bridge {bridge} already exists, reusing")
        try:
            _run_cmd(job, ["ip", "netns", "exec", ns, "ip", "link", "set", vxlan_if, "master", bridge], timeout=10)
        except RuntimeError:
            pass
        try:
            _run_cmd(job, ["ip", "netns", "exec", ns, "ip", "link", "set", vxlan_if, "up"], timeout=10)
        except RuntimeError:
            pass
        try:
            _run_cmd(job, ["ip", "netns", "exec", ns, "ip", "link", "set", bridge, "up"], timeout=10)
        except RuntimeError:
            pass

        # Create dummy bridge in host namespace for libvirt validation.
        # This bridge carries no traffic — the qemu hook moves TAPs to the
        # namespace bridge on VM start. We disable forwarding to prevent
        # cross-project leaks if the hook ever fails.
        try:
            subprocess.run(["ip", "link", "show", bridge], capture_output=True, check=True, timeout=5)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            _run_cmd(job, ["ip", "link", "add", bridge, "type", "bridge"], timeout=10)
            # Disable forwarding on dummy bridge for security isolation.
            # If the qemu hook fails to move a TAP, it stays on this bridge
            # with no connectivity — preventing cross-project traffic.
            subprocess.run(["ip", "link", "set", bridge, "type", "bridge",
                            "forward_delay", "99", "ageing_time", "0"],
                           capture_output=True, timeout=5)
        _run_cmd(job, ["ip", "link", "set", bridge, "up"], timeout=10)

        # Assign bridge IP if DHCP/DNS is enabled
        if net.get("dhcp_enabled") or net.get("dns_enabled"):
            dhcp_cfg = net.get("dhcp_config", {})
            gateway_ip = dhcp_cfg.get("gateway", "")
            if gateway_ip and cidr:
                prefix = cidr.split("/")[1] if "/" in cidr else "24"
                try:
                    _run_cmd(job, ["ip", "netns", "exec", ns, "ip", "addr", "add",
                                    f"{gateway_ip}/{prefix}", "dev", bridge], timeout=10)
                except RuntimeError:
                    pass

        _job_log(job,f"VXLAN {vxlan_if} (VNI {vni}) + bridge {bridge} configured")

    # ── DHCP (dnsmasq inside namespace) ──
    for net in networks:
        if not net.get("dhcp_enabled"):
            continue
        vni = int(net["vni"])
        bridge = net["bridge_name"]
        dhcp_cfg = net.get("dhcp_config", {})
        range_start = dhcp_cfg.get("range_start", "")
        range_end = dhcp_cfg.get("range_end", "")
        lease_time = dhcp_cfg.get("lease_time", "24h")
        if not (range_start and range_end):
            continue

        pid_short = project_id[:8]
        dnsmasq_conf = f"/etc/dnsmasq.d/troshka-{pid_short}-{vni}.conf"
        dnsmasq_pid = f"/run/troshka-dnsmasq-{pid_short}-{vni}.pid"
        dnsmasq_lease = f"/var/lib/troshka/dnsmasq-{pid_short}-{vni}.leases"

        pid = project_id[:8]
        bmc_bridge = f"br-bmc-{pid}"
        conf_lines = [
            f"interface={bridge}",
            "bind-dynamic",
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

        # PXE config
        pxe = net.get("pxe_config")
        if pxe:
            if pxe.get("server_mode") == "builtin" and pxe.get("tftp_root"):
                tftp_r = pxe["tftp_root"]
                conf_lines.append("enable-tftp")
                conf_lines.append(f"tftp-root={tftp_r}")
                boot_file = "pxelinux.0"
                for candidate in ["BOOTX64.EFI", "grubx64.efi", "pxelinux.0"]:
                    if os.path.isfile(os.path.join(tftp_r, candidate)):
                        boot_file = candidate
                        break
                conf_lines.append(f"dhcp-boot={boot_file}")
            else:
                method = pxe.get("method", "legacy")
                if method == "legacy" and pxe.get("next_server") and pxe.get("boot_file"):
                    conf_lines.append(f"dhcp-boot={pxe['boot_file']},{pxe['next_server']},{pxe['next_server']}")
                elif method == "ipxe" and pxe.get("ipxe_script_url"):
                    conf_lines.append(f"dhcp-boot={pxe['ipxe_script_url']}")
                elif method == "uefi-http" and pxe.get("uefi_boot_url"):
                    conf_lines.append(f"dhcp-boot={pxe['uefi_boot_url']}")

        os.makedirs("/etc/dnsmasq.d", exist_ok=True)
        with open(dnsmasq_conf, "w") as f:
            f.write("\n".join(conf_lines) + "\n")

        # Kill existing dnsmasq for this VNI and wait for port release
        if os.path.exists(dnsmasq_pid):
            try:
                with open(dnsmasq_pid) as f:
                    old_pid = int(f.read().strip())
                os.kill(old_pid, signal.SIGTERM)
                for _ in range(20):
                    try:
                        os.kill(old_pid, 0)
                        time.sleep(0.25)
                    except ProcessLookupError:
                        break
                else:
                    os.kill(old_pid, signal.SIGKILL)
            except (ValueError, ProcessLookupError, PermissionError):
                pass
            try:
                os.remove(dnsmasq_pid)
            except FileNotFoundError:
                pass

        _run_cmd(job, ["ip", "netns", "exec", ns, "dnsmasq", f"--conf-file={dnsmasq_conf}"], timeout=10)
        try:
            with open(dnsmasq_pid) as _pf:
                _dpid = _pf.read().strip()
            subprocess.run(
                ["auditctl", "-a", "exit,always", "-F", "arch=b64", "-S", "kill",
                 "-F", f"a0={_dpid}", "-k", "dnsmasq-kill"],
                capture_output=True, timeout=5,
            )
        except Exception:
            pass
        _job_log(job,f"dnsmasq started for VNI {vni} on {bridge}")

    # ── nftables inside namespace (flush if already exists, silence expected errors) ──
    for tbl in ["filter", "nat"]:
        subprocess.run(["ip", "netns", "exec", ns, "nft", "flush", "table", "inet", tbl],
                        capture_output=True, timeout=10)
        subprocess.run(["ip", "netns", "exec", ns, "nft", "delete", "table", "inet", tbl],
                        capture_output=True, timeout=10)
    _run_cmd(job, ["ip", "netns", "exec", ns, "nft", "add", "table", "inet", "filter"], timeout=10)
    _run_cmd(job, ["ip", "netns", "exec", ns, "nft", "add", "chain", "inet", "filter", "forward",
                    "{ type filter hook forward priority 0; policy drop; }"], timeout=10)
    _run_cmd(job, ["ip", "netns", "exec", ns, "nft", "add", "table", "inet", "nat"], timeout=10)
    _run_cmd(job, ["ip", "netns", "exec", ns, "nft", "add", "chain", "inet", "nat", "postrouting",
                    "{ type nat hook postrouting priority 100; }"], timeout=10)
    _run_cmd(job, ["ip", "netns", "exec", ns, "nft", "add", "chain", "inet", "nat", "prerouting",
                    "{ type nat hook prerouting priority -100; }"], timeout=10)
    # Masquerade outbound traffic from bridges
    _run_cmd(job, ["ip", "netns", "exec", ns, "nft", "add", "rule", "inet", "nat", "postrouting",
                    "oifname", veth_ns, "masquerade"], timeout=10)

    # Intra-bridge forwarding
    for net in networks:
        bridge = net["bridge_name"]
        _run_cmd(job, ["ip", "netns", "exec", ns, "nft", "add", "rule", "inet", "filter", "forward",
                        "iifname", bridge, "oifname", bridge, "accept"], timeout=10)

    # Router: inter-bridge forwarding
    for router in routers:
        vnis = router.get("connected_vnis", [])
        for i, vni_a in enumerate(vnis):
            for vni_b in vnis[i + 1:]:
                br_a = f"br-{vni_a}"
                br_b = f"br-{vni_b}"
                _run_cmd(job, ["ip", "netns", "exec", ns, "nft", "add", "rule", "inet", "filter", "forward",
                                "iifname", br_a, "oifname", br_b, "accept"], timeout=10)
                _run_cmd(job, ["ip", "netns", "exec", ns, "nft", "add", "rule", "inet", "filter", "forward",
                                "iifname", br_b, "oifname", br_a, "accept"], timeout=10)

    # Allow established/related + bridge→veth outbound
    _run_cmd(job, ["ip", "netns", "exec", ns, "nft", "add", "rule", "inet", "filter", "forward",
                    "ct", "state", "established,related", "accept"], timeout=10)
    for net in networks:
        bridge = net["bridge_name"]
        _run_cmd(job, ["ip", "netns", "exec", ns, "nft", "add", "rule", "inet", "filter", "forward",
                        "iifname", bridge, "oifname", veth_ns, "accept"], timeout=10)

    # Port forward DNAT inside namespace
    pf_transit_ips = {}
    if gateway and gateway.get("mode") == "nat-portforward":
        for pf_idx, pf in enumerate(gateway.get("port_forwards", [])):
            ext_port = pf.get("extPort", "")
            int_ip = pf.get("intIp", "")
            int_port = pf.get("intPort", "")
            if ext_port and int_ip and int_port:
                pf_transit_ip = f"172.30.{transit_octet3}.{10 + pf_idx}"
                pf_transit_ips[pf_idx] = pf_transit_ip
                try:
                    _run_cmd(job, ["ip", "netns", "exec", ns, "ip", "addr", "add",
                                    f"{pf_transit_ip}/24", "dev", veth_ns], timeout=10)
                except RuntimeError:
                    pass  # May already exist
                _run_cmd(job, ["ip", "netns", "exec", ns, "nft", "add", "rule", "inet", "nat", "prerouting",
                                "ip", "daddr", pf_transit_ip, "tcp", "dport", str(ext_port),
                                "dnat", "ip", "to", f"{int_ip}:{int_port}"], timeout=10)
                _run_cmd(job, ["ip", "netns", "exec", ns, "nft", "add", "rule", "inet", "filter", "forward",
                                "iifname", veth_ns, "tcp", "dport", str(int_port), "accept"], timeout=10)

    _job_log(job,"Namespace nftables configured")

    # ── nftables in HOST namespace ──
    if gateway and gateway.get("mode") in ("nat", "nat-portforward"):
        fwd_chain = f"troshka-fwd-{pid}"
        post_chain = f"troshka-post-{pid}"
        pre_chain = f"troshka-pre-{pid}"

        # These may fail if tables/chains already exist — that's OK
        def _nft_try(cmd):
            try:
                _run_cmd(job, cmd, timeout=10)
            except RuntimeError:
                pass

        _nft_try(["nft", "add", "table", "inet", "filter"])
        _nft_try(["nft", "add", "chain", "inet", "filter", "forward",
                   "{ type filter hook forward priority 0; policy accept; }"])
        _nft_try(["nft", "add", "table", "inet", "nat"])
        _nft_try(["nft", "add", "chain", "inet", "nat", "postrouting",
                   "{ type nat hook postrouting priority 100; }"])
        _nft_try(["nft", "add", "chain", "inet", "nat", "prerouting",
                   "{ type nat hook prerouting priority -100; }"])
        _nft_try(["nft", "add", "chain", "inet", "filter", fwd_chain])
        _nft_try(["nft", "flush", "chain", "inet", "filter", fwd_chain])
        _nft_try(["nft", "add", "chain", "inet", "nat", post_chain])
        _nft_try(["nft", "flush", "chain", "inet", "nat", post_chain])
        _nft_try(["nft", "add", "chain", "inet", "nat", pre_chain])
        _nft_try(["nft", "flush", "chain", "inet", "nat", pre_chain])

        # Check if jump rules exist, add if not
        for (table, chain, jump_chain) in [
            ("filter", "forward", fwd_chain),
            ("nat", "postrouting", post_chain),
            ("nat", "prerouting", pre_chain),
        ]:
            check = subprocess.run(
                ["nft", "list", "chain", "inet", table, chain],
                capture_output=True, text=True, timeout=5,
            )
            if f"jump {jump_chain}" not in check.stdout:
                _nft_try(["nft", "add", "rule", "inet", table, chain, "jump", jump_chain])

        # Forward traffic through veth
        _run_cmd(job, ["nft", "add", "rule", "inet", "filter", fwd_chain,
                        "iifname", veth_host, "accept"], timeout=10)
        _run_cmd(job, ["nft", "add", "rule", "inet", "filter", fwd_chain,
                        "oifname", veth_host, "accept"], timeout=10)
        # Masquerade transit traffic
        _run_cmd(job, ["nft", "add", "rule", "inet", "nat", post_chain,
                        "ip", "saddr", transit_cidr, "masquerade"], timeout=10)

        # EIP port forward DNAT in host namespace
        if gateway.get("mode") == "nat-portforward":
            for pf_idx, pf in enumerate(gateway.get("port_forwards", [])):
                ext_port = pf.get("extPort", "")
                int_ip = pf.get("intIp", "")
                int_port = pf.get("intPort", "")
                priv_ip = pf.get("_private_ip", "")
                pf_transit_ip = pf_transit_ips.get(pf_idx, transit_ns_ip)
                if ext_port and int_ip and int_port:
                    if priv_ip:
                        _run_cmd(job, ["nft", "add", "rule", "inet", "nat", pre_chain,
                                        "ip", "daddr", priv_ip, "tcp", "dport", str(ext_port),
                                        "dnat", "ip", "to", f"{pf_transit_ip}:{ext_port}"], timeout=10)
                    else:
                        _job_log(job,f"Skipping port forward :{ext_port} — no EIP private IP yet")

        _job_log(job,"Host nftables configured")

    return {
        "project_id": project_id,
        "namespace": ns,
        "networks": len(networks),
        "status": "configured",
    }

COMMAND_HANDLERS["networks/full-setup"] = _handle_network_full_setup


def _handle_lb_setup(job, params):
    """Set up HAProxy load balancer inside project namespace."""
    ns = params["ns"]
    project_id = _validate_project_id(params["project_id"])
    pid = project_id[:8]
    frontends = params.get("frontends", [])
    backends = params.get("backends", [])
    lb_ip = params.get("lb_ip", "")
    bind_addr = lb_ip if lb_ip else "*"

    # Assign LB IP to the first bridge in the namespace
    if lb_ip:
        bridges = subprocess.run(
            ["ip", "netns", "exec", ns, "ip", "-o", "link", "show", "type", "bridge"],
            capture_output=True, text=True, timeout=10,
        )
        bridge_name = ""
        for line in bridges.stdout.strip().split("\n"):
            if line and "br-bmc" not in line:
                bridge_name = line.split(":")[1].strip().split("@")[0]
                break
        if bridge_name:
            try:
                _run_cmd(job, ["ip", "netns", "exec", ns, "ip", "addr", "add",
                                f"{lb_ip}/24", "dev", bridge_name], timeout=10)
            except RuntimeError:
                pass

    haproxy_conf = f"/etc/haproxy/troshka-{pid}.cfg"
    haproxy_pid = f"/run/troshka-haproxy-{pid}.pid"

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
            lines.append(f"    server {be['name']} {be['ip']}:{fe['backendPort']} check")
        lines.append("")

    config_content = "\n".join(lines)
    _job_log(job,f"Writing HAProxy config to {haproxy_conf}")

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
    _run_cmd(job, ["ip", "netns", "exec", ns, "haproxy", "-f", haproxy_conf, "-D", "-p", haproxy_pid], timeout=10)
    _job_log(job,f"HAProxy started in namespace {ns}")
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

    _job_log(job,f"HAProxy teardown complete for project {pid}")
    return {"status": "torn_down"}

COMMAND_HANDLERS["lb/teardown"] = _handle_lb_teardown


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

    # Delete VXLAN interfaces inside namespace BEFORE deleting the namespace.
    # ip netns del destroys the interfaces but does NOT release VNI registrations
    # from the kernel, causing "A VXLAN device with the specified VNI already exists"
    # on the next deploy. Explicitly deleting them first releases the VNIs.
    for vni in vni_list:
        vxlan_if = f"vxlan-{vni}"
        try:
            _run_cmd(job, ["ip", "netns", "exec", ns, "ip", "link", "del", vxlan_if], timeout=10)
        except RuntimeError:
            pass
        try:
            _run_cmd(job, ["ip", "link", "del", vxlan_if], timeout=10)
        except RuntimeError:
            pass

    # Kill HAProxy if running
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

    # Kill dnsmasq by PID file (not pkill — that kills ALL dnsmasq on the host)
    pid_short = project_id[:8] if project_id else ns.replace("troshka-", "")
    for pidfile in glob.glob(f"/run/troshka-dnsmasq-{pid_short}-*.pid"):
        try:
            with open(pidfile) as f:
                dnsmasq_pid = int(f.read().strip())
            os.kill(dnsmasq_pid, 9)
            _job_log(job,f"Killed dnsmasq PID {dnsmasq_pid}")
        except (FileNotFoundError, ValueError, ProcessLookupError, OSError):
            pass
        try:
            os.remove(pidfile)
        except FileNotFoundError:
            pass
    # Clean up config and lease files
    for pat in [f"/etc/dnsmasq.d/troshka-{pid_short}-*.conf",
                f"/var/lib/troshka/dnsmasq-{pid_short}-*.leases"]:
        for f in glob.glob(pat):
            try:
                os.remove(f)
            except FileNotFoundError:
                pass

    # Delete namespace
    try:
        _run_cmd(job, ["ip", "netns", "del", ns], timeout=10)
    except RuntimeError:
        _job_log(job,f"Namespace {ns} may not exist")

    # Delete host-side veth
    try:
        _run_cmd(job, ["ip", "link", "del", veth_host], timeout=10)
    except RuntimeError:
        pass

    # Clean up host-side nftables chains for this project
    fwd_chain = f"troshka-fwd-{pid}"
    post_chain = f"troshka-post-{pid}"
    pre_chain = f"troshka-pre-{pid}"
    # Remove jump rules from main chains first, then delete the project chains
    for table, main_chain, proj_chain in [
        ("filter", "forward", fwd_chain),
        ("nat", "postrouting", post_chain),
        ("nat", "prerouting", pre_chain),
    ]:
        try:
            result = subprocess.run(
                ["nft", "-a", "list", "chain", "inet", table, main_chain],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.split("\n"):
                if f"jump {proj_chain}" in line:
                    handle = line.strip().split("# handle ")[-1]
                    subprocess.run(
                        ["nft", "delete", "rule", "inet", table, main_chain, "handle", handle],
                        capture_output=True, timeout=5,
                    )
        except Exception:
            pass
        try:
            _run_cmd(job, ["nft", "flush", "chain", "inet", table, proj_chain], timeout=10)
            _run_cmd(job, ["nft", "delete", "chain", "inet", table, proj_chain], timeout=10)
        except RuntimeError:
            pass

    # Clean up dnsmasq files
    for vni in vni_list:
        for path in [
            f"/run/troshka-dnsmasq-{vni}.pid",
            f"/etc/dnsmasq.d/troshka-{vni}.conf",
            f"/var/lib/troshka/dnsmasq-{vni}.leases",
        ]:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass

    # Clean up PXE boot services
    for vni in vni_list:
        pid_file = f"/run/troshka-pxe-http-{vni}.pid"
        if os.path.exists(pid_file):
            try:
                with open(pid_file) as f:
                    http_pid = int(f.read().strip())
                os.kill(http_pid, signal.SIGTERM)
            except (ValueError, ProcessLookupError, PermissionError):
                pass
            try:
                os.remove(pid_file)
            except FileNotFoundError:
                pass
        mount_point = f"/var/lib/troshka/pxe/{vni}/mnt"
        try:
            subprocess.run(["umount", mount_point], capture_output=True, timeout=10)
        except (subprocess.TimeoutExpired, OSError):
            pass
        pxe_dir = f"/var/lib/troshka/pxe/{vni}"
        if os.path.isdir(pxe_dir):
            import shutil
            try:
                shutil.rmtree(pxe_dir)
            except OSError:
                pass

    # Delete bridges in host namespace (safe since VNIs are never recycled)
    for vni in vni_list:
        bridge = f"br-{vni}"
        try:
            _run_cmd(job, ["ip", "link", "delete", bridge], timeout=10)
            _job_log(job,f"Removed bridge: {bridge}")
        except RuntimeError:
            pass

    return {"project_id": project_id, "status": "torn_down"}

COMMAND_HANDLERS["networks/full-teardown"] = _handle_network_full_teardown


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

        with _tf.TemporaryDirectory(dir="/var/lib/troshka/tmp") as tmpdir:
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
            _run_cmd(job, [
                "xorriso", "-as", "genisoimage",
                "-output", path,
                "-volid", "cidata",
                "-joliet", "-rock",
                tmpdir + "/",
            ])
        _chown_qemu(path)
        created += 1
        _job_log(job,f"Seed ISO created: {path}")

    return {"created": created, "status": "completed"}

COMMAND_HANDLERS["seeds/create-batch"] = _handle_seed_create_batch


_METADATA_SCRIPT_TEMPLATE = '''
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
'''


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
        _job_log(job,f"Killed existing metadata service (if any)")
    except RuntimeError:
        _job_log(job,f"No existing metadata service to kill")

    # Step 2: Add metadata IP to each bridge inside namespace
    for bridge in bridges:
        try:
            _run_cmd(job, [
                "ip", "netns", "exec", namespace,
                "ip", "addr", "add", "169.254.169.254/32", "dev", bridge
            ], timeout=10)
            _job_log(job,f"Added metadata IP to {bridge} in {namespace}")
        except RuntimeError as e:
            if "File exists" in str(e) or "RTNETLINK answers: File exists" in str(e):
                _job_log(job,f"Metadata IP already exists on {bridge}, continuing")
            else:
                raise

    # Step 3: Write metadata service script
    script_path = f"/opt/troshka/metadata-{project_id[:8]}.py"
    configs_json = json.dumps(vm_configs)
    script_content = _METADATA_SCRIPT_TEMPLATE.format(configs_json=configs_json)

    os.makedirs("/opt/troshka", exist_ok=True)
    with open(script_path, "w") as f:
        f.write(script_content)
    _job_log(job,f"Wrote metadata service script to {script_path}")

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
        _job_log(job,f"Warning: metadata service may have failed to start (check {log_file})")
        return {"status": "started", "pid": None, "warning": "Process exited immediately"}

    pid = proc.pid
    _job_log(job,f"Started metadata service in {namespace} (PID {pid}, log: {log_file})")

    return {"status": "started", "pid": pid}

COMMAND_HANDLERS["metadata/deploy"] = _handle_metadata_deploy


def _handle_eip_configure(job, params):
    project_id = _validate_project_id(params["project_id"])
    eip_mappings = params.get("eip_mappings", [])
    ns = f"troshka-{project_id[:8]}"

    for mapping in eip_mappings:
        public_ip = _validate_ip(mapping["public_ip"])
        private_ip = _validate_ip(mapping["private_ip"])
        _run_cmd(job, [
            "ip", "netns", "exec", ns, "nft", "add", "rule",
            "ip", "nat", "postrouting",
            "ip", "saddr", private_ip,
            "counter", "masquerade",
        ])

    return {"project_id": project_id, "status": "configured"}

COMMAND_HANDLERS["eips/configure"] = _handle_eip_configure


# ── Operations handlers ──

def _handle_gc_discover(job, params):
    """Scan host for orphaned resources (dirs, domains, bridges, namespaces, cache items)."""
    known_project_ids = params.get("known_project_ids", [])
    known_domains = params.get("known_domains", [])

    orphan_dirs = []
    orphan_domains = []
    orphan_bridges = []
    orphan_namespaces = []
    cache_items = []

    # 1. Scan VM dirs for orphan project dirs (local + shared storage)
    for vms_dir in ["/var/lib/troshka/vms", "/var/lib/troshka/shared/vms",
                    "/var/lib/troshka/local/vms"]:
        if not os.path.exists(vms_dir):
            continue
        try:
            for entry in os.listdir(vms_dir):
                if entry not in known_project_ids:
                    full_path = os.path.join(vms_dir, entry)
                    if os.path.isdir(full_path):
                        orphan_dirs.append(full_path + "/")
                        _job_log(job,f"Orphan dir: {full_path}/")
        except Exception as e:
            _job_log(job,f"Failed to scan {vms_dir}: {e}")

    # 2. List all virsh domains starting with troshka- that don't belong to known projects
    known_domain_prefixes = set(known_domains)
    try:
        result = subprocess.run(
            ["virsh", "list", "--all", "--name"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            for domain in result.stdout.strip().split("\n"):
                domain = domain.strip()
                if not domain.startswith("troshka-"):
                    continue
                if not any(domain.startswith(prefix) for prefix in known_domain_prefixes):
                    orphan_domains.append(domain)
                    _job_log(job,f"Orphan domain: {domain}")
    except Exception as e:
        _job_log(job,f"Failed to list virsh domains: {e}")

    # 3. List orphan bridges — both br-troshka-* and dummy br-{vni} bridges
    #    not referenced by any defined VM
    try:
        import re as _re_gc
        all_vm_bridges = set()
        vm_list = subprocess.run(["virsh", "list", "--all", "--name"],
                                 capture_output=True, text=True, timeout=10)
        for vm_name in vm_list.stdout.strip().split("\n"):
            if not vm_name.strip():
                continue
            xml = subprocess.run(["virsh", "dumpxml", vm_name.strip()],
                                 capture_output=True, text=True, timeout=10)
            if xml.returncode == 0:
                all_vm_bridges.update(_re_gc.findall(r"source bridge='([^']+)'", xml.stdout))

        result = subprocess.run(
            ["ip", "-o", "link", "show", "type", "bridge"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                parts = line.split(":", 2)
                if len(parts) >= 2:
                    bridge_name = parts[1].strip().split("@")[0]
                    if bridge_name.startswith("br-") and bridge_name not in all_vm_bridges:
                        # Skip namespace-internal bridges (they're inside namespaces, not in host)
                        ns_check = subprocess.run(
                            ["ip", "link", "show", bridge_name],
                            capture_output=True, timeout=5)
                        if ns_check.returncode == 0:
                            orphan_bridges.append(bridge_name)
                            _job_log(job,f"Orphan bridge: {bridge_name}")
    except Exception as e:
        _job_log(job,f"Failed to list bridges: {e}")

    # 4. List namespaces matching troshka-* that don't belong to known projects
    known_ns_prefixes = {f"troshka-{pid[:8]}" for pid in known_project_ids}
    try:
        result = subprocess.run(
            ["ip", "netns", "list"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                if line.startswith("troshka-"):
                    ns_name = line.split()[0]
                    if ns_name not in known_ns_prefixes:
                        orphan_namespaces.append(ns_name)
                        _job_log(job,f"Orphan namespace: {ns_name}")
    except Exception as e:
        _job_log(job,f"Failed to list namespaces: {e}")

    # 5. Scan cache dirs for staleness (report all items, backend will decide eviction)
    local = _config.get("local_mount", "/var/lib/troshka/local")
    cache_dirs = [
        (f"{local}/cache/patterns", "pattern"),
        ("/var/lib/troshka/cache/patterns", "pattern"),
        ("/var/lib/troshka/cache/snapshots", "snapshot"),
        ("/var/lib/troshka/images", "image"),
    ]
    for cache_dir, item_type in cache_dirs:
        if os.path.exists(cache_dir):
            try:
                for entry in os.listdir(cache_dir):
                    full_path = os.path.join(cache_dir, entry)
                    try:
                        stat = os.stat(full_path)
                        age_hours = (time.time() - stat.st_atime) / 3600
                        cache_items.append({
                            "path": full_path,
                            "type": item_type,
                            "age_hours": int(age_hours),
                        })
                    except Exception:
                        pass
            except Exception as e:
                _job_log(job,f"Failed to scan {cache_dir}: {e}")

    # 6. Clean orphan dnsmasq lease files (stale files from deleted projects)
    known_prefixes = {pid[:8] for pid in known_project_ids}
    for lf in glob.glob("/var/lib/troshka/dnsmasq-*.leases"):
        prefix = os.path.basename(lf).replace("dnsmasq-", "").split("-")[0]
        if prefix not in known_prefixes:
            try:
                os.remove(lf)
                _job_log(job, f"Cleaned orphan lease: {os.path.basename(lf)}")
            except OSError:
                pass

    # 7. Discover orphaned BMC directories
    orphaned_bmc = []
    bmc_base = "/var/lib/troshka/bmc"
    known_bmc = set(params.get("known_bmc_project_ids", []))
    if os.path.isdir(bmc_base):
        for entry in os.listdir(bmc_base):
            full = os.path.join(bmc_base, entry)
            if os.path.isdir(full) and entry not in known_bmc:
                orphaned_bmc.append(entry)
                _job_log(job,f"Orphaned BMC dir: {entry}")

    # Scan S3 temp dir for stale files (older than 1 hour)
    stale_temps = []
    _s3_tmpdir = os.path.join(_config.get("local_mount", "/var/lib/troshka/local"), "tmp")
    if os.path.exists(_s3_tmpdir):
        now = time.time()
        try:
            for entry in os.listdir(_s3_tmpdir):
                full_path = os.path.join(_s3_tmpdir, entry)
                try:
                    age = now - os.stat(full_path).st_mtime
                    if age > 3600:
                        stale_temps.append(full_path)
                        _job_log(job, f"Stale temp file ({int(age)}s old): {full_path}")
                except OSError:
                    pass
        except OSError as e:
            _job_log(job, f"Failed to scan temp dir: {e}")

    return {
        "orphan_dirs": orphan_dirs,
        "orphan_domains": orphan_domains,
        "orphan_bridges": orphan_bridges,
        "orphan_namespaces": orphan_namespaces,
        "cache_items": cache_items,
        "orphaned_bmc_project_ids": orphaned_bmc,
        "stale_temps": stale_temps,
    }

COMMAND_HANDLERS["gc/discover"] = _handle_gc_discover


def _handle_gc_clean(job, params):
    """Remove specific orphaned resources provided by the backend."""
    orphan_dirs = params.get("orphan_dirs", [])
    orphan_domains = params.get("orphan_domains", [])
    orphan_bridges = params.get("orphan_bridges", [])
    orphan_namespaces = params.get("orphan_namespaces", [])
    cache_items = params.get("cache_items", [])

    removed_dirs = 0
    removed_domains = 0
    removed_bridges = 0
    removed_namespaces = 0
    removed_cache = 0

    # 1. Remove orphan dirs (validated under /var/lib/troshka/)
    for path in orphan_dirs:
        try:
            validated = _validate_path(path)
            if os.path.isdir(validated):
                shutil.rmtree(validated)
                _job_log(job,f"Removed dir: {validated}")
                removed_dirs += 1
        except Exception as e:
            _job_log(job,f"Failed to remove {path}: {e}")

    # 2. Remove orphan domains (virsh destroy + undefine)
    for domain in orphan_domains:
        try:
            _validate_domain_name(domain)
            # Try to destroy (force stop) — may fail if already stopped
            try:
                _run_cmd(job, ["virsh", "destroy", domain], timeout=30)
            except RuntimeError:
                _job_log(job,f"Domain {domain} may already be stopped")
            _run_cmd(job, ["virsh", "undefine", domain, "--nvram"], timeout=30)
            _job_log(job,f"Removed domain: {domain}")
            removed_domains += 1
        except Exception as e:
            _job_log(job,f"Failed to remove domain {domain}: {e}")

    # 3. Remove orphan bridges
    for bridge in orphan_bridges:
        try:
            _validate_bridge_name(bridge)
            _run_cmd(job, ["ip", "link", "delete", bridge], timeout=10)
            _job_log(job,f"Removed bridge: {bridge}")
            removed_bridges += 1
        except Exception as e:
            _job_log(job,f"Failed to remove bridge {bridge}: {e}")

    # 4. Remove orphan namespaces
    for ns in orphan_namespaces:
        try:
            # Validate it starts with troshka-
            if not ns.startswith("troshka-"):
                raise ValueError(f"Invalid namespace name: {ns}")
            _run_cmd(job, ["ip", "netns", "delete", ns], timeout=10)
            _job_log(job,f"Removed namespace: {ns}")
            removed_namespaces += 1
        except Exception as e:
            _job_log(job,f"Failed to remove namespace {ns}: {e}")

    # 5. Remove cache items (validated paths)
    for path in cache_items:
        try:
            validated = _validate_path(path)
            if os.path.isdir(validated):
                shutil.rmtree(validated)
                _job_log(job,f"Removed cache dir: {validated}")
            else:
                os.remove(validated)
                _job_log(job,f"Removed cache file: {validated}")
            removed_cache += 1
        except FileNotFoundError:
            _job_log(job,f"Cache item not found (skipped): {path}")
        except Exception as e:
            _job_log(job,f"Failed to remove cache item {path}: {e}")

    # 6. Clean up orphaned BMC resources
    orphan_bmc_ids = params.get("orphan_bmc_project_ids", [])
    removed_bmc = 0
    for project_id in orphan_bmc_ids:
        bmc_dir = f"/var/lib/troshka/bmc/{project_id}"
        if os.path.isdir(bmc_dir):
            # Kill any running BMC processes
            for fname in os.listdir(bmc_dir):
                if fname.endswith(".pid"):
                    pid_path = os.path.join(bmc_dir, fname)
                    try:
                        with open(pid_path) as f:
                            p = int(f.read().strip())
                        os.kill(p, signal.SIGTERM)
                        _job_log(job,f"Killed BMC process PID {p} ({fname})")
                    except (ValueError, ProcessLookupError, PermissionError, FileNotFoundError):
                        pass
            # Remove libvirt storage pool
            pool_name = f"troshka-vmedia-{project_id[:8]}"
            subprocess.run(["virsh", "pool-destroy", pool_name], capture_output=True, timeout=10)
            subprocess.run(["virsh", "pool-undefine", pool_name], capture_output=True, timeout=10)
            # Remove directory
            shutil.rmtree(bmc_dir, ignore_errors=True)
            _job_log(job,f"Removed BMC dir + pool: {bmc_dir}")
            removed_bmc += 1

        # Remove BMC bridge
        pid_short = project_id[:8]
        bridge = f"br-bmc-{pid_short}"
        ns = f"troshka-{pid_short}"
        try:
            _run_cmd(job, ["ip", "netns", "exec", ns, "ip", "link", "del", bridge], timeout=10)
        except RuntimeError:
            pass
        try:
            _run_cmd(job, ["ip", "link", "del", bridge], timeout=10)
        except RuntimeError:
            pass

    # 7. Remove stale temp files (containment check prevents path traversal)
    removed_temps = 0
    _s3_tmpdir = os.path.join(_config.get("local_mount", "/var/lib/troshka/local"), "tmp")
    real_tmpdir = os.path.realpath(_s3_tmpdir)
    for path in params.get("stale_temps", []):
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
            removed_temps += 1
        except OSError as e:
            _job_log(job, f"Failed to remove {path}: {e}")

    return {
        "removed_dirs": removed_dirs,
        "removed_domains": removed_domains,
        "removed_bridges": removed_bridges,
        "removed_namespaces": removed_namespaces,
        "removed_cache": removed_cache,
        "removed_bmc": removed_bmc,
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
                capture_output=True, text=True, timeout=5,
            )
            if "shut off" in result.stdout:
                break
            time.sleep(1)
    except RuntimeError:
        _job_log(job,"VM may already be stopped")

    # Get disk path from domain XML
    result = subprocess.run(
        ["virsh", "domblklist", domain, "--details"],
        capture_output=True, text=True, timeout=10,
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
    _run_cmd(job, ["qemu-img", "convert", "-O", "qcow2", disk_path, output_path], timeout=3600)

    return {"domain": domain, "output_path": output_path, "status": "created"}

COMMAND_HANDLERS["snapshots/create"] = _handle_snapshot_create


def _get_disk_path_by_index(domain, disk_index):
    """Get disk path from virsh domblklist by index."""
    result = subprocess.run(
        ["virsh", "domblklist", domain, "--details"],
        capture_output=True, text=True, timeout=10,
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

    raise RuntimeError(f"Disk index {disk_index} not found for domain {domain} (found {disk_count} disks)")



def _s3_upload(job, local_path, s3_url, aws_access_key="", aws_secret_key="", aws_region="us-east-1"):
    """Upload a file to S3 using aws cli with file-size progress monitoring."""
    total_bytes = os.path.getsize(local_path)
    total_gb = round(total_bytes / (1024**3), 1)
    env = os.environ.copy()
    _s3_tmpdir = os.path.join(_config.get("local_mount", "/var/lib/troshka/local"), "tmp")
    os.makedirs(_s3_tmpdir, exist_ok=True)
    env["TMPDIR"] = _s3_tmpdir
    if aws_access_key:
        env["AWS_ACCESS_KEY_ID"] = aws_access_key
        env["AWS_SECRET_ACCESS_KEY"] = aws_secret_key
        env["AWS_DEFAULT_REGION"] = aws_region
    aws_bin = "/opt/troshka/venv/bin/aws"
    if not os.path.exists(aws_bin):
        aws_bin = "aws"
    proc = subprocess.Popen(
        [aws_bin, "s3", "cp", local_path, s3_url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
    )
    while proc.poll() is None:
        try:
            with open(f"/proc/{proc.pid}/io") as f:
                for line in f:
                    if line.startswith("read_bytes:"):
                        read_bytes = int(line.split(":")[1].strip())
                        cur_gb = round(read_bytes / (1024**3), 1)
                        pct = min(100, int(read_bytes * 100 / total_bytes)) if total_bytes > 0 else 0
                        _job_log(job, f"Uploading: {cur_gb} of {total_gb} GB ({pct}%)")
                        break
        except (OSError, FileNotFoundError):
            pass
        time.sleep(5)
    if proc.returncode != 0:
        raise RuntimeError(f"S3 upload failed (exit {proc.returncode})")


def _s3_download(job, s3_url, local_path, aws_access_key="", aws_secret_key="", aws_region="us-east-1"):
    """Download a file from S3 using aws cli with file-size progress monitoring."""
    env = os.environ.copy()
    _s3_tmpdir = os.path.join(_config.get("local_mount", "/var/lib/troshka/local"), "tmp")
    os.makedirs(_s3_tmpdir, exist_ok=True)
    env["TMPDIR"] = _s3_tmpdir
    if aws_access_key:
        env["AWS_ACCESS_KEY_ID"] = aws_access_key
        env["AWS_SECRET_ACCESS_KEY"] = aws_secret_key
        env["AWS_DEFAULT_REGION"] = aws_region
    aws_bin = "/opt/troshka/venv/bin/aws"
    if not os.path.exists(aws_bin):
        aws_bin = "aws"
    proc = subprocess.Popen(
        [aws_bin, "s3", "cp", s3_url, local_path],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
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
    if proc.returncode != 0:
        raise RuntimeError(f"S3 download failed (exit {proc.returncode})")


def _handle_snapshot_capture(job, params):
    """Capture a disk snapshot: flatten, upload to S3, cache locally."""
    domain = _validate_domain_name(params["domain_name"])
    disk_index = int(params["disk_index"])
    s3_url = params.get("s3_url", "")
    cache_path = _validate_path(params["cache_path"])
    aws_access_key = params.get("aws_access_key_id", "")
    aws_secret_key = params.get("aws_secret_access_key", "")
    aws_region = params.get("aws_region", "us-east-1")

    import tempfile as _tf

    running = _is_domain_running(domain)
    snapshotted = False
    if running:
        snapshotted = _snapshot_domain(job, domain)

    disk_path = _get_disk_path_by_index(domain, disk_index)
    if snapshotted:
        backing = subprocess.run(
            ["qemu-img", "info", "--output=json", disk_path],
            capture_output=True, text=True, timeout=30)
        if backing.returncode == 0:
            import json as _json
            bfn = _json.loads(backing.stdout).get("full-backing-filename", "")
            if bfn and os.path.exists(bfn):
                disk_path = bfn
    _job_log(job,f"Disk {disk_index} path: {disk_path}")

    _local_tmp = os.path.join(_config.get("local_mount", "/var/lib/troshka/local"), "tmp")
    os.makedirs(_local_tmp, exist_ok=True)
    try:
        with _tf.TemporaryDirectory(dir=_local_tmp) as tmpdir:
            tmp_flat = os.path.join(tmpdir, "flat.qcow2")
            _job_log(job,"Flattening disk...")
            cmd = ["qemu-img", "convert", "-c", "-o", "compression_type=zstd", "-O", "qcow2"]
            if running and not snapshotted:
                cmd.insert(2, "-U")
            cmd.extend([disk_path, tmp_flat])
            _run_cmd(job, cmd, timeout=3600)

            _job_log(job,"Uploading to S3...")
            _s3_upload(job, tmp_flat, s3_url, aws_access_key, aws_secret_key, aws_region)

            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            _job_log(job,f"Caching to {cache_path}...")
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
            ["virsh", "domstate", domain], capture_output=True, text=True, timeout=5)
        return result.returncode == 0 and "running" in result.stdout
    except Exception:
        return False


def _cleanup_stale_snapshots(job, domain):
    """Clean up any leftover .troshka-capture overlays from previous captures.
    Must be called BEFORE creating a new snapshot."""
    result = subprocess.run(
        ["virsh", "domblklist", domain, "--details"],
        capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        return
    for line in result.stdout.strip().split("\n"):
        parts = line.split()
        if len(parts) >= 4 and parts[1] == "disk" and ".troshka-capture" in parts[3]:
            target = parts[2]
            overlay = parts[3]
            _job_log(job, f"Found stale overlay on {target}, cleaning up...")
            # Abort any active block job first
            subprocess.run(["virsh", "blockjob", domain, target, "--abort"],
                           capture_output=True, text=True, timeout=30)
            # Wait for abort to complete
            for _ in range(60):
                info = subprocess.run(["virsh", "blockjob", domain, target, "--info"],
                                      capture_output=True, text=True, timeout=5)
                if info.returncode != 0 or "No current block job" in info.stderr:
                    break
                time.sleep(1)
            # Commit and pivot
            r = subprocess.run(
                ["virsh", "blockcommit", domain, target, "--active", "--pivot", "--wait"],
                capture_output=True, text=True, timeout=300)
            if r.returncode == 0:
                try:
                    os.remove(overlay)
                except OSError:
                    pass
                _job_log(job, f"Cleaned stale overlay for {target}")
            else:
                _job_log(job, f"Could not clean stale overlay for {target}: {r.stderr.strip()}")


def _snapshot_domain(job, domain):
    """Fstrim → clean stale overlays → freeze → snapshot → thaw.
    Total freeze time < 1 second.
    Returns True if snapshot created, False if failed (use -U fallback)."""
    try:
        r = subprocess.run(["virsh", "domfstrim", domain],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            _job_log(job, f"Trimmed free blocks: {domain}")
    except Exception:
        pass

    _cleanup_stale_snapshots(job, domain)

    frozen = False
    try:
        r = subprocess.run(["virsh", "domfsfreeze", domain],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            frozen = True
    except Exception:
        pass

    try:
        result = subprocess.run(
            ["virsh", "snapshot-create-as", domain, "--name", "troshka-capture",
             "--disk-only", "--atomic", "--no-metadata"],
            capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip())
    except Exception as e:
        _job_log(job, f"Snapshot failed ({e}), using crash-consistent mode")
        if frozen:
            subprocess.run(["virsh", "domfsthaw", domain],
                           capture_output=True, text=True, timeout=10)
        return False
    finally:
        if frozen:
            subprocess.run(["virsh", "domfsthaw", domain],
                           capture_output=True, text=True, timeout=10)

    _job_log(job, "Snapshot created, VM running on overlay (freeze < 1s)")
    return True


def _commit_snapshot(job, domain):
    """Block-commit overlays back to base, wait, pivot, delete overlay."""
    result = subprocess.run(
        ["virsh", "domblklist", domain, "--details"],
        capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        return
    for line in result.stdout.strip().split("\n"):
        parts = line.split()
        if len(parts) >= 4 and parts[1] == "disk" and ".troshka-capture" in parts[3]:
            target = parts[2]
            overlay = parts[3]
            _job_log(job, f"Committing overlay {target} back to base...")
            r = subprocess.run(
                ["virsh", "blockcommit", domain, target, "--active", "--pivot",
                 "--wait", "--verbose"],
                capture_output=True, text=True, timeout=3600)
            if r.returncode == 0:
                try:
                    os.remove(overlay)
                except OSError:
                    pass
                _job_log(job, f"Overlay committed and removed for {target}")
            else:
                _job_log(job, f"Block-commit failed for {target}: {r.stderr.strip()}")
                # Don't leave a broken state — abort and try once more
                subprocess.run(["virsh", "blockjob", domain, target, "--abort"],
                               capture_output=True, text=True, timeout=30)
                time.sleep(2)
                r2 = subprocess.run(
                    ["virsh", "blockcommit", domain, target, "--active", "--pivot", "--wait"],
                    capture_output=True, text=True, timeout=300)
                if r2.returncode == 0:
                    try:
                        os.remove(overlay)
                    except OSError:
                        pass
                    _job_log(job, f"Overlay committed on retry for {target}")
                else:
                    _job_log(job, f"WARNING: overlay stuck for {target}, needs manual cleanup")




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
    import tempfile as _tf

    running = False
    snapshotted = False
    if domain_name:
        running = _is_domain_running(domain_name)
        if running:
            snapshotted = _snapshot_domain(job, domain_name)

    result_disks = []
    try:
      for disk_info in disks:
        disk_path = _validate_path(disk_info["disk_path"])
        s3_url = disk_info["s3_url"]
        cache_path = _validate_path(disk_info["cache_path"])

        # After snapshot, disk_path is now read-only (snapshot overlay sits on top).
        # Flatten disk_path directly — qemu-img convert follows its backing chain.

        if not os.path.exists(disk_path):
            raise RuntimeError(f"Disk not found: {disk_path}")

        _local_tmp = os.path.join(_config.get("local_mount", "/var/lib/troshka/local"), "tmp")
        os.makedirs(_local_tmp, exist_ok=True)
        with _tf.TemporaryDirectory(dir=_local_tmp) as tmpdir:
            tmp_flat = os.path.join(tmpdir, "flat.qcow2")
            src_size = os.path.getsize(disk_path)
            src_size_gb = round(src_size / (1024**3), 1)
            _job_log(job,f"Flattening {os.path.basename(disk_path)} ({src_size_gb} GB)...")

            flatten_done = threading.Event()
            def _monitor_flatten():
                while not flatten_done.is_set():
                    try:
                        if os.path.exists(tmp_flat):
                            cur = os.path.getsize(tmp_flat)
                            cur_gb = round(cur / (1024**3), 1)
                            _job_log(job, f"Flattening: {cur_gb} of {src_size_gb} GB")
                    except OSError:
                        pass
                    flatten_done.wait(10)
            mon = threading.Thread(target=_monitor_flatten, daemon=True)
            mon.start()

            cmd = ["qemu-img", "convert", "-c", "-o", "compression_type=zstd", "-O", "qcow2"]
            if running and not snapshotted:
                cmd.insert(2, "-U")
            cmd.extend([disk_path, tmp_flat])
            _run_cmd(job, cmd, timeout=3600)
            flatten_done.set()

            flat_size = os.path.getsize(tmp_flat)
            flat_size_gb = round(flat_size / (1024**3), 1)
            _job_log(job, f"Flattened: {flat_size_gb} GB (compressed)")

            # Start overlay commit in background — safe because upload reads from local temp, not the VM disk
            commit_thread = None
            if snapshotted:
                def _do_commit():
                    _commit_snapshot(job, domain_name)
                commit_thread = threading.Thread(target=_do_commit, daemon=True)
                commit_thread.start()
                snapshotted = False

            _job_log(job, f"Uploading {flat_size_gb} GB to S3...")
            _s3_upload(job, tmp_flat, s3_url, aws_access_key, aws_secret_key, aws_region)

            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            _job_log(job, f"Caching to {cache_path}...")
            shutil.copy(tmp_flat, cache_path)

            if commit_thread:
                commit_thread.join(timeout=600)

        size_bytes = os.path.getsize(cache_path)
        result_disks.append({"size_bytes": size_bytes})
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
        capture_output=True, text=True, timeout=10,
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
    _run_cmd(job, ["qemu-img", "convert", "-O", "qcow2", disk_path, output_path], timeout=3600)

    return {"domain": domain, "output_path": output_path, "status": "exported"}

COMMAND_HANDLERS["patterns/export"] = _handle_pattern_export


# ── Host handlers ──

@route("GET", "/host/disk-usage")
def handle_disk_usage(handler, params):
    """Return disk usage stats for all mounted partitions."""
    handler._send_json(200, {"partitions": _get_partitions()})


@route("GET", "/vms/states")
def handle_vm_states(handler, params):
    """Return all troshka-* domain states in one call."""
    global _libvirt_events_available
    if _libvirt_events_available:
        with _vm_state_cache_lock:
            domains = {name: {"state": info["state"]} for name, info in _vm_state_cache.items()}
        handler._send_json(200, {"domains": domains, "source": "events"})
        return
    domains = {}
    try:
        result = subprocess.run(["virsh", "list", "--all", "--name"],
                                capture_output=True, text=True, timeout=10)
        for name in result.stdout.strip().split("\n"):
            name = name.strip()
            if not name or not name.startswith("troshka-"):
                continue
            st = subprocess.run(["virsh", "domstate", name],
                                capture_output=True, text=True, timeout=5)
            if st.returncode == 0:
                raw = st.stdout.strip().lower().replace(" ", "_")
                state_map = {"running": "running", "shut_off": "shut_off", "paused": "paused",
                             "in_shutdown": "shutting_down", "crashed": "crashed",
                             "pmsuspended": "suspended", "idle": "unknown"}
                domains[name] = {"state": state_map.get(raw, raw)}
    except Exception as e:
        logger.warning("Failed to list VM states: %s", e)
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
        with _vm_events_lock:
            _vm_events.append({"domain": name, "state": state, "timestamp": now})
            while len(_vm_events) > 500:
                _vm_events.pop(0)

    def _block_threshold_cb(conn, dom, dev, path, threshold, opaque):
        name = dom.name()
        if not name.startswith("troshka-"):
            return
        now = time.time()
        event = {
            "type": "block_threshold",
            "domain": name,
            "disk": dev,
            "threshold_bytes": threshold,
            "timestamp": now,
        }
        with _vm_events_lock:
            _vm_events.append(event)
            while len(_vm_events) > 500:
                _vm_events.pop(0)
        logger.warning("Block threshold exceeded: %s disk %s", name, dev)

        # Re-arm at next increment (80% → 90%)
        try:
            info = dom.blockInfo(dev)
            if info:
                capacity = info[0]
                new_threshold = int(capacity * 0.9)
                if new_threshold > threshold:
                    dom.setBlockThreshold(dev, new_threshold)
        except Exception:
            pass

    def _event_loop():
        while True:
            try:
                _lv.virEventRunDefaultImpl()
            except Exception:
                time.sleep(1)

    def _seed_cache(conn):
        """Populate cache with current states on startup."""
        try:
            for dom in conn.listAllDomains():
                name = dom.name()
                if not name.startswith("troshka-"):
                    continue
                info = dom.info()
                state_map = {
                    _lv.VIR_DOMAIN_RUNNING: "running",
                    _lv.VIR_DOMAIN_PAUSED: "paused",
                    _lv.VIR_DOMAIN_SHUTDOWN: "shutting_down",
                    _lv.VIR_DOMAIN_SHUTOFF: "shut_off",
                    _lv.VIR_DOMAIN_CRASHED: "crashed",
                    _lv.VIR_DOMAIN_PMSUSPENDED: "suspended",
                }
                with _vm_state_cache_lock:
                    _vm_state_cache[name] = {"state": state_map.get(info[0], "unknown"), "since": time.time()}
        except Exception as e:
            logger.warning("Failed to seed VM state cache: %s", e)

    try:
        _lv.virEventRegisterDefaultImpl()
        conn = _lv.open("qemu:///system")
        if conn is None:
            logger.warning("Failed to open libvirt connection for events")
            return
        conn.domainEventRegisterAny(None, _lv.VIR_DOMAIN_EVENT_ID_LIFECYCLE, _lifecycle_cb, None)
        conn.domainEventRegisterAny(None, _lv.VIR_DOMAIN_EVENT_ID_BLOCK_THRESHOLD, _block_threshold_cb, None)
        conn.setKeepAlive(5, 3)
        _seed_cache(conn)
        threading.Thread(target=_event_loop, daemon=True, name="libvirt-events").start()
        _libvirt_events_available = True
        logger.info("Libvirt event loop started (%d domains cached)", len(_vm_state_cache))
    except Exception as e:
        logger.warning("Failed to start libvirt event loop: %s", e)


def _handle_resize_storage(job, params):
    """Resize /var/lib/troshka filesystem using xfs_growfs."""
    _run_cmd(job, ["xfs_growfs", "/var/lib/troshka"], timeout=120)
    return {"status": "resized"}

COMMAND_HANDLERS["host/resize-storage"] = _handle_resize_storage


def _handle_files_remove(job, params):
    """Remove files or directories under /var/lib/troshka."""
    paths = params.get("paths", [])
    if not paths:
        raise ValueError("Missing required parameter: paths")

    removed = 0
    for path in paths:
        validated_path = _validate_path(path)
        try:
            if os.path.isdir(validated_path):
                shutil.rmtree(validated_path)
                _job_log(job,f"Removed directory: {validated_path}")
            else:
                os.remove(validated_path)
                _job_log(job,f"Removed file: {validated_path}")
            removed += 1
        except FileNotFoundError:
            _job_log(job,f"Skipped (not found): {validated_path}")
        except PermissionError:
            try:
                subprocess.run(["sudo", "-u", "qemu", "rm", "-rf", "--", validated_path], timeout=10, check=True)
                _job_log(job,f"Removed as qemu: {validated_path}")
                removed += 1
            except Exception as e2:
                _job_log(job,f"Failed to remove {validated_path}: {e2}")
                raise
        except Exception as e:
            _job_log(job,f"Failed to remove {validated_path}: {e}")
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

def _drain_and_update(script_path, new_path, force):
    """Background thread: drain jobs, then update and restart."""
    global _draining
    _draining = True
    _drain_cancel.clear()
    logger.info("Drain started, force=%s", force)

    # Short-lived polling jobs that should never block an update
    _SKIP_DRAIN = {"vms/state", "host/disk-usage", "gc/discover"}
    if not force:
        start = time.time()
        while time.time() - start < 10:
            with _jobs_lock:
                blocking = [j for j in _jobs.values()
                            if j["status"] == "running"
                            and j.get("command", "") not in _SKIP_DRAIN]
            if not blocking:
                break
            if _drain_cancel.is_set():
                logger.info("Drain cancelled — new work arrived")
                _draining = False
                try:
                    os.remove(new_path)
                except OSError:
                    pass
                return
            logger.info("Drain waiting on %d job(s): %s",
                        len(blocking), ", ".join(j.get("command", "?") for j in blocking))
            time.sleep(1)

    if _drain_cancel.is_set():
        logger.info("Drain cancelled before restart")
        _draining = False
        return

    with _jobs_lock:
        for job in _jobs.values():
            if job["status"] == "running" and job.get("_process"):
                try:
                    job["_process"].terminate()
                except Exception as e:
                    logger.warning("Failed to terminate job %s: %s", job["job_id"], e)

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
    proc = subprocess.run(["nft", "list", "ruleset"], capture_output=True, text=True, timeout=5)
    if proc.returncode != 0:
        return {"flushed_chains": 0, "error": "nft list failed"}

    # First pass: flush base chains (removes jump rules to troshka chains)
    for table_chain in [("filter", "forward"), ("nat", "postrouting"), ("nat", "prerouting")]:
        try:
            _run_cmd(job, ["nft", "flush", "chain", "inet", table_chain[0], table_chain[1]], timeout=5)
        except RuntimeError:
            pass

    # Second pass: find and delete all troshka-* chains
    for line in proc.stdout.split("\n"):
        line = line.strip()
        if line.startswith("chain troshka-"):
            chain_name = line.split()[1]
            # Determine which table this chain is in
            table_type = "nat" if ("post" in chain_name or "pre" in chain_name) else "filter"
            try:
                _run_cmd(job, ["nft", "flush", "chain", "inet", table_type, chain_name], timeout=5)
                _run_cmd(job, ["nft", "delete", "chain", "inet", table_type, chain_name], timeout=5)
                flushed += 1
                _job_log(job,f"Deleted chain {table_type}/{chain_name}")
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


# ── Server factory ──

_start_time = time.time()


def create_server(config):
    """Create and return an HTTPS server (does not start serving)."""
    server = ThreadingHTTPServer(("0.0.0.0", config["port"]), TroshkadHandler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(config["tls_cert"], config["tls_key"])
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    return server


# ── Main ──

def main():
    global _config, _start_time
    conf_path = sys.argv[1] if len(sys.argv) > 1 else "/opt/troshka/troshkad.conf"
    _config = load_config(conf_path)
    _start_time = time.time()

    cleanup_thread = threading.Thread(target=_job_cleanup_loop, daemon=True)
    cleanup_thread.start()

    # Restore services from previous deploy
    _restore_bmc_services()
    _restore_dnsmasq()

    # Watchdog: check dnsmasq every 30s, restart if dead
    dnsmasq_watchdog = threading.Thread(target=_dnsmasq_watchdog_loop, daemon=True)
    dnsmasq_watchdog.start()

    _start_libvirt_event_loop()

    server = create_server(_config)
    logger.info("troshkad %s listening on port %d", VERSION, _config["port"])

    def shutdown(signum, frame):
        logger.info("Received signal %d, shutting down", signum)
        # Kill all running job subprocesses immediately
        with _jobs_lock:
            for job in _jobs.values():
                if job["status"] == "running" and job.get("_process"):
                    try:
                        job["_process"].kill()
                        logger.info("Killed job %s subprocess", job["job_id"])
                    except Exception:
                        pass
        # Shutdown server in a thread with a hard timeout
        def _do_shutdown():
            server.shutdown()
        t = threading.Thread(target=_do_shutdown, daemon=True)
        t.start()
        t.join(timeout=5)
        if t.is_alive():
            logger.warning("Server shutdown timed out after 5s, forcing exit")
            os._exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        logger.info("troshkad stopped")


def _check_and_restart_dnsmasq():
    """Check all dnsmasq PID files — restart any that died."""
    restarted = 0
    for pidfile in glob.glob("/run/troshka-dnsmasq-*.pid"):
        # PID file: troshka-dnsmasq-{project_id[:8]}-{vni}.pid
        conf_name = os.path.basename(pidfile).replace("troshka-dnsmasq-", "troshka-").replace(".pid", ".conf")
        conf_path = f"/etc/dnsmasq.d/{conf_name}"
        if not os.path.exists(conf_path):
            # Orphan PID file — config gone, remove it
            try:
                os.remove(pidfile)
            except OSError:
                pass
            continue
        # Check if any VM domains exist for this project
        parts = os.path.basename(pidfile).replace("troshka-dnsmasq-", "").replace(".pid", "").split("-")
        project_prefix = parts[0] if parts else ""
        if project_prefix:
            domain_check = subprocess.run(
                ["virsh", "list", "--all", "--name"],
                capture_output=True, text=True, timeout=5)
            has_domains = any(f"troshka-{project_prefix}-" in line for line in domain_check.stdout.split("\n"))
            ns_check = subprocess.run(["ip", "netns", "list"], capture_output=True, text=True, timeout=5)
            has_namespace = f"troshka-{project_prefix}" in ns_check.stdout
            if not has_domains and not has_namespace:
                try:
                    os.remove(pidfile)
                    os.remove(conf_path)
                    for lf in glob.glob(f"/var/lib/troshka/dnsmasq-{project_prefix}-*.leases"):
                        os.remove(lf)
                    logger.info("Cleaned orphan dnsmasq files for deleted project %s", project_prefix)
                except OSError:
                    pass
                continue
        alive = False
        try:
            with open(pidfile) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)
            alive = True
        except (FileNotFoundError, ValueError, ProcessLookupError, OSError):
            pass
        if not alive:
            # Try to find out why it died
            try:
                with open(pidfile) as f:
                    dead_pid = int(f.read().strip())
                # Check /proc for exit info if recently dead
                logger.warning("dnsmasq PID %d from %s is dead — restarting", dead_pid, os.path.basename(pidfile))
            except (FileNotFoundError, ValueError):
                logger.warning("dnsmasq PID file %s missing or corrupt — restarting", os.path.basename(pidfile))
            # Find the namespace from the config file
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
            if not ns_name:
                continue
            # Verify namespace exists
            ns_check = subprocess.run(["ip", "netns", "list"], capture_output=True, text=True, timeout=5)
            if ns_name not in ns_check.stdout:
                continue
            try:
                subprocess.run(
                    ["ip", "netns", "exec", ns_name, "dnsmasq", f"--conf-file={conf_path}"],
                    capture_output=True, timeout=10,
                )
                # Read the new PID and set an audit watch on it
                try:
                    with open(pidfile.replace(".conf", ".pid").replace("/etc/dnsmasq.d/", "/run/")) as pf:
                        new_pid = pf.read().strip()
                    subprocess.run(
                        ["auditctl", "-a", "exit,always", "-F", "arch=b64", "-S", "kill",
                         "-F", f"a0={new_pid}", "-k", "dnsmasq-kill"],
                        capture_output=True, timeout=5,
                    )
                except Exception:
                    pass
                logger.info("dnsmasq restored for %s", conf_name)
                restarted += 1
            except Exception as e:
                logger.warning("Failed to restart dnsmasq %s: %s", conf_name, e)
    return restarted


def _restore_dnsmasq():
    """Restore dnsmasq for all active namespaces on troshkad startup."""
    restarted = _check_and_restart_dnsmasq()
    if restarted:
        logger.info("dnsmasq restore: restarted %d instance(s)", restarted)


def _dnsmasq_watchdog_loop():
    """Periodically check dnsmasq is alive, restart if not."""
    while True:
        time.sleep(5)
        try:
            _check_and_restart_dnsmasq()
        except Exception as e:
            logger.warning("dnsmasq watchdog error: %s", e)


def _restore_bmc_services():
    """Restart BMC services (sushy-emulator, vbmcd) from existing configs on troshkad startup."""
    bmc_base = "/var/lib/troshka/bmc"
    venv_bin = "/opt/troshka/venv/bin"
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
            subprocess.run(["ip", "netns", "exec", ns, "true"], capture_output=True, timeout=5, check=True)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
            logger.info("BMC restore: namespace %s not found, skipping %s", ns, project_dir[:8])
            continue

        # Restart sushy-emulator processes
        for fname in os.listdir(bmc_dir):
            if fname.startswith("sushy-") and fname.endswith(".conf"):
                conf_path = os.path.join(bmc_dir, fname)
                pid_path = conf_path.replace(".conf", ".pid")
                # Kill stale process if any
                if os.path.exists(pid_path):
                    try:
                        with open(pid_path) as f:
                            old_pid = int(f.read().strip())
                        os.kill(old_pid, signal.SIGTERM)
                    except (ValueError, ProcessLookupError, PermissionError):
                        pass
                proc = subprocess.Popen(
                    ["ip", "netns", "exec", ns, f"{venv_bin}/sushy-emulator", "--config", conf_path],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                with open(pid_path, "w") as f:
                    f.write(str(proc.pid))
                logger.info("BMC restore: sushy-emulator started for %s (PID %d)", fname, proc.pid)

        # Restart vbmcd
        vbmcd_conf = os.path.join(bmc_dir, "virtualbmc.conf")
        vbmcd_pid_path = os.path.join(bmc_dir, "vbmcd.pid")
        if os.path.exists(vbmcd_conf):
            # Kill stale vbmcd
            if os.path.exists(vbmcd_pid_path):
                try:
                    with open(vbmcd_pid_path) as f:
                        old_pid = int(f.read().strip())
                    os.kill(old_pid, signal.SIGTERM)
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

            env = os.environ.copy()
            env["VIRTUALBMC_CONFIG"] = vbmcd_conf
            subprocess.Popen(
                ["ip", "netns", "exec", ns, f"{venv_bin}/vbmcd"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=env, start_new_session=True,
            )
            # Wait for vbmcd to write its PID file
            for _ in range(20):
                time.sleep(0.5)
                if os.path.exists(vbmcd_pid_path):
                    break
            logger.info("BMC restore: vbmcd started for %s", project_dir[:8])

            # Re-register vbmc entries from the config dir
            vbmcd_conf_dir = os.path.join(bmc_dir, "vbmcd")
            if os.path.isdir(vbmcd_conf_dir):
                for entry in os.listdir(vbmcd_conf_dir):
                    entry_path = os.path.join(vbmcd_conf_dir, entry)
                    if os.path.isdir(entry_path) and entry.startswith("troshka-"):
                        try:
                            subprocess.run(
                                ["ip", "netns", "exec", ns, f"{venv_bin}/vbmc", "start", entry],
                                capture_output=True, text=True, env=env, timeout=10,
                            )
                            logger.info("BMC restore: vbmc started %s", entry)
                        except (subprocess.TimeoutExpired, Exception):
                            logger.warning("BMC restore: failed to start vbmc %s", entry)

    logger.info("BMC restore complete")


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
        _job_log(job,"No BMC-enabled VMs, skipping")
        return {"status": "skipped"}

    pid = project_id[:8]
    ns = f"troshka-{pid}"
    bridge = f"br-bmc-{pid}"
    prefix = bmc_cidr.split("/")[1] if "/" in bmc_cidr else "24"
    bmc_dir = f"/var/lib/troshka/bmc/{project_id}"
    venv_bin = "/opt/troshka/venv/bin"

    os.makedirs(bmc_dir, exist_ok=True)

    # 1. Create BMC bridge inside namespace
    try:
        _run_cmd(job, ["ip", "netns", "exec", ns, "ip", "link", "del", bridge], timeout=10)
    except RuntimeError:
        pass
    _run_cmd(job, ["ip", "netns", "exec", ns, "ip", "link", "add", bridge, "type", "bridge"], timeout=10)
    _run_cmd(job, ["ip", "netns", "exec", ns, "ip", "addr", "add",
                    f"{bmc_gateway_ip}/{prefix}", "dev", bridge], timeout=10)
    _run_cmd(job, ["ip", "netns", "exec", ns, "ip", "link", "set", bridge, "up"], timeout=10)

    # Dummy bridge in host namespace for libvirt validation
    try:
        subprocess.run(["ip", "link", "show", bridge], capture_output=True, check=True, timeout=5)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        _run_cmd(job, ["ip", "link", "add", bridge, "type", "bridge"], timeout=10)
    _run_cmd(job, ["ip", "link", "set", bridge, "up"], timeout=10)

    _job_log(job,f"BMC bridge {bridge} created in namespace {ns}")

    # 2. Assign BMC IPs to the bridge
    for vm in vms:
        bmc_ip = _validate_ip(vm["bmc_ip"])
        _run_cmd(job, ["ip", "netns", "exec", ns, "ip", "addr", "add",
                        f"{bmc_ip}/{prefix}", "dev", bridge], timeout=10)

    # 3. Create htpasswd file for sushy basic auth (bcrypt format required by sushy-tools)
    htpasswd_path = os.path.join(bmc_dir, "htpasswd")
    bcrypt_hash = subprocess.run(
        [f"{venv_bin}/python3", "-c",
         f"import bcrypt; print(bcrypt.hashpw({bmc_password!r}.encode(), bcrypt.gensalt()).decode())"],
        capture_output=True, text=True, timeout=10,
    ).stdout.strip()
    with open(htpasswd_path, "w") as f:
        f.write(f"{bmc_username}:{bcrypt_hash}\n")

    # 4. Create per-project libvirt storage pool for virtual media
    vmedia_dir = os.path.join(bmc_dir, "vmedia")
    os.makedirs(vmedia_dir, exist_ok=True)
    pool_name = f"troshka-vmedia-{pid}"
    # Remove existing pool if any
    subprocess.run(["virsh", "pool-destroy", pool_name], capture_output=True, timeout=10)
    subprocess.run(["virsh", "pool-undefine", pool_name], capture_output=True, timeout=10)
    subprocess.run(["virsh", "pool-define-as", pool_name, "dir", "--target", vmedia_dir],
                   capture_output=True, timeout=10)
    subprocess.run(["virsh", "pool-start", pool_name], capture_output=True, timeout=10)
    subprocess.run(["virsh", "pool-autostart", pool_name], capture_output=True, timeout=10)
    _job_log(job,f"Storage pool {pool_name} created at {vmedia_dir}")

    # 5. Start sushy-emulator per VM
    for vm in vms:
        domain_name = _validate_domain_name(vm["domain_name"])
        bmc_ip = _validate_ip(vm["bmc_ip"])
        vm_short = domain_name.split("-")[-1] if "-" in domain_name else domain_name[:8]

        # Get libvirt UUID for ALLOWED_INSTANCES (sushy uses UUIDs, not names)
        dom_uuid = ""
        try:
            result = subprocess.run(["virsh", "domuuid", domain_name],
                                    capture_output=True, text=True, timeout=5)
            dom_uuid = result.stdout.strip()
        except Exception:
            pass

        conf_path = os.path.join(bmc_dir, f"sushy-{vm_short}.conf")
        with open(conf_path, "w") as f:
            f.write(f"SUSHY_EMULATOR_LISTEN_IP = '{bmc_ip}'\n")
            f.write("SUSHY_EMULATOR_LISTEN_PORT = 8000\n")
            f.write("SUSHY_EMULATOR_LIBVIRT_URI = 'qemu:///system'\n")
            f.write("SUSHY_EMULATOR_FEATURE_SET = 'vmedia'\n")
            f.write("SUSHY_EMULATOR_IGNORE_BOOT_DEVICE = False\n")
            f.write("SUSHY_EMULATOR_VMEDIA_VERIFY_SSL = False\n")
            f.write(f"SUSHY_EMULATOR_STORAGE_POOL = '{pool_name}'\n")
            f.write(f"SUSHY_EMULATOR_AUTH_FILE = '{htpasswd_path}'\n")
            if dom_uuid:
                f.write(f"SUSHY_EMULATOR_ALLOWED_INSTANCES = ['{dom_uuid}']\n")

        pid_path = os.path.join(bmc_dir, f"sushy-{vm_short}.pid")

        if os.path.exists(pid_path):
            try:
                with open(pid_path) as f:
                    old_pid = int(f.read().strip())
                os.kill(old_pid, signal.SIGTERM)
            except (ValueError, ProcessLookupError, PermissionError):
                pass

        proc = subprocess.Popen(
            ["ip", "netns", "exec", ns, f"{venv_bin}/sushy-emulator", "--config", conf_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        with open(pid_path, "w") as f:
            f.write(str(proc.pid))

        _job_log(job,f"sushy-emulator started for {domain_name} at {bmc_ip}:8000 (PID {proc.pid})")

    # 5. Start vbmcd and register VMs for IPMI
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

    vbmcd_pid_path = os.path.join(bmc_dir, "vbmcd.pid")
    if os.path.exists(vbmcd_pid_path):
        try:
            with open(vbmcd_pid_path) as f:
                old_pid = int(f.read().strip())
            os.kill(old_pid, signal.SIGTERM)
            # Wait for process to exit before removing PID file
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

    env = os.environ.copy()
    env["VIRTUALBMC_CONFIG"] = vbmcd_conf_path
    proc = subprocess.Popen(
        ["ip", "netns", "exec", ns, f"{venv_bin}/vbmcd"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=env, start_new_session=True,
    )
    # Don't write PID file — vbmcd manages its own via pid_file in config.
    # Wait for vbmcd to be ready (writes its PID file and opens ZMQ port).
    for _ in range(20):
        time.sleep(0.5)
        if os.path.exists(vbmcd_pid_path):
            break

    _job_log(job,f"vbmcd started (wrapper PID {proc.pid})")

    for vm in vms:
        domain_name = _validate_domain_name(vm["domain_name"])
        bmc_ip = _validate_ip(vm["bmc_ip"])

        _run_cmd(job, ["ip", "netns", "exec", ns, f"{venv_bin}/vbmc", "add", domain_name,
                        "--port", "623", "--address", bmc_ip,
                        "--username", bmc_username, "--password", bmc_password,
                        "--libvirt-uri", "qemu:///system"], timeout=30)
        _run_cmd(job, ["ip", "netns", "exec", ns, f"{venv_bin}/vbmc", "start", domain_name], timeout=30)
        _job_log(job,f"vbmc registered {domain_name} at {bmc_ip}:623")

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
        _run_cmd(job, ["ip", "netns", "exec", ns, "ip", "link", "del", bridge], timeout=10)
    except RuntimeError:
        pass
    _run_cmd(job, ["ip", "netns", "exec", ns, "ip", "link", "add", bridge, "type", "bridge"], timeout=10)
    _run_cmd(job, ["ip", "netns", "exec", ns, "ip", "addr", "add",
                    f"{bmc_gateway_ip}/{prefix}", "dev", bridge], timeout=10)
    _run_cmd(job, ["ip", "netns", "exec", ns, "ip", "link", "set", bridge, "up"], timeout=10)

    # Dummy bridge in host namespace for libvirt validation
    try:
        subprocess.run(["ip", "link", "show", bridge], capture_output=True, check=True, timeout=5)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        _run_cmd(job, ["ip", "link", "add", bridge, "type", "bridge"], timeout=10)
    _run_cmd(job, ["ip", "link", "set", bridge, "up"], timeout=10)

    # Assign BMC IPs to the bridge
    for vm in params.get("vms", []):
        bmc_ip = _validate_ip(vm["bmc_ip"])
        _run_cmd(job, ["ip", "netns", "exec", ns, "ip", "addr", "add",
                        f"{bmc_ip}/{prefix}", "dev", bridge], timeout=10)

    _job_log(job,f"BMC bridge {bridge} created (services not started)")
    return {"status": "ok", "bridge": bridge}

COMMAND_HANDLERS["bmc/create-bridge"] = _handle_bmc_create_bridge


def _handle_bmc_teardown(job, params):
    """Tear down all BMC endpoints for a project."""
    project_id = _validate_project_id(params["project_id"])
    pid = project_id[:8]
    ns = f"troshka-{pid}"
    bridge = f"br-bmc-{pid}"
    bmc_dir = f"/var/lib/troshka/bmc/{project_id}"
    venv_bin = "/opt/troshka/venv/bin"

    killed = 0

    if os.path.isdir(bmc_dir):
        for fname in os.listdir(bmc_dir):
            if fname.startswith("sushy-") and fname.endswith(".pid"):
                pid_path = os.path.join(bmc_dir, fname)
                try:
                    with open(pid_path) as f:
                        p = int(f.read().strip())
                    os.kill(p, signal.SIGTERM)
                    killed += 1
                    _job_log(job,f"Killed sushy-emulator PID {p}")
                except (ValueError, ProcessLookupError, PermissionError):
                    pass

    # Kill vbmcd directly — all vbmc entries die with it, no need for graceful stop
    vbmcd_pid_path = os.path.join(bmc_dir, "vbmcd.pid")
    if os.path.exists(vbmcd_pid_path):
        try:
            with open(vbmcd_pid_path) as f:
                p = int(f.read().strip())
            os.kill(p, signal.SIGTERM)
            killed += 1
            _job_log(job,f"Killed vbmcd PID {p}")
        except (ValueError, ProcessLookupError, PermissionError):
            pass

    try:
        _run_cmd(job, ["ip", "netns", "exec", ns, "ip", "link", "del", bridge], timeout=10)
        _job_log(job,f"Removed BMC bridge {bridge} from namespace")
    except RuntimeError:
        pass

    try:
        _run_cmd(job, ["ip", "link", "del", bridge], timeout=10)
    except RuntimeError:
        pass

    # Destroy libvirt storage pool for virtual media
    pool_name = f"troshka-vmedia-{pid}"
    subprocess.run(["virsh", "pool-destroy", pool_name], capture_output=True, timeout=10)
    subprocess.run(["virsh", "pool-undefine", pool_name], capture_output=True, timeout=10)
    _job_log(job,f"Removed storage pool {pool_name}")

    if os.path.isdir(bmc_dir):
        shutil.rmtree(bmc_dir, ignore_errors=True)
        _job_log(job,f"Removed BMC config dir: {bmc_dir}")

    return {"status": "ok", "killed": killed}

COMMAND_HANDLERS["bmc/teardown"] = _handle_bmc_teardown


def _handle_bmc_status(job, params):
    """Check status of BMC processes for a project."""
    project_id = _validate_project_id(params["project_id"])
    bmc_dir = f"/var/lib/troshka/bmc/{project_id}"

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
                result["sushy_processes"].append({"pid": p, "file": fname, "alive": True})
            except (ValueError, ProcessLookupError, PermissionError, FileNotFoundError):
                result["sushy_processes"].append({"file": fname, "alive": False})

    vbmcd_pid_path = os.path.join(bmc_dir, "vbmcd.pid")
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
    state_proc = subprocess.run(["virsh", "domstate", domain], capture_output=True, text=True, timeout=10)
    state = state_proc.stdout.strip()
    _job_log(job,f"VM state: {state}")

    cmd = [
        "virsh", "migrate",
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


def _handle_vm_serial_exec(job, params):
    """Execute a command on a VM via serial console using pexpect fdspawn on the raw PTY."""
    domain = _validate_domain_name(params["domain_name"])
    username = params.get("username", "root")
    password = params.get("password", "")
    command = params.get("command", "")
    timeout_secs = min(params.get("timeout", 10), 60)

    if not command:
        raise RuntimeError("No command specified")

    import re
    result = subprocess.run(
        ["virsh", "dumpxml", domain],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Cannot get XML for {domain}: {result.stderr}")
    pty_match = re.search(r"source path='(/dev/pts/\d+)'", result.stdout)
    if not pty_match:
        raise RuntimeError(f"No serial console PTY found for {domain}")
    pty_path = pty_match.group(1)

    for sp in ["/opt/troshka/venv/lib/python3.12/site-packages",
               "/opt/troshka/venv/lib/python3.13/site-packages"]:
        if sp not in sys.path and os.path.isdir(sp):
            sys.path.insert(0, sp)
    from pexpect import fdpexpect, TIMEOUT, EOF

    fd = os.open(pty_path, os.O_RDWR)
    child = fdpexpect.fdspawn(fd, encoding="utf-8", timeout=timeout_secs)

    SHELL = r'[#\$] '
    ANY_PROMPT = ["login:", SHELL, r"[>%] ", TIMEOUT]

    def _login():
        if not password:
            raise RuntimeError("VM is at login prompt but no password provided")
        child.send(username + "\r")
        child.expect("[Pp]assword:", timeout=5)
        child.send(password + "\r")
        idx = child.expect([SHELL, "Last login", "incorrect", TIMEOUT], timeout=10)
        if idx == 1:
            child.expect(SHELL, timeout=5)
        elif idx != 0:
            raise RuntimeError("Login failed")

    try:
        # Always restore echo first in case previous call left it off
        child.send("stty echo 2>/dev/null\r")
        time.sleep(0.3)

        # Poke console
        child.send("\x03\r")
        time.sleep(0.5)
        idx = child.expect(ANY_PROMPT, timeout=3)
        if idx == 0:
            _login()
        elif idx == 3:
            child.send("\r")
            idx2 = child.expect(ANY_PROMPT, timeout=3)
            if idx2 == 0:
                _login()
            elif idx2 == 3:
                return {"domain": domain, "output": "", "error": "Console not responding"}

        # Run command via temp file, read back with split marker
        import random
        rid = random.randint(10000, 99999)
        outf = f"/tmp/.t{rid}"
        # Marker constructed from two variables so it never appears literally in the echo
        marker = f"XDONE{rid}X"
        child.send(f"__a=XDONE; __b={rid}X; ({command}) > {outf} 2>&1; cat {outf}; rm -f {outf}; echo $__a$__b; unset __a __b\r")
        child.expect(marker, timeout=timeout_secs)
        raw = child.before or ""

        # Strip ANSI escapes and clean
        raw = re.sub(r'\x1b\[[0-9;?]*[a-zA-Z]|\x1b\].*?\x07', '', raw)
        raw = raw.replace('\r\n', '\n').replace('\r', '')
        out_lines = []
        for l in raw.split("\n"):
            clean = l.strip()
            if not clean:
                continue
            # Skip echoed command line and marker artifacts
            if "__a=" in clean or "__b=" in clean or f"cat {outf}" in clean or marker in clean:
                continue
            # Strip custom prompt prefix if present
            if re.match(r'^\S+[>#\$%]\s', clean):
                clean = re.sub(r'^\S+[>#\$%]\s+', '', clean).strip()
            if clean:
                out_lines.append(clean)

        return {"domain": domain, "output": "\n".join(out_lines)}
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
    command = params.get("command", "")
    timeout_secs = min(params.get("timeout", 10), 60)

    if not command:
        raise RuntimeError("No command specified")
    if not vm_ip:
        raise RuntimeError("No VM IP specified")
    if not password:
        raise RuntimeError("No password specified")

    ns = f"troshka-{project_id[:8]}" if project_id else ""
    ns_prefix = ["ip", "netns", "exec", ns] if ns else []

    result = subprocess.run(
        ns_prefix + [
            "sshpass", "-p", password,
            "ssh", "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
            "-o", f"ConnectTimeout={min(timeout_secs, 10)}",
            f"{username}@{vm_ip}",
            command,
        ],
        capture_output=True, text=True, timeout=timeout_secs + 5,
    )
    output = result.stdout.strip()
    error = result.stderr.strip() if result.returncode != 0 else ""
    return {"output": output, "error": error, "exit_code": result.returncode}

COMMAND_HANDLERS["vm/ssh-exec"] = _handle_vm_ssh_exec


if __name__ == "__main__":
    main()
