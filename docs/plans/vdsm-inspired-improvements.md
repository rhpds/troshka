# VDSM-Inspired Improvements Plan

## Context

After implementing libvirt lifecycle events and batch VM state polling (commit ee01da3), these are the remaining improvements inspired by how oVirt/VDSM handles storage and VM management.

## 1. Connection Pooling (High Impact, Low Effort)

**Problem:** Every `troshkad_request()` creates a new `http.client.HTTPSConnection` — fresh TCP + TLS handshake per request. With batch polling this is 1 per host per 5s cycle, but deploy operations still make dozens of sequential calls.

**VDSM approach:** Persistent connections to managed hosts.

**Implementation:**
- Replace `http.client.HTTPSConnection` with `urllib3.PoolManager` in `troshkad_client.py`
- Per-host connection pool (maxsize=5), keyed by `host.ip_address`
- Keep cert fingerprint verification via `assert_fingerprint` parameter
- Pool auto-handles keep-alive, reconnection, and connection reuse
- Falls back gracefully if pool connection fails

**Files:** `src/backend/app/services/troshkad_client.py`

## 2. Block Threshold Events (Medium Impact, Medium Effort)

**Problem:** No visibility into disk growth until FSx fills up. Discovered this session — overlays grew to 6GB within minutes of cluster startup.

**VDSM approach:** Register `VIR_DOMAIN_EVENT_ID_BLOCK_THRESHOLD` with libvirt. Get notified when a disk exceeds a threshold instead of polling.

**Implementation:**
- In troshkad's libvirt event loop, register block threshold callbacks alongside lifecycle events
- Set threshold at 80% of FSx free space / number of active VMs
- When threshold fires, push a warning event to the backend
- Backend can surface it in the UI (disk usage warning) or trigger auto-actions
- Pair with the `/vms/events` endpoint already implemented

**Files:** `src/troshkad/troshkad.py` (event loop), `src/backend/app/services/ws_pubsub.py` (warning notifications)

## 3. Live Merge (Medium Impact, High Effort)

**Problem:** Pattern capture requires external snapshots + flatten + S3 upload. The flatten step copies the entire disk chain to a temp file, which takes 10+ minutes for large disks.

**VDSM approach:** `virDomainBlockCommit()` with `VIR_DOMAIN_BLOCK_COMMIT_ACTIVE` flag — merges overlay into base while the VM is running. No temp files, no full disk copy.

**Implementation:**
- Use `virsh blockcommit --active --pivot` instead of `qemu-img convert`
- Monitor via `VIR_DOMAIN_EVENT_ID_BLOCK_JOB` events
- After commit completes, the active disk IS the flattened result — upload directly to S3
- Eliminates the temp flatten step and ~50% of the capture time
- Risk: blockcommit modifies the active disk in place — need careful error handling

**Files:** `src/troshkad/troshkad.py` (capture handler), `src/backend/app/services/pattern_service.py`

**Note:** This was the approach that previously corrupted the library image via hard links. Now that we use direct backing references (no copies), the risk profile is different — blockcommit would write to the project's overlay, not the shared cache. Needs careful analysis.

## 4. Root FS Space Monitor (Low Effort)

**Problem:** Root FS filled to 100% this session (pattern cache + stale temp files), causing agent lockups. No warning until everything broke.

**Implementation:**
- Add root FS check to troshkad health endpoint (`/health`)
- Health poller checks `root_free_pct` and warns if < 15%
- Surface warning on hosts page in the UI
- Agent install script should also verify root FS has adequate space

**Files:** `src/troshkad/troshkad.py` (health), `src/backend/app/services/health_poller.py`

## 5. S3 Download Temp File Cleanup (Low Effort)

**Problem:** Failed S3 downloads leave temp files in `/tmp/` on the host. Multiple failures filled root FS with ~7GB of orphan temp files.

**Implementation:**
- `aws s3 cp` uses `/tmp` for multipart assembly by default
- Set `AWS_TMPDIR` env var to `/var/lib/troshka/local/tmp/` (on EBS, not root)
- Add cleanup of stale files in `/var/lib/troshka/local/tmp/` to GC discover handler
- Existing temp files from `qemu-img convert` already use this path

**Files:** `src/troshkad/troshkad.py` (S3 download function, GC handler)

## Priority Order

1. **Connection pooling** — quick win, reduces all troshkad communication latency
2. **Root FS monitor** — prevents repeat of today's disk-full lockup
3. **S3 temp cleanup** — prevents repeat of temp file accumulation
4. **Block threshold events** — proactive disk monitoring for scale
5. **Live merge** — performance optimization for pattern capture, higher risk

## Session Summary (for context)

Key issues debugged and fixed this session:
- WS poller crashed on pattern channel IDs (`pattern:xxx` treated as UUID)
- Agent HTTP server was single-threaded — saturated during disk copies → ThreadingHTTPServer
- Per-project backing image copies caused 44GB NFS-to-NFS copies per deploy → eliminated copies, overlays reference cache directly
- Pattern capture flattened wrong file (library image instead of overlay) → fixed backing chain logic
- Root FS filled by pattern cache on wrong partition + orphan temp files
- SharedCacheEntry stuck in "downloading" state blocked deploys
- `wait_for_job` crashed on first connection error → 12-failure tolerance
- False IP change on backend restart triggered unnecessary SG updates
- Implemented libvirt lifecycle events + batch GET /vms/states endpoint
