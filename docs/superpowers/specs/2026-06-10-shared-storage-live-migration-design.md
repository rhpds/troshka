# Shared Storage & Live Migration Design

**Date:** 2026-06-10
**Status:** Draft

## Problem

Each KVM host has an isolated 500 GB EBS data volume. VMs cannot migrate between hosts — the only "migration" path is snapshot → S3 upload → re-deploy on a new host (cold migration). At scale (200 OCP clusters across 50 hosts), duplicate image caches waste storage and cost money. Live migration requires shared storage visible from both source and destination hosts.

## Goals

1. **Live migration**: move running VMs between hosts with minimal downtime
2. **Storage consolidation**: eliminate duplicate image caches across hosts, reduce EBS volume count
3. **Backward compatibility**: single-host / dev deployments continue to work with local EBS
4. **Flexibility**: support managed FSx, BYO NFS, or local-only modes

## Solution: Hybrid Shared Storage with FSx for OpenZFS

### Storage Modes

Three modes, configured at the storage pool level:

| Mode | Storage | Live Migration | Use Case |
|------|---------|---------------|----------|
| `local` | Local EBS at `/var/lib/troshka` | No | Single-host, dev |
| `shared-fsx` | FSx OpenZFS via NFS + small local EBS | Yes | Production multi-host |
| `shared-byo` | User-provided NFS + small local EBS | Yes | Custom infrastructure |

### Why FSx for OpenZFS

| Option | Cost/TB/month | Latency | Ops Burden | Verdict |
|--------|--------------|---------|------------|---------|
| EFS | ~$300 + transfer | 2-10 ms | None | Too slow for VM I/O |
| FSx ONTAP | ~$497 | Sub-ms | None | Good but expensive |
| FSx OpenZFS | ~$258 | Sub-ms | None | Best value, ZFS compression |
| Self-hosted NFS | ~$302 | 1-2 ms | High (SPOF, patching) | Viable but operational burden |

FSx OpenZFS provides sub-millisecond latency, ZFS LZ4 compression (1.5-3x savings on qcow2), instant snapshots, and fully managed operations. Billed per-second with no minimum commitment.

---

## Architecture

### Storage Pools

A **storage pool** groups hosts that share storage. All hosts in a pool:
- Mount the same NFS volume (if shared mode)
- Can live-migrate VMs between each other
- Share image/pattern cache

A host belongs to exactly one pool. The pool is the migration domain.

```yaml
# Managed FSx pool
storage_pool:
  name: "prod-east-1a"
  mode: "shared-fsx"
  az: "us-east-1b"
  fsx_throughput_mbps: 2048
  fsx_storage_gb: 5000

# BYO NFS pool
storage_pool:
  name: "lab-nfs"
  mode: "shared-byo"
  nfs_endpoint: "10.0.1.50:/exports/troshka"

# Local-only pool (default)
storage_pool:
  name: "dev-local"
  mode: "local"
```

### Directory Layout

**Shared mode:**
```
/var/lib/troshka/
├── shared/                          ← NFS mount (FSx or BYO)
│   ├── images/                      ← Library images (backing files)
│   │   ├── {item_id}.qcow2
│   │   └── {item_id}.iso
│   ├── vms/                         ← Active VM disks (overlays)
│   │   └── {project_id}/
│   │       └── {vm[:8]}-{disk[:8]}.qcow2
│   └── cache/
│       ├── patterns/{pattern_id}/   ← Pattern disk cache
│       └── snapshots/{item_id}/     ← Snapshot disk cache
│
├── local/                           ← Local EBS (ephemeral/host-specific)
│   ├── tmp/                         ← Scratch space
│   ├── pxe/{vni}/                   ← PXE boot artifacts
│   └── bmc/{project_id}/           ← BMC configs
│
└── seeds/                           ← Cloud-init seed ISOs (local)
    └── {project_id}/
        └── {vm[:8]}-seed.iso
```

**Local mode:** unchanged from current layout — everything under `/var/lib/troshka/` with no `shared/` or `local/` subdirectories.

### Path Resolution

Troshkad receives `storage_mode` in its config and resolves paths accordingly:

| File Type | `local` mode | `shared-*` mode |
|-----------|-------------|-----------------|
| VM disks | `/var/lib/troshka/vms/` | `/var/lib/troshka/shared/vms/` |
| Library images | `/var/lib/troshka/images/` | `/var/lib/troshka/shared/images/` |
| Pattern cache | `/var/lib/troshka/cache/patterns/` | `/var/lib/troshka/shared/cache/patterns/` |
| Snapshot cache | `/var/lib/troshka/cache/snapshots/` | `/var/lib/troshka/shared/cache/snapshots/` |
| Seed ISOs | `/var/lib/troshka/vms/{project}/` | `/var/lib/troshka/seeds/{project}/` |
| PXE artifacts | `/var/lib/troshka/pxe/` | `/var/lib/troshka/local/pxe/` |
| BMC configs | `/var/lib/troshka/bmc/` | `/var/lib/troshka/local/bmc/` |
| Tmp | `/var/lib/troshka/tmp/` | `/var/lib/troshka/local/tmp/` |

### Storage Hierarchy

```
S3 (cold)  →  FSx OpenZFS (hot/shared)  →  All KVM hosts (NFS mount)
  patterns       /images/ (backing files)      /var/lib/troshka/shared
  snapshots      /vms/ (active disks)          mounted via NFS
  library        /cache/ (pattern cache)
```

One S3→FSx download populates the shared filesystem. All hosts in the pool see it immediately.

---

## FSx Lifecycle Management

### Provisioning

When a `shared-fsx` pool is created:

1. Probe AZ capacity for desired instance types via `describe_instance_type_offerings` with `LocationType=availability-zone`
2. Select best AZ (most instance type coverage)
3. Ensure subnet exists in that AZ (auto-create if needed within existing VPC)
4. Call `create_file_system` (OpenZFS, `SINGLE_AZ_2`)
5. Poll until status = `AVAILABLE`
6. Store filesystem ID, DNS name, mount IP in pool record

**FSx settings:**
- Compression: `LZ4`
- NFS exports: `no_root_squash`, `rw`, `sync`
- Auto-import from S3: disabled (managed by backend)
- Automatic backups: disabled (S3 is cold storage)

### Host Mounting

Cloud-init for shared-mode hosts:

```bash
mkdir -p /var/lib/troshka/shared /var/lib/troshka/local /var/lib/troshka/seeds
mount -t nfs -o nfsvers=4.1,nconnect=16,hard,_netdev \
  ${FSX_DNS}:/fsx/troshka /var/lib/troshka/shared
echo "${FSX_DNS}:/fsx/troshka /var/lib/troshka/shared nfs4 \
  nfsvers=4.1,nconnect=16,hard,_netdev 0 0" >> /etc/fstab
setsebool -P virt_use_nfs 1
```

- `nconnect=16`: 16 parallel TCP connections per mount for throughput
- `hard`: retries indefinitely on server unreachability (prevents VM disk corruption)
- `_netdev`: waits for network before mounting

### Scaling

- **Throughput**: increase/decrease via `update_file_system` (no downtime)
- **Capacity**: FSx OpenZFS Intelligent-Tiering auto-manages hot/cold data
- **Monitoring**: CloudWatch metrics surfaced on admin dashboard

### AZ Probing

Before committing to an AZ:

```
User wants: m7i.16xlarge + m7i.metal-48xl in us-east-1

API: describe_instance_type_offerings(LocationType=availability-zone)

  us-east-1a: m7i.16xlarge ✓  m7i.metal-48xl ✗
  us-east-1b: m7i.16xlarge ✓  m7i.metal-48xl ✓
  us-east-1c: m7i.16xlarge ✓  m7i.metal-48xl ✓

→ Select us-east-1b or us-east-1c
→ Provision FSx + subnet there
```

This replaces the current reactive fallback (try AZ → fail → try next) with proactive validation.

### Teardown

1. Verify no hosts assigned to pool
2. Verify no projects with VMs on shared storage
3. `delete_file_system`
4. Remove pool record

---

## Live Migration

### Prerequisites

- Shared storage (NFS mount visible from both hosts)
- Disk cache mode: `cache=none,io=native` (set during VM definition for shared pools)
- Same CPU family (guaranteed by controlling instance type per pool)
- Same libvirt UID/GID (guaranteed by consistent OS image)
- Security group allows ports 49152-49215 between hosts

### Disk Cache Mode

For shared storage pools, all VM disks use `cache=none,io=native`:
- `cache=none`: writes go directly to NFS, no host-side write cache (required for migration)
- `io=native`: kernel async I/O for efficient NFS write batching
- FSx sub-millisecond latency keeps this acceptable (~1-2ms guest-visible write latency)
- ZFS ARC on FSx server provides read caching

Local mode continues to use default `writeback` cache.

The storage mode is passed to troshkad as `"disk_cache": "none"` or `"disk_cache": "writeback"` in the deploy payload.

### Migration Flow

New backend service: `migration_service.py`

```
migrate_project(project_id, source_host_id, target_host_id)
```

1. **Validate** — both hosts in same storage pool, target has capacity, project is deployed
2. **Prepare networks on target** — `/networks/setup` for each project network
3. **Prepare BMC on target** (if applicable) — `/bmc/setup`
4. **Prepare External IPs on target** (if applicable) — reassign ENI IPs, update nftables
5. **Live-migrate each VM** — in start order, via new troshkad endpoint
6. **Tear down source** — `/networks/full-teardown`, `/bmc/teardown`
7. **Update DB** — set project `host_id` to target

VMs migrate in the project's `startOrder` sequence. If a VM migration fails, already-migrated VMs stay on target, remaining stay on source (split state reported as error).

### New Troshkad Endpoint

```
POST /commands/vm/migrate
{
  "domain": "troshka-{proj[:8]}-{vm[:8]}",
  "target_host": "10.0.1.45",
  "target_port": 49152
}
```

Executes: `virsh migrate --live --verbose --persistent --undefinesource tcp://{target}/system {domain}`

- `--persistent`: defines VM on target
- `--undefinesource`: removes VM from source
- Returns migration stats (time, downtime, data transferred)

### Host Evacuation

`evacuate_host(host_id)`:

1. List all projects on host
2. Find target hosts in same pool with available capacity
3. Bin-pack projects across targets by RAM/CPU
4. Migrate each project sequentially
5. Mark source host as `maintenance`

---

## Download Coordination

### Problem

Current `fcntl.flock()` is host-local — invisible to other NFS clients. With shared storage, multiple hosts could race to download the same image.

### Solution: Backend-Level Coordination

New model tracks shared cache state:

```python
class SharedCacheEntry(Base):
    storage_pool_id  # FK to StoragePool
    item_type        # "image", "pattern", "snapshot"
    item_id          # library_item_id or pattern_id
    status           # "downloading", "ready", "error"
    file_path        # relative path on shared storage
```

**Download flow (shared mode):**

1. Deploy needs image X on pool P
2. Backend checks `SharedCacheEntry(pool=P, item=X)`
   - `ready` → skip download, use existing path
   - `downloading` → wait/poll until ready
   - not found → create entry as `downloading`, pick any host in pool to download, update to `ready` on success

One download serves all hosts. The backend is the single coordinator — no distributed file locking needed.

**Local mode:** unchanged, existing `flock()` mechanism.

### Garbage Collection

**Local mode**: unchanged — current per-host GC behavior (capacity sync, orphan cleanup, network repair, cache eviction).

**Shared mode**: GC operates at the **pool level**, not per host. The existing GC steps adapt:

**1. Capacity Sync**
- Reports FSx volume usage (via CloudWatch or `df` on any host's NFS mount) instead of local EBS usage
- Local EBS capacity still tracked per host for the small local volumes

**2. Orphan Cleanup (`/shared/vms/`)**
- Scans `/shared/vms/` for project directories
- A project directory is orphaned if the project no longer exists in the DB OR is not deployed to any host in the pool
- Compared to local mode: must check all hosts in the pool, not just the host running GC

**3. Cache Eviction (`/shared/images/`, `/shared/cache/`)**
- Uses `SharedCacheEntry` table to find stale entries
- An image is stale if:
  - No project in the entire pool references it as a backing file (join across all projects on all hosts in pool)
  - AND it has been unused for longer than `gc.shared_cache_stale_hours` (default 168h / 7 days)
- Eviction deletes the file from shared storage AND removes the `SharedCacheEntry` row
- Backing images actively used by any running VM in the pool are never evicted

**4. Network Repair**
- Unchanged — runs per host since networks are host-local (bridges, namespaces, nftables)

**5. Local Artifact Cleanup**
- New step for shared mode: cleans orphaned seed ISOs, PXE artifacts, BMC configs from `/local/` on each host
- A seed ISO is orphaned if its project is no longer deployed on this specific host
- PXE/BMC artifacts follow same logic

**GC Trigger:**
- Shared pool GC runs when: admin clicks "Clean" on the pool, a host connects to a shared pool, or on a configurable schedule
- Only one GC run per pool at a time (backend-level mutex, not file lock)
- GC picks any available host in the pool to execute filesystem scans via troshkad

---

## Data Model

### New Models

**StoragePool:**
```python
class StoragePool(Base):
    __tablename__ = "storage_pools"

    id: Mapped[str]                          # UUID
    name: Mapped[str]
    mode: Mapped[str]                        # "local", "shared-fsx", "shared-byo"
    az: Mapped[str | None]                   # required for shared modes
    subnet_id: Mapped[str | None]            # pinned subnet

    # FSx fields (shared-fsx only)
    fsx_filesystem_id: Mapped[str | None]
    fsx_dns_name: Mapped[str | None]
    fsx_mount_ip: Mapped[str | None]
    fsx_throughput_mbps: Mapped[int | None]
    fsx_storage_gb: Mapped[int | None]

    # BYO NFS fields (shared-byo only)
    nfs_endpoint: Mapped[str | None]

    status: Mapped[str]                      # "creating", "available", "error", "deleting"
    provider_id: Mapped[str]                 # FK to Provider

    hosts: Mapped[list["Host"]] = relationship(back_populates="storage_pool")
    cache_entries: Mapped[list["SharedCacheEntry"]] = relationship(...)
```

**SharedCacheEntry:**
```python
class SharedCacheEntry(Base):
    __tablename__ = "shared_cache_entries"

    id: Mapped[str]
    storage_pool_id: Mapped[str]             # FK to StoragePool
    item_type: Mapped[str]                   # "image", "pattern", "snapshot"
    item_id: Mapped[str]
    status: Mapped[str]                      # "downloading", "ready", "error"
    file_path: Mapped[str]                   # relative path on shared storage
    size_bytes: Mapped[int | None]
    downloaded_by_host_id: Mapped[str | None]
    created_at: Mapped[datetime]
```

### Modified Models

**Host:** add `storage_pool_id: Mapped[str | None]` (FK, nullable for backward compat)

**Provider:** add `default_instance_types: Mapped[list | None]` (JSONB, for AZ probing)

### No Changes

Project, LibraryItem, Pattern, Network — unchanged.

---

## API Endpoints

### New Router: `storage_pools.py`

```
POST   /api/storage-pools                    # Create pool
GET    /api/storage-pools                    # List pools
GET    /api/storage-pools/{id}               # Pool details + hosts + cache stats
PATCH  /api/storage-pools/{id}               # Update (resize FSx)
DELETE /api/storage-pools/{id}               # Delete (must be empty)
GET    /api/storage-pools/{id}/cache         # List cached items
DELETE /api/storage-pools/{id}/cache/{entry}  # Evict cache entry
POST   /api/storage-pools/{id}/probe-azs     # Probe AZ capacity for instance types
```

### Modified Endpoints

```
POST   /api/hosts                            # Add storage_pool_id
POST   /api/projects/{id}/migrate            # Trigger project migration
POST   /api/hosts/{id}/evacuate              # Evacuate all projects
```

---

## Security

### IAM Permissions (new)

```json
{
  "Effect": "Allow",
  "Action": [
    "fsx:CreateFileSystem", "fsx:DeleteFileSystem", "fsx:DescribeFileSystems",
    "fsx:UpdateFileSystem", "fsx:CreateVolume", "fsx:DeleteVolume",
    "fsx:DescribeVolumes", "fsx:UpdateVolume",
    "fsx:TagResource", "fsx:UntagResource", "fsx:ListTagsForResource"
  ],
  "Resource": "*"
}
```

One-time (first FSx in account):
```json
{
  "Effect": "Allow",
  "Action": "iam:CreateServiceLinkedRole",
  "Resource": "arn:aws:iam::*:role/aws-service-role/fsx.amazonaws.com/*",
  "Condition": { "StringLike": { "iam:AWSServiceName": "fsx.amazonaws.com" } }
}
```

### Security Group Rules (new)

| Port | Protocol | Source | Purpose |
|------|----------|-------|---------|
| 2049 | TCP | Self (same SG) | NFS |
| 49152-49215 | TCP | Self (same SG) | Live migration |

### NFS Security

- `no_root_squash` on exports (required for QEMU/libvirt)
- `setsebool -P virt_use_nfs 1` on all hosts
- Same qemu UID/GID across all hosts (enforced by consistent OS image)

---

## Frontend

### New: Storage Pools Page (`/admin/storage-pools`)

- Pool cards: name, mode, AZ, status, host count, cache usage
- Create pool flow: select mode → probe AZs (for FSx) → configure → create
- Pool detail: host list, cache entries, resize controls

### Modified: Project Page

- "Migrate" button on deployed projects (when host is in shared pool)
- Modal: select target host from same pool → confirm → progress indicator

### Modified: Host Detail

- "Evacuate" button (when host is in shared pool)
- Shows migration plan (which projects go where) → confirm → sequential migration

### Modified: Host Provisioning

- Storage pool selector dropdown
- AZ shown (determined by pool, not editable)
- Instance type validated against pool's AZ

### No Changes

Canvas, topology editor, library, patterns, console/VNC, deploy flow.

---

## Configuration

### `config.yaml`

```yaml
storage:
  default_mode: "local"
  fsx:
    deployment_type: "SINGLE_AZ_2"
    compression: "LZ4"
    auto_backup: false
    root_squash: "no_root_squash"
  nfs:
    mount_options: "nfsvers=4.1,nconnect=16,hard,_netdev"
    cache_mode: "none"
    io_mode: "native"
  gc:
    shared_cache_stale_hours: 168
```

### Troshkad Config

```json
{
  "storage_mode": "shared",
  "shared_mount": "/var/lib/troshka/shared",
  "local_mount": "/var/lib/troshka/local"
}
```

---

## Cost Analysis

### 200-OCP Scenario (50 hosts)

| Component | Shared Model | Current (Local EBS) |
|-----------|-------------|-------------------|
| FSx OpenZFS (5 TB, 2 GBps) | $970/month | — |
| Local EBS (50 GB × 50 hosts) | $200/month | — |
| Local EBS (500 GB × 50 hosts) | — | $2,050/month |
| **Total** | **$1,170/month** | **$2,050/month** |

**43% cost reduction** with shared storage, plus live migration capability. ZFS compression likely increases savings further.

**Break-even**: ~3 hosts. Below 3, local EBS is cheaper.

### Dev Environment

Minimal FSx (128 GB, 160 MBps): ~$53/month persistent, or ~$0.88 per 12-hour session (per-second billing, no minimum).

---

## Rollout Phases

1. **Phase 1**: StoragePool model + API + FSx provisioning (hosts default to local)
2. **Phase 2**: Shared storage path resolution in troshkad + download coordination
3. **Phase 3**: Live migration endpoint + backend orchestration
4. **Phase 4**: Frontend (pool management UI, migrate button, evacuate)

Each phase is independently deployable. Existing deployments unaffected.
