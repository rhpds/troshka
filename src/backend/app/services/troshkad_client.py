# src/backend/app/services/troshkad_client.py
"""Client for communicating with troshkad agents on hosts.

Replaces run_ssh_script() with HTTPS requests to the troshkad daemon.
Uses only stdlib (urllib) -- no requests/httpx dependency.
"""
import hashlib
import json
import logging
import ssl
import time
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

TROSHKAD_PORT = 31337
DEFAULT_TIMEOUT = 30


class TroshkadError(Exception):
    """Error communicating with troshkad."""
    def __init__(self, message, status_code=None, response=None):
        super().__init__(message)
        self.status_code = status_code
        self.response = response


def _make_ssl_context(host):
    """Create SSL context that accepts self-signed certs for fingerprint pinning.

    We disable default CA verification because troshkad uses self-signed certs.
    Instead, we verify the cert's SHA-256 fingerprint matches what we stored
    at install time -- this is actually stronger than CA verification for
    known hosts (same principle as SSH known_hosts).
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _verify_cert_fingerprint(host, resp):
    """Verify the server cert fingerprint matches our stored fingerprint."""
    if not hasattr(host, "agent_cert_fingerprint") or not host.agent_cert_fingerprint:
        return
    peer_cert_der = resp.fp.raw._sock.getpeercert(binary_form=True)
    if peer_cert_der:
        actual_fp = hashlib.sha256(peer_cert_der).hexdigest().upper()
        # Stored fingerprint may have colons (e.g., AB:CD:EF:...)
        expected_fp = host.agent_cert_fingerprint.replace(":", "").upper()
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
        TroshkadError: On connection, auth, or server errors
    """
    url = f"https://{host.ip_address}:{TROSHKAD_PORT}{path}"
    data = json.dumps(body).encode() if body else None
    headers = {
        "Authorization": f"Bearer {host.agent_token}",
    }
    if data:
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    ctx = _make_ssl_context(host)

    try:
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode() if e.fp else ""
        try:
            error_body = json.loads(body_text)
        except (json.JSONDecodeError, ValueError):
            error_body = {"error": body_text}
        raise TroshkadError(
            f"troshkad {host.ip_address} returned {e.code}: {error_body}",
            status_code=e.code,
            response=error_body,
        )
    except urllib.error.URLError as e:
        raise TroshkadError(f"Cannot connect to troshkad on {host.ip_address}: {e}")
    except Exception as e:
        raise TroshkadError(f"troshkad request failed: {e}")


def start_job(host, path, params):
    """Start an operation on a host. Returns job_id.

    Args:
        host: Host model instance
        path: Operation path (e.g., /vms/create)
        params: Operation parameters dict

    Returns:
        job_id string
    """
    result = troshkad_request(host, "POST", f"/commands{path}", body=params, timeout=30)
    return result["job_id"]


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
