# src/troshkad/troshkad.py
"""troshkad — Troshka host agent daemon.

Single-file Python daemon managing QEMU/libvirt on the host.
Exposes a structured HTTPS REST API for the Troshka backend.
Requires only Python 3.9+ stdlib — no pip dependencies.
"""
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

VERSION = "2026.06.08.1"

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

    max_jobs = _config.get("max_concurrent_jobs", 10)
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
_BRIDGE_RE = re.compile(r"^br-troshka-[a-f0-9]+$")
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
    if not normalized.startswith("/var/lib/troshka/"):
        raise ValueError(f"Path must be under /var/lib/troshka/: {path}")
    if os.path.exists(normalized):
        real = os.path.realpath(normalized)
        if not real.startswith("/var/lib/troshka/"):
            raise ValueError(f"Path resolves outside /var/lib/troshka/: {path}")
        return real
    return normalized


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


def _run_cmd(job, cmd, timeout=600):
    """Run a subprocess command, appending output to job. Stores process handle in job for drain."""
    job["output"].append(f"$ {' '.join(cmd)}")
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
        job["output"].extend(stdout.strip().split("\n"))
    if stderr:
        job["output"].extend(stderr.strip().split("\n"))
    if proc.returncode != 0:
        raise RuntimeError(f"Command failed (exit {proc.returncode}): {' '.join(cmd)}")
    return proc


# ── VM handlers ──

def _handle_vm_create(job, params):
    domain = _validate_domain_name(params["domain_name"])
    vcpus = int(params["vcpus"])
    ram_mb = int(params["ram_mb"])
    disks = params.get("disks", [])
    networks = params.get("networks", [])
    seed_iso = params.get("seed_iso")

    cmd = [
        "virt-install",
        "--name", domain,
        "--vcpus", str(vcpus),
        "--memory", str(ram_mb),
        "--os-variant", "generic",
        "--noautoconsole",
        "--noreboot",
        "--import",
    ]
    for disk in disks:
        path = _validate_path(disk["path"])
        bus = _validate_bus(disk.get("bus", "virtio"))
        cmd.extend(["--disk", f"path={path},bus={bus}"])
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
    _run_cmd(job, cmd, timeout=600)
    return {"domain": domain, "status": "created"}

COMMAND_HANDLERS["vms/create"] = _handle_vm_create


def _handle_vm_destroy(job, params):
    domain = _validate_domain_name(params["domain_name"])
    # Destroy (force stop) — may fail if already stopped, that's OK
    try:
        _run_cmd(job, ["virsh", "destroy", domain], timeout=30)
    except RuntimeError:
        job["output"].append("Domain may already be stopped, continuing with undefine")
    _run_cmd(job, ["virsh", "undefine", domain, "--nvram", "--remove-all-storage"], timeout=30)
    return {"domain": domain, "status": "destroyed"}

COMMAND_HANDLERS["vms/destroy"] = _handle_vm_destroy


def _handle_vm_start(job, params):
    domain = _validate_domain_name(params["domain_name"])
    _run_cmd(job, ["virsh", "start", domain], timeout=60)
    return {"domain": domain, "status": "started"}

COMMAND_HANDLERS["vms/start"] = _handle_vm_start


def _handle_vm_stop(job, params):
    domain = _validate_domain_name(params["domain_name"])
    _run_cmd(job, ["virsh", "shutdown", domain], timeout=60)
    return {"domain": domain, "status": "stopped"}

COMMAND_HANDLERS["vms/stop"] = _handle_vm_stop


def _handle_vm_reboot(job, params):
    domain = _validate_domain_name(params["domain_name"])
    _run_cmd(job, ["virsh", "reboot", domain], timeout=60)
    return {"domain": domain, "status": "rebooted"}

COMMAND_HANDLERS["vms/reboot"] = _handle_vm_reboot


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
        cmd.extend(["-b", backing, "-F", fmt])
    cmd.extend([path, f"{size_gb}G"])
    _run_cmd(job, cmd)
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
    return {"path": path, "status": "created"}

COMMAND_HANDLERS["seeds/create"] = _handle_seed_create


def _handle_image_cache(job, params):
    url = _validate_url(params["url"])
    dest_path = _validate_path(params["dest_path"])
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    _run_cmd(job, ["curl", "-fSL", "-o", dest_path, url], timeout=3600)
    fmt = params.get("expected_format")
    if fmt == "qcow2":
        _run_cmd(job, ["qemu-img", "check", dest_path], timeout=60)
    return {"path": dest_path, "status": "cached"}

COMMAND_HANDLERS["images/cache"] = _handle_image_cache


def _handle_library_import(job, params):
    """Download image, optionally flatten, optionally upload to S3 multipart."""
    download_url = _validate_url(params["download_url"])
    cache_path = _validate_path(params["cache_path"])
    flatten = params.get("flatten", False)
    s3_multipart = params.get("s3_multipart")

    temp_files = []
    try:
        # 1. Download the file
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        job["output"].append(f"Downloading from {download_url}...")
        _run_cmd(job, ["curl", "-fSL", "-o", cache_path, download_url], timeout=7200)

        # 2. Flatten if requested
        if flatten:
            job["output"].append("Flattening QCOW2 chain...")
            flat_path = cache_path + ".flat"
            temp_files.append(flat_path)
            _run_cmd(job, ["qemu-img", "convert", "-O", "qcow2", cache_path, flat_path], timeout=3600)
            os.rename(flat_path, cache_path)
            temp_files.remove(flat_path)
            job["output"].append("Flattening complete")

        # 3. S3 multipart upload if requested
        etags = []
        if s3_multipart:
            part_size_bytes = s3_multipart["part_size_bytes"]
            upload_parts = s3_multipart["upload_parts"]
            job["output"].append(f"Splitting file into {len(upload_parts)} parts...")

            # Split file
            import tempfile as _tf
            with _tf.TemporaryDirectory(dir="/var/lib/troshka/tmp") as tmpdir:
                tmp_prefix = os.path.join(tmpdir, "part-")
                _run_cmd(job, ["split", "-b", str(part_size_bytes), "-d", cache_path, tmp_prefix], timeout=600)

                # Upload each part
                part_files = sorted(glob.glob(f"{tmp_prefix}*"))
                for idx, part_file in enumerate(part_files):
                    part_num = idx + 1
                    if part_num > len(upload_parts):
                        job["output"].append(f"Warning: more parts than presigned URLs, skipping part {part_num}")
                        continue

                    presigned_url = upload_parts[idx]["presigned_url"]
                    job["output"].append(f"Uploading part {part_num}/{len(upload_parts)}...")

                    # Use curl to upload and capture response headers
                    proc = subprocess.Popen(
                        ["curl", "-sfL", "-X", "PUT", "-T", part_file, "-D-", "-o", "/dev/null", presigned_url],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    )
                    stdout, stderr = proc.communicate(timeout=600)
                    if proc.returncode != 0:
                        raise RuntimeError(f"Part {part_num} upload failed: {stderr}")

                    # Extract ETag from response headers
                    etag = ""
                    for line in stdout.split("\n"):
                        if line.lower().startswith("etag:"):
                            etag = line.split(":", 1)[1].strip()
                            break

                    if not etag:
                        raise RuntimeError(f"No ETag in response for part {part_num}")

                    etags.append({"part": part_num, "etag": etag})
                    job["output"].append(f"Part {part_num} uploaded, ETag: {etag}")

        # Get final file size
        size_bytes = os.path.getsize(cache_path)

        result = {"status": "completed", "size_bytes": size_bytes}
        if etags:
            result["etags"] = etags

        return result

    finally:
        # Cleanup any temp files
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
        job["output"].append(f"Namespace {ns} may already exist, continuing")

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
        job["output"].append(f"Namespace {ns} may not exist, continuing")

    return {"network": network_name, "status": "removed"}

COMMAND_HANDLERS["networks/teardown"] = _handle_network_teardown


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

    # 1. Scan /var/lib/troshka/vms/ for orphan project dirs
    vms_dir = "/var/lib/troshka/vms"
    if os.path.exists(vms_dir):
        try:
            for entry in os.listdir(vms_dir):
                if entry not in known_project_ids:
                    full_path = os.path.join(vms_dir, entry)
                    if os.path.isdir(full_path):
                        orphan_dirs.append(full_path + "/")
                        job["output"].append(f"Orphan dir: {full_path}/")
        except Exception as e:
            job["output"].append(f"Failed to scan {vms_dir}: {e}")

    # 2. List all virsh domains starting with troshka-
    try:
        result = subprocess.run(
            ["virsh", "list", "--all", "--name"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            for domain in result.stdout.strip().split("\n"):
                domain = domain.strip()
                if domain.startswith("troshka-") and domain not in known_domains:
                    orphan_domains.append(domain)
                    job["output"].append(f"Orphan domain: {domain}")
    except Exception as e:
        job["output"].append(f"Failed to list virsh domains: {e}")

    # 3. List bridges matching br-troshka-*
    try:
        result = subprocess.run(
            ["ip", "-o", "link", "show", "type", "bridge"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                if "br-troshka-" in line:
                    parts = line.split(":", 2)
                    if len(parts) >= 2:
                        bridge_name = parts[1].strip().split("@")[0]
                        if bridge_name.startswith("br-troshka-"):
                            orphan_bridges.append(bridge_name)
                            job["output"].append(f"Orphan bridge: {bridge_name}")
    except Exception as e:
        job["output"].append(f"Failed to list bridges: {e}")

    # 4. List namespaces matching troshka-*
    try:
        result = subprocess.run(
            ["ip", "netns", "list"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                if line.startswith("troshka-"):
                    ns_name = line.split()[0]
                    orphan_namespaces.append(ns_name)
                    job["output"].append(f"Orphan namespace: {ns_name}")
    except Exception as e:
        job["output"].append(f"Failed to list namespaces: {e}")

    # 5. Scan cache dirs for staleness (report all items, backend will decide eviction)
    cache_dirs = [
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
                job["output"].append(f"Failed to scan {cache_dir}: {e}")

    return {
        "orphan_dirs": orphan_dirs,
        "orphan_domains": orphan_domains,
        "orphan_bridges": orphan_bridges,
        "orphan_namespaces": orphan_namespaces,
        "cache_items": cache_items,
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
                job["output"].append(f"Removed dir: {validated}")
                removed_dirs += 1
        except Exception as e:
            job["output"].append(f"Failed to remove {path}: {e}")

    # 2. Remove orphan domains (virsh destroy + undefine)
    for domain in orphan_domains:
        try:
            _validate_domain_name(domain)
            # Try to destroy (force stop) — may fail if already stopped
            try:
                _run_cmd(job, ["virsh", "destroy", domain], timeout=30)
            except RuntimeError:
                job["output"].append(f"Domain {domain} may already be stopped")
            _run_cmd(job, ["virsh", "undefine", domain, "--nvram"], timeout=30)
            job["output"].append(f"Removed domain: {domain}")
            removed_domains += 1
        except Exception as e:
            job["output"].append(f"Failed to remove domain {domain}: {e}")

    # 3. Remove orphan bridges
    for bridge in orphan_bridges:
        try:
            _validate_bridge_name(bridge)
            _run_cmd(job, ["ip", "link", "delete", bridge], timeout=10)
            job["output"].append(f"Removed bridge: {bridge}")
            removed_bridges += 1
        except Exception as e:
            job["output"].append(f"Failed to remove bridge {bridge}: {e}")

    # 4. Remove orphan namespaces
    for ns in orphan_namespaces:
        try:
            # Validate it starts with troshka-
            if not ns.startswith("troshka-"):
                raise ValueError(f"Invalid namespace name: {ns}")
            _run_cmd(job, ["ip", "netns", "delete", ns], timeout=10)
            job["output"].append(f"Removed namespace: {ns}")
            removed_namespaces += 1
        except Exception as e:
            job["output"].append(f"Failed to remove namespace {ns}: {e}")

    # 5. Remove cache items (validated paths)
    for path in cache_items:
        try:
            validated = _validate_path(path)
            if os.path.isdir(validated):
                shutil.rmtree(validated)
                job["output"].append(f"Removed cache dir: {validated}")
            else:
                os.remove(validated)
                job["output"].append(f"Removed cache file: {validated}")
            removed_cache += 1
        except FileNotFoundError:
            job["output"].append(f"Cache item not found (skipped): {path}")
        except Exception as e:
            job["output"].append(f"Failed to remove cache item {path}: {e}")

    return {
        "removed_dirs": removed_dirs,
        "removed_domains": removed_domains,
        "removed_bridges": removed_bridges,
        "removed_namespaces": removed_namespaces,
        "removed_cache": removed_cache,
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
        job["output"].append("VM may already be stopped")

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


def _handle_snapshot_capture(job, params):
    """Capture a disk snapshot: flatten, upload to S3, cache locally."""
    domain = _validate_domain_name(params["domain_name"])
    disk_index = int(params["disk_index"])
    presigned_url = _validate_url(params["presigned_url"])
    cache_path = _validate_path(params["cache_path"])

    import tempfile as _tf

    # Get disk path
    disk_path = _get_disk_path_by_index(domain, disk_index)
    job["output"].append(f"Disk {disk_index} path: {disk_path}")

    # Flatten to temp file
    with _tf.TemporaryDirectory(dir="/var/lib/troshka/tmp") as tmpdir:
        tmp_flat = os.path.join(tmpdir, "flat.qcow2")
        job["output"].append("Flattening disk...")
        _run_cmd(job, ["qemu-img", "convert", "-O", "qcow2", disk_path, tmp_flat], timeout=3600)

        # Upload to S3
        job["output"].append("Uploading to S3...")
        _run_cmd(job, ["curl", "-sfL", "-X", "PUT", "-T", tmp_flat, presigned_url], timeout=3600)

        # Copy to cache
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        job["output"].append(f"Caching to {cache_path}...")
        shutil.copy(tmp_flat, cache_path)

    size_bytes = os.path.getsize(cache_path)
    return {"status": "uploaded", "size_bytes": size_bytes}

COMMAND_HANDLERS["snapshots/capture"] = _handle_snapshot_capture


def _handle_pattern_capture(job, params):
    """Capture multiple disks for pattern export."""
    domain = _validate_domain_name(params["domain_name"])
    disks = params.get("disks", [])

    import tempfile as _tf

    result_disks = []

    for disk_info in disks:
        disk_index = int(disk_info["disk_index"])
        presigned_url = _validate_url(disk_info["presigned_url"])
        cache_path = _validate_path(disk_info["cache_path"])

        # Get disk path
        disk_path = _get_disk_path_by_index(domain, disk_index)
        job["output"].append(f"Disk {disk_index} path: {disk_path}")

        # Flatten to temp file
        with _tf.TemporaryDirectory(dir="/var/lib/troshka/tmp") as tmpdir:
            tmp_flat = os.path.join(tmpdir, "flat.qcow2")
            job["output"].append(f"Flattening disk {disk_index}...")
            _run_cmd(job, ["qemu-img", "convert", "-O", "qcow2", disk_path, tmp_flat], timeout=3600)

            # Upload to S3
            job["output"].append(f"Uploading disk {disk_index} to S3...")
            _run_cmd(job, ["curl", "-sfL", "-X", "PUT", "-T", tmp_flat, presigned_url], timeout=3600)

            # Copy to cache
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            job["output"].append(f"Caching disk {disk_index} to {cache_path}...")
            shutil.copy(tmp_flat, cache_path)

        size_bytes = os.path.getsize(cache_path)
        result_disks.append({"size_bytes": size_bytes})

    return {"status": "uploaded", "disks": result_disks}

COMMAND_HANDLERS["patterns/capture"] = _handle_pattern_capture


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
    """Return disk usage stats for /var/lib/troshka."""
    try:
        stat = shutil.disk_usage("/var/lib/troshka")
        free_bytes = stat.free
        total_bytes = stat.total
        used_pct = round((stat.used / stat.total) * 100, 2) if stat.total > 0 else 0
    except Exception:
        # If path doesn't exist or is inaccessible, return error fallback
        free_bytes = 0
        total_bytes = 0
        used_pct = 100
    handler._send_json(200, {
        "free_bytes": free_bytes,
        "total_bytes": total_bytes,
        "used_pct": used_pct,
    })


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
                job["output"].append(f"Removed directory: {validated_path}")
            else:
                os.remove(validated_path)
                job["output"].append(f"Removed file: {validated_path}")
            removed += 1
        except FileNotFoundError:
            job["output"].append(f"Skipped (not found): {validated_path}")
        except Exception as e:
            job["output"].append(f"Failed to remove {validated_path}: {e}")
            raise

    return {"removed": removed}

COMMAND_HANDLERS["files/remove"] = _handle_files_remove


# ── Update mechanism ──

def _do_update_restart(script_path, new_path):
    """Move new script into place and exit (systemd will restart)."""
    os.rename(new_path, script_path)
    logger.info("Update installed, exiting for systemd restart")
    os._exit(0)


def _drain_and_update(script_path, new_path, force):
    """Background thread: drain jobs, then update and restart."""
    global _draining
    _draining = True
    logger.info("Drain started, force=%s", force)

    drain_timeout = _config.get("drain_timeout_seconds", 30)
    if not force:
        # Wait for running jobs to complete
        start = time.time()
        while _running_job_count() > 0:
            if time.time() - start > drain_timeout:
                logger.warning("Drain timeout exceeded, terminating remaining jobs")
                break
            time.sleep(0.5)

    # Terminate any remaining job subprocesses
    with _jobs_lock:
        for job in _jobs.values():
            if job["status"] == "running" and job.get("_process"):
                try:
                    job["_process"].terminate()
                except Exception as e:
                    logger.warning("Failed to terminate job %s: %s", job["job_id"], e)

    _do_update_restart(script_path, new_path)


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
    server = HTTPServer(("0.0.0.0", config["port"]), TroshkadHandler)
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

    server = create_server(_config)
    logger.info("troshkad %s listening on port %d", VERSION, _config["port"])

    def shutdown(signum, frame):
        logger.info("Received signal %d, shutting down", signum)
        server.shutdown()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        logger.info("troshkad stopped")


if __name__ == "__main__":
    main()
