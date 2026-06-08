# SSH-to-Troshkad Migration Design Spec

**Date**: 2026-06-08
**Status**: Draft
**Author**: prutledg + Claude
**Depends on**: `docs/superpowers/specs/2026-06-08-troshkad-design.md`

## Problem

The troshkad daemon and backend client are built and tested, but the backend still uses `run_ssh_script()` for all host operations. There are 47 `run_ssh_script()` call sites across 9 files. Until these are migrated, troshkad is deployed but unused.

## Solution

Migrate all 47 call sites to use `troshkad_client`. This requires:
1. Adding 8 new troshkad endpoints for operations not yet covered
2. Migrating each backend service file to use `troshkad_client` instead of `run_ssh_script()`
3. Removing `run_ssh_script()` after all callers are migrated

## New Troshkad Endpoints

### Immediate Endpoints (no job)

| Method | Path | Purpose | Response |
|--------|------|---------|----------|
| GET | `/host/disk-usage` | Disk space on /var/lib/troshka | `{free_bytes, total_bytes, used_pct}` |

### Job-Based Endpoints

| Method | Path | Purpose | Key Parameters |
|--------|------|---------|----------------|
| POST | `/host/resize-storage` | `xfs_growfs /var/lib/troshka` | (none) |
| POST | `/files/remove` | Remove specific files under /var/lib/troshka | `paths: [str]` (each validated under /var/lib/troshka/) |
| POST | `/gc/discover` | Scan host for orphaned resources | `known_project_ids: [str]`, `known_domains: [str]` |
| POST | `/gc/clean` | Remove specific orphaned resources | `orphan_dirs: [str]`, `orphan_domains: [str]`, `orphan_bridges: [str]`, `orphan_namespaces: [str]`, `cache_items: [str]` |
| POST | `/library/import` | Full pipeline: download → flatten → chunk → S3 upload | See below |
| POST | `/snapshots/capture` | Flatten disk → upload to S3 → cache locally | See below |
| POST | `/patterns/capture` | Capture all disks from a project for pattern export | See below |

The existing `gc/run` stub is replaced by `gc/discover` + `gc/clean`. The existing `snapshots/create` (VM shutdown + qcow2 convert) stays — `snapshots/capture` adds the S3 upload pipeline.

### Library Import Endpoint Detail

**Request:**
```json
{
  "download_url": "https://presigned-s3-url-or-http-url",
  "cache_path": "/var/lib/troshka/images/item-id.qcow2",
  "flatten": true,
  "s3_multipart": {
    "part_size_bytes": 104857600,
    "upload_parts": [
      {"part_num": 1, "presigned_url": "https://s3..."},
      {"part_num": 2, "presigned_url": "https://s3..."}
    ]
  }
}
```

**Troshkad pipeline:**
1. Download file to cache_path (`curl`, progress reported to job.output)
2. If `flatten`: `qemu-img convert` to temp file, replace original
3. If `s3_multipart`: split file, upload each part via presigned URL, collect ETags
4. Cleanup temp files on success or failure

**Response (job result):**
```json
{
  "etags": [{"part": 1, "etag": "\"abc123\""}],
  "size_bytes": 12345678
}
```

Backend completes the S3 multipart upload with the ETags — this step stays on the backend side (needs the S3 client).

### Snapshot Capture Endpoint Detail

**Request:**
```json
{
  "domain_name": "troshka-aabbccdd-11223344",
  "disk_index": 0,
  "presigned_url": "https://s3-presigned-put-url",
  "cache_path": "/var/lib/troshka/cache/snapshots/item-id/disk.qcow2"
}
```

**Troshkad pipeline:**
1. Get disk path from `virsh domblklist`
2. Flatten with `qemu-img convert` to temp file
3. Upload via `curl -T` to presigned PUT URL
4. Copy to cache_path
5. Return `{"size_bytes": ..., "disk_path": "..."}`

### Pattern Capture Endpoint Detail

**Request:**
```json
{
  "domain_name": "troshka-aabbccdd-11223344",
  "disks": [
    {"disk_index": 0, "presigned_url": "https://s3...", "cache_path": "/var/lib/troshka/cache/patterns/pat-id/disk0.qcow2"},
    {"disk_index": 1, "presigned_url": "https://s3...", "cache_path": "/var/lib/troshka/cache/patterns/pat-id/disk1.qcow2"}
  ]
}
```

**Troshkad pipeline:** Same as snapshot capture but iterates over multiple disks.

### GC Discover Endpoint Detail

**Request:**
```json
{
  "known_project_ids": ["uuid1", "uuid2"],
  "known_domains": ["troshka-aabb-1122", "troshka-ccdd-3344"]
}
```

**Troshkad scans:**
1. List all `/var/lib/troshka/vms/*/` directories
2. List all `virsh` domains starting with `troshka-`
3. List all bridges matching `br-troshka-*`
4. List all namespaces matching `troshka-*`
5. List cache items with access times (patterns, snapshots, images)
6. Compare against known lists

**Response (job result):**
```json
{
  "orphan_dirs": ["/var/lib/troshka/vms/unknown-uuid/"],
  "orphan_domains": ["troshka-dead-beef"],
  "orphan_bridges": ["br-troshka-dead"],
  "orphan_namespaces": ["troshka-deadbeef"],
  "cache_items": [
    {"path": "/var/lib/troshka/cache/patterns/old/", "type": "pattern", "age_hours": 48}
  ]
}
```

### GC Clean Endpoint Detail

**Request:**
```json
{
  "orphan_dirs": ["/var/lib/troshka/vms/unknown-uuid/"],
  "orphan_domains": ["troshka-dead-beef"],
  "orphan_bridges": ["br-troshka-dead"],
  "orphan_namespaces": ["troshka-deadbeef"],
  "cache_items": ["/var/lib/troshka/cache/patterns/old/"]
}
```

**Troshkad cleans:**
1. `rm -rf` each orphan dir (validated under /var/lib/troshka/)
2. `virsh destroy` + `virsh undefine` each orphan domain
3. `ip link delete` each orphan bridge
4. `ip netns delete` each orphan namespace
5. `rm -rf` each cache item

**Response:** `{"removed_dirs": 1, "removed_domains": 1, "removed_bridges": 0, ...}`

## Backend Migration Pattern

Every call site transforms from:

```python
# Before (SSH)
result = run_ssh_script(host.ip_address, host.private_key, script, timeout=120)
if not result["success"]:
    raise SomeError(result["output"])

# After (troshkad)
from app.services.troshkad_client import start_job, wait_for_job

job_id = start_job(host, "/vms/create", {"domain_name": ..., "vcpus": ...})
job = wait_for_job(host, job_id, timeout=120)
if job["status"] == "failed":
    raise SomeError(job["result"]["error"])
```

For immediate endpoints:
```python
# Before
result = run_ssh_script(host.ip_address, host.private_key, "stat ...", timeout=15)

# After
data = troshkad_request(host, "GET", "/host/disk-usage", timeout=15)
```

## Function Signature Changes

Functions that currently take `(host_ip, private_key)` change to take the `host` model object:

| File | Function | Before | After |
|------|----------|--------|-------|
| `deploy_service.py` | `check_host_disk_space()` | `(host_ip, private_key)` | `(host)` |
| `deploy_service.py` | `cache_library_images()` | `(host_ip, private_key, ...)` | `(host, ...)` |
| `gc_service.py` | `discover_orphans()` | `(host_ip, private_key, ...)` | `(host, ...)` |
| `gc_service.py` | `clean_orphans()` | `(host_ip, private_key, ...)` | `(host, ...)` |
| `gc_service.py` | `repair_networks()` | `(host_ip, private_key, ...)` | `(host, ...)` |
| `eip_service.py` | `_detect_primary_iface()` | `(host_ip, private_key)` | removed (folded into network handler) |

All callers update accordingly — anywhere that passes `host.ip_address, host.private_key` now passes `host`.

## Migration Order

### Phase 1: New Troshkad Endpoints

Add the 8 missing endpoints to `troshkad.py` with tests. This is done before any backend migration.

### Phase 2: Backend Migration (by file, simplest first)

| Order | File | Calls | Complexity |
|-------|------|-------|------------|
| 1 | `eip_service.py` | 1 | Low — remove `_detect_primary_iface`, fold into network handler |
| 2 | `hosts.py` | 2 | Low — disk usage + xfs_growfs |
| 3 | `gc_service.py` | 4 | Medium — discover + clean + bridge check + repair |
| 4 | `eips.py` | 1 | Low — sync EIPs via network setup |
| 5 | `snapshot_service.py` | 1 | Medium — capture pipeline |
| 6 | `pattern_service.py` | 1 | Medium — capture pipeline |
| 7 | `library.py` | 13 | High — entire import flow collapses to one endpoint call |
| 8 | `deploy_service.py` | 16 | High — deploy, start, stop, destroy, cache |
| 9 | `projects.py` | 10 | High — start_vm, reconfigure, redeploy |

Each file migration is a self-contained commit with tests.

### Phase 3: Cleanup

1. Remove `run_ssh_script()` from `deploy_service.py`
2. Remove `check_host_disk_space()` wrapper (replaced by troshkad_request to `/host/disk-usage`)
3. Remove `private_key` usage from all non-install code
4. Update imports across all files

## What Stays as SSH

Only `agent_deployer.py`:
- `wait_for_ssh()` — polls for SSH readiness on new hosts
- `deploy_agent()` — runs install script + SCPs troshkad.py

These stay because troshkad isn't running yet during initial agent installation. The `private_key` column on Host remains for this purpose.

## Testing Strategy

- Each new troshkad endpoint gets unit tests (mock subprocess)
- Each backend file migration updates existing tests to mock `troshkad_client` instead of `run_ssh_script`
- Integration test: verify `troshkad.py` imports cleanly with all new handlers registered
- Final: full backend test suite passes

## Out of Scope

- Backend retry queue for drain/503 handling (separate follow-up)
- SG IP scoping (separate follow-up)
- Health check polling (separate follow-up)
- Admin UI for troshkad (separate follow-up)
