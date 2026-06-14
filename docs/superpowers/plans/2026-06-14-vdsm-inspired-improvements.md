# VDSM-Inspired Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship 5 independent host management improvements: connection pooling, partition monitoring, S3 temp redirect, block threshold events with storage auto-extend, and streaming pattern capture.

**Architecture:** Each improvement is a self-contained module with no cross-dependencies. They can be implemented in any order, but priority order is: 1→2→3→4→5. Items 1, 3, 5 are troshkad/backend only. Items 2 and 4 touch the full stack (troshkad + backend + DB migration + frontend).

**Tech Stack:** Python 3.11, urllib3, FastAPI, SQLAlchemy 2, Alembic, Next.js 15, PatternFly 6

---

## Module 1: Connection Pooling

Replace per-request `http.client.HTTPSConnection` with `urllib3.HTTPSConnectionPool` in the backend's troshkad client. urllib3 is already a dependency.

### Task 1.1: Update tests for urllib3-based client

**Files:**
- Modify: `src/backend/tests/test_troshkad_client.py`

- [ ] **Step 1: Rewrite test mocks from http.client to urllib3**

The existing tests mock `http.client.HTTPSConnection`. Replace them to mock `urllib3.HTTPSConnectionPool.urlopen` instead. The `FakeHost` fixture stays the same.

```python
# src/backend/tests/test_troshkad_client.py
"""Tests for troshkad_client -- mocks urllib3 to test client logic."""
import hashlib
import json
import unittest
from unittest.mock import patch, MagicMock, PropertyMock

from app.services.troshkad_client import (
    troshkad_request, start_job, poll_job, check_disk_usage,
    TroshkadError, _get_pool,
)


FAKE_CERT_DER = b"fake-cert-der-bytes-for-testing"
FAKE_FINGERPRINT = hashlib.sha256(FAKE_CERT_DER).hexdigest().upper()


class FakeHost:
    ip_address = "10.0.0.1"
    agent_token = "a" * 64
    agent_cert_fingerprint = FAKE_FINGERPRINT


class NoFingerprintHost:
    ip_address = "10.0.0.1"
    agent_token = "a" * 64
    agent_cert_fingerprint = None


def _mock_response(body, status=200):
    """Create a mock urllib3 HTTPResponse."""
    resp = MagicMock()
    resp.status = status
    resp.data = json.dumps(body).encode() if isinstance(body, dict) else body
    return resp


class TestTroshkadClient(unittest.TestCase):

    @patch("app.services.troshkad_client._get_pool")
    def test_troshkad_request_sends_auth(self, mock_get_pool):
        pool = MagicMock()
        pool.urlopen.return_value = _mock_response({"status": "ok"})
        mock_get_pool.return_value = pool

        result = troshkad_request(FakeHost(), "GET", "/health")
        self.assertEqual(result["status"], "ok")
        call_kwargs = pool.urlopen.call_args
        headers = call_kwargs[1].get("headers", {}) if call_kwargs[1] else call_kwargs[0][2] if len(call_kwargs[0]) > 2 else {}
        self.assertIn("Authorization", headers)

    @patch("app.services.troshkad_client._get_pool")
    def test_start_job_returns_job_id(self, mock_get_pool):
        pool = MagicMock()
        pool.urlopen.return_value = _mock_response({"job_id": "test-123", "status": "running"})
        mock_get_pool.return_value = pool

        job_id = start_job(FakeHost(), "/vms/create", {"domain_name": "test"})
        self.assertEqual(job_id, "test-123")

    @patch("app.services.troshkad_client._get_pool")
    def test_poll_job_returns_status(self, mock_get_pool):
        pool = MagicMock()
        pool.urlopen.return_value = _mock_response({
            "job_id": "test-123", "status": "completed",
            "result": {"domain": "test"}, "output": [],
        })
        mock_get_pool.return_value = pool

        job = poll_job(FakeHost(), "test-123")
        self.assertEqual(job["status"], "completed")

    def test_missing_fingerprint_raises(self):
        with self.assertRaises(TroshkadError) as ctx:
            _get_pool(NoFingerprintHost())
        self.assertIn("No cert fingerprint", str(ctx.exception))

    @patch("app.services.troshkad_client._get_pool")
    def test_503_retries(self, mock_get_pool):
        pool = MagicMock()
        pool.urlopen.side_effect = [
            _mock_response({"error": "draining"}, status=503),
            _mock_response({"status": "ok"}),
        ]
        mock_get_pool.return_value = pool

        with patch("app.services.troshkad_client.time.sleep"):
            result = troshkad_request(FakeHost(), "GET", "/health")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(pool.urlopen.call_count, 2)

    @patch("app.services.troshkad_client._get_pool")
    def test_connection_error_retries(self, mock_get_pool):
        from urllib3.exceptions import MaxRetryError, NewConnectionError
        pool = MagicMock()
        pool.urlopen.side_effect = [
            MaxRetryError(pool, "/health", reason=NewConnectionError(pool, "Connection refused")),
            _mock_response({"status": "ok"}),
        ]
        mock_get_pool.return_value = pool

        with patch("app.services.troshkad_client.time.sleep"):
            result = troshkad_request(FakeHost(), "GET", "/health")
        self.assertEqual(result["status"], "ok")

    @patch("app.services.troshkad_client._get_pool")
    def test_ssl_error_on_fingerprint_mismatch(self, mock_get_pool):
        from urllib3.exceptions import SSLError
        pool = MagicMock()
        pool.urlopen.side_effect = SSLError("fingerprint mismatch")
        mock_get_pool.return_value = pool

        with self.assertRaises(TroshkadError) as ctx:
            troshkad_request(FakeHost(), "GET", "/health")
        self.assertIn("Certificate", str(ctx.exception))

    @patch("app.services.troshkad_client._get_pool")
    def test_check_disk_usage(self, mock_get_pool):
        pool = MagicMock()
        pool.urlopen.return_value = _mock_response({"free_bytes": 380*1024**3, "total_bytes": 500*1024**3, "used_pct": 24})
        mock_get_pool.return_value = pool
        result = check_disk_usage(FakeHost())
        self.assertEqual(result["used_pct"], 24)

    @patch("app.services.troshkad_client._get_pool")
    def test_start_job_retries_during_drain(self, mock_get_pool):
        pool = MagicMock()
        call_count = 0
        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return _mock_response({"status": "draining", "error": "draining for update"}, status=503)
            return _mock_response({"job_id": "new-job-123", "status": "running"})
        pool.urlopen.side_effect = side_effect
        mock_get_pool.return_value = pool

        with patch("app.services.troshkad_client.time.sleep"):
            job_id = start_job(FakeHost(), "/vms/create", {"domain_name": "test"})
        self.assertEqual(job_id, "new-job-123")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_troshkad_client.py -v`
Expected: FAIL — `_get_pool` doesn't exist yet.

### Task 1.2: Implement urllib3 connection pooling

**Files:**
- Modify: `src/backend/app/services/troshkad_client.py`

- [ ] **Step 3: Replace http.client with urllib3 pool**

Rewrite `troshkad_client.py` to use `urllib3.HTTPSConnectionPool` with `assert_fingerprint`:

```python
# src/backend/app/services/troshkad_client.py
"""Client for communicating with troshkad agents on hosts.

Uses urllib3 connection pooling with cert fingerprint pinning.
Per-host pools are cached at module level, keyed by IP + fingerprint.
"""
import json
import logging
import time

import urllib3
from urllib3.exceptions import SSLError, MaxRetryError, TimeoutError as U3Timeout

logger = logging.getLogger(__name__)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TROSHKAD_PORT = 31337
DEFAULT_TIMEOUT = 30

_DRAIN_RETRY_INTERVAL = 5
_DRAIN_RETRY_TIMEOUT = 330

_pools: dict[str, urllib3.HTTPSConnectionPool] = {}


class TroshkadError(Exception):
    """Error communicating with troshkad."""
    def __init__(self, message, status_code=None, response=None):
        super().__init__(message)
        self.status_code = status_code
        self.response = response


def _get_pool(host):
    """Get or create a connection pool for this host.

    Pool key includes the cert fingerprint so an agent reinstall
    (new cert) automatically creates a new pool.
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


def troshkad_request(host, method, path, body=None, timeout=DEFAULT_TIMEOUT, retries=3):
    """Make an HTTPS request to a host's troshkad agent with automatic retry."""
    pool = _get_pool(host)
    last_error = None

    for attempt in range(retries):
        headers = {"Authorization": f"Bearer {host.agent_token}"}
        encoded_body = None
        if body is not None:
            encoded_body = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"

        try:
            resp = pool.urlopen(
                method, path, body=encoded_body, headers=headers,
                timeout=urllib3.Timeout(connect=10, read=timeout),
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
                    logger.info("troshkad %s returned 503, retrying in 5s (%d/%d)...",
                                host.ip_address, attempt + 1, retries)
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
            raise TroshkadError(
                f"Certificate verification failed for {host.ip_address}: {e} "
                "-- the agent cert may have changed. Re-install the agent."
            )
        except (MaxRetryError, U3Timeout, OSError) as e:
            last_error = TroshkadError(f"Cannot connect to troshkad on {host.ip_address}: {e}")
            if attempt < retries - 1:
                logger.info("troshkad %s connection failed, retrying in 5s (%d/%d)...",
                            host.ip_address, attempt + 1, retries)
                time.sleep(5)
                continue
            raise last_error
        except Exception as e:
            raise TroshkadError(f"troshkad request failed: {e}")

    raise last_error or TroshkadError(f"troshkad request failed after {retries} retries")
```

The rest of the file (`start_job`, `poll_job`, `wait_for_job`, `check_health`, `push_update`, `get_vm_state`, `get_all_vm_states`, `get_vnc_port`, `get_vm_config`, `reconfigure_vm`, `undefine_vm`, `check_disk_usage`) remains unchanged — they all call `troshkad_request()` internally.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_troshkad_client.py -v`
Expected: All pass.

- [ ] **Step 5: Run full test suite to check for regressions**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/ -v`
Expected: All pass. Other tests that mock `http.client.HTTPSConnection` directly (unlikely) would need updating.

- [ ] **Step 6: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/troshkad_client.py src/backend/tests/test_troshkad_client.py
git commit -m "feat: replace http.client with urllib3 connection pooling in troshkad client"
```

---

## Module 2: Partition Monitoring

Report all mounted partitions from troshkad. Health poller evaluates thresholds and stores warnings on the Host model. Frontend shows warning badges.

### Task 2.1: Add partition reporting to troshkad health

**Files:**
- Modify: `src/troshkad/troshkad.py` (the `_get_capacity()` function near line 136 and `handle_disk_usage` near line 3197)

- [ ] **Step 1: Add `_get_partitions()` function after `_get_capacity()` (around line 185)**

```python
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
```

- [ ] **Step 2: Include partitions in the health response**

In `handle_health()` (the GET `/health` handler — find it by searching for `COMMAND_HANDLERS["health"]` or the function that calls `_get_capacity()`), add `"partitions": _get_partitions()` to the response dict alongside the existing `"capacity"` field.

Find the handler — it's likely something like:

```python
@route("GET", "/health")
def handle_health(handler, params):
    ...
    handler._send_json(200, {
        "status": "ok",
        "version": VERSION,
        "capacity": _get_capacity(),
        "partitions": _get_partitions(),  # ADD THIS LINE
        ...
    })
```

- [ ] **Step 3: Update `handle_disk_usage` to return all partitions**

Replace the existing `handle_disk_usage` (line ~3197) to return the full partition list:

```python
@route("GET", "/host/disk-usage")
def handle_disk_usage(handler, params):
    """Return disk usage stats for all mounted partitions."""
    handler._send_json(200, {"partitions": _get_partitions()})
```

- [ ] **Step 4: Commit troshkad changes**

```bash
cd /Users/prutledg/troshka && git add src/troshkad/troshkad.py
git commit -m "feat: report all mounted partitions in troshkad health endpoint"
```

### Task 2.2: Add storage_warnings column to Host model

**Files:**
- Modify: `src/backend/app/models/host.py`
- Create: `src/backend/alembic/versions/xxxx_add_storage_warnings_to_hosts.py`

- [ ] **Step 5: Add `storage_warnings` JSONB column to Host model**

In `src/backend/app/models/host.py`, add the import and column:

```python
# Add to imports at top of file:
from sqlalchemy.dialects.postgresql import JSONB

# Add column to Host class, after storage_pool_id:
    storage_warnings: Mapped[list | None] = mapped_column(JSONB, default=None)
```

- [ ] **Step 6: Create alembic migration**

Run: `cd src/backend && ./venv/bin/python3 -m alembic revision -m "add storage_warnings to hosts"`

Then edit the generated migration file:

```python
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

def upgrade():
    op.add_column('hosts', sa.Column('storage_warnings', JSONB, nullable=True))

def downgrade():
    op.drop_column('hosts', 'storage_warnings')
```

- [ ] **Step 7: Run migration**

Run: `cd src/backend && ./venv/bin/python3 -m alembic upgrade head`

- [ ] **Step 8: Commit model + migration**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/models/host.py src/backend/alembic/versions/
git commit -m "feat: add storage_warnings JSONB column to hosts"
```

### Task 2.3: Health poller evaluates partition thresholds

**Files:**
- Modify: `src/backend/app/services/health_poller.py`
- Modify: `src/backend/tests/test_health_poller.py`

- [ ] **Step 9: Write test for partition threshold evaluation**

Add to `src/backend/tests/test_health_poller.py`:

```python
    @patch("app.core.database.SessionLocal")
    @patch("app.services.troshkad_client.check_health")
    def test_partition_warning_stored_on_host(self, mock_check, mock_session_cls):
        from app.services.health_poller import _poll_hosts

        host = MagicMock()
        host.id = "test-host-uuid-1234"
        host.agent_status = "connected"
        host.last_health_at = datetime.now(timezone.utc)
        host.agent_token = "token123"
        host.storage_warnings = None

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = [host]
        mock_session_cls.return_value = mock_db

        mock_check.return_value = {
            "status": "ok", "version": "1.0",
            "capacity": {},
            "partitions": [
                {"mount": "/", "used_pct": 92.1, "total_bytes": 100*1024**3,
                 "used_bytes": 92*1024**3, "free_bytes": 8*1024**3,
                 "device": "/dev/nvme0n1p1", "fstype": "xfs"},
                {"mount": "/var/lib/troshka", "used_pct": 45.0, "total_bytes": 500*1024**3,
                 "used_bytes": 225*1024**3, "free_bytes": 275*1024**3,
                 "device": "/dev/nvme1n1", "fstype": "xfs"},
            ],
        }

        _poll_hosts()

        self.assertIsNotNone(host.storage_warnings)
        self.assertEqual(len(host.storage_warnings), 1)
        self.assertEqual(host.storage_warnings[0]["mount"], "/")
        self.assertEqual(host.storage_warnings[0]["level"], "critical")

    @patch("app.core.database.SessionLocal")
    @patch("app.services.troshkad_client.check_health")
    def test_partition_warnings_cleared_when_healthy(self, mock_check, mock_session_cls):
        from app.services.health_poller import _poll_hosts

        host = MagicMock()
        host.id = "test-host-uuid-1234"
        host.agent_status = "connected"
        host.last_health_at = datetime.now(timezone.utc)
        host.agent_token = "token123"
        host.storage_warnings = [{"mount": "/", "used_pct": 92.1, "level": "critical"}]

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = [host]
        mock_session_cls.return_value = mock_db

        mock_check.return_value = {
            "status": "ok", "version": "1.0",
            "capacity": {},
            "partitions": [
                {"mount": "/", "used_pct": 60.0, "total_bytes": 100*1024**3,
                 "used_bytes": 60*1024**3, "free_bytes": 40*1024**3,
                 "device": "/dev/nvme0n1p1", "fstype": "xfs"},
            ],
        }

        _poll_hosts()

        self.assertIsNone(host.storage_warnings)
```

- [ ] **Step 10: Run tests to verify they fail**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_health_poller.py -v`
Expected: FAIL — no partition evaluation logic yet.

- [ ] **Step 11: Add partition evaluation to health poller**

In `src/backend/app/services/health_poller.py`, add an `_evaluate_partitions` function and call it from `_poll_hosts`:

```python
_WARNING_PCT = 85
_CRITICAL_PCT = 95


def _evaluate_partitions(health):
    """Check partition usage and return warnings list, or None if all healthy."""
    partitions = health.get("partitions", [])
    if not partitions:
        return None
    warnings = []
    for p in partitions:
        pct = p.get("used_pct", 0)
        if pct >= _CRITICAL_PCT:
            warnings.append({"mount": p["mount"], "used_pct": pct, "level": "critical"})
        elif pct >= _WARNING_PCT:
            warnings.append({"mount": p["mount"], "used_pct": pct, "level": "warning"})
    return warnings if warnings else None
```

Then in `_poll_hosts()`, inside the `if health:` block, after updating capacity, add:

```python
                    host.storage_warnings = _evaluate_partitions(health)
```

- [ ] **Step 12: Run tests to verify they pass**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_health_poller.py -v`
Expected: All pass.

- [ ] **Step 13: Run full test suite**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/ -v`
Expected: All pass.

- [ ] **Step 14: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/health_poller.py src/backend/tests/test_health_poller.py
git commit -m "feat: evaluate partition thresholds in health poller, store warnings on host"
```

### Task 2.4: Frontend warning badges on hosts page

**Files:**
- Modify: `src/frontend/src/app/admin/hosts/page.tsx`

- [ ] **Step 15: Add warning icon to hosts table**

In the hosts admin page (`src/frontend/src/app/admin/hosts/page.tsx`), find where the host name or status is rendered in the table. Add a warning indicator when `storage_warnings` is non-empty.

Add the PatternFly icon import at the top:

```tsx
import { ExclamationTriangleIcon, ExclamationCircleIcon } from "@patternfly/react-icons";
import { Tooltip } from "@patternfly/react-core";
```

Then next to the host name/status cell, add a conditional badge:

```tsx
{host.storage_warnings && host.storage_warnings.length > 0 && (
  <Tooltip
    content={
      <div>
        {host.storage_warnings.map((w: any, i: number) => (
          <div key={i}>
            {w.mount}: {w.used_pct}% used ({w.level})
          </div>
        ))}
      </div>
    }
  >
    {host.storage_warnings.some((w: any) => w.level === "critical") ? (
      <ExclamationCircleIcon style={{ color: "var(--pf-t--global--color--status--danger--default)", marginLeft: 8 }} />
    ) : (
      <ExclamationTriangleIcon style={{ color: "var(--pf-t--global--color--status--warning--default)", marginLeft: 8 }} />
    )}
  </Tooltip>
)}
```

Also ensure the API response type includes `storage_warnings` — check the `HostResponse` schema in `src/backend/app/api/hosts.py` and add the field if needed:

```python
storage_warnings: list | None = None
```

- [ ] **Step 16: Commit**

```bash
cd /Users/prutledg/troshka && git add src/frontend/src/app/admin/hosts/page.tsx src/backend/app/api/hosts.py
git commit -m "feat: show storage warning badges on hosts admin page"
```

---

## Module 3: S3 Temp File Redirect

Set `TMPDIR` in subprocess env for S3 operations to keep temp files off root. Add stale temp cleanup to GC.

### Task 3.1: Redirect S3 temp files and add GC cleanup

**Files:**
- Modify: `src/troshkad/troshkad.py`

- [ ] **Step 1: Add TMPDIR to S3 subprocess calls**

In `_s3_download()` (line ~2843) and `_s3_upload()` (line ~2810), after the `env = os.environ.copy()` line, add:

```python
    _s3_tmpdir = os.path.join(_config.get("local_mount", "/var/lib/troshka/local"), "tmp")
    os.makedirs(_s3_tmpdir, exist_ok=True)
    env["TMPDIR"] = _s3_tmpdir
```

This goes right after the `env = os.environ.copy()` line and before the `if aws_access_key:` block in both functions.

- [ ] **Step 2: Add stale temp cleanup to GC discover handler**

In `_handle_gc_discover()` (line ~2461), add a step after the existing scans. Append this before the return statement:

```python
    # 6. Scan S3 temp dir for stale files (older than 1 hour)
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
```

Also include `stale_temps` in the return dict that `_handle_gc_discover` returns.

Then in `_handle_gc_clean()` (line ~2617), add cleanup for `stale_temps` from the discover result:

```python
    for path in items.get("stale_temps", []):
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            _job_log(job, f"Removed stale temp: {path}")
        except OSError as e:
            _job_log(job, f"Failed to remove {path}: {e}")
```

- [ ] **Step 3: Commit**

```bash
cd /Users/prutledg/troshka && git add src/troshkad/troshkad.py
git commit -m "feat: redirect S3 temp files to data partition, clean stale temps in GC"
```

---

## Module 4: Block Threshold Events + Storage Auto-Extend

Libvirt block threshold events in troshkad, auto-extend policy per pool (FSx) and per host (EBS), manual extend buttons in UI.

### Task 4.1: Add auto-extend columns to StoragePool and Host models

**Files:**
- Modify: `src/backend/app/models/storage_pool.py`
- Modify: `src/backend/app/models/host.py`
- Create: `src/backend/alembic/versions/xxxx_add_auto_extend_columns.py`

- [ ] **Step 1: Add auto-extend columns to StoragePool**

In `src/backend/app/models/storage_pool.py`, add to the `StoragePool` class after `status`:

```python
from sqlalchemy import Boolean

    auto_extend_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_extend_threshold_pct: Mapped[int] = mapped_column(Integer, default=80)
    auto_extend_increment_gb: Mapped[int] = mapped_column(Integer, default=64)
    auto_extend_max_gb: Mapped[int | None] = mapped_column(Integer, nullable=True)
```

- [ ] **Step 2: Add auto-extend columns to Host**

In `src/backend/app/models/host.py`, add to the `Host` class after `storage_warnings`:

```python
from sqlalchemy import Boolean

    auto_extend_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_extend_threshold_pct: Mapped[int] = mapped_column(Integer, default=80)
    auto_extend_increment_gb: Mapped[int] = mapped_column(Integer, default=100)
    auto_extend_max_gb: Mapped[int | None] = mapped_column(Integer, nullable=True)
```

- [ ] **Step 3: Create alembic migration**

Run: `cd src/backend && ./venv/bin/python3 -m alembic revision -m "add auto_extend columns to storage_pools and hosts"`

Edit the migration:

```python
from alembic import op
import sqlalchemy as sa

def upgrade():
    op.add_column('storage_pools', sa.Column('auto_extend_enabled', sa.Boolean(), server_default='false', nullable=False))
    op.add_column('storage_pools', sa.Column('auto_extend_threshold_pct', sa.Integer(), server_default='80', nullable=False))
    op.add_column('storage_pools', sa.Column('auto_extend_increment_gb', sa.Integer(), server_default='64', nullable=False))
    op.add_column('storage_pools', sa.Column('auto_extend_max_gb', sa.Integer(), nullable=True))
    op.add_column('hosts', sa.Column('auto_extend_enabled', sa.Boolean(), server_default='false', nullable=False))
    op.add_column('hosts', sa.Column('auto_extend_threshold_pct', sa.Integer(), server_default='80', nullable=False))
    op.add_column('hosts', sa.Column('auto_extend_increment_gb', sa.Integer(), server_default='100', nullable=False))
    op.add_column('hosts', sa.Column('auto_extend_max_gb', sa.Integer(), nullable=True))

def downgrade():
    op.drop_column('hosts', 'auto_extend_max_gb')
    op.drop_column('hosts', 'auto_extend_increment_gb')
    op.drop_column('hosts', 'auto_extend_threshold_pct')
    op.drop_column('hosts', 'auto_extend_enabled')
    op.drop_column('storage_pools', 'auto_extend_max_gb')
    op.drop_column('storage_pools', 'auto_extend_increment_gb')
    op.drop_column('storage_pools', 'auto_extend_threshold_pct')
    op.drop_column('storage_pools', 'auto_extend_enabled')
```

- [ ] **Step 4: Run migration**

Run: `cd src/backend && ./venv/bin/python3 -m alembic upgrade head`

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/models/storage_pool.py src/backend/app/models/host.py src/backend/alembic/versions/
git commit -m "feat: add auto-extend config columns to storage_pools and hosts"
```

### Task 4.2: Storage extend service

**Files:**
- Create: `src/backend/app/services/storage_extend.py`
- Create: `src/backend/tests/test_storage_extend.py`

- [ ] **Step 6: Write test for storage extend service**

```python
# src/backend/tests/test_storage_extend.py
"""Tests for storage auto-extend service."""
import os
import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

os.environ["TROSHKA_DATABASE__URL"] = "sqlite:///./test.db"


class TestStorageExtend(unittest.TestCase):

    def test_should_extend_host_above_threshold(self):
        from app.services.storage_extend import should_extend_host
        host = MagicMock()
        host.auto_extend_enabled = True
        host.auto_extend_threshold_pct = 80
        host.auto_extend_max_gb = 1000
        host.storage_size_gb = 500
        host.storage_warnings = [{"mount": "/var/lib/troshka", "used_pct": 85.0, "level": "warning"}]

        result = should_extend_host(host)
        self.assertTrue(result)

    def test_should_not_extend_host_disabled(self):
        from app.services.storage_extend import should_extend_host
        host = MagicMock()
        host.auto_extend_enabled = False
        host.auto_extend_threshold_pct = 80
        host.storage_warnings = [{"mount": "/var/lib/troshka", "used_pct": 85.0, "level": "warning"}]

        result = should_extend_host(host)
        self.assertFalse(result)

    def test_should_not_extend_host_at_max(self):
        from app.services.storage_extend import should_extend_host
        host = MagicMock()
        host.auto_extend_enabled = True
        host.auto_extend_threshold_pct = 80
        host.auto_extend_max_gb = 500
        host.storage_size_gb = 500
        host.storage_warnings = [{"mount": "/var/lib/troshka", "used_pct": 85.0, "level": "warning"}]

        result = should_extend_host(host)
        self.assertFalse(result)

    def test_should_extend_pool_above_threshold(self):
        from app.services.storage_extend import should_extend_pool
        pool = MagicMock()
        pool.auto_extend_enabled = True
        pool.auto_extend_threshold_pct = 80
        pool.auto_extend_max_gb = 1000
        pool.fsx_storage_gb = 256
        pool.mode = "shared-fsx"

        result = should_extend_pool(pool, current_used_pct=85.0)
        self.assertTrue(result)

    def test_should_not_extend_byo_pool(self):
        from app.services.storage_extend import should_extend_pool
        pool = MagicMock()
        pool.auto_extend_enabled = True
        pool.auto_extend_threshold_pct = 80
        pool.mode = "shared-byo"

        result = should_extend_pool(pool, current_used_pct=90.0)
        self.assertFalse(result)
```

- [ ] **Step 7: Run tests to verify they fail**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_storage_extend.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 8: Implement storage extend service**

```python
# src/backend/app/services/storage_extend.py
"""Auto-extend and manual extend for FSx (pool) and EBS (host) storage."""
import logging
import time

logger = logging.getLogger(__name__)

_last_extend: dict[str, float] = {}
_COOLDOWN_SECONDS = 600


def _on_cooldown(target_id: str) -> bool:
    last = _last_extend.get(target_id, 0)
    return (time.time() - last) < _COOLDOWN_SECONDS


def _mark_extended(target_id: str):
    _last_extend[target_id] = time.time()


def should_extend_host(host) -> bool:
    if not host.auto_extend_enabled:
        return False
    if host.auto_extend_max_gb and host.storage_size_gb >= host.auto_extend_max_gb:
        return False
    if _on_cooldown(f"host:{host.id}"):
        return False
    warnings = host.storage_warnings or []
    data_mounts = ["/var/lib/troshka", "/var/lib/troshka/local"]
    for w in warnings:
        if w["mount"] in data_mounts and w["used_pct"] >= host.auto_extend_threshold_pct:
            return True
    return False


def should_extend_pool(pool, current_used_pct: float) -> bool:
    if pool.mode != "shared-fsx":
        return False
    if not pool.auto_extend_enabled:
        return False
    if pool.auto_extend_max_gb and (pool.fsx_storage_gb or 0) >= pool.auto_extend_max_gb:
        return False
    if _on_cooldown(f"pool:{pool.id}"):
        return False
    return current_used_pct >= pool.auto_extend_threshold_pct


def extend_host_ebs(host, db, increment_gb: int | None = None):
    """Extend a host's EBS data volume. Returns new size or raises."""
    increment = increment_gb or host.auto_extend_increment_gb
    new_size = host.storage_size_gb + increment

    if host.auto_extend_max_gb:
        new_size = min(new_size, host.auto_extend_max_gb)
    if new_size <= host.storage_size_gb:
        raise ValueError(f"Cannot extend: already at max ({host.storage_size_gb} GB)")

    provider = host.provider
    if not provider:
        raise ValueError("No provider associated with host")
    creds = provider.get_credentials()

    from app.services.provisioner import _get_ec2_client
    ec2 = _get_ec2_client(credentials=creds)

    volumes = ec2.describe_volumes(Filters=[
        {"Name": "attachment.instance-id", "Values": [host.instance_id]},
        {"Name": "attachment.device", "Values": ["/dev/sdf", "/dev/xvdf"]},
    ])
    if not volumes["Volumes"]:
        raise ValueError("No data volume found on instance")

    vol_id = volumes["Volumes"][0]["VolumeId"]
    old_size = host.storage_size_gb

    ec2.modify_volume(VolumeId=vol_id, Size=new_size)
    logger.info("Extended EBS volume %s from %d to %d GB for host %s",
                vol_id, old_size, new_size, host.id[:8])

    if host.agent_status == "connected":
        from app.services.troshkad_client import start_job, wait_for_job
        job_id = start_job(host, "/host/resize-storage", {})
        wait_for_job(host, job_id, timeout=30)

    host.storage_size_gb = new_size
    db.commit()
    _mark_extended(f"host:{host.id}")
    return {"old_size_gb": old_size, "new_size_gb": new_size, "volume_id": vol_id}


def extend_pool_fsx(pool, db, increment_gb: int | None = None):
    """Extend an FSx filesystem. Returns new size or raises."""
    increment = increment_gb or pool.auto_extend_increment_gb
    new_size = (pool.fsx_storage_gb or 0) + increment

    if pool.auto_extend_max_gb:
        new_size = min(new_size, pool.auto_extend_max_gb)
    if new_size <= (pool.fsx_storage_gb or 0):
        raise ValueError(f"Cannot extend: already at max ({pool.fsx_storage_gb} GB)")

    import math
    min_grow = math.ceil((pool.fsx_storage_gb or 64) * 1.1)
    if new_size < min_grow:
        new_size = min_grow

    from app.models.provider import Provider
    provider = db.query(Provider).get(pool.provider_id)
    if not provider:
        raise ValueError("No provider associated with pool")
    creds = provider.get_credentials()

    from app.services.storage_pool_service import update_fsx_storage
    old_size = pool.fsx_storage_gb or 0
    update_fsx_storage(creds, provider.default_region, pool.fsx_filesystem_id, new_size)

    pool.fsx_storage_gb = new_size
    db.commit()
    _mark_extended(f"pool:{pool.id}")
    logger.info("Extended FSx %s from %d to %d GB for pool %s",
                pool.fsx_filesystem_id, old_size, new_size, pool.name)
    return {"old_size_gb": old_size, "new_size_gb": new_size, "filesystem_id": pool.fsx_filesystem_id}
```

- [ ] **Step 9: Run tests to verify they pass**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_storage_extend.py -v`
Expected: All pass.

- [ ] **Step 10: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/storage_extend.py src/backend/tests/test_storage_extend.py
git commit -m "feat: storage extend service for FSx pools and EBS hosts"
```

### Task 4.3: Wire auto-extend into health poller

**Files:**
- Modify: `src/backend/app/services/health_poller.py`

- [ ] **Step 11: Add auto-extend trigger to health poller**

After the partition evaluation line in `_poll_hosts()` (the `host.storage_warnings = _evaluate_partitions(health)` line added in Module 2), add:

```python
                    if host.storage_warnings:
                        try:
                            from app.services.storage_extend import should_extend_host, extend_host_ebs
                            if should_extend_host(host):
                                logger.info("Auto-extending EBS for host %s", host.id[:8])
                                extend_host_ebs(host, db)
                        except Exception:
                            logger.warning("Auto-extend failed for host %s", host.id[:8], exc_info=True)
```

Also add FSx pool-level check. Inside the per-host loop, after the EBS auto-extend check, add a pool-level check using a `_checked_pools` set (initialized before the loop) to avoid duplicate checks:

```python
        _checked_pools = set()  # ADD before the for host in hosts: loop
```

Then inside the loop, after the host EBS auto-extend block:

```python
                    if host.storage_pool_id and host.storage_pool_id not in _checked_pools:
                        _checked_pools.add(host.storage_pool_id)
                        pool = host.storage_pool
                        if pool and pool.mode == "shared-fsx":
                            partitions = health.get("partitions", [])
                            shared_mount = next((p for p in partitions if "shared" in p.get("mount", "")), None)
                            if shared_mount:
                                try:
                                    from app.services.storage_extend import should_extend_pool, extend_pool_fsx
                                    if should_extend_pool(pool, shared_mount["used_pct"]):
                                        logger.info("Auto-extending FSx for pool %s", pool.name)
                                        extend_pool_fsx(pool, db)
                                except Exception:
                                    logger.warning("Auto-extend failed for pool %s", pool.name, exc_info=True)
```

- [ ] **Step 12: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/health_poller.py
git commit -m "feat: wire auto-extend into health poller for EBS and FSx"
```

### Task 4.4: Manual extend API endpoints

**Files:**
- Modify: `src/backend/app/api/hosts.py`
- Modify: `src/backend/app/api/storage_pools.py`

- [ ] **Step 13: Add manual extend endpoint for hosts**

In `src/backend/app/api/hosts.py`, add after the existing `resize_storage` route:

```python
@router.post("/{host_id}/extend-storage")
def extend_storage(host_id: str, body: dict | None = None,
                   user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Auto-extend the host's EBS data volume by the configured increment."""
    host = db.query(Host).filter_by(id=host_id).first()
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
    if not host.instance_id:
        raise HTTPException(status_code=400, detail="No EC2 instance associated")

    increment_gb = (body or {}).get("increment_gb")
    from app.services.storage_extend import extend_host_ebs
    try:
        result = extend_host_ebs(host, db, increment_gb=increment_gb)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return result
```

- [ ] **Step 14: Add manual extend endpoint for pools**

In `src/backend/app/api/storage_pools.py`, add after the existing `update_pool` route:

```python
@router.post("/{pool_id}/extend")
def extend_pool(pool_id: str, body: dict | None = None,
                user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Extend the FSx filesystem by the configured increment."""
    pool = db.query(StoragePool).get(pool_id)
    if not pool:
        raise HTTPException(404, "Storage pool not found")
    if pool.mode != "shared-fsx":
        raise HTTPException(400, "Only FSx pools can be extended")

    increment_gb = (body or {}).get("increment_gb")
    from app.services.storage_extend import extend_pool_fsx
    try:
        result = extend_pool_fsx(pool, db, increment_gb=increment_gb)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return result
```

- [ ] **Step 15: Update PATCH endpoints to accept auto-extend fields**

In the existing `StoragePoolUpdate` and `HostResponse` schemas (find these in the respective API files or schema files), add:

```python
auto_extend_enabled: bool | None = None
auto_extend_threshold_pct: int | None = None
auto_extend_increment_gb: int | None = None
auto_extend_max_gb: int | None = None
```

And in the PATCH handlers, apply these fields when provided.

- [ ] **Step 16: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/api/hosts.py src/backend/app/api/storage_pools.py
git commit -m "feat: manual extend endpoints and auto-extend config for pools and hosts"
```

### Task 4.5: Block threshold events in troshkad

**Files:**
- Modify: `src/troshkad/troshkad.py`

- [ ] **Step 17: Register block threshold callback in libvirt event loop**

In `_start_libvirt_event_loop()` (line ~3260), after the lifecycle callback registration, add:

```python
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

        # Re-arm at next increment (e.g., 80% → 90%)
        try:
            info = dom.blockInfo(dev)
            if info:
                capacity = info[0]
                new_threshold = int(capacity * 0.9)
                if new_threshold > threshold:
                    dom.setBlockThreshold(dev, new_threshold)
        except Exception:
            pass
```

Then register it after the lifecycle registration:

```python
        conn.domainEventRegisterAny(None, _lv.VIR_DOMAIN_EVENT_ID_BLOCK_THRESHOLD,
                                     _block_threshold_cb, None)
```

- [ ] **Step 18: Set initial thresholds on VM start**

In the VM creation handler (find `_handle_vm_create` or equivalent), after the domain is started, set block thresholds on each disk:

```python
    # Set block thresholds on disks
    threshold_pct = params.get("block_threshold_pct", 80)
    try:
        import libvirt as _lv
        conn = _lv.open("qemu:///system")
        if conn:
            dom = conn.lookupByName(domain_name)
            if dom:
                blklist = subprocess.run(
                    ["virsh", "domblklist", domain_name, "--details"],
                    capture_output=True, text=True, timeout=10)
                if blklist.returncode == 0:
                    for line in blklist.stdout.strip().split("\n"):
                        parts = line.split()
                        if len(parts) >= 4 and parts[1] == "disk":
                            target = parts[2]
                            try:
                                info = dom.blockInfo(target)
                                if info and info[0] > 0:
                                    threshold = int(info[0] * threshold_pct / 100)
                                    dom.setBlockThreshold(target, threshold)
                            except Exception:
                                pass
            conn.close()
    except Exception:
        pass
```

- [ ] **Step 19: Commit**

```bash
cd /Users/prutledg/troshka && git add src/troshkad/troshkad.py
git commit -m "feat: libvirt block threshold events with re-arming in troshkad"
```

### Task 4.6: Frontend auto-extend settings

**Files:**
- Modify: `src/frontend/src/app/admin/storage-pools/page.tsx`
- Modify: `src/frontend/src/app/admin/hosts/page.tsx`

- [ ] **Step 20: Add auto-extend settings to storage pools page**

On the storage pools admin page, add an expandable settings section per pool. Use PatternFly `ExpandableSection`, `Switch`, `NumberInput`, and `Button`:

```tsx
<ExpandableSection toggleText="Auto-Extend Settings">
  <FormGroup label="Auto-extend enabled">
    <Switch isChecked={pool.auto_extend_enabled} onChange={(_, val) => updatePool(pool.id, { auto_extend_enabled: val })} />
  </FormGroup>
  <FormGroup label="Threshold (%)">
    <NumberInput value={pool.auto_extend_threshold_pct} min={50} max={95}
      onMinus={() => updatePool(pool.id, { auto_extend_threshold_pct: pool.auto_extend_threshold_pct - 5 })}
      onPlus={() => updatePool(pool.id, { auto_extend_threshold_pct: pool.auto_extend_threshold_pct + 5 })}
      onChange={(e) => updatePool(pool.id, { auto_extend_threshold_pct: Number((e.target as HTMLInputElement).value) })} />
  </FormGroup>
  <FormGroup label="Increment (GB)">
    <NumberInput value={pool.auto_extend_increment_gb} min={10}
      onMinus={() => updatePool(pool.id, { auto_extend_increment_gb: pool.auto_extend_increment_gb - 10 })}
      onPlus={() => updatePool(pool.id, { auto_extend_increment_gb: pool.auto_extend_increment_gb + 10 })}
      onChange={(e) => updatePool(pool.id, { auto_extend_increment_gb: Number((e.target as HTMLInputElement).value) })} />
  </FormGroup>
  <FormGroup label="Max size (GB, blank = no limit)">
    <TextInput type="number" value={pool.auto_extend_max_gb ?? ""} onChange={(_, val) => updatePool(pool.id, { auto_extend_max_gb: val ? Number(val) : null })} />
  </FormGroup>
  <Button variant="secondary" onClick={() => extendPool(pool.id)}>Extend Now</Button>
</ExpandableSection>
```

The `extendPool` function calls `POST /api/storage-pools/{id}/extend` and shows a confirmation modal first with current → new size.

- [ ] **Step 21: Add auto-extend settings to hosts page**

Same pattern on the hosts admin page. Per-host expandable section with the same controls, calling `PATCH /api/hosts/{id}` for config and `POST /api/hosts/{id}/extend-storage` for manual extend.

- [ ] **Step 22: Commit**

```bash
cd /Users/prutledg/troshka && git add src/frontend/src/app/admin/storage-pools/page.tsx src/frontend/src/app/admin/hosts/page.tsx
git commit -m "feat: auto-extend settings and manual extend buttons in admin UI"
```

---

## Module 5: Streaming Pattern Capture

Replace temp-file flatten+upload with a piped pipeline: `qemu-img convert | tee cache | aws s3 cp -`.

### Task 5.1: Implement streaming capture pipeline

**Files:**
- Modify: `src/troshkad/troshkad.py`

- [ ] **Step 1: Add streaming pipeline helper function**

Add this helper near the S3 functions (after `_s3_upload`, around line 2840):

```python
def _streaming_capture_upload(job, disk_path, s3_url, cache_path, aws_access_key="",
                               aws_secret_key="", aws_region="us-east-1", use_unsafe=False):
    """Pipe qemu-img convert → tee (cache) → aws s3 cp (S3).
    
    Three-process pipeline: compress, tee to local cache, and upload
    to S3 simultaneously. No temp file needed.
    """
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)

    env = os.environ.copy()
    if aws_access_key:
        env["AWS_ACCESS_KEY_ID"] = aws_access_key
        env["AWS_SECRET_ACCESS_KEY"] = aws_secret_key
        env["AWS_DEFAULT_REGION"] = aws_region
    _s3_tmpdir = os.path.join(_config.get("local_mount", "/var/lib/troshka/local"), "tmp")
    os.makedirs(_s3_tmpdir, exist_ok=True)
    env["TMPDIR"] = _s3_tmpdir

    aws_bin = "/opt/troshka/venv/bin/aws"
    if not os.path.exists(aws_bin):
        aws_bin = "aws"

    convert_cmd = ["qemu-img", "convert", "-c", "-o", "compression_type=zstd", "-O", "qcow2"]
    if use_unsafe:
        convert_cmd.insert(2, "-U")
    convert_cmd.extend([disk_path, "/dev/stdout"])

    qemu_proc = subprocess.Popen(convert_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    tee_proc = subprocess.Popen(["tee", cache_path], stdin=qemu_proc.stdout,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    qemu_proc.stdout.close()
    s3_proc = subprocess.Popen([aws_bin, "s3", "cp", "-", s3_url],
                                stdin=tee_proc.stdout, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, env=env)
    tee_proc.stdout.close()

    # Monitor progress via cache file size
    monitor_done = threading.Event()
    def _monitor():
        while not monitor_done.is_set():
            try:
                if os.path.exists(cache_path):
                    cur = os.path.getsize(cache_path)
                    if cur > 0:
                        cur_gb = round(cur / (1024**3), 1)
                        _job_log(job, f"Streaming: {cur_gb} GB compressed")
            except OSError:
                pass
            monitor_done.wait(10)
    mon = threading.Thread(target=_monitor, daemon=True)
    mon.start()

    s3_proc.wait()
    tee_proc.wait()
    qemu_proc.wait()
    monitor_done.set()

    if qemu_proc.returncode != 0:
        stderr = qemu_proc.stderr.read().decode().strip()
        try:
            os.remove(cache_path)
        except OSError:
            pass
        raise RuntimeError(f"qemu-img convert failed (exit {qemu_proc.returncode}): {stderr}")

    if s3_proc.returncode != 0:
        stderr = s3_proc.stderr.read().decode().strip()
        try:
            os.remove(cache_path)
        except OSError:
            pass
        raise RuntimeError(f"S3 upload failed (exit {s3_proc.returncode}): {stderr}")

    if tee_proc.returncode != 0:
        try:
            os.remove(cache_path)
        except OSError:
            pass
        raise RuntimeError(f"tee failed (exit {tee_proc.returncode})")

    size_bytes = os.path.getsize(cache_path)
    size_gb = round(size_bytes / (1024**3), 1)
    _job_log(job, f"Streaming capture complete: {size_gb} GB compressed")
    return size_bytes
```

- [ ] **Step 2: Update `_handle_pattern_capture_direct` to use streaming**

Replace the flatten+upload section in `_handle_pattern_capture_direct` (line ~3090). The snapshot flow stays the same — only the middle section changes.

Replace this block (approximately lines 3103-3155):

```python
        _local_tmp = os.path.join(_config.get("local_mount", "/var/lib/troshka/local"), "tmp")
        os.makedirs(_local_tmp, exist_ok=True)
        with _tf.TemporaryDirectory(dir=_local_tmp) as tmpdir:
            tmp_flat = os.path.join(tmpdir, "flat.qcow2")
            # ... flatten + upload + cache ...
```

With:

```python
        use_unsafe = running and not snapshotted
        _job_log(job, f"Streaming compress+upload for {os.path.basename(disk_path)}...")

        commit_thread = None
        if snapshotted:
            def _do_commit():
                _commit_snapshot(job, domain_name)
            commit_thread = threading.Thread(target=_do_commit, daemon=True)
            commit_thread.start()
            snapshotted = False

        try:
            size_bytes = _streaming_capture_upload(
                job, disk_path, s3_url, cache_path,
                aws_access_key, aws_secret_key, aws_region,
                use_unsafe=use_unsafe,
            )
        except RuntimeError:
            _job_log(job, "Streaming capture failed, falling back to temp file approach")
            import tempfile as _tf
            _local_tmp = os.path.join(_config.get("local_mount", "/var/lib/troshka/local"), "tmp")
            os.makedirs(_local_tmp, exist_ok=True)
            with _tf.TemporaryDirectory(dir=_local_tmp) as tmpdir:
                tmp_flat = os.path.join(tmpdir, "flat.qcow2")
                cmd = ["qemu-img", "convert", "-c", "-o", "compression_type=zstd", "-O", "qcow2"]
                if use_unsafe:
                    cmd.insert(2, "-U")
                cmd.extend([disk_path, tmp_flat])
                _run_cmd(job, cmd, timeout=3600)
                _s3_upload(job, tmp_flat, s3_url, aws_access_key, aws_secret_key, aws_region)
                os.makedirs(os.path.dirname(cache_path), exist_ok=True)
                shutil.copy(tmp_flat, cache_path)
            size_bytes = os.path.getsize(cache_path)

        if commit_thread:
            commit_thread.join(timeout=600)

        result_disks.append({"size_bytes": size_bytes})
```

Note: the overlay commit starts right after the snapshot completes (in parallel with the streaming upload), same as the current code. The `snapshotted = False` flag prevents the finally block from committing again.

- [ ] **Step 3: Run full test suite to check for regressions**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/ -v`
Expected: All pass. Pattern capture tests shouldn't break since they test the backend service, not troshkad internals.

- [ ] **Step 4: Commit**

```bash
cd /Users/prutledg/troshka && git add src/troshkad/troshkad.py
git commit -m "feat: streaming pattern capture — pipe compress+upload, eliminate temp file"
```
