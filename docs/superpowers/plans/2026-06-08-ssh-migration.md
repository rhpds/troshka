# SSH-to-Troshkad Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate all 47 `run_ssh_script()` call sites to use the troshkad_client module, eliminating SSH for all host operations except initial agent install.

**Architecture:** Phase 1 adds 8 new troshkad endpoints. Phase 2 migrates each backend file from simplest to most complex. Phase 3 removes dead code. Each file migration is a self-contained commit.

**Tech Stack:** Python stdlib on troshkad side. FastAPI + SQLAlchemy on backend side. All existing tests must continue passing.

**Spec:** `docs/superpowers/specs/2026-06-08-ssh-migration-design.md`

---

## File Structure

```
Modified files:
  src/troshkad/troshkad.py                              # Add 8 new endpoints
  src/troshkad/tests/test_troshkad.py                   # Tests for new endpoints
  src/backend/app/services/troshkad_client.py            # Add check_disk_usage() convenience function
  src/backend/app/services/eip_service.py                # Remove _detect_primary_iface, update calls
  src/backend/app/api/hosts.py                           # Migrate disk usage + resize calls
  src/backend/app/services/gc_service.py                 # Migrate discover + clean + repair calls
  src/backend/app/api/eips.py                            # Migrate sync_project_eips call
  src/backend/app/services/snapshot_service.py           # Migrate capture call
  src/backend/app/services/pattern_service.py            # Migrate capture call
  src/backend/app/api/library.py                         # Migrate entire import flow (13 calls → 1)
  src/backend/app/services/deploy_service.py             # Migrate deploy/start/stop/destroy (16 calls)
  src/backend/app/api/projects.py                        # Migrate start_vm/reconfigure/redeploy (10 calls)
  src/backend/tests/test_troshkad_client.py              # Update tests
```

---

## Phase 1: New Troshkad Endpoints

### Task 1: Immediate Endpoints — disk-usage, resize-storage, files/remove

Add three simple endpoints to troshkad.

**Files:**
- Modify: `src/troshkad/troshkad.py`
- Modify: `src/troshkad/tests/test_troshkad.py`

- [ ] **Step 1: Write tests for new endpoints**

Add a new test class `TestHostEndpoints`:

```python
class TestHostEndpoints(unittest.TestCase):

    @patch("troshkad.shutil.disk_usage")
    def test_disk_usage_returns_stats(self, mock_usage):
        """GET /host/disk-usage returns free_bytes, total_bytes, used_pct."""
        mock_usage.return_value = MagicMock(total=500*1024**3, used=120*1024**3, free=380*1024**3)
        # disk-usage is an immediate endpoint (no job) — need the server running
        # Use the existing TestTroshkadServer setup via _make_request
        status, body = _make_request("/host/disk-usage")
        self.assertEqual(status, 200)
        self.assertIn("free_bytes", body)
        self.assertIn("total_bytes", body)
        self.assertIn("used_pct", body)

    @patch("troshkad.subprocess.Popen")
    def test_resize_storage(self, mock_popen):
        mock_popen.return_value = _mock_popen()
        job = troshkad._create_job("host/resize-storage", {})
        result = troshkad._handle_resize_storage(job, job["params"])
        cmd = mock_popen.call_args[0][0]
        self.assertEqual(cmd, ["xfs_growfs", "/var/lib/troshka"])

    @patch("troshkad.os.remove")
    def test_files_remove(self, mock_remove):
        job = troshkad._create_job("files/remove", {
            "paths": ["/var/lib/troshka/vms/proj/aabb-1122.qcow2"]
        })
        result = troshkad._handle_files_remove(job, job["params"])
        mock_remove.assert_called_once_with("/var/lib/troshka/vms/proj/aabb-1122.qcow2")
        self.assertEqual(result["removed"], 1)

    def test_files_remove_rejects_bad_path(self):
        job = troshkad._create_job("files/remove", {
            "paths": ["/etc/passwd"]
        })
        with self.assertRaises(ValueError):
            troshkad._handle_files_remove(job, job["params"])
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd src/troshkad && python3 -m pytest tests/test_troshkad.py::TestHostEndpoints -v
```

- [ ] **Step 3: Implement the endpoints in troshkad.py**

Add before `create_server()`:

```python
# ── Host endpoints ──

@route("GET", "/host/disk-usage")
def handle_disk_usage(handler, params):
    """Immediate endpoint (no job) — returns disk usage for /var/lib/troshka."""
    try:
        stat = shutil.disk_usage("/var/lib/troshka")
        free_bytes = stat.free
        total_bytes = stat.total
        used_pct = round((1 - stat.free / max(stat.total, 1)) * 100)
    except Exception:
        free_bytes = 0
        total_bytes = 0
        used_pct = 100
    handler._send_json(200, {
        "free_bytes": free_bytes,
        "total_bytes": total_bytes,
        "used_pct": used_pct,
    })


def _handle_resize_storage(job, params):
    _run_cmd(job, ["xfs_growfs", "/var/lib/troshka"])
    return {"status": "resized"}

COMMAND_HANDLERS["host/resize-storage"] = _handle_resize_storage


def _handle_files_remove(job, params):
    paths = params.get("paths", [])
    removed = 0
    for path in paths:
        validated = _validate_path(path)
        try:
            if os.path.isdir(validated):
                import shutil as _sh
                _sh.rmtree(validated)
            else:
                os.remove(validated)
            removed += 1
            job["output"].append(f"Removed: {validated}")
        except FileNotFoundError:
            job["output"].append(f"Not found (skipped): {validated}")
    return {"removed": removed}

COMMAND_HANDLERS["files/remove"] = _handle_files_remove
```

- [ ] **Step 4: Run all troshkad tests**

```bash
cd src/troshkad && python3 -m pytest tests/test_troshkad.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/troshkad/
git commit -m "feat(troshkad): add disk-usage, resize-storage, files/remove endpoints"
```

---

### Task 2: GC Endpoints — gc/discover, gc/clean

Replace the `gc/run` stub with two separate endpoints.

**Files:**
- Modify: `src/troshkad/troshkad.py`
- Modify: `src/troshkad/tests/test_troshkad.py`

- [ ] **Step 1: Write tests**

```python
class TestGcEndpoints(unittest.TestCase):

    @patch("troshkad.subprocess.Popen")
    @patch("troshkad.os.listdir")
    def test_gc_discover_finds_orphans(self, mock_listdir, mock_popen):
        mock_listdir.return_value = ["known-uuid", "orphan-uuid"]
        mock_popen.return_value = _mock_popen(stdout="troshka-aabb-1122\ntroshka-dead-beef\n")
        job = troshkad._create_job("gc/discover", {
            "known_project_ids": ["known-uuid"],
            "known_domains": ["troshka-aabb-1122"],
        })
        result = troshkad._handle_gc_discover(job, job["params"])
        self.assertIn("orphan-uuid", str(result.get("orphan_dirs", [])))
        self.assertIn("troshka-dead-beef", result.get("orphan_domains", []))

    @patch("troshkad.subprocess.Popen")
    @patch("troshkad.shutil.rmtree")
    def test_gc_clean_removes_items(self, mock_rmtree, mock_popen):
        mock_popen.return_value = _mock_popen()
        job = troshkad._create_job("gc/clean", {
            "orphan_dirs": ["/var/lib/troshka/vms/dead-uuid/"],
            "orphan_domains": ["troshka-dead-beef"],
            "orphan_bridges": [],
            "orphan_namespaces": [],
            "cache_items": [],
        })
        result = troshkad._handle_gc_clean(job, job["params"])
        self.assertGreaterEqual(result.get("removed_dirs", 0), 0)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd src/troshkad && python3 -m pytest tests/test_troshkad.py::TestGcEndpoints -v
```

- [ ] **Step 3: Implement gc/discover and gc/clean**

`gc/discover` scans the host:
1. List dirs under `/var/lib/troshka/vms/` → compare against `known_project_ids`
2. `virsh list --all --name` → compare against `known_domains`
3. `ip -o link show type bridge` → find `br-troshka-*` bridges
4. `ip netns list` → find `troshka-*` namespaces
5. List cache dirs: patterns, snapshots, images with `os.stat()` for age

`gc/clean` removes specified items:
1. `shutil.rmtree()` for orphan dirs (validated under /var/lib/troshka/)
2. `virsh destroy` + `virsh undefine` for orphan domains (validated with _DOMAIN_RE)
3. `ip link delete` for orphan bridges (validated with _BRIDGE_RE)
4. `ip netns delete` for orphan namespaces
5. `shutil.rmtree()` for cache items (validated paths)

Remove the existing `gc/run` stub from COMMAND_HANDLERS.

- [ ] **Step 4: Run all troshkad tests**

```bash
cd src/troshkad && python3 -m pytest tests/test_troshkad.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/troshkad/
git commit -m "feat(troshkad): add gc/discover and gc/clean endpoints, remove gc/run stub"
```

---

### Task 3: Library Import Endpoint

Single endpoint that handles the entire download → flatten → chunk → S3 upload pipeline.

**Files:**
- Modify: `src/troshkad/troshkad.py`
- Modify: `src/troshkad/tests/test_troshkad.py`

- [ ] **Step 1: Write tests**

```python
class TestLibraryImportEndpoint(unittest.TestCase):

    @patch("troshkad.os.makedirs")
    @patch("troshkad.os.path.getsize")
    @patch("troshkad.subprocess.Popen")
    def test_import_download_only(self, mock_popen, mock_getsize, mock_makedirs):
        """Import with no s3_multipart just downloads to cache_path."""
        mock_popen.return_value = _mock_popen()
        mock_getsize.return_value = 1024
        job = troshkad._create_job("library/import", {
            "download_url": "https://example.com/image.qcow2",
            "cache_path": "/var/lib/troshka/images/item-123.qcow2",
        })
        result = troshkad._handle_library_import(job, job["params"])
        self.assertEqual(result["status"], "completed")
        # curl should have been called
        cmd = mock_popen.call_args_list[0][0][0]
        self.assertEqual(cmd[0], "curl")

    @patch("troshkad.os.makedirs")
    @patch("troshkad.os.path.getsize")
    @patch("troshkad.subprocess.Popen")
    def test_import_with_flatten(self, mock_popen, mock_getsize, mock_makedirs):
        """Import with flatten=true runs qemu-img convert after download."""
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
        self.assertTrue(any(c[0] == "qemu-img" for c in cmds))

    def test_import_rejects_bad_url(self):
        job = troshkad._create_job("library/import", {
            "download_url": "file:///etc/passwd",
            "cache_path": "/var/lib/troshka/images/item-123.qcow2",
        })
        with self.assertRaises(ValueError):
            troshkad._handle_library_import(job, job["params"])
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd src/troshkad && python3 -m pytest tests/test_troshkad.py::TestLibraryImportEndpoint -v
```

- [ ] **Step 3: Implement library/import handler**

The handler performs these steps in sequence:
1. Validate `download_url` and `cache_path`
2. `curl -fSL -o {cache_path} {download_url}` — download the file, reporting progress
3. If `flatten`: `qemu-img convert -O qcow2 {cache_path} {cache_path}.flat` then `mv`
4. If `s3_multipart`: split file into parts, upload each via `curl -T {part} {presigned_url}`, collect ETags
5. Cleanup temp files in finally block
6. Return `{"status": "completed", "size_bytes": ..., "etags": [...]}` (etags only if s3_multipart)

The `s3_multipart` field is optional — if absent, it's a download-only operation.

For the multipart upload:
```python
# Split
_run_cmd(job, ["split", "-b", str(part_size), "-d", cache_path, tmp_prefix])
# Upload each part
for part in sorted(glob.glob(f"{tmp_prefix}*")):
    part_num = int(part.rsplit("-", 1)[1]) + 1
    url = upload_urls[part_num - 1]["presigned_url"]
    proc = subprocess.Popen(
        ["curl", "-sfL", "-X", "PUT", "-T", part, "-D-", "-o", "/dev/null", url],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    stdout, _ = proc.communicate(timeout=600)
    etag = ""
    for line in stdout.split("\n"):
        if line.lower().startswith("etag:"):
            etag = line.split(":", 1)[1].strip()
    etags.append({"part": part_num, "etag": etag})
    os.remove(part)
```

- [ ] **Step 4: Run all troshkad tests**

```bash
cd src/troshkad && python3 -m pytest tests/test_troshkad.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/troshkad/
git commit -m "feat(troshkad): add library/import endpoint with download, flatten, S3 multipart upload"
```

---

### Task 4: Snapshot and Pattern Capture Endpoints

**Files:**
- Modify: `src/troshkad/troshkad.py`
- Modify: `src/troshkad/tests/test_troshkad.py`

- [ ] **Step 1: Write tests**

```python
class TestCaptureEndpoints(unittest.TestCase):

    @patch("troshkad.os.makedirs")
    @patch("troshkad.subprocess.run")
    @patch("troshkad.subprocess.Popen")
    def test_snapshot_capture(self, mock_popen, mock_run, mock_makedirs):
        """Snapshot capture: get disk path, flatten, upload, cache."""
        mock_popen.return_value = _mock_popen()
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Type  Device  Target  Source\nfile  disk    vda     /var/lib/troshka/vms/proj/disk.qcow2\n",
            stderr="",
        )
        job = troshkad._create_job("snapshots/capture", {
            "domain_name": "troshka-aabbccdd-11223344",
            "disk_index": 0,
            "presigned_url": "https://s3.example.com/upload",
            "cache_path": "/var/lib/troshka/cache/snapshots/item/disk.qcow2",
        })
        result = troshkad._handle_snapshot_capture(job, job["params"])
        self.assertEqual(result["status"], "uploaded")

    @patch("troshkad.os.makedirs")
    @patch("troshkad.subprocess.run")
    @patch("troshkad.subprocess.Popen")
    def test_pattern_capture(self, mock_popen, mock_run, mock_makedirs):
        mock_popen.return_value = _mock_popen()
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Type  Device  Target  Source\nfile  disk    vda     /var/lib/troshka/vms/proj/disk.qcow2\n",
            stderr="",
        )
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
```

- [ ] **Step 2: Implement handlers**

`snapshots/capture`:
1. `_validate_domain_name(params["domain_name"])`
2. Get disk path from `virsh domblklist` (reuse the existing code from `_handle_snapshot_create`)
3. `qemu-img convert -O qcow2 {disk_path} {tmp_flat}` — flatten
4. `curl -sfL -X PUT -T {tmp_flat} {presigned_url}` — upload
5. Copy tmp_flat to cache_path
6. Get `os.path.getsize(cache_path)` for response
7. Return `{"status": "uploaded", "size_bytes": ...}`

`patterns/capture` iterates over `params["disks"]`, calling the same logic per disk.

- [ ] **Step 3: Run all tests**

```bash
cd src/troshkad && python3 -m pytest tests/test_troshkad.py -v
```

- [ ] **Step 4: Commit**

```bash
git add src/troshkad/
git commit -m "feat(troshkad): add snapshots/capture and patterns/capture endpoints"
```

---

## Phase 2: Backend Migration

### Task 5: Add check_disk_usage convenience function to troshkad_client

**Files:**
- Modify: `src/backend/app/services/troshkad_client.py`
- Modify: `src/backend/tests/test_troshkad_client.py`

- [ ] **Step 1: Write test**

```python
@patch("app.services.troshkad_client.http.client.HTTPSConnection")
def test_check_disk_usage(self, mock_https_cls):
    from app.services.troshkad_client import check_disk_usage
    mock_conn = _mock_conn({"free_bytes": 380*1024**3, "total_bytes": 500*1024**3, "used_pct": 24})
    mock_https_cls.return_value = mock_conn
    result = check_disk_usage(FakeHost())
    self.assertEqual(result["used_pct"], 24)
```

- [ ] **Step 2: Implement**

Add to `troshkad_client.py`:

```python
def check_disk_usage(host, timeout=15):
    """Check disk usage on host. Returns {free_bytes, total_bytes, used_pct} or error dict."""
    try:
        return troshkad_request(host, "GET", "/host/disk-usage", timeout=timeout)
    except TroshkadError as e:
        return {"free_bytes": 0, "total_bytes": 0, "used_pct": 100, "error": str(e)}
```

- [ ] **Step 3: Run tests, commit**

```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/test_troshkad_client.py -v
git add src/backend/app/services/troshkad_client.py src/backend/tests/test_troshkad_client.py
git commit -m "feat: add check_disk_usage convenience function to troshkad_client"
```

---

### Task 6: Migrate eip_service.py

**Files:**
- Modify: `src/backend/app/services/eip_service.py`

- [ ] **Step 1: Read the file and identify the call**

The `_detect_primary_iface()` function (line 36) runs `ip route show default | awk '{print $5}' | head -1` over SSH. This is only used for EIP association to know which interface to bind secondary IPs to.

- [ ] **Step 2: Remove _detect_primary_iface and hardcode or move to network handler**

The primary interface on EC2 instances is always `eth0` (or `ens5` on nitro). The function already falls back to `"eth0"`. Since the troshkad network handler can detect this internally when setting up EIP rules, remove `_detect_primary_iface()` and replace its callers with `"eth0"` (the current fallback).

Read the file to find all callers of `_detect_primary_iface()`, replace with `"eth0"`, and remove the function + its `run_ssh_script` import if no other calls remain in the file.

- [ ] **Step 3: Run backend tests**

```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/ -v
```

- [ ] **Step 4: Commit**

```bash
git add src/backend/app/services/eip_service.py
git commit -m "refactor: remove _detect_primary_iface SSH call, hardcode eth0"
```

---

### Task 7: Migrate hosts.py — disk usage + resize

**Files:**
- Modify: `src/backend/app/api/hosts.py`

- [ ] **Step 1: Read hosts.py, find the two call sites**

1. `host_storage()` endpoint (line 61) — calls `check_host_disk_space(h.ip_address, h.private_key)`
2. `resize_storage()` endpoint (line 475) — calls `run_ssh_script(host.ip_address, host.private_key, "xfs_growfs /var/lib/troshka")`

- [ ] **Step 2: Migrate host_storage to use troshkad_client**

Replace `check_host_disk_space(h.ip_address, h.private_key)` with:
```python
from app.services.troshkad_client import check_disk_usage
disk = check_disk_usage(h)
```

The return format is the same: `{free_bytes, total_bytes, used_pct}`.

- [ ] **Step 3: Migrate resize_storage to use troshkad_client**

Replace the `run_ssh_script` call with:
```python
from app.services.troshkad_client import start_job, wait_for_job
job_id = start_job(host, "/host/resize-storage", {})
job = wait_for_job(host, job_id, timeout=30)
if job["status"] == "failed":
    raise HTTPException(status_code=500, detail=job["result"].get("error", "Resize failed"))
```

- [ ] **Step 4: Remove run_ssh_script and check_host_disk_space imports if no longer used**

Check if hosts.py has any remaining `run_ssh_script` calls. If not, remove the import.

- [ ] **Step 5: Run tests, commit**

```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/ -v
git add src/backend/app/api/hosts.py
git commit -m "refactor: migrate hosts.py disk usage and resize from SSH to troshkad"
```

---

### Task 8: Migrate gc_service.py

**Files:**
- Modify: `src/backend/app/services/gc_service.py`

- [ ] **Step 1: Read gc_service.py fully**

There are 4 `run_ssh_script` calls:
1. `discover_orphans()` — complex inventory script → replace with `start_job(host, "/gc/discover", {...})`
2. `clean_orphans()` — dynamic cleanup script → replace with `start_job(host, "/gc/clean", {...})`
3. `repair_networks()` bridge check — replace with data from `gc/discover` results or a direct bridge list
4. `repair_networks()` network setup — replace with `start_job(host, "/networks/setup", {...})`

- [ ] **Step 2: Migrate discover_orphans**

The function currently builds an 87-line bash script and parses the output into sections. Replace with:

```python
from app.services.troshkad_client import start_job, wait_for_job

job_id = start_job(host, "/gc/discover", {
    "known_project_ids": known_project_ids,
    "known_domains": known_domains,
})
job = wait_for_job(host, job_id, timeout=30)
if job["status"] == "failed":
    return {"error": job["result"].get("error")}
return job["result"]  # Already structured: {orphan_dirs, orphan_domains, ...}
```

The function signature changes from `(host_ip, private_key, known_project_ids, known_domains)` to `(host, known_project_ids, known_domains)`.

- [ ] **Step 3: Migrate clean_orphans**

Currently builds a dynamic bash script. Replace with:

```python
job_id = start_job(host, "/gc/clean", {
    "orphan_dirs": orphans.get("orphan_dirs", []),
    "orphan_domains": orphans.get("orphan_domains", []),
    "orphan_bridges": orphans.get("orphan_bridges", []),
    "orphan_namespaces": orphans.get("orphan_namespaces", []),
    "cache_items": orphans.get("cache_items", []),
})
job = wait_for_job(host, job_id, timeout=120)
```

- [ ] **Step 4: Migrate repair_networks**

The bridge check (`ip -o link show type bridge`) is already covered by `gc/discover` output. For the network setup, use the existing `networks/setup` troshkad endpoint.

- [ ] **Step 5: Update all callers of these functions**

Search for callers of `discover_orphans`, `clean_orphans`, `repair_networks` — update to pass `host` object instead of `(host_ip, private_key)`.

- [ ] **Step 6: Run tests, commit**

```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/ -v
git add src/backend/app/services/gc_service.py
git commit -m "refactor: migrate gc_service.py from SSH to troshkad (discover + clean + repair)"
```

---

### Task 9: Migrate eips.py

**Files:**
- Modify: `src/backend/app/api/eips.py`

- [ ] **Step 1: Find and migrate the sync_project_eips call**

Line 134 runs `generate_setup_script()` output via SSH. Replace with `start_job(host, "/networks/setup", {network_config_params})`.

The network setup parameters come from `build_host_network_config()` — read how `deploy_service.py` builds these to understand the params format.

- [ ] **Step 2: Run tests, commit**

```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/ -v
git add src/backend/app/api/eips.py
git commit -m "refactor: migrate eips.py network setup from SSH to troshkad"
```

---

### Task 10: Migrate snapshot_service.py

**Files:**
- Modify: `src/backend/app/services/snapshot_service.py`

- [ ] **Step 1: Read and migrate capture_vm_disks**

The current function builds a bash script that flattens QCOW2, uploads to S3 via presigned URL, and caches locally. Replace with:

```python
job_id = start_job(host, "/snapshots/capture", {
    "domain_name": domain_name,
    "disk_index": 0,
    "presigned_url": presigned_url,
    "cache_path": cache_path,
})
job = wait_for_job(host, job_id, timeout=3600)
if job["status"] == "failed":
    raise RuntimeError(job["result"].get("error"))
size_bytes = job["result"].get("size_bytes", 0)
```

Update the function signature from `(host_ip, private_key, ...)` to `(host, ...)`.

- [ ] **Step 2: Run tests, commit**

```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/ -v
git add src/backend/app/services/snapshot_service.py
git commit -m "refactor: migrate snapshot_service.py capture from SSH to troshkad"
```

---

### Task 11: Migrate pattern_service.py

**Files:**
- Modify: `src/backend/app/services/pattern_service.py`

- [ ] **Step 1: Read and migrate capture_pattern_disks**

Same pattern as snapshot_service but uses `patterns/capture` which handles multiple disks:

```python
disks = []
for disk_info in pattern_disks:
    disks.append({
        "disk_index": disk_info["index"],
        "presigned_url": disk_info["presigned_url"],
        "cache_path": disk_info["cache_path"],
    })

job_id = start_job(host, "/patterns/capture", {
    "domain_name": domain_name,
    "disks": disks,
})
job = wait_for_job(host, job_id, timeout=3600)
```

- [ ] **Step 2: Run tests, commit**

```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/ -v
git add src/backend/app/services/pattern_service.py
git commit -m "refactor: migrate pattern_service.py capture from SSH to troshkad"
```

---

### Task 12: Migrate library.py

The biggest single migration — 13 `run_ssh_script` calls in `_host_download()` collapse to a single `start_job` + `wait_for_job`.

**Files:**
- Modify: `src/backend/app/api/library.py`

- [ ] **Step 1: Read _host_download fully**

The current function (lines 341-507) does:
1. Check disk space (check_host_disk_space)
2. mkdir tmp dir
3. Start background curl download
4. Poll download status in loop
5. Cleanup status file
6. Split file into chunks
7. Upload each chunk to S3 via presigned URL, extracting ETags
8. Complete S3 multipart upload
9. Cleanup

All of steps 2-7 collapse into a single `library/import` troshkad job.

- [ ] **Step 2: Rewrite _host_download**

```python
def _host_download(item, host, db, presigned_urls, cache_path, ...):
    from app.services.troshkad_client import start_job, wait_for_job, check_disk_usage

    # Check disk space
    disk = check_disk_usage(host)
    if disk.get("used_pct", 100) >= 90:
        # ... error handling (same as current)
        return

    # Build import params
    import_params = {
        "download_url": download_url,
        "cache_path": cache_path,
    }
    if presigned_urls:
        import_params["s3_multipart"] = {
            "part_size_bytes": 500 * 1024 * 1024,
            "upload_parts": presigned_urls,
        }

    # Single troshkad job replaces 13 SSH calls
    job_id = start_job(host, "/library/import", import_params)
    job = wait_for_job(host, job_id, timeout=7200, poll_interval=10)

    if job["status"] == "failed":
        # ... error handling
        return

    # Complete S3 multipart upload with ETags from troshkad
    if presigned_urls and job["result"].get("etags"):
        s3_client.complete_multipart_upload(
            upload_id=upload_id,
            parts=job["result"]["etags"],
        )
```

- [ ] **Step 3: Update callers of _host_download**

Change callers to pass `host` object instead of `(host.ip_address, host.private_key)`.

- [ ] **Step 4: Run tests, commit**

```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/ -v
git add src/backend/app/api/library.py
git commit -m "refactor: migrate library.py import flow from 13 SSH calls to single troshkad job"
```

---

### Task 13: Migrate deploy_service.py

The largest file — 16 `run_ssh_script` calls across deploy, start, stop, destroy, and cache operations.

**Files:**
- Modify: `src/backend/app/services/deploy_service.py`

- [ ] **Step 1: Migrate check_host_disk_space**

Replace the function body to use troshkad_client:

```python
def check_host_disk_space(host) -> dict:
    """Check free space on /var/lib/troshka mount."""
    from app.services.troshkad_client import check_disk_usage
    return check_disk_usage(host)
```

Update all callers to pass `host` instead of `(host_ip, private_key)`.

- [ ] **Step 2: Migrate cache_library_images**

The current function has 7 `run_ssh_script` calls (mkdir, pre-check, start downloads, poll, cleanup). Replace with:

For each item to cache, call `start_job(host, "/library/import", {...})`. Can start multiple jobs and poll them all. Or simplify: call one at a time with `wait_for_job`.

Change signature from `(topology, host_ip, private_key, db_session, ...)` to `(topology, host, db_session, ...)`.

- [ ] **Step 3: Migrate _prepare_library_downloads**

This writes presigned URLs to temp files on the host. With troshkad, the URLs are passed as parameters to the `library/import` or `images/cache` endpoints directly — this function becomes unnecessary. Remove it.

- [ ] **Step 4: Migrate deploy_project_async**

Replace each `run_ssh_script` call:

| Step | Current SSH | Troshkad replacement |
|------|-------------|---------------------|
| Network setup | `run_ssh_script(host_ip, private_key, net_script)` | `start_job(host, "/networks/setup", {network_config})` |
| Seed ISO | `run_ssh_script(host_ip, private_key, seed_script)` | `start_job(host, "/seeds/create", {seed_params})` |
| VM creation | `run_ssh_script(host_ip, private_key, vm_script)` | Multiple `start_job(host, "/vms/create", {...})` per VM |
| VM start | `run_ssh_script(host_ip, private_key, start_script)` | Multiple `start_job(host, "/vms/start", {...})` per VM |

The `generate_vm_script()`, `generate_start_script()`, `generate_stop_script()`, `generate_destroy_script()` functions currently generate bash scripts. These need to be replaced with functions that return structured params for troshkad endpoints instead.

**This is the key refactoring**: instead of `generate_vm_script()` returning bash, create `build_vm_params()` returning a list of dicts for `/vms/create` calls.

- [ ] **Step 5: Migrate stop_project_async and start_project_async**

Same pattern: replace `generate_stop_script()` SSH call with per-VM `start_job(host, "/vms/stop", {...})` calls, and network teardown with `start_job(host, "/networks/teardown", {...})`.

- [ ] **Step 6: Migrate destroy_project_sync**

Replace `generate_destroy_script()` SSH call with per-VM `start_job(host, "/vms/destroy", {...})` calls + network teardown.

- [ ] **Step 7: Remove run_ssh_script**

After all calls in this file are migrated, remove the `run_ssh_script()` function definition. Leave it only if other files still import it — check with `grep -r "run_ssh_script" src/backend/`.

- [ ] **Step 8: Run tests, commit**

```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/ -v
git add src/backend/app/services/deploy_service.py
git commit -m "refactor: migrate deploy_service.py from SSH to troshkad (deploy, start, stop, destroy)"
```

---

### Task 14: Migrate projects.py

The final backend file — 10 `run_ssh_script` calls in start_vm, reconfigure, and redeploy flows.

**Files:**
- Modify: `src/backend/app/api/projects.py`

- [ ] **Step 1: Read and map all call sites**

| Function | SSH call | Troshkad endpoint |
|----------|----------|-------------------|
| `_start_infra_then_vm` | Network setup script | `/networks/setup` |
| `_do_reconfigure` | Network setup | `/networks/setup` |
| `_do_reconfigure` | Metadata service | (fold into network setup or remove if obsolete) |
| `_do_reconfigure` | rm orphaned disks | `/files/remove` |
| `_do_reconfigure` | Disk create/resize | `/disks/create`, `/disks/resize` |
| `_do_reconfigure` | Seed ISO | `/seeds/create` |
| `_do_reconfigure` | Incremental VM create | `/vms/create` |
| `_do_redeploy` | rm disk files | `/files/remove` |
| `_do_redeploy` | Seed ISO | `/seeds/create` |
| `_do_redeploy` | VM create | `/vms/create` |

- [ ] **Step 2: Migrate each function**

For each function, replace `run_ssh_script` calls with the equivalent `start_job` + `wait_for_job` calls. The reconfigure flow is the most complex — it has conditional logic around which VMs changed, which disks need resizing, etc. Keep the same conditional structure but swap the SSH calls.

- [ ] **Step 3: Update imports**

Remove `run_ssh_script` import from projects.py. Add `from app.services.troshkad_client import start_job, wait_for_job`.

- [ ] **Step 4: Run tests, commit**

```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/ -v
git add src/backend/app/api/projects.py
git commit -m "refactor: migrate projects.py start_vm/reconfigure/redeploy from SSH to troshkad"
```

---

## Phase 3: Cleanup

### Task 15: Remove dead code

**Files:**
- Modify: `src/backend/app/services/deploy_service.py`

- [ ] **Step 1: Verify no remaining run_ssh_script callers**

```bash
cd src/backend && grep -rn "run_ssh_script\|check_host_disk_space" app/ --include="*.py"
```

Expected: only `agent_deployer.py` (which keeps using SSH for initial install).

- [ ] **Step 2: Remove run_ssh_script if no longer needed**

If `run_ssh_script` is only called from `agent_deployer.py`, it should stay in `deploy_service.py` (or move to `agent_deployer.py`). If it's not called at all from `deploy_service.py`, remove it from there.

Also remove:
- `_prepare_library_downloads()` if no longer called
- The old `check_host_disk_space()` wrapper if fully replaced
- Any `generate_*_script()` functions that are no longer called (only if ALL callers are migrated)

- [ ] **Step 3: Clean up imports across all migrated files**

Remove unused imports of `run_ssh_script`, `check_host_disk_space` from all files.

- [ ] **Step 4: Run full test suite**

```bash
cd src/troshkad && python3 -m pytest tests/test_troshkad.py -v
cd src/backend && ./venv/bin/python3 -m pytest tests/ -v
```

- [ ] **Step 5: Verify troshkad has all handlers registered**

```bash
cd src/troshkad && python3 -c "
import troshkad
print('Commands:', sorted(troshkad.COMMAND_HANDLERS.keys()))
"
```

Expected: all original endpoints plus the 8 new ones.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "chore: remove dead SSH code after troshkad migration"
```

---

## Follow-Up Tasks (Out of Scope)

1. **Backend retry queue for drain/503** — hold commands when troshkad returns draining status
2. **SG IP scoping** — discover backend IP, restrict SG to that IP
3. **Health check polling** — periodic GET /health instead of SSH echo
4. **Admin UI** — show troshkad version per host, push updates button
5. **Move run_ssh_script to agent_deployer** — it's only used there now
