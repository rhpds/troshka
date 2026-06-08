# src/backend/app/services/troshkad_client.py
"""Client for communicating with troshkad agents on hosts.

Replaces run_ssh_script() with HTTPS requests to the troshkad daemon.
Uses only stdlib (http.client, ssl) -- no requests/httpx dependency.
Uses http.client directly (not urllib) so we can verify the peer cert
fingerprint before reading the response.
"""
import hashlib
import http.client
import json
import logging
import ssl
import time

logger = logging.getLogger(__name__)

TROSHKAD_PORT = 31337
DEFAULT_TIMEOUT = 30

_DRAIN_RETRY_INTERVAL = 5  # seconds between retries during drain
_DRAIN_RETRY_TIMEOUT = 330  # max seconds to wait (slightly > troshkad's 300s drain timeout)


class TroshkadError(Exception):
    """Error communicating with troshkad."""
    def __init__(self, message, status_code=None, response=None):
        super().__init__(message)
        self.status_code = status_code
        self.response = response


def _make_ssl_context():
    """Create SSL context that accepts self-signed certs.

    We disable CA verification because troshkad uses self-signed certs.
    Security is provided by cert fingerprint pinning after connection --
    same principle as SSH known_hosts.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _verify_cert_fingerprint(conn, host):
    """Verify the server cert SHA-256 fingerprint matches what we stored at install time.

    Raises TroshkadError on mismatch or missing fingerprint.
    """
    fingerprint = getattr(host, "agent_cert_fingerprint", None)
    if not fingerprint:
        raise TroshkadError(
            f"No cert fingerprint stored for host {host.ip_address} -- "
            "cannot verify identity. Re-install the agent to generate credentials."
        )
    peer_cert_der = conn.sock.getpeercert(binary_form=True)
    if not peer_cert_der:
        raise TroshkadError(f"No peer certificate received from {host.ip_address}")
    actual_fp = hashlib.sha256(peer_cert_der).hexdigest().upper()
    expected_fp = fingerprint.replace(":", "").upper()
    if actual_fp != expected_fp:
        raise TroshkadError(
            f"Certificate fingerprint mismatch on {host.ip_address}: "
            f"expected {expected_fp[:16]}..., got {actual_fp[:16]}..."
        )


def troshkad_request(host, method, path, body=None, timeout=DEFAULT_TIMEOUT):
    """Make an HTTPS request to a host's troshkad agent.

    Args:
        host: Host model instance (needs ip_address, agent_token, agent_cert_fingerprint)
        method: HTTP method (GET, POST)
        path: URL path (e.g., /health, /commands/vms/create)
        body: Request body dict (will be JSON-encoded)
        timeout: Request timeout in seconds

    Returns:
        Parsed JSON response dict

    Raises:
        TroshkadError: On connection, auth, cert mismatch, or server errors
    """
    data = json.dumps(body).encode() if body else None
    headers = {
        "Authorization": f"Bearer {host.agent_token}",
    }
    if data:
        headers["Content-Type"] = "application/json"

    ctx = _make_ssl_context()
    conn = http.client.HTTPSConnection(
        host.ip_address, TROSHKAD_PORT, context=ctx, timeout=timeout,
    )

    try:
        conn.request(method, path, body=data, headers=headers)
        # Verify cert fingerprint BEFORE reading response
        _verify_cert_fingerprint(conn, host)
        resp = conn.getresponse()
        resp_body = resp.read().decode()

        if resp.status >= 400:
            try:
                error_body = json.loads(resp_body)
            except (json.JSONDecodeError, ValueError):
                error_body = {"error": resp_body}
            raise TroshkadError(
                f"troshkad {host.ip_address} returned {resp.status}: {error_body}",
                status_code=resp.status,
                response=error_body,
            )
        return json.loads(resp_body)
    except TroshkadError:
        raise
    except ConnectionError as e:
        raise TroshkadError(f"Cannot connect to troshkad on {host.ip_address}: {e}")
    except TimeoutError as e:
        raise TroshkadError(f"Connection to troshkad on {host.ip_address} timed out: {e}")
    except Exception as e:
        raise TroshkadError(f"troshkad request failed: {e}")
    finally:
        conn.close()


def start_job(host, path, params):
    """Start an operation on a host. Returns job_id.

    If troshkad is draining (503), waits and retries until it comes back up
    or the timeout expires.

    Args:
        host: Host model instance
        path: Operation path (e.g., /vms/create)
        params: Operation parameters dict

    Returns:
        job_id string
    """
    deadline = time.time() + _DRAIN_RETRY_TIMEOUT

    while True:
        try:
            result = troshkad_request(host, "POST", f"/commands{path}", body=params, timeout=30)
            return result["job_id"]
        except TroshkadError as e:
            if e.status_code == 503 and e.response and e.response.get("status") == "draining":
                if time.time() >= deadline:
                    raise TroshkadError(
                        f"troshkad on {host.ip_address} still draining after {_DRAIN_RETRY_TIMEOUT}s",
                        status_code=503,
                    )
                logger.info("troshkad %s is draining, retrying in %ds...", host.ip_address, _DRAIN_RETRY_INTERVAL)
                time.sleep(_DRAIN_RETRY_INTERVAL)
                continue
            raise  # Non-draining errors propagate immediately


def poll_job(host, job_id):
    """Poll job status on a host. Returns job dict."""
    return troshkad_request(host, "GET", f"/jobs/{job_id}", timeout=15)


def wait_for_job(host, job_id, timeout=600, poll_interval=5):
    """Poll until job completes or fails. Returns final job state.

    Raises:
        TroshkadError: If timeout reached or connection lost
    """
    deadline = time.time() + timeout
    last_output_len = 0

    while time.time() < deadline:
        job = poll_job(host, job_id)

        # Log new output lines
        new_lines = job.get("output", [])[last_output_len:]
        for line in new_lines:
            logger.info("troshkad %s [%s]: %s", host.ip_address, job_id[:8], line)
        last_output_len = len(job.get("output", []))

        if job["status"] in ("completed", "failed"):
            return job

        time.sleep(poll_interval)

    raise TroshkadError(f"Job {job_id} timed out after {timeout}s on {host.ip_address}")


def check_health(host):
    """Check troshkad health. Returns health dict or None on error."""
    try:
        return troshkad_request(host, "GET", "/health", timeout=10)
    except TroshkadError:
        return None


def push_update(host, script_bytes, version, force=False):
    """Push a troshkad update to a host.

    Args:
        host: Host model instance
        script_bytes: New troshkad.py content as bytes
        version: Version string
        force: Skip graceful drain

    Returns:
        Response dict from troshkad
    """
    import base64
    path = "/admin/update"
    if force:
        path += "?force=true"
    return troshkad_request(host, "POST", path, body={
        "script": base64.b64encode(script_bytes).decode(),
        "version": version,
    }, timeout=30)


def get_vm_state(host, domain_name, timeout=15):
    """Get VM state. Returns state string or 'not_found'."""
    try:
        job_id = start_job(host, "/vms/state", {"domain_name": domain_name})
        job = wait_for_job(host, job_id, timeout=timeout, poll_interval=2)
        if job["status"] == "completed":
            return job["result"].get("state", "unknown")
        return "unknown"
    except TroshkadError:
        return "not_found"


def get_vnc_port(host, domain_name, timeout=15):
    """Get VNC port for a VM. Returns port int or None."""
    try:
        job_id = start_job(host, "/vms/vnc-port", {"domain_name": domain_name})
        job = wait_for_job(host, job_id, timeout=timeout, poll_interval=2)
        if job["status"] == "completed":
            return job["result"].get("vnc_port")
        return None
    except TroshkadError:
        return None


def get_vm_config(host, domain_name, timeout=15):
    """Get VM config. Returns config dict or None."""
    try:
        job_id = start_job(host, "/vms/config", {"domain_name": domain_name})
        job = wait_for_job(host, job_id, timeout=timeout, poll_interval=2)
        if job["status"] == "completed":
            return job["result"]
        return None
    except TroshkadError:
        return None


def reconfigure_vm(host, domain_name, timeout=60, **kwargs):
    """Reconfigure a VM. kwargs: boot_devs, vcpus, ram_mb, nics, disks, cdroms, restart."""
    params = {"domain_name": domain_name, **kwargs}
    job_id = start_job(host, "/vms/reconfigure", params)
    job = wait_for_job(host, job_id, timeout=timeout, poll_interval=2)
    if job["status"] == "failed":
        raise TroshkadError(f"Reconfigure failed: {job['result'].get('error')}")
    return job["result"]


def undefine_vm(host, domain_name, remove_storage=True, timeout=30):
    """Undefine a VM."""
    job_id = start_job(host, "/vms/undefine", {"domain_name": domain_name, "remove_storage": remove_storage})
    job = wait_for_job(host, job_id, timeout=timeout, poll_interval=2)
    return job["status"] == "completed"


def check_disk_usage(host, timeout=15):
    """Check disk usage on host. Returns {free_bytes, total_bytes, used_pct} or error dict."""
    try:
        return troshkad_request(host, "GET", "/host/disk-usage", timeout=timeout)
    except TroshkadError as e:
        return {"free_bytes": 0, "total_bytes": 0, "used_pct": 100, "error": str(e)}
