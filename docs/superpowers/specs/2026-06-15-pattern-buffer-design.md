# Pattern Buffer — Dedicated Storage Worker Host

**Date**: 2026-06-15
**Status**: Design

## Problem

Pattern captures (flatten + compress + S3 upload) run on the VM host, consuming CPU and disk I/O that competes with running VMs. A 14 GB flatten+compress can saturate a host for 20+ minutes. Library imports and snapshot captures have the same problem.

## Solution

A dedicated small instance per storage pool — the **pattern buffer** (`host_type = "pattern_buffer"`) — that handles all heavy I/O. VM hosts only do sub-second snapshots and serve frozen disks over the network. All flatten, compress, upload, and cache-seeding work moves to the pattern buffer.

## Architecture

### Host Type

- `host_type = "pattern_buffer"` on the Host model (alongside `"shared"`)
- One per pool, referenced by `StoragePool.worker_host_id` (FK to Host)
- Instance type configurable per pool: `StoragePool.worker_instance_type` (default: `c6id.xlarge` — 4 vCPU, 8 GB RAM, 237 GB NVMe)
- Runs troshkad like any other host, same agent install flow
- Lives in the same VPC/subnet as pool hosts, uses private IPs

### Lifecycle

- **Auto-provisioned** when a storage pool is created (or when first host joins a pool that lacks a worker)
- **Replace/Add**: admin UI button on the pool page if worker is missing, dead, or needs replacement
- **Not a hard dependency**: if no pattern buffer exists for a pool, all operations fall back to the current behavior (VM host does everything)
- Pattern buffer is excluded from VM placement — it never runs user VMs

### Capture Flow (New)

Today: VM host snapshots → flattens → compresses → uploads to S3 → caches

New:

1. **Backend** orchestrates, detects pool has a pattern buffer
2. **VM host** (troshkad): snapshot the VM (sub-second freeze/thaw), start `qemu-nbd --read-only --tls-creds` serving the frozen base disk on an allocated port
3. **Pattern buffer** (troshkad): `qemu-img convert --object tls-creds-x509 nbd+tls://vm-host-private-ip:port/disk -c -o compression_type=zstd -O qcow2 /local/flat.qcow2`
4. **Pattern buffer**: upload flat.qcow2 to S3 + copy to shared storage cache (NFS/FSx)
5. **Backend** tells VM host: stop NBD export, block-commit the snapshot overlay
6. VM resumes on the committed base disk (already running the whole time on the overlay)

VM host impact after snapshot: near zero — `qemu-nbd` serves read-only blocks, minimal CPU.

### Other Workloads on Pattern Buffer

- **Snapshot captures**: same NBD flow as pattern captures
- **Library imports**: ISO/qcow2 downloads from URLs, S3 downloads, qemu-img conversions — all run on pattern buffer instead of VM hosts
- **S3 uploads**: any operation that needs to push large files to S3

### NBD Security

`qemu-nbd` and `qemu-img` both support TLS natively via `--tls-creds` / `--object tls-creds-x509`. We use the **existing pool-level PKI** (same CA and per-host certs used for libvirt TLS migration):

- VM host starts NBD with its pool TLS cert: `qemu-nbd --tls-creds=troshka-tls ...`
- Pattern buffer connects with its pool TLS cert: `qemu-img convert --object tls-creds-x509,id=tls0,dir=/path/to/certs,endpoint=client nbd+tls://...`
- Mutual authentication via the pool CA
- No new PKI infrastructure needed

### Networking

- Same VPC/subnet as pool hosts
- Security group additions (private IPs only):
  - NBD port range: TCP 10809-10829 (supports up to 20 concurrent exports per host)
  - troshkad: TCP 31337 (already open for pool members)
- Pattern buffer gets pool TLS certs via the same health-poller cert-signing flow

## Model Changes

### StoragePool

- `worker_host_id`: nullable FK to Host — the pattern buffer for this pool
- `worker_instance_type`: string, default `"c6id.xlarge"` — EC2 instance type for the worker

### Host

- `host_type`: add `"pattern_buffer"` as a valid value
- Pattern buffer hosts are excluded from `find_available_host()` placement queries
- Pattern buffer shows in admin hosts page with a distinct badge/label

## Troshkad New Endpoints

### VM Host

- `POST /nbd/export` — snapshot VM, start `qemu-nbd --read-only --tls-creds` on allocated port. Params: `domain_name`, `disk_path`. Returns: `port`, `export_name`.
- `POST /nbd/stop` — kill qemu-nbd process, block-commit snapshot overlay. Params: `domain_name`, `port`.

### Pattern Buffer

- `POST /nbd/pull-flatten` — connect to remote NBD, flatten+compress to local NVMe. Params: `nbd_host`, `nbd_port`, `export_name`, `output_path`, `tls_dir`. Returns: `size_bytes`, `output_path`.
- Existing `patterns/capture-direct` handler refactored: flatten step replaced with NBD pull from remote host instead of local `qemu-img convert`.
- Existing S3 upload and cache-copy code reused as-is.

## Backend Orchestration

`pattern_service.py` capture flow changes:

1. Look up `pool.worker_host_id` — if set, use pattern buffer flow; if not, fall back to current flow
2. For each VM's disks:
   a. `start_job(vm_host, "/nbd/export", {domain_name, disk_path})` → get port
   b. `start_job(worker_host, "/nbd/pull-flatten", {nbd_host: vm_host.private_ip, port, ...})` → wait for flatten
   c. `start_job(worker_host, "s3-upload + cache-copy", {...})` → wait for upload
   d. `start_job(vm_host, "/nbd/stop", {domain_name, port})` → cleanup
3. Progress tracking: worker reports flatten %, upload %, cache % — same `_capture_progress` dict

## Admin UI

- Pool detail page: shows pattern buffer status (connected/disconnected/none)
- "Add Pattern Buffer" / "Replace Pattern Buffer" button
- Hosts page: pattern buffer hosts shown with a distinct label, not counted in VM capacity

## Fallback Behavior

- No pattern buffer for pool → current flow (VM host does everything)
- Pattern buffer disconnected at capture time → fall back to current flow, log warning
- Pattern buffer provisioning failure → pool still works, admin can retry

## Security Group Rules

Added to pool security group (in addition to existing NFS + libvirt TLS rules):

| Port | Protocol | Source | Purpose |
|------|----------|--------|---------|
| 10809-10829 | TCP | pool SG | NBD exports (TLS) |

## IAM

No changes — pattern buffer uses the same instance profile as other pool hosts (S3 access, Route53 for console).

## Implementation Order

1. Model changes: `StoragePool.worker_host_id`, `worker_instance_type`, Host `host_type` values
2. Troshkad NBD endpoints: `/nbd/export`, `/nbd/stop`, `/nbd/pull-flatten`
3. Provisioner: auto-provision pattern buffer when pool is created
4. Backend orchestration: pattern_service capture flow with NBD
5. Security group updates: NBD port range
6. Admin UI: pool page pattern buffer status + add/replace button
7. Fallback: graceful degradation when no pattern buffer
