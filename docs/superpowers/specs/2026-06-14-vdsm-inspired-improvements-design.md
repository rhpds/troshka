# VDSM-Inspired Improvements Design

## Overview

Five independent improvements to Troshka's host management, inspired by oVirt/VDSM patterns. Each ships as a separate change with no cross-dependencies.

Priority order: connection pooling → partition monitoring → S3 temp redirect → block threshold + auto-extend → streaming capture.

## 1. Connection Pooling

### Problem

Every `troshkad_request()` creates a new `http.client.HTTPSConnection` — fresh TCP + TLS handshake per request. During deploys this means dozens of sequential handshakes to the same host.

### Design

Replace `http.client` with `urllib3.HTTPSConnectionPool` in `troshkad_client.py`. urllib3 is already a backend dependency.

**Pool cache:** Module-level `dict[str, urllib3.HTTPSConnectionPool]` keyed by `f"{host.ip_address}:{fingerprint}"`. The fingerprint in the key ensures a cert change (agent reinstall) automatically creates a new pool — stale connections to old certs are never reused.

**Fingerprint verification:** `urllib3.HTTPSConnectionPool(assert_fingerprint=sha256_fp)` — fail-closed at TLS level. Replaces the current manual post-connect `_verify_cert_fingerprint()` check. On mismatch, urllib3 raises `SSLError`; we catch it and re-raise as `TroshkadError` with a clear "cert mismatch — reinstall agent" message.

**SSL context:** `cert_reqs="CERT_NONE"` (self-signed certs) + `assert_fingerprint` for identity. Same security model, cleaner implementation.

**Pool sizing:** `maxsize=4` connections per host. Covers health polling + concurrent operations. urllib3 handles overflow gracefully (creates temporary connections beyond maxsize).

**Retry logic:** Keep existing retry-with-backoff in `troshkad_request()`. Disable urllib3's built-in retries (`retries=False`). Our retry logic is tuned for troshkad-specific behavior (503 during drain, connection failures with 5s backoff).

**Pool eviction:** No explicit eviction. Pools accumulate as hosts are added — even with 100 hosts, ~100 pool objects is negligible memory. Idle pools are cleaned up on process restart.

**Recovery path:** If a host's agent is reinstalled (new cert), SSH is always available as the out-of-band path. The fail-closed fingerprint check means connections fail cleanly rather than silently talking to the wrong cert.

### Files

- `src/backend/app/services/troshkad_client.py` — replace http.client with urllib3 pool

## 2. Partition Monitoring

### Problem

No visibility into disk usage across host partitions. Root FS filled to 100% with no warning, causing agent lockups. Current monitoring only covers `/var/lib/troshka`.

### Design

Report all mounted partitions from troshkad. Health poller evaluates thresholds and surfaces warnings on the hosts page.

**Troshkad — `/health` endpoint changes:**

Add a `partitions` array to the health response. Each entry:
```json
{
  "mount": "/",
  "total_bytes": 107374182400,
  "used_bytes": 91268055040,
  "free_bytes": 16106127360,
  "used_pct": 85.0,
  "device": "/dev/nvme0n1p1",
  "fstype": "xfs"
}
```

Discovery via `/proc/mounts` + `shutil.disk_usage()` per mount. Filter out pseudo-filesystems: proc, sysfs, devtmpfs, tmpfs, cgroup, cgroup2, overlay, devpts, mqueue, hugetlbfs, debugfs, tracefs, securityfs, pstore, bpf, fusectl, configfs, autofs, nfsd, rpc_pipefs, binfmt_misc. Deduplicate by device (same device mounted multiple times — keep the first).

Backward compatible: existing `storage_total_gb` / `storage_used_gb` fields stay in the health response.

**Backend — health poller:**

Parse the `partitions` array from each host's health response. For each partition, check `used_pct` against thresholds:
- Warning: ≥ 85%
- Critical: ≥ 95%

Store warnings on the Host model in a new `storage_warnings` JSONB column:
```json
[
  {"mount": "/", "used_pct": 92.1, "level": "critical"},
  {"mount": "/var/lib/troshka/shared", "used_pct": 86.0, "level": "warning"}
]
```

Cleared when partition drops below 85%. The health poller already runs on a regular cycle — this adds partition evaluation to the existing loop.

**Frontend — hosts admin page:**

Yellow (warning) or red (critical) icon next to host name when `storage_warnings` is non-empty. Tooltip or expandable row shows which partition(s) and usage percentage. No new page — badge/indicator on the existing hosts table.

### Files

- `src/troshkad/troshkad.py` — health handler: add partitions array
- `src/backend/app/services/health_poller.py` — threshold evaluation, store warnings
- `src/backend/app/models/host.py` — add `storage_warnings` JSONB column
- `src/backend/alembic/versions/` — migration for new column
- `src/frontend/src/app/admin/hosts/page.tsx` — warning badges

## 3. S3 Temp File Redirect

### Problem

`aws s3 cp` uses system `/tmp` for multipart assembly by default. Failed or interrupted transfers leave orphan temp files on the root partition. Multiple failures accumulated ~7GB of orphan files, contributing to root FS filling up.

### Design

Redirect S3 temp files to the data partition. Clean up stale ones in GC.

**S3 subprocess env:** In `_s3_download()` and `_s3_upload()`, pass `env={**os.environ, "TMPDIR": "/var/lib/troshka/local/tmp/"}` to the subprocess. The AWS CLI respects `TMPDIR` for multipart staging. The `local/` prefix ensures temp files land on local NVMe even on shared-storage hosts — NFS temp files would be slow and leave locks on failure.

**Directory creation:** `os.makedirs("/var/lib/troshka/local/tmp", exist_ok=True)` before the subprocess call.

**GC cleanup:** In the existing GC discover handler, add a step that scans `/var/lib/troshka/local/tmp/` and removes files older than 1 hour. S3 multipart assembly shouldn't take more than a few minutes — anything older is orphaned. Uses `os.stat().st_mtime` for age check.

**Interaction with streaming capture (item #5):** Once streaming capture lands, the `_s3_upload` path for pattern capture will no longer use `aws s3 cp` (it becomes a piped pipeline). The `TMPDIR` redirect remains necessary for `_s3_download` and non-capture upload paths.

### Files

- `src/troshkad/troshkad.py` — S3 functions (env override) + GC handler (stale cleanup)

## 4. Block Threshold Events + Storage Auto-Extend

### Problem

No proactive warning when disk images grow toward capacity limits. Admins discover problems when deploys fail or VMs pause on a full filesystem. No mechanism to extend FSx or EBS capacity without manual AWS console intervention.

### Design

Three pieces: libvirt block threshold events in troshkad, auto-extend policy per storage pool (FSx) and per host (EBS), manual extend buttons in the UI.

**Troshkad — block threshold events:**

In `_start_libvirt_event_loop()`, register `VIR_DOMAIN_EVENT_ID_BLOCK_THRESHOLD` callback alongside the existing lifecycle callback.

When a domain is started, set a threshold on each disk target. The deploy flow passes the threshold percentage from the host's `auto_extend_threshold_pct` config (or pool's, for shared storage) as a parameter in the VM creation request to troshkad. Default 80% if not configured.

Threshold callback pushes an event to the backend via the existing `/vms/events` WebSocket mechanism:
```json
{
  "type": "block_threshold",
  "domain": "troshka-abcd1234-efgh5678",
  "disk": "vda",
  "used_bytes": 42949672960,
  "threshold_bytes": 34359738368
}
```

Re-arming: libvirt thresholds are one-shot. After firing, re-register at the next increment (e.g., fired at 80% → set next at 90%) to avoid spam while still tracking continued growth.

**Storage extend — two scopes:**

FSx is pool-level shared storage. EBS is per-host local storage. Each gets its own auto-extend configuration.

StoragePool model — new columns (FSx extend):
- `auto_extend_enabled` — Boolean, default False
- `auto_extend_threshold_pct` — Integer, default 80
- `auto_extend_increment_gb` — Integer, default 64
- `auto_extend_max_gb` — Integer, nullable (null = no cap)

Host model — new columns (EBS extend):
- `auto_extend_enabled` — Boolean, default False
- `auto_extend_threshold_pct` — Integer, default 80
- `auto_extend_increment_gb` — Integer, default 100
- `auto_extend_max_gb` — Integer, nullable (null = no cap)

**Auto-extend service — `storage_extend.py`:**

Primary trigger: health poller partition monitoring (item #2) detects data partition crossing the threshold. Also triggered by block threshold events from libvirt (secondary signal — catches VM-specific growth).

Extend logic by storage type:
- **FSx:** `boto3 update_file_system(StorageCapacityInGiB=current + increment)`. Online resize, no downtime. Pool-scoped — one API call extends storage for all hosts.
- **EBS:** `boto3 modify_volume(Size=current + increment)` then `xfs_growfs` via troshkad endpoint. Online resize, no downtime. Host-scoped — each host's volume extended individually.
- **BYO NFS:** Warning only, no auto-extend. Admin manages their own storage.

Cooldown: don't extend more than once per 10 minutes per target to prevent rapid-fire extends.

Logging: all extend operations are logged with before/after sizes for audit.

**API endpoints:**
- `POST /api/admin/storage-pools/{id}/extend` — manual FSx extend. Optional `increment_gb` override.
- `POST /api/admin/hosts/{id}/extend-storage` — manual EBS extend. Optional `increment_gb` override.
- Existing `PATCH` endpoints for pools and hosts extended to accept the auto-extend config fields.

**Frontend:**

Storage pools admin page — expand each pool card to show:
- Current FSx usage vs capacity
- Auto-extend toggle, threshold %, increment GB, max GB inputs
- "Extend Now" button with confirmation modal (current size → new size)

Hosts admin page — per-host:
- Current EBS usage (already shown via partition monitoring badges from item #2)
- Auto-extend toggle, threshold %, increment GB, max GB inputs
- "Extend Storage" button with confirmation modal

### Files

- `src/troshkad/troshkad.py` — block threshold event registration and re-arming
- `src/backend/app/models/storage_pool.py` — 4 new columns
- `src/backend/app/models/host.py` — 4 new columns
- `src/backend/alembic/versions/` — migration for both tables
- `src/backend/app/services/storage_extend.py` — new file, extend logic for FSx and EBS
- `src/backend/app/api/admin.py` — manual extend endpoints
- `src/backend/app/services/health_poller.py` — trigger auto-extend from partition warnings
- `src/frontend/src/app/admin/storage-pools/page.tsx` — FSx extend settings + button
- `src/frontend/src/app/admin/hosts/page.tsx` — EBS extend settings + button

## 5. Streaming Pattern Capture

### Problem

Pattern capture writes a full compressed copy of each disk to local NVMe as a temp file, then uploads it to S3, then copies it to cache. For a 40GB disk this means writing ~10GB compressed, then reading it back for upload. Wall clock = compress time + upload time (sequential). Requires local NVMe space for the temp file.

### Design

Pipe `qemu-img convert` output through a shell pipeline that tees to local cache and streams to S3 simultaneously. No temp file, no new dependencies, troshkad stays stdlib-only.

**Pipeline:**
```
qemu-img convert -c -o compression_type=zstd -O qcow2 <source> /dev/stdout \
  | tee <cache_path> \
  | aws s3 cp - s3://<bucket>/<key> --region <region>
```

The AWS CLI is a thin wrapper around boto3's `TransferManager` — same multipart upload performance, same 8MB chunk / 10 concurrent thread defaults. No reason to add boto3 to troshkad.

**Python implementation** with `subprocess.Popen`:
```python
qemu_proc = Popen(
    ["qemu-img", "convert", "-c", "-o", "compression_type=zstd",
     "-O", "qcow2", src, "/dev/stdout"],
    stdout=PIPE)
tee_proc = Popen(
    ["tee", cache_path],
    stdin=qemu_proc.stdout, stdout=PIPE)
s3_proc = Popen(
    ["aws", "s3", "cp", "-", s3_url, "--region", region],
    stdin=tee_proc.stdout, env=s3_env)
```

Three-process pipeline, OS-managed, no temp file, no new dependencies.

**Error handling:**
- Check return codes of all three processes after completion.
- If `s3_proc` fails: delete incomplete cache file, log error. `aws s3 cp` handles its own multipart abort internally.
- If `qemu_proc` fails: downstream processes get broken pipe, both fail. Delete incomplete cache file.
- If `tee_proc` fails: s3_proc gets broken pipe. Delete incomplete cache file.
- On any failure, fall back to current temp-file approach for that capture attempt.

**Fallback:** If `qemu-img convert` to `/dev/stdout` fails (older qemu versions), fall back to current temp-file approach. Detect capability at agent startup via `qemu-img convert -O qcow2 /dev/null /dev/stdout` test, report in `/health` response.

**Cache file handling:** `tee` writes the cache file as the stream flows. On success, the cache file is the complete compressed qcow2 — same as what the old path produced via `shutil.copy(tmp_flat, cache_path)`. `os.makedirs(os.path.dirname(cache_path), exist_ok=True)` before starting the pipeline.

**Progress monitoring:** Current approach monitors temp file growth for progress logging. With streaming, monitor the cache file size instead (same `os.path.getsize()` in a monitoring thread). The rate approximates both compress and upload progress since they're overlapped.

**Performance estimate:**
- Current (40GB disk, 20GB used, 10GB compressed): ~10 min compress + ~2 min upload = **12 min sequential**
- Streaming: compress and upload overlap. Bottleneck is the slower of the two (compression, CPU-bound). Upload happens concurrently. **~10 min total**, saving the sequential upload time.
- Bonus: no local NVMe scratch space needed for temp file.

**Snapshot flow unchanged:** The snapshot → overlay → commit-back flow is identical. Only the flatten+upload step in the middle changes from temp-file to streaming pipeline.

**What doesn't change:**
- Backend pattern service — same `start_job` params, same result format
- S3 download path — still uses `aws s3 cp` directly
- Non-capture uploads (snapshots, library items) — keep `aws s3 cp` with temp files for simplicity
- Overlay commit — still uses `virsh blockcommit --active --pivot`

### Files

- `src/troshkad/troshkad.py` — rewrite `_handle_pattern_capture_direct` flatten+upload step, new pipeline helper function
