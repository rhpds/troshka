# Troshka Architecture

Troshka is a nested VM environment builder designed for lab and demo infrastructure. It enables users to create complex multi-VM topologies with custom networking, storage, and external access through an intuitive web interface.

## 1. System Overview

Troshka follows a three-tier architecture: browser-based frontend, REST/WebSocket API backend, and distributed host agents running on bare-metal or cloud VMs.

```
┌─────────────────────────────────────────────────────────────────┐
│                        Browser (Client)                         │
│                  Next.js 15 App (port 3100)                     │
└────────────────────────┬────────────────────────────────────────┘
                         │ HTTP/WebSocket
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│                   FastAPI Backend (port 8200)                   │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐         │
│  │   REST API   │  │  WebSocket   │  │   Services   │         │
│  │  (17 routes) │  │   (pubsub)   │  │  (business)  │         │
│  └──────────────┘  └──────────────┘  └──────────────┘         │
└───────┬──────────────────────┬──────────────────────┬──────────┘
        │                      │                      │
        ▼                      ▼                      ▼
┌──────────────┐     ┌──────────────┐      ┌──────────────────┐
│  PostgreSQL  │     │  S3 Storage  │      │  Host Agents     │
│   (port      │     │   (library/  │      │   (troshkad)     │
│    5433)     │     │   patterns)  │      │   port 31337     │
└──────────────┘     └──────────────┘      └──────────────────┘
                                                     │
                                                     ▼
                                           ┌──────────────────┐
                                           │   QEMU/libvirt   │
                                           │   VMs + VXLAN    │
                                           └──────────────────┘
```

### Tech Stack Summary

| Component | Technology | Version |
|-----------|-----------|---------|
| Backend | FastAPI | Python 3.11 |
| Database | PostgreSQL | 16 |
| ORM | SQLAlchemy | 2.0+ |
| Frontend | Next.js | 15 (App Router) |
| UI Framework | PatternFly | 6 |
| Topology Canvas | React Flow | - |
| State Management | Zustand | - |
| Host Agent | Python stdlib | 3.9+ |
| Virtualization | QEMU/libvirt | - |
| Networking | VXLAN + nftables | - |
| Console | noVNC + troshka-vncd | - |

## 2. Component Architecture

### Backend Structure

The FastAPI backend is organized into distinct layers:

**API Routes** (`app/api/`)
- 17 route modules registered in `main.py`
- auth, projects, vms, networks, disks, library, patterns, hosts, providers, eips, storage_pools, dns_providers, api_keys, portal, templates, registry_credential_routes, ws

**Models** (`app/models/`)
- SQLAlchemy 2.0+ ORM models with `Mapped[type]` syntax
- UUIDs stored as strings: `UUID(as_uuid=False)`
- Relationships use `back_populates` with cascade rules
- JSONB columns for flexible topology storage

**Services** (`app/services/`)
- Function-based modules (not classes)
- Background operations spawn daemon threads
- Fresh DB sessions via `SessionLocal()` in threads
- Module-level dicts for in-memory state tracking

### Frontend Architecture

**Pages** (`src/frontend/app/`)
- Next.js 15 App Router with `"use client"` directive
- Raw `fetch()` for API calls (no TanStack Query)
- PatternFly 6 components: PageSection, Toolbar, Card, Button
- `useState` + `useEffect` for state management

**Canvas** (`src/frontend/components/canvas/`)
- React Flow for topology editing
- Zustand store (`useCanvasStore`) for nodes, edges, selections
- Auto-save debounced 1s after changes
- Node types: `vmNode`, `networkNode`, `storageNode`, `containerNode` (single containers and pods)

**Topology Storage**
- Source of truth: `Project.topology` JSONB column
- Frontend syncs to/from Zustand on load/save
- Deploy uses `deployed_topology` snapshot

### Database Design

**PostgreSQL 16** (port 5433 in dev, 5432 in prod)
- Primary storage for all entities: users, providers, hosts, projects, VMs, networks, patterns, library
- JSONB columns for topology, deploy progress, host warnings, BMC config
- UUID primary keys stored as strings
- SQLite for tests with type compiler overrides

**S3 Storage**
- Library items: `s3://troshka-images/library/{item_id}.{format}`
- Patterns: `s3://troshka-images/patterns/{pattern_id}/{vm_id[:8]}-{disk_id[:8]}.{format}`
- Snapshots: `s3://troshka-images/snapshots/{snapshot_id}/{vm_id[:8]}-{disk_id[:8]}.{format}`
- Multipart upload for large files

## 3. Provider Abstraction

All cloud-specific operations go through a unified `ProviderDriver` interface defined in `src/backend/app/services/providers/base.py`. The backend dispatches to provider-specific drivers via `get_provider_driver(provider)`.

### ProviderDriver Interface

17 methods define the contract:
- `provision_host()`, `terminate_host()`, `get_host_status()`, `resize_host()`, `extend_host_storage()`
- `get_host_powerstate()`, `start_host()`, `stop_host()`
- `setup_console()`, `create_console_record()`, `delete_console_record()`, `delete_console()`
- `delete_key_pair()`
- `allocate_eip()`, `associate_eip()`, `release_eip()`, `update_eip_ports()`

### Provider Comparison

| Feature | AWS (ec2.py) | GCP (gcp.py) | Azure (azure.py) | OCP Virt (ocpvirt.py) |
|---------|--------------|--------------|------------------|-----------------------|
| Nested Virt | AMI support | `enableNestedVirtualization=True` | Esv5 series native | KubeVirt nested | 
| EIP Support | Secondary ENI private IPs | Static external IPs | Public IPs (Standard SKU) | Not supported |
| Shared Storage | FSx OpenZFS | Not yet | Azure Files NFS v2 | Ceph-NFS |
| Console TLS | Route53 DNS + certbot | Cloud DNS + certbot-dns-google | Azure DNS + certbot-dns-azure | OCP Routes (edge TLS) |
| Image Format | AMI ID | GCE image path | Managed image resource ID | - |
| SSH User | ec2-user | troshka | troshka | cloud-user |
| Data Disk Path | /dev/nvme1n1 | /dev/sdb | /dev/disk/azure/scsi1/lun0 | /dev/vdb |

**AWS**
- Delegates to legacy `provisioner.py` functions
- VPC setup creates S3 Gateway Endpoint (free, no NAT fees)
- FSx OpenZFS for shared storage (single-AZ, LZ4, nconnect=16)
- Security group rules for SSH, console, agent, VXLAN, NFS, libvirt TLS

**GCP**
- Self-contained driver (~800 lines)
- N2-highmem instance types (8 GiB RAM per vCPU)
- Network tags: `troshka-host` for firewall rules
- PAYG images from `rhel-cloud`, BYOS from `rhel-byos-cloud` (needs RHSM)
- Pattern buffer uses `e2-standard-2` (no nested virt needed)

**Azure**
- Self-contained driver (~880 lines)
- Esv5 series (8 GiB/vCPU, Intel, nested virt native)
- RHEL BYOS images (marketplace terms acceptance required)
- Azure Files NFS Premium v2 for shared storage (~$0.10/GiB/month, online resize, private endpoint)
- Terminate cleanup: VM → OS disk → data disk → NIC → public IP in order
- Always `deallocate()` not `power_off()` to release compute billing

**OCP Virt**
- Creates KubeVirt VMs inside OpenShift namespaces
- Ceph-NFS storage via `ocs-storagecluster-ceph-nfs` storage class
- Console via OCP edge Routes (TLS terminated by OCP router)
- External access via OCP Routes for port 443/80 forwards (no EIPs)
- Routes created during deploy: `{vm_name}-{port}.apps.{cluster_domain}`
- Resize requires stop → modify → start (disabled for now)

## 4. Host Agent — troshkad

`src/troshkad/troshkad.py` is a single-file Python daemon managing QEMU/libvirt on each host. It requires only Python 3.9+ stdlib (no pip dependencies).

### Architecture

- **Protocol**: HTTPS on port 31337
- **Auth**: Bearer token (shared secret in config)
- **Cert pinning**: Client validates SHA-256 fingerprint
- **Threading**: `ThreadingHTTPServer` with daemon threads
- **Version**: SHA-256 content hash (stamped at push time)

### Job System

1. Request arrives at HTTP handler
2. `_create_job(command, params)` allocates UUID, stores in `_jobs` dict
3. Worker thread dispatches to `_handle_{command}()` function
4. Handler appends log lines via `_job_log(job, message)`
5. On completion: `_complete_job(job, "completed"|"failed", result)`
6. Client polls `GET /jobs/{id}` for status/output
7. Cancellation: `DELETE /jobs/{id}` sets `_cancelled` flag, kills subprocess

### Handler Categories

**VM Lifecycle** (12 handlers)
- `vm_create`, `vm_destroy`, `vm_force_off`, `vm_start`, `vm_stop`, `vm_reboot`
- `vm_state`, `vm_list`, `vm_vnc_port`, `vm_config`, `vm_reconfigure`, `vm_undefine`

**Disk & Storage** (5 handlers)
- `disk_create`, `disk_resize`, `seed_create`, `seed_create_batch`, `image_cache`

**Networking** (8 handlers)
- `network_setup`, `network_teardown`, `list_bridges`, `network_full_setup`, `network_full_teardown`
- `lb_setup`, `lb_teardown`, `nft_reset`

**External Access** (2 handlers)
- `eip_configure`, `metadata_deploy`

**Patterns & Snapshots** (5 handlers)
- `snapshot_create`, `snapshot_capture`, `pattern_capture_direct`, `pattern_export`
- `nbd_export`, `nbd_stop`, `nbd_pull_flatten`

**BMC** (4 handlers)
- `bmc_setup`, `bmc_create_bridge`, `bmc_teardown`, `bmc_status`

**Migration** (2 handlers)
- `vm_migrate`, `tls_update_certs`

**Container & Pod Lifecycle** (8 handlers)
- `container_create`, `container_start`, `container_stop`, `container_restart`, `container_destroy`
- `container_states`, `container_logs`
- `pod_create`, `pod_start`, `pod_destroy`

**Execution** (4 handlers)
- `vm_serial_exec`, `vm_ssh_exec`, `vm_file_push_job`, `vm_modify_fs`

**Garbage Collection** (2 handlers)
- `gc_discover`, `gc_clean`

**PXE Boot** (1 handler)
- `pxe_setup`

**Clock** (1 handler)
- `vm_set_clock`

**Utilities** (6 handlers)
- `library_import`, `resize_storage`, `files_remove`, `files_stat`, `upload_and_cache`

**Total: 62+ handlers**

### Version Stamping & Updates

- Source code: `VERSION = "dev"`
- Backend stamps with SHA-256 hash at push time via `update-agent.sh`
- Agent computes own hash on startup if unstamped
- Update endpoint: `POST /admin/update` replaces running daemon binary
- Separate `troshka-vncd` daemon updated via `POST /admin/update-vncd`

## 5. VNC Console — troshka-vncd

`src/troshka-vncd/troshka-vncd.py` is a separate WebSocket-to-VNC relay daemon running on each host.

### Architecture

```
Browser (noVNC client)
    │ WSS (443)
    ▼
wss://{instance_id}.{base_domain}/ws/{jwt}
    │ Validate JWT (5 min TTL, single-use)
    ▼
troshka-vncd daemon (Python + websockets library)
    │ Resolve VNC port via virsh dumpxml
    ▼
localhost:590X (QEMU VNC socket)
```

### Two-Hop Proxy Design

Unlike traditional SSH tunnels (browser → backend → SSH → VNC), Troshka uses a direct proxy:
1. Backend issues short-lived JWT (5 min, single-use) signed with host's agent token
2. Browser connects directly to `wss://{instance_id}.{base_domain}/ws/{jwt}`
3. `troshka-vncd` validates JWT, parses domain XML for VNC port, proxies binary frames
4. No SSH tunnel, no backend middleman — lower latency, better scalability

### TLS Certificates

**Per-Provider DNS Setup**
- Route53 (AWS): hosted zone + certbot DNS-01 challenge
- Cloud DNS (GCP): `certbot-dns-google` plugin
- Azure DNS (Azure): `certbot-dns-azure` plugin
- OCP Routes (OCP Virt): TLS terminated by OCP router, vncd runs with `--no-tls`

**Certificate Management**
- Let's Encrypt via certbot installed in `/opt/troshka/venv/`
- Certs at `/etc/letsencrypt/live/{fqdn}/`
- Auto-renewal via cron: `certbot renew --quiet`
- Instance profile (AWS/Azure) or service account (GCP) for DNS-01 challenge

### Console Frontend

**noVNC Client** (`@novnc/novnc`)
- Page at `/console?vm=&project=&name=`
- Bare layout (no app header)
- `focusOnClick=true` for keyboard input

**Virtual Keyboard**
- Popup window at `/console/keyboard?name=`
- Communicates via `postMessage` with same-origin restriction
- Key macros: Linux/Windows dropdowns
- `sendCombo()`: press keys down in order, release in reverse (standard VNC pattern)

### Console Configuration

- Fully automated from admin UI: Providers page → "Setup Console" → enter domain
- Config stored on Provider model: `console_zone_id`, `console_base_domain`, `console_nameservers`
- Each host gets A record: `{instance_id}.{base_domain}` → public IP
- NS delegation: UI shows nameservers, admin adds NS records in parent zone
- No config.yaml — console config lives on the Provider, not in files

## 6. Networking Model

Troshka uses VXLAN overlays with network namespaces for project isolation.

### VNI Allocation

- Globally unique across all projects (enables multi-host VXLAN peering)
- Monotonically increasing, never recycled
- High-water mark persisted to `/var/lib/troshka/.vni_hwm` on hosts
- Never use the `Network.vni` column (it's unused)

### Network Namespaces

Each project gets its own namespace: `troshka-{project_id[:8]}`
- Prevents CIDR collisions between projects
- Policy routing alone is insufficient for overlapping CIDRs
- All bridges, VXLAN devices, and gateway interfaces live in the namespace
- nftables chains scoped per-namespace

### VXLAN Topology

```
┌─────────────────────────────────────────────────────────────┐
│ Network Namespace: troshka-{project_id}                     │
│                                                               │
│  ┌──────────────┐       ┌──────────────┐                    │
│  │ VM (veth)    │       │ VM (veth)    │                    │
│  │ 10.0.1.10/24 │       │ 10.0.1.11/24 │                    │
│  └──────┬───────┘       └──────┬───────┘                    │
│         │                      │                             │
│         ▼                      ▼                             │
│  ┌─────────────────────────────────────┐                    │
│  │ Bridge: br-{vni}                    │                    │
│  │ (no IP, L2 only)                    │                    │
│  └──────────────────┬──────────────────┘                    │
│                     │                                        │
│                     ▼                                        │
│              ┌──────────────┐                                │
│              │ vxlan-{vni}  │  (VXLAN ID={vni})             │
│              │ (MTU 1450)   │                                │
│              └──────┬───────┘                                │
└─────────────────────┼────────────────────────────────────────┘
                      │ Encapsulated traffic
                      ▼
              Physical NIC (ens5, eth0, etc.)
```

### Gateway Node

When a network has `isGateway=true`, a gateway VM is created with:
- One interface on the internal network (e.g., 10.0.1.1/24)
- One interface on a "public" network (NAT to host)
- IP forwarding enabled
- Default route on internal network points to gateway

### External Access

**Toggle**: `externalAccess` on gateway node
- When ON: provision EIPs, configure port forwards
- When OFF: no EIPs, no port forwards (gateway stays for outbound NAT only)

**EIP Implementation**
- Secondary private IP addresses on host's primary ENI (AWS/Azure)
- Static external IPs (GCP)
- Not supported on OCP Virt
- nftables DNAT rules map public IP:port → VM private IP:port
- Per-project nftables chains for isolation

### nftables Chains

Each project gets dedicated chains in the host's default namespace:
- `troshka-{project_id[:8]}-dnat-pre`: DNAT rules in PREROUTING
- `troshka-{project_id[:8]}-dnat-out`: DNAT rules in OUTPUT (for hairpin NAT)
- `troshka-{project_id[:8]}-filter`: FORWARD rules

Cleanup: `nft delete table inet troshka-{project_id[:8]}` removes all rules atomically.

## 7. Storage Architecture

Troshka supports four storage modes: local (default), FSx OpenZFS (AWS), Azure Files NFS (Azure), and bring-your-own NFS.

### Storage Modes

| Mode | Provider | Technology | Path | Use Case |
|------|----------|-----------|------|----------|
| `local` | Any | Local disk | `/var/lib/troshka/local/` | Single-host, no migration |
| `shared-fsx` | AWS | FSx OpenZFS | `/var/lib/troshka/shared/` | Multi-host pool with live migration |
| `shared-azure-files` | Azure | Azure Files NFS v2 | `/var/lib/troshka/shared/` | Multi-host pool with live migration |
| `shared-byo` | Any | User NFS | `/var/lib/troshka/shared/` | Custom NFS infrastructure |

### Storage Pools

**Definition**: Group of hosts sharing NFS storage, enabling live migration between pool members

**Characteristics**
- All hosts in a pool must use the same storage mode
- Hosts without a `storage_pool_id` operate in local mode (backward compatible)
- FSx/Azure Files pools require an availability zone and provider
- BYO pools don't require AZ or provider (user manages their own NFS)

### FSx OpenZFS (AWS)

**Configuration**
- Single-AZ deployment (multi-AZ not supported by OpenShift installer)
- LZ4 compression enabled
- Mount options: `nconnect=16,cache=none,io=native` for VM disks
- Per-second billing, no minimum commitment (~$53/month for 128 GB/160 MBps)

**Security**
- NFS traffic (TCP 2049) via security group rules
- No public access (private subnet only)

### Azure Files NFS v2

**Configuration**
- NFS Premium tier
- ~$0.10/GiB/month (billed hourly)
- Online resize (no downtime)
- Network ACL deny-all + mandatory private endpoint

**Setup**
- Private endpoint in same VNet as hosts
- Subnet delegation to `Microsoft.Storage/storageAccounts`
- NSG allows NFS (TCP 2049) from host subnet

### Download Coordination

**SharedCacheEntry Table**
- Tracks what's cached on shared storage
- One download serves all hosts in the pool
- Prevents duplicate downloads of same library item/pattern
- GC cross-references this table before evicting cache

### Path Resolution

The troshkad helper `_storage_path()` routes file operations:
- `storage_mode="local"` → `/var/lib/troshka/local/vms/` or `/var/lib/troshka/local/cache/`
- `storage_mode="shared-*"` → `/var/lib/troshka/shared/vms/` or `/var/lib/troshka/shared/cache/`

**Cache Paths**
- Images (library): `/var/lib/troshka/{local|shared}/images/{item_id}.{format}`
- Patterns: `/var/lib/troshka/local/cache/patterns/{pattern_id}/` (always local NVMe for pattern buffer)
- Snapshots: `/var/lib/troshka/{local|shared}/cache/snapshots/{item_id}/`
- VM disks: `/var/lib/troshka/{local|shared}/vms/{project_id}/{vm_id[:8]}-{disk_id[:8]}.{format}`

### Auto-Extend

**Supported Resources**
- AWS: EBS volumes (host data disks), FSx file systems
- Azure: Managed disks, Azure Files shares

**Configuration Columns** (on `storage_pools` and `hosts` tables)
- `auto_extend_enabled`: boolean
- `auto_extend_threshold_pct`: trigger percentage (e.g., 85)
- `auto_extend_increment_gb`: how much to grow
- `auto_extend_max_gb`: stop growing at this size

**EBS Extend**
- `ModifyVolume` API
- Requires `describe-volumes-modifications` polling (can take minutes)
- Online operation (no reboot)

**FSx Extend**
- `UpdateFileSystem` API with `StorageCapacityReservationGiB`
- 6-hour cooldown between extends
- Backend catches this error and returns a clear message

## 8. Deploy Pipeline

Deploy translates canvas topology (JSONB) into running VMs and networks on a host.

### Flow

```
Topology JSONB
    │ Placement: auto-select pool, least-loaded host
    ▼
Backend Validation
    │ Capacity check, quota limits
    ▼
Provision Resources (if needed)
    │ Create EIPs, allocate networks
    ▼
Parallel VM Deployment
    ├─> Download images from S3/URL
    ├─> Create disks (qcow2)
    ├─> Generate cloud-init seed ISOs
    ├─> Define VMs (virsh define)
    ├─> Setup networks/BMC
    └─> Start VMs in order
    │
    ▼
Post-Deploy
    │ Create DNS records, update project state
    ▼
Active Project
```

### Deploy Steps (Checkpointed)

1. `eips` — Allocate external IPs
2. `networks` — Setup VXLAN bridges, namespaces
3. `seeds` — Generate cloud-init ISOs
4. `images` — Download library items to host cache
5. `disks` — Create qcow2 disks with backing files
6. `vms` — Define libvirt domains
7. `starting` — Start VMs in dependency order
8. `dns` — Create DNS records (if provider attached)
9. `done` — Mark project active

**Resume on Restart**
- Deploy step persisted to `Project.deploy_step` column
- Backend startup checks for stuck projects in "deploying" state
- Resumes from last checkpoint via `deploy_project_async(resume_from=step)`

### Parallel Execution

Within each step, per-VM operations run concurrently:
- Disk creation spawns thread per disk
- VM definition and start run in parallel across VMs
- Network setup serialized via `_network_lock` (prevents nftables race conditions)

### Progress Tracking

**Byte-Level Downloads**
- Module-level `_deploy_progress` dict stores active transfers
- WebSocket publishes `{"type": "deploy-progress", "progress": {...}}` to project subscribers
- Frontend polls every 3s while deploy is active

**Detail Format**
```json
{
  "step": "images",
  "detail": "Downloading rhel-9.4.iso",
  "items": [
    {
      "name": "rhel-9.4.iso",
      "downloaded_bytes": 524288000,
      "total_bytes": 1073741824,
      "percent": 48.8
    }
  ]
}
```

### Reconfigure vs Redeploy

**Reconfigure**: Modify running VMs (add RAM, CPUs, disks, NICs) without full teardown
- Stops VMs → updates domain XML → restarts VMs
- Used for minor changes

**Redeploy**: Full teardown → recreate from topology
- Wipes all project resources on host
- Provisions fresh VMs, networks, disks
- Used for major changes (network topology, boot order, firmware)

## 9. Patterns & Snapshots

Patterns capture entire project topology as reusable templates. Snapshots save individual VM disk states.

### Pattern State Machine

```
creating → capturing → available
              ↓
            error
```

**Creating**
- Backend validation, S3 prefix creation
- Frontend shows read-only card (buttons disabled, delete hidden)

**Capturing**
- Stop VMs → flatten qcow2 (merge backing chain) → upload S3 → cleanup
- Job IDs tracked in module-level `_pattern_capture_jobs`
- Cancellation: delete pattern → cancel troshkad jobs → clean S3 prefix → remove host cache

**Available**
- Pattern ready for deploy
- S3 objects at `s3://troshka-images/patterns/{pattern_id}/{vm_id}-{disk_id}.qcow2`

### Pattern Buffer Architecture

**Problem**: Direct disk capture on shared NFS is slow (limited by NFS write throughput)

**Solution**: Dedicated pattern buffer host with local NVMe SSD
- Separate host type: `host_type="pattern-buffer"`
- NBD-based capture: export disks via NBD → pull to buffer → flatten → upload S3
- Pattern cache always at `/var/lib/troshka/local/cache/patterns/` (never shared NFS)
- Uses same `default_image` as regular hosts (extra packages harmless)

### Deploy from Pattern

**ID Remapping** (critical for correctness)

When deploying a pattern, ALL ID references must be remapped:
- Node IDs (VM, network, storage)
- Edge `source`, `target`, `sourceHandle`, `targetHandle`
- NIC IDs and MAC addresses (regenerate)
- Disk controller IDs
- `bootDevices[]` array (list of storage node IDs)
- `startOrder[].vmId` and `startOrder[].waitForVm`
- `externalIps[].vmId`
- `hiddenNodeIds[]` array

**Download & Extract**
1. Fetch pattern from `GET /patterns/{id}`
2. Download disks from S3 to host cache
3. Create qcow2 disks from cached pattern disks
4. Apply remapped topology
5. Deploy as normal project

### Snapshots

**Scope**: Per-VM disk state capture

**Flow**
1. User selects VM from project → "Create Snapshot"
2. Frontend checks for name conflicts (duplicate prevention)
3. Backend stops VM → creates qcow2 snapshot via `qemu-img snapshot -c` → restarts VM
4. Snapshot stored on host at `/var/lib/troshka/cache/snapshots/{snapshot_id}/`
5. Background upload to S3 (async)

**Restore**
- Download snapshot from S3
- Replace VM disk with snapshot qcow2
- Restart VM

## 10. Library System

The library provides reusable disk images and ISOs for VM creation.

### User Libraries

- Type: `type="personal"` (NOT `type="user"`)
- One personal library per user (auto-created via `_ensure_user_library()`)
- Query: `Library.filter_by(type="personal", user_id=...)`

### Library Item Upload

**Multipart Upload** (S3)
1. Client requests `POST /library/items` with `{name, format, size_bytes, ...}`
2. Backend initiates multipart upload, returns `{upload_id, upload_urls: [{part_num, url}]}`
3. Client PUTs each part to its presigned URL
4. Client calls `POST /library/items/{id}/complete-upload` with `{parts: [{part_num, etag}]}`
5. Backend completes multipart upload, marks item available

**URL Import**
- `POST /library/items` with `{url: "https://..."}`
- Backend downloads URL to host cache
- Background upload to S3
- Supports HTTP, HTTPS, S3 presigned URLs

### Sharing by Email

- `POST /library/items/{id}/share` with `{email: "user@example.com"}`
- Creates read-only reference in recipient's personal library
- Shared item points to original S3 object (no duplication)

### ISO Management

**PXE Boot**
- User selects an ISO via `pxeBootIsoId` on VM node data
- Deploy flow caches ISO → extracts kernel/initrd with `isoinfo` → enables dnsmasq TFTP
- Boot files at `/var/lib/troshka/pxe/{vni}/tftpboot/`
- HTTP install source at `http://{host_ip}:{8080+(vni%1000)}/`

**Cloud-Init**
- ISOs attached as CD-ROM devices
- Seed ISOs created with NoCloud datasource (cidata volume label)
- Custom user-data YAML validated before appending

## 11. Live Migration

Troshka supports live migration of VMs between hosts in the same storage pool.

### PKI Infrastructure

**Pool-Level CA**
- 10-year certificate authority stored on `StoragePool.ca_cert` and `ca_key`
- Created during pool setup
- Used to sign host certificates

**Host Certificates**
- 1-year validity, signed by pool CA
- Subject Alternative Names (SANs): both public and private IPs
- Re-signed hourly by health poller (ensures fresh certs even after IP changes)
- Pushed to hosts via troshkad `POST /tls/update-certs` endpoint

**CA Renewal**
- Health poller checks CA expiry
- Auto-renews at 90 days remaining
- Generates new CA cert/key, re-signs all host certs, pushes to all hosts

### Libvirt TLS

**Configuration**
- Mutual TLS with pool CA verification
- No `tls_no_verify_certificate` (proper cert validation)
- libvirt listens on TCP 16514
- Migration data port range: TCP 49152-49215

**Security Group Rules** (shared pools only)
- NFS (TCP 2049) for FSx/Azure Files/BYO
- libvirt TLS (TCP 16514)
- libvirt migration data (TCP 49152-49215)

### Migration Flow

**Live Migration** (running VMs)
1. Setup networks/BMC on target host
2. `virsh migrate --live --persistent --undefinesource --tls` via private IP
3. Teardown networks/BMC on source host
4. Update `Project.host_id` in DB

**Cold Migration** (stopped VMs)
- Same flow, but omit `--live` flag
- VM state: defined but not running

### Migration Orchestration

The `migration_service.py` coordinates the full sequence:
1. Validate: same pool, different host, project active
2. Setup target: create networks, BMC bridges
3. Migrate VMs: preserve start order dependencies
4. Teardown source: remove networks, BMC, nftables chains
5. Update DB: project host assignment

### Host Evacuation

**Use Case**: Maintenance, resize, or decommission

**Flow**
1. Mark host as "draining" (prevents new deploys)
2. For each project on host:
   - Select least-loaded host in same pool
   - Migrate project to new host
3. Wait for all migrations to complete
4. Host is empty, ready for maintenance

## 12. Virtual BMC

Troshka provides IPMI and Redfish endpoints for VMs to support bare-metal provisioning workflows (e.g., OpenShift IPI).

### Architecture

Each BMC-enabled VM gets:
- One `sushy-emulator` instance (Redfish)
- One `vbmc` instance (IPMI)

### BMC Bridge Network

**Per-Project BMC Network**
- Bridge: `br-bmc-{project_id[:8]}`
- Lives inside project namespace
- Auto-created when first VM enables BMC
- Topology: `networkType: "bmc"` on a networkNode

**IP Assignment**
- BMC IPs allocated from BMC network CIDR
- dnsmasq serves DHCP on BMC bridge
- No internet access (isolated L2 network)

### BMC Configuration

**Credentials**
- Stored in topology JSONB: `{username, password}` per VM
- Preserved in patterns for lab instruction stability
- No centralized credential storage

**Config Files** (per-project)
- `/var/lib/troshka/bmc/{project_id}/sushy-{vm_id}.conf`
- `/var/lib/troshka/bmc/{project_id}/vbmcd.conf`
- `/var/lib/troshka/bmc/{project_id}/htpasswd`

### Tools Installation

BMC tools live in `/opt/troshka/venv/`:
- `sushy-tools` (Redfish emulator)
- `virtualbmc` (IPMI emulator)
- `libvirt-python` (dependency)

### Deploy Order

1. Define VMs (create libvirt domain XML)
2. Setup BMC (create bridge, start sushy/vbmc)
3. Start VMs (normal boot)

**Why This Order?**
- sushy-emulator needs domain XML to exist
- BMC must be reachable before VM PXE boots
- Boot server may query Redfish API before VM starts

## 13. Health & Garbage Collection

### Health Poller

Background thread runs periodic checks on all connected hosts.

**Schedule**
- Interval: 30 seconds (configurable via `config.health.interval_seconds`)
- Disconnect threshold: 90 seconds (configurable via `config.health.disconnect_after_seconds`)

**Operations**
1. Call `GET /health` on each connected host
2. Update `last_health_at` timestamp
3. Sync capacity (vcpus, ram, storage) from live host data
4. Update `agent_version` from response
5. Detect disconnected hosts (mark as disconnected after timeout)
6. Auto-reconnect hosts that come back online

**Storage Monitoring**
- Reports all mounted partitions via troshkad `/health` endpoint
- Evaluates partition thresholds: 85% warning, 95% critical
- Stores `storage_warnings` JSONB on Host model
- Frontend shows warning badges on hosts admin page
- Skips `/mnt/iso`, `/boot`, `/boot/efi`, and iso9660 filesystems

**Certificate Management**
- Re-signs host TLS certs hourly (via `POST /tls/update-certs` on troshkad)
- Checks CA expiry (renews at 90 days remaining)
- Ensures host certs always valid even after IP changes

### Garbage Collector

Reconciles DB state with host reality. Runs on:
- Host agent connect (first handshake)
- Admin "Clean" button (manual trigger)
- Future: cron schedule

**Pipeline Steps**

1. **Capacity Sync**
   - Recalculate `used_vcpus` and `used_ram_mb` from active projects
   - Ensures counters match reality (fixes drift from crashes)

2. **Orphan Discovery**
   - `POST /gc/discover` to troshkad: list all domains, bridges, namespaces
   - Returns libvirt domain names and network bridge names

3. **Orphan Cleanup**
   - Cross-reference against DB projects
   - Delete domains not in DB: `virsh destroy` → `virsh undefine --remove-all-storage`
   - Remove bridges not in DB: `ip link del`
   - Remove namespaces not in DB: `ip netns del`

4. **Network Repair**
   - For each active project, check if networks exist on host
   - Recreate missing bridges, VXLAN devices, namespaces
   - Fixes network state after host reboots

5. **Cache Eviction**
   - Cross-reference host cache dirs against DB records
   - Patterns: `/var/lib/troshka/local/cache/patterns/` vs `Pattern` table
   - Library items: `/var/lib/troshka/images/` vs `LibraryItem` table
   - Shared storage: check `SharedCacheEntry` table (one record serves all pool hosts)
   - Delete orphaned entries immediately (no age threshold)

6. **Temp Dir Cleanup**
   - Cross-reference `/tmp/troshka-*` dirs against running jobs' `_tmpdirs`
   - Delete anything not owned by a running job
   - No age threshold (immediate cleanup)

7. **S3 Orphan Cleanup**
   - Scan `patterns/`, `snapshots/`, `library/` prefixes
   - Delete objects with no matching DB record
   - Abort stale multipart uploads (>7 days old)

8. **SharedCacheEntry Cleanup**
   - Delete DB records pointing to deleted patterns or library items
   - Ensures cache coordination stays in sync with DB

9. **Capacity Re-Sync**
   - Run capacity sync again after cache cleanup
   - Counters reflect freed disk space

**Dry-Run Mode**
- `reconcile_host(host_id, dry_run=True)` reports what would be cleaned
- No deletions, just logging
- Used for manual inspection before cleanup

### Wipe vs Clean

**Wipe** (`POST /hosts/{id}/wipe`)
- Nuclear option: delete ALL project data on host
- Removes VMs, networks, disks, nftables chains
- Preserves `/var/lib/troshka/images/` (library cache) and pattern cache
- Used before host termination

**Clean** (`POST /hosts/{id}/gc`)
- Surgical cleanup: only orphaned resources
- Preserves active projects
- Used for routine maintenance

## 14. Authentication & Authorization

Troshka supports two auth modes: OIDC (production) and dev-mode (local development).

### OIDC Mode

**Flow**
1. OAuth proxy (e.g., oauth2-proxy) sits in front of FastAPI backend
2. Proxy validates OIDC token with Red Hat SSO
3. Proxy forwards requests with `X-Forwarded-Email` and `X-Forwarded-User` headers
4. Backend calls `_upsert_sso_user()` to create/update User record
5. Role assigned via `role_for_email()`: checks `admin_users` and `operator_users` CSV config

**Configuration**
- `config.auth.oauth_enabled=true`
- `config.auth.admin_users="alice@example.com,bob@example.com"`
- `config.auth.operator_users="charlie@example.com"`

### Dev Mode

**Flow**
1. No OAuth proxy
2. Backend auto-authenticates as `local-dev@troshka` admin user
3. Calls `_get_or_create_dev_user()` on every request
4. No login required, instant access

**Configuration**
- `config.auth.oauth_enabled=false` (default)

### API Keys

**Format**: `trk_{random}` prefix (32 random bytes, base64-encoded)

**Storage**
- Hashed via `hash_key()` (HMAC-SHA256 with server secret)
- Stored in `ApiKey` table: `key_hash`, `user_id`, `is_active`, `expires_at`, `last_used_at`

**Usage**
1. User creates key via `POST /api-keys`
2. Backend returns full key once (never stored plaintext)
3. Client includes in `Authorization: Bearer trk_...` header
4. Backend validates via `_get_user_from_api_key()` dependency

**Expiry**
- Optional `expires_at` timestamp
- Checked on every request
- `last_used_at` updated on successful auth

### Portal Tokens

**Use Case**: Short-lived, project-scoped tokens for external tools (e.g., AgnosticD)

**Format**: JWT signed with backend secret

**Payload**
```json
{
  "sub": "user_id",
  "project_id": "...",
  "exp": 1234567890
}
```

**Creation**
- `POST /portal/token` with `{project_id, ttl_minutes}`
- Returns signed JWT
- TTL limited to 60 minutes max

**Usage**
- External tool includes in `Authorization: Bearer {token}` header
- Backend validates signature, checks expiry, extracts project_id
- Enforces project-scoped access (can't access other projects)

### Roles

| Role | Level | Permissions |
|------|-------|-------------|
| user | 0 | Own projects, patterns, library items |
| operator | 1 | All user permissions + read-only admin pages |
| admin | 2 | All operator permissions + manage providers, hosts, storage pools |

**Enforcement**
- `Depends(require_role("admin"))` on admin-only routes
- `role_levels` dict: `{"user": 0, "operator": 1, "admin": 2}`
- Comparison: `user_level >= required_level`

### Encryption

**Fernet Symmetric Encryption** (for sensitive fields)
- OCP pull secret: `User.ocp_pull_secret_encrypted`
- Red Hat offline token: `User.rh_offline_token_encrypted`
- Key: `config.auth.encryption_key` (32 random bytes, base64-encoded)
- Auto-generated if not set (regenerates on restart — not suitable for production)

## 15. Red Hat Image Builder

Troshka integrates with Red Hat Image Builder API to build custom RHEL host images with all packages pre-installed. This eliminates RHSM registration at boot and PAYG image premiums.

### User Flow

1. Settings page → save Red Hat offline token (get from https://access.redhat.com/management/api)
2. Provider page → "Build Host Image" → wait ~15 min
3. Image auto-set as `default_image` on provider
4. Future host provisions use custom image (no package install delays)

### Token Management

**Offline Token** (encrypted storage)
- User pastes from Red Hat API portal
- Stored encrypted on `User.rh_offline_token_encrypted` (Fernet)
- Exchange for access token via Red Hat SSO on each API call

**Access Token** (short-lived)
- 15-minute TTL
- Auto-refreshed on 401 responses
- Not stored (regenerated from offline token as needed)

### Build Flow

1. User clicks "Build Host Image" on provider
2. Backend submits compose request via `POST /api/image-builder/v1/compose`
3. Background thread polls `GET /api/image-builder/v1/composes/{id}` every 30s
4. On completion, download image metadata
5. Register image with cloud provider
6. Set `provider.default_image` to new image ID

**Progress Tracking**
- Module-level `_build_progress` dict: `{provider_id: {status, message, percent}}`
- Frontend polls `GET /providers/{id}/build-image/status`
- Lost on backend restart (resume not implemented)

### Cloud-Specific Formats

**AWS**
- Output: AMI ID (e.g., `ami-0123456789abcdef0`)
- Image Builder API handles upload to user's AWS account
- Requires AWS credentials in Image Builder console

**GCP**
- Output: GCE image path (e.g., `projects/{red-hat-project}/global/images/{name}`)
- Built in Red Hat's project, shared with service account
- `share_with_accounts` must use `serviceAccount:` prefix
- GCP driver handles cross-project image paths

**Azure**
- Output: Managed image resource ID (e.g., `/subscriptions/.../images/...`)
- Requires Azure service principal with Contributor role on resource group
- Image Builder's service principal (`b94bb246-b02c-4985-9c22-d44e66f657f4`) needs Contributor

**One-Time Azure Setup**
```bash
az ad sp create --id b94bb246-b02c-4985-9c22-d44e66f657f4
az role assignment create --assignee b94bb246-b02c-4985-9c22-d44e66f657f4 \
  --role Contributor --scope /subscriptions/{SUB_ID}/resourceGroups/{RG_NAME}
```

### Image Contents

**Packages** (from `image_builder_service.py`)
- qemu-kvm, libvirt, virt-install
- dnsmasq, nftables, python3
- xorriso, ncat, sshpass, nfs-utils
- cloud-init, cloud-utils-growpart

**Services Enabled**
- libvirtd, nftables, sshd

**Benefits**
- No RHSM registration needed at boot
- No package install delays (10+ minutes saved)
- No PAYG premium (uses BYOS entitlement)
- Faster host provisioning

### Pattern Buffer Hosts

Pattern buffer hosts also use `default_image` — extra packages are harmless (they just don't use libvirt).

### Cancellation

**Not Implemented**
- No cancel button on frontend
- No cancel endpoint on backend
- Image Builder API doesn't support compose cancellation
- Progress shows "building..." until completion or error
