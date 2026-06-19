# src/backend/app/services/troshkad_client.py
"""Client for communicating with troshkad agents on hosts.

Replaces run_ssh_script() with HTTPS requests to the troshkad daemon.
Uses urllib3 connection pooling for performance and reliability.
"""
import json
import logging
import time

import urllib3
from urllib3.exceptions import MaxRetryError, SSLError
from urllib3.exceptions import TimeoutError as U3Timeout

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

TROSHKAD_PORT = 31337
DEFAULT_TIMEOUT = 30

_DRAIN_RETRY_INTERVAL = 5  # seconds between retries during drain
_DRAIN_RETRY_TIMEOUT = (
    330  # max seconds to wait (slightly > troshkad's 300s drain timeout)
)

# Connection pool cache (one pool per host)
_pools: dict[str, urllib3.HTTPSConnectionPool] = {}


class TroshkadError(Exception):
    """Error communicating with troshkad."""

    def __init__(self, message, status_code=None, response=None):
        super().__init__(message)
        self.status_code = status_code
        self.response = response


def _get_pool(host):
    """Get or create a urllib3 connection pool for a host.

    Uses cert fingerprint pinning for security (same principle as SSH known_hosts).
    Pools are cached per host IP address to enable connection reuse.
    """
    fingerprint = getattr(host, "agent_cert_fingerprint", None)
    if not fingerprint:
        raise TroshkadError(
            f"No cert fingerprint stored for host {host.ip_address} -- "
            "cannot verify identity. Re-install the agent to generate credentials."
        )

    fp_clean = fingerprint.replace(":", "").upper()
    key = f"{host.ip_address}:{fp_clean}"
    pool = _pools.get(key)
    if pool is None:
        pool = urllib3.HTTPSConnectionPool(
            host.ip_address,
            port=TROSHKAD_PORT,
            maxsize=4,
            cert_reqs="CERT_NONE",
            assert_fingerprint=fp_clean,
            retries=False,
            timeout=urllib3.Timeout(connect=10, read=DEFAULT_TIMEOUT),
        )
        _pools[key] = pool
    return pool


def troshkad_request(
    host,
    method,
    path,
    body=None,
    timeout=DEFAULT_TIMEOUT,
    retries=3,
    allow_disconnected=False,
):
    """Make an HTTPS request to a host's troshkad agent with automatic retry.

    Retries on connection errors and 503s. Non-retryable errors (auth, 4xx) fail immediately.
    Uses urllib3 connection pooling for better performance and reliability.

    If the host is marked disconnected, fails immediately (circuit breaker)
    unless allow_disconnected=True (used by health poller to probe for reconnection).
    """
    if not allow_disconnected and getattr(host, "agent_status", None) == "disconnected":
        raise TroshkadError(
            f"Host {host.ip_address} is disconnected — skipping request"
        )

    pool = _get_pool(host)  # Get or create pool (validates fingerprint exists)
    last_error = None

    for attempt in range(retries):
        encoded_body = json.dumps(body).encode() if body else None
        headers = {
            "Authorization": f"Bearer {host.agent_token}",
        }
        if encoded_body:
            headers["Content-Type"] = "application/json"

        try:
            resp = pool.urlopen(
                method,
                path,
                body=encoded_body,
                headers=headers,
                timeout=timeout,
                retries=False,  # We handle retries ourselves
            )
            resp_body = resp.data.decode()

            if resp.status >= 400:
                try:
                    error_body = json.loads(resp_body)
                except (json.JSONDecodeError, ValueError):
                    error_body = {"error": resp_body}
                err = TroshkadError(
                    f"troshkad {host.ip_address} returned {resp.status}: {error_body}",
                    status_code=resp.status,
                    response=error_body,
                )
                if resp.status == 503 and attempt < retries - 1:
                    last_error = err
                    logger.info(
                        "troshkad %s returned 503, retrying in 5s (%d/%d)...",
                        host.ip_address,
                        attempt + 1,
                        retries,
                    )
                    time.sleep(5)
                    continue
                raise err

            result = json.loads(resp_body)
            if attempt > 0:
                logger.info("troshkad %s connection re-established", host.ip_address)
            return result

        except TroshkadError:
            raise
        except SSLError as e:
            # Fingerprint mismatch or other SSL errors
            raise TroshkadError(
                f"Certificate verification failed for {host.ip_address}: {e}"
            )
        except (MaxRetryError, U3Timeout) as e:
            last_error = TroshkadError(
                f"Cannot connect to troshkad on {host.ip_address}: {e}"
            )
            if attempt < retries - 1:
                logger.info(
                    "troshkad %s connection failed, retrying in 5s (%d/%d)...",
                    host.ip_address,
                    attempt + 1,
                    retries,
                )
                time.sleep(5)
                continue
            raise last_error
        except Exception as e:
            raise TroshkadError(f"troshkad request failed: {e}")

    raise last_error or TroshkadError(
        f"troshkad request failed after {retries} retries"
    )


def troshkad_request_raw(
    host, method, path, body=None, headers=None, timeout=DEFAULT_TIMEOUT
):
    """Make a raw HTTPS request — supports binary request/response bodies.

    Unlike troshkad_request(), this does not JSON-encode the body or JSON-parse
    the response. Caller is responsible for encoding/decoding.

    Returns urllib3.HTTPResponse so caller can access .data (bytes) and .status.
    """
    pool = _get_pool(host)
    req_headers = {"Authorization": f"Bearer {host.agent_token}"}
    if headers:
        req_headers.update(headers)

    try:
        resp = pool.urlopen(
            method,
            path,
            body=body,
            headers=req_headers,
            timeout=timeout,
            retries=False,
        )
        if resp.status >= 400:
            resp_text = resp.data.decode(errors="replace")
            try:
                error_body = json.loads(resp_text)
            except (json.JSONDecodeError, ValueError):
                error_body = {"error": resp_text}
            raise TroshkadError(
                f"troshkad {host.ip_address} returned {resp.status}: {error_body}",
                status_code=resp.status,
                response=error_body,
            )
        return resp
    except TroshkadError:
        raise
    except SSLError as e:
        raise TroshkadError(
            f"Certificate verification failed for {host.ip_address}: {e}"
        )
    except (MaxRetryError, U3Timeout) as e:
        raise TroshkadError(f"Cannot connect to troshkad on {host.ip_address}: {e}")
    except Exception as e:
        raise TroshkadError(f"troshkad request failed: {e}")


def troshkad_upload_to_vm(
    host,
    file_bytes,
    project_id,
    vm_ip,
    username,
    password,
    remote_path,
    mode="0644",
    timeout=3600,
):
    """Upload a file to a VM via troshkad's /vm/file-push endpoint.

    For files >10MB, troshkad returns 202 with a job_id; we poll until complete.
    For smaller files, troshkad does the SCP synchronously and returns 200.
    """
    import urllib.parse

    qs = urllib.parse.urlencode(
        {
            "project_id": project_id,
            "vm_ip": vm_ip,
            "username": username,
            "password": password,
            "remote_path": remote_path,
            "mode": mode,
        }
    )
    resp = troshkad_request_raw(
        host,
        "POST",
        f"/vm/file-push?{qs}",
        body=file_bytes,
        headers={"Content-Type": "application/octet-stream"},
        timeout=min(timeout, 120),
    )
    result = json.loads(resp.data.decode())

    if resp.status == 202 and "job_id" in result:
        job = wait_for_job(host, result["job_id"], timeout=timeout)
        if job["status"] == "failed":
            raise TroshkadError(f"File push failed: {job['result'].get('error')}")
        return job["result"]

    return result


def troshkad_download_from_vm(
    host, project_id, vm_ip, username, password, remote_path, timeout=3600
):
    """Download a file from a VM via troshkad's /vm/file-pull endpoint.

    Returns raw bytes of the file content.
    """
    resp = troshkad_request_raw(
        host,
        "POST",
        "/vm/file-pull",
        body=json.dumps(
            {
                "project_id": project_id,
                "vm_ip": vm_ip,
                "username": username,
                "password": password,
                "remote_path": remote_path,
            }
        ).encode(),
        headers={"Content-Type": "application/json"},
        timeout=timeout,
    )
    return resp.data


def start_job(host, path, params, request_timeout=30):
    """Start an operation on a host. Returns job_id.

    If troshkad is draining (503), waits and retries until it comes back up
    or the timeout expires.

    Args:
        host: Host model instance
        path: Operation path (e.g., /vms/create)
        params: Operation parameters dict
        request_timeout: HTTP connection/request timeout in seconds

    Returns:
        job_id string
    """
    deadline = time.time() + _DRAIN_RETRY_TIMEOUT

    while True:
        try:
            result = troshkad_request(
                host, "POST", f"/commands{path}", body=params, timeout=request_timeout
            )
            return result["job_id"]
        except TroshkadError as e:
            retryable = (
                e.status_code == 503
                or "Cannot connect" in str(e)
                or "timed out" in str(e)
            )
            if retryable:
                if time.time() >= deadline:
                    raise
                reason = (
                    "unreachable"
                    if not e.status_code
                    else (
                        "draining"
                        if e.response and e.response.get("status") == "draining"
                        else "busy (job queue full)"
                    )
                )
                remaining = int(deadline - time.time())
                logger.info(
                    "troshkad %s %s is %s, retrying in %ds (%ds remaining)...",
                    host.ip_address,
                    path,
                    reason,
                    _DRAIN_RETRY_INTERVAL,
                    remaining,
                )
                time.sleep(_DRAIN_RETRY_INTERVAL)
                continue
            raise


def poll_job(host, job_id):
    """Poll job status on a host. Returns job dict."""
    return troshkad_request(host, "GET", f"/jobs/{job_id}", timeout=15)


def cancel_job(host, job_id):
    """Cancel a running job on a host. Returns job dict."""
    return troshkad_request(host, "DELETE", f"/jobs/{job_id}", timeout=15)


def wait_for_job(host, job_id, timeout=600, poll_interval=5):
    """Poll until job completes or fails. Returns final job state.

    Uses fast initial polling (0.1s, 0.3s, 0.5s, 1s) then falls back
    to poll_interval for long-running jobs.

    Raises:
        TroshkadError: If timeout reached or connection lost
    """
    deadline = time.time() + timeout
    last_output_len = 0

    consecutive_failures = 0
    max_consecutive_failures = 12
    poll_count = 0
    fast_delays = [0.1, 0.3, 0.5, 1.0]

    while time.time() < deadline:
        try:
            job = poll_job(host, job_id)
            consecutive_failures = 0
        except TroshkadError as e:
            if e.status_code == 404:
                logger.warning(
                    "Job %s not found on %s (agent may have restarted), assuming completed",
                    job_id[:8],
                    host.ip_address,
                )
                return {
                    "job_id": job_id,
                    "status": "completed",
                    "output": [],
                    "result": {},
                }
            consecutive_failures += 1
            if consecutive_failures >= max_consecutive_failures:
                raise
            logger.info(
                "Job %s poll failed on %s (%d/%d), retrying: %s",
                job_id[:8],
                host.ip_address,
                consecutive_failures,
                max_consecutive_failures,
                e,
            )
            time.sleep(poll_interval)
            continue

        # Log new output lines
        new_lines = job.get("output", [])[last_output_len:]
        for line in new_lines:
            logger.info("troshkad %s [%s]: %s", host.ip_address, job_id[:8], line)
        last_output_len = len(job.get("output", []))

        if job["status"] in ("completed", "failed"):
            return job

        delay = (
            fast_delays[poll_count] if poll_count < len(fast_delays) else poll_interval
        )
        poll_count += 1
        time.sleep(delay)

    raise TroshkadError(f"Job {job_id} timed out after {timeout}s on {host.ip_address}")


def check_health(host):
    """Check troshkad health. Returns health dict or None on error.

    Uses retries=1 (no retry) since the health poller calls this every 30s —
    retrying here just multiplies the load on unreachable hosts.
    Passes allow_disconnected=True so the health poller can probe for reconnection.
    """
    try:
        return troshkad_request(
            host, "GET", "/health", timeout=10, retries=1, allow_disconnected=True
        )
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
    return troshkad_request(
        host,
        "POST",
        path,
        body={
            "script": base64.b64encode(script_bytes).decode(),
            "version": version,
        },
        timeout=30,
    )


def push_vncd_update(host, script_bytes: bytes):
    """Push a troshka-vncd update to a host."""
    import base64

    troshkad_request(
        host,
        "POST",
        "/admin/update-vncd",
        body={
            "script": base64.b64encode(script_bytes).decode(),
        },
        timeout=30,
    )


def get_vm_state(host, domain_name, timeout=15):
    """Get VM state and boot config. Returns dict with 'state' and 'boot_devs'."""
    try:
        result = troshkad_request(
            host,
            "POST",
            "/commands/vms/state",
            body={"domain_name": domain_name},
            timeout=5,
            retries=1,
        )
        job_id = result["job_id"]
        job = wait_for_job(host, job_id, timeout=timeout, poll_interval=2)
        if job["status"] == "completed":
            result = job["result"]
            return {
                "state": result.get("state", "unknown"),
                "boot_devs": result.get("boot_devs", []),
            }
        return {"state": "unknown", "boot_devs": []}
    except TroshkadError:
        return {"state": "not_found", "boot_devs": []}


def get_all_vm_states(host, timeout=10):
    """Get all troshka-* domain states in one batch call.

    Returns dict mapping domain_name -> state string, or None if host
    doesn't support the batch endpoint (old agent).
    """
    try:
        result = troshkad_request(
            host, "GET", "/vms/states", timeout=timeout, retries=1
        )
        return {name: info["state"] for name, info in result.get("domains", {}).items()}
    except TroshkadError:
        return None


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
    job_id = start_job(
        host,
        "/vms/undefine",
        {"domain_name": domain_name, "remove_storage": remove_storage},
    )
    job = wait_for_job(host, job_id, timeout=timeout, poll_interval=2)
    return job["status"] == "completed"


def check_disk_usage(host, timeout=15, retries=3):
    """Check disk usage on host. Returns {free_bytes, total_bytes, used_pct, partitions} for the troshka partition."""
    import time

    for attempt in range(retries):
        try:
            result = troshkad_request(host, "GET", "/host/disk-usage", timeout=timeout)
            if "partitions" in result:
                partitions = result["partitions"]
                primary = None
                for p in partitions:
                    if p.get("mount") == "/var/lib/troshka":
                        primary = p
                        break
                if not primary:
                    for p in partitions:
                        if p.get("mount") == "/":
                            primary = p
                            break
                if not primary:
                    primary = partitions[0] if partitions else {}
                return {**primary, "partitions": partitions}
            return result
        except TroshkadError:
            if attempt == retries - 1:
                raise
            time.sleep(2)
