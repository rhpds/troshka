# src/troshkad/troshkad.py
"""troshkad — Troshka host agent daemon.

Single-file Python daemon managing QEMU/libvirt on the host.
Exposes a structured HTTPS REST API for the Troshka backend.
Requires only Python 3.9+ stdlib — no pip dependencies.
"""
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
