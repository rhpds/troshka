# Troshka Development Guide

## Architecture

Nested VM environment builder: FastAPI backend + Next.js frontend + libvirt host agents.

- **Backend**: `src/backend/` — Python 3.11, FastAPI, SQLAlchemy 2, Alembic, Dynaconf
- **Frontend**: `src/frontend/` — Next.js 15 (App Router), PatternFly 6, React Flow, Zustand
- **Config**: `src/backend/config/config.yaml` (overrides: `config.local.yaml`, env vars `TROSHKA_*`)
- **Database**: PostgreSQL 16 (port 5433 in dev), SQLite for tests

## Dev Environment

```bash
./dev-services.sh start          # Start everything (PostgreSQL + backend + frontend)
./dev-services.sh restart backend # Restart backend only (frontend hot-reloads)
./scripts/host-ssh.sh            # SSH into first connected host (credentials from DB)
./scripts/host-ssh.sh -- <cmd>   # Run command on host
./scripts/host-db.sh             # Interactive Python shell with DB session + models
./scripts/host-db.sh "<code>"    # Run inline DB query
./scripts/update-agent.sh       # Push troshkad update via API (fast, stamps version)
./scripts/reinstall-agent.sh    # Full SSH reinstall (for broken agents)
```

- Backend: http://localhost:8200 (no auto-reload — restart required for Python changes)
- Frontend: http://localhost:3100 (hot-reloads)
- Dev mode auto-authenticates as admin

## Running Tests

```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/ -v
```

Tests use SQLite with type compiler overrides for JSONB/UUID. Auth is dev-mode (auto-authenticates).

### Git Commands — ALWAYS Use Absolute Paths

Never `cd` into a subdirectory and then run `git add` with relative paths — this doubles the path segment and fails. Always use one of:

```bash
# Option 1: absolute path (preferred)
git add /Users/prutledg/troshka/src/backend/app/api/file.py

# Option 2: cd to project root first
cd /Users/prutledg/troshka && git add src/backend/app/api/file.py

# Option 3: git status --short to see actual paths, then use those
```

## Key Patterns

### Backend Models (SQLAlchemy 2.0+)
- `Mapped[type]` + `mapped_column()` syntax
- UUIDs as strings: `UUID(as_uuid=False), default=lambda: str(uuid.uuid4())`
- Relationships: `back_populates`, `cascade="all, delete-orphan"` for children
- Register new models in `src/backend/app/models/__init__.py`

### Backend API Routes
- Router: `APIRouter(prefix="/resource", tags=["resource"])`
- Auth: `user: User = Depends(get_current_user)`
- Async operations: spawn `threading.Thread(daemon=True)`, never block HTTP requests
- Register new routers in `src/backend/app/main.py`

### Backend Services
- Function-based modules (not classes)
- Background threads get fresh DB sessions: `SessionLocal()`
- Progress tracking: module-level dicts (e.g., `_deploy_progress`)
- Host operations: `troshkad_client.start_job()` / `poll_job()` / `wait_for_job()` / `cancel_job()`

### Frontend Pages
- `"use client"` directive on all pages
- Raw `fetch()` for API calls (no TanStack Query)
- `useState` + `useEffect` for state management
- PatternFly components: `PageSection`, `Toolbar`, `Card`, `Button`

### Console (Direct Proxy)
- VNC console at `/console?vm=&project=&name=` — bare layout (no app header)
- **Direct proxy**: Browser → `wss://{instance_id}.{base_domain}/ws/{jwt}` → troshka-vncd → localhost VNC (2 hops, no SSH tunnel)
- Backend issues a short-lived JWT (5 min, single-use) signed with the host's agent token
- `troshka-vncd` daemon on each host validates JWT, resolves VNC port via `virsh dumpxml`, proxies binary frames
- TLS via Let's Encrypt (certbot DNS-01 challenge with Route53 instance profile)
- noVNC (`@novnc/novnc`), `focusOnClick=true`
- Virtual keyboard at `/console/keyboard?name=` — popup via `window.open()`
- Keyboard communicates via `postMessage` with same-origin restriction (never `"*"`)
- Key macros: Linux/Windows dropdowns send X11 keysyms via `sendCombo()`
- `sendCombo()`: press all keys down in order, release in reverse — standard VNC key combo pattern

### Console Route53 Setup
- Requires a Route53 hosted zone for console domains (e.g., `troshka.dev.rhdp.net`)
- Config: `console.hosted_zone_id` and `console.base_domain` in `config.local.yaml`
- Each host gets an A record: `{instance_id}.{base_domain}` → public IP
- DNS record created automatically during host provisioning, deleted on removal
- IAM: `troshka-certbot-role` + `troshka-certbot-profile` created during VPC setup
- Instance profile attached to EC2 instances — allows certbot DNS-01 without storing AWS creds on hosts
- certbot installed in `/opt/troshka/venv/`, certs at `/etc/letsencrypt/live/{fqdn}/`
- Auto-renewal via cron: `certbot renew --quiet`
- `console_domain` stored on Host model, set during provisioning
- **IAM policy note**: `route53:GetChange` requires `Resource: "*"` (not scoped to hosted zone)

### Canvas
- Topology stored as JSONB in `Project.topology` (source of truth)
- Zustand store: `useCanvasStore` for nodes, edges, selections
- Node types: `vmNode`, `networkNode`, `storageNode`
- Auto-save: debounced 1s after changes via `_saveTopologyToApi`

## Important Conventions

### Library System
- User libraries use `type="personal"` (NOT `type="user"`)
- Always use `_ensure_user_library()` or `Library.filter_by(type="personal")`

### VNI Allocation
- VNIs are globally unique across all projects (for multi-host VXLAN peering)
- Monotonically increasing, never recycled — high-water mark persisted to `.vni_hwm` file
- Never use the `Network.vni` column (it's unused)

### Topology Remapping (Patterns/Deploy)
- When cloning topology, remap ALL ID references:
  - Node IDs, edge source/target, edge sourceHandle/targetHandle
  - NIC IDs + MACs, disk controller IDs
  - `bootDevices[]` (storage node IDs)
  - `startOrder[].vmId`, `startOrder[].waitForVm`
  - `externalIps[].vmId`, `hiddenNodeIds[]`

### PXE Network Boot
- Firmware (BIOS/UEFI) and Secure Boot are per-VM settings, not per-network
- Two modes: **Troshka managed** (auto-extracts kernel/initrd from library ISO) and **BYO** (user provides boot server)
- Managed mode: VM selects an install ISO via `pxeBootIsoId` on the VM node data
- Deploy flow: cache ISO → extract kernel/initrd with `isoinfo` → enable dnsmasq TFTP → start HTTP server for install source
- PXE boot files: `/var/lib/troshka/pxe/{vni}/tftpboot/` (kernel, initrd, pxelinux.0, pxelinux.cfg/default)
- ISO mount: `/var/lib/troshka/pxe/{vni}/mnt/` (loop-mounted read-only, served via HTTP)
- HTTP install source port: `8080 + (vni % 1000)`, deterministic per network
- Troshkad handler: `/pxe/setup` (extract + mount + serve), cleaned up by `/networks/full-teardown`
- Auto-detects kernel/initrd paths for RHEL, Ubuntu, Debian, SLES ISOs
- The deploy path reads PXE config from topology JSONB, not from Network model/schemas
- `virt-install --boot uefi` for UEFI VMs; `firmware.feature0` flags for Secure Boot

### Cloud-Init
- Seed ISO with NoCloud datasource (cidata volume label)
- `instance-id` must be unique per deploy (UUID suffix) for cloud-init to re-run
- `chpasswd` uses new `users:` format (not deprecated `list: |`)
- Custom user-data is YAML-validated before appending

### Troshkad (Host Agent Daemon)
- Single-file Python daemon at `src/troshkad/troshkad.py` — stdlib only, no pip
- Backend client: `src/backend/app/services/troshkad_client.py` — urllib3 connection pooling with cert fingerprint pinning
- HTTPS on port 31337, bearer token auth
- All host operations go through troshkad — SSH only for initial install
- **troshka-vncd**: separate daemon (`src/troshka-vncd/troshka-vncd.py`) for VNC console relay — port 443, `websockets` library, systemd-managed
- vncd updates pushed via `/admin/update-vncd` endpoint on troshkad, also handled by `update-agent.sh`
- **Qemu hook** (`/etc/libvirt/hooks/qemu`): lives ONLY in agent install script (`agent_deployer.py`), must NOT call `virsh` (deadlocks virtqemud), parses XML from stdin
- **Python string escaping**: backslashes in install script heredocs must be doubled (`\\(`, `\\1`, `\\K`)
- **Shared ISOs**: hard-linked into VM dirs (not symlinked) — prevents qemu permission denied and survives `virsh undefine --remove-all-storage`
- **File ownership**: chown to `qemu:qemu` after creating disks, seeds, and hard links
- **Download locking**: `fcntl.flock()` prevents concurrent downloads of same file
- **Wipe preserves cache**: never deletes `/var/lib/troshka/images/` or `/var/lib/troshka/cache/`
- **Job cancellation**: `DELETE /jobs/{job_id}` sets `_cancelled` flag and kills active subprocess; handlers check `_cancelled` between steps
- **Version**: `VERSION = "dev"` in source, stamped with SHA-256 content hash at push time
- Agent install restarts `virtqemud` so hook changes take effect

### Host Operations
- Disk paths: `/var/lib/troshka/vms/{project_id}/{vm_id[:8]}-{disk_id[:8]}.{format}`
- Image cache: `/var/lib/troshka/images/{item_id}.{format}`
- Pattern cache: `/var/lib/troshka/local/cache/patterns/{pattern_id}/` (always local NVMe, never shared NFS — each host downloads from S3)
- Snapshot cache: `/var/lib/troshka/cache/snapshots/{item_id}/`
- PXE boot files: `/var/lib/troshka/pxe/{vni}/tftpboot/` and `/var/lib/troshka/pxe/{vni}/mnt/`
- Domain names: `troshka-{project_id[:8]}-{vm_id[:8]}`
- BMC config: `/var/lib/troshka/bmc/{project_id}/` (sushy configs, vbmcd PID, htpasswd)
- Flatten qcow2 before S3 upload (merge backing chain for standalone images)

### Virtual BMC (IPMI & Redfish)
- Per-VM BMC endpoints: one sushy-emulator + one vbmc per BMC-enabled VM
- BMC tools live in `/opt/troshka/venv/` (sushy-tools, virtualbmc, libvirt-python)
- BMC bridge: `br-bmc-{project_id[:8]}` inside project namespace
- BMC config: `/var/lib/troshka/bmc/{project_id}/` (sushy configs, vbmcd config, htpasswd)
- BMC network node: `networkType: "bmc"` on a networkNode, auto-created when first VM enables BMC
- Credentials stored in topology JSONB (preserved in patterns for lab instruction stability)
- Troshkad endpoints: `/bmc/setup`, `/bmc/teardown`, `/bmc/status`
- Deploy order: BMC setup runs after VM definition but before VM startup

### Garbage Collector
- Runs on host agent connect, admin Clean button, or future cron
- Steps: capacity sync → orphan cleanup → network repair → cache eviction → S3 cleanup → SharedCacheEntry cleanup
- Cache eviction: cross-references host cache dirs against DB records (patterns + library items), deletes orphaned entries immediately
- Temp dir cleanup: cross-references against running jobs' `_tmpdirs` — anything not owned by a running job is deleted immediately (no age threshold)
- S3 orphan cleanup: `clean_s3_orphans()` scans `patterns/`, `snapshots/`, `library/` prefixes, deletes objects with no matching DB record, aborts stale multipart uploads
- SharedCacheEntry cleanup: deletes DB records pointing to deleted patterns/library items
- Capacity re-sync: re-runs after cache cleanup so counters reflect freed disk space
- Dry-run mode: `reconcile_host(host_id, dry_run=True)` reports what would be cleaned without deleting

### Pattern Save State
- Backend `Pattern.state`: "creating" → "capturing" → "available" or "error"
- Frontend patterns page shows read-only cards during save (buttons disabled, delete hidden)
- Auto-polls every 3s while any pattern is in creating/capturing state; 10s baseline poll + visibilitychange for tab-switch refresh
- **Cancellation**: deleting a capturing pattern cancels troshkad jobs (kills S3 uploads/flattens), cleans up S3 prefix, and removes host cache

### Deploy Pipeline
- Parallel VM deployment: disk creation, VM definition, and start run concurrently per VM
- Progress: byte-level download tracking with active transfer detail
- External access toggle: `externalAccess` on gateway node — when off, no EIPs or port forwards are provisioned (gateway stays for outbound NAT)
- Topology templates: predefined OCP templates with version dropdown, deploy time estimates, auto-sizing from install results

### Health Poller & Storage Monitoring
- `health_poller.py` runs periodic checks on all connected hosts
- Reports all mounted partitions via troshkad `/health` endpoint (not just root)
- Evaluates partition thresholds, stores `storage_warnings` JSONB on Host model
- Frontend shows warning badges on hosts admin page when partitions exceed thresholds
- Re-signs host TLS certs hourly, checks CA expiry (renews at 90 days)

### Storage Auto-Extend
- Auto-extend for EBS volumes and FSx file systems when usage exceeds threshold
- Config columns on `storage_pools` and `hosts`: `auto_extend_enabled`, `auto_extend_threshold_pct`, `auto_extend_increment_gb`, `auto_extend_max_gb`
- Manual extend via admin UI (pool page "Extend Now" button) with real-time capacity polling
- FSx has a 6-hour cooldown between extends — backend catches this error and returns a clear message
- `storage_extend.py` service handles both FSx and EBS extend logic
- EBS: `ModifyVolume` API, requires `describe-volumes-modifications` polling
- FSx: `UpdateFileSystem` API with `StorageCapacityReservationGiB`

### Libvirt Events (troshkad)
- Lifecycle events: `VIR_DOMAIN_EVENT_ID_LIFECYCLE` callback for start/stop/crash/reboot detection
- Block threshold events: `VIR_DOMAIN_EVENT_ID_BLOCK_THRESHOLD` for disk usage alerts, auto-re-arms after trigger
- Batch VM state polling: `POST /vms/states` returns all domain states in one call (replaces per-VM polling)

### DNS Providers
- `dns_providers` API + admin page for managing external DNS (Route53, etc.)
- Projects can optionally attach a DNS provider + domain + GUID for automated DNS record management

### Duplicate Name Prevention
- Projects, patterns, library items, and snapshots enforce unique names per user
- Frontend pre-checks before destructive operations (e.g., check before VM shutdown for snapshot)

### AWS Provider Setup
- IAM user: `troshka` with inline policy `troshka-policy`
- Credentials stored in `~/secrets/troshka-aws.env`
- Required IAM permissions:
  - **EC2**: RunInstances, TerminateInstances, StopInstances, StartInstances, RebootInstances, ModifyInstanceAttribute, Describe{Instances,InstanceTypes,Images,Vpcs,Subnets,AvailabilityZones}, CreateKeyPair, DeleteKeyPair, CreateTags
  - **EBS**: CreateVolume, DeleteVolume, AttachVolume, DetachVolume, DescribeVolumes, ModifyVolume
  - **VPC**: Create/Delete/Modify{Vpc,Subnet,VpcAttribute,SubnetAttribute}, Create/Delete/Attach/Detach InternetGateway, Create/DeleteRoute, Describe{RouteTables,InternetGateways}, AssociateRouteTable
  - **Security Groups**: Create/Delete, Describe{SecurityGroups,SecurityGroupRules}, Authorize/RevokeSecurityGroupIngress
  - **Elastic IPs**: Allocate/Release/Associate/Disassociate Address, DescribeAddresses, AssignPrivateIpAddresses, UnassignPrivateIpAddresses
  - **FSx**: CreateFileSystem, DeleteFileSystem, DescribeFileSystems, UpdateFileSystem, CreateVolume, DeleteVolume, DescribeVolumes, UpdateVolume, TagResource, UntagResource, ListTagsForResource
  - **VPC Endpoints**: CreateVpcEndpoint, DeleteVpcEndpoints, DescribeVpcEndpoints, ModifyVpcEndpoint
  - **IAM** (one-time): CreateServiceLinkedRole for fsx.amazonaws.com
  - **S3**: PutObject, GetObject, DeleteObject, HeadObject, ListBucket on `troshka-images`
- IAM policy is a managed policy `troshka-policy` — source of truth at `infra/iam-policy.json`
- VPC setup creates subnets in all AZs — provisioner retries across AZs if instance type not supported
- VPC setup creates an S3 Gateway Endpoint — keeps S3 traffic on AWS private network (free, no NAT fees)
- Provisioner never falls back to default VPC — requires explicit VPC setup
- `Setup VPC` auto-creates a troshka-managed VPC if none exists (tagged `ManagedBy: troshka`)
- VPC discovery only lists troshka-managed VPCs, not all VPCs in the account

### Shared Storage & Live Migration
- **Storage pools** group hosts sharing NFS storage — all hosts in a pool can live-migrate VMs between each other
- Three modes: `shared-fsx` (managed FSx OpenZFS), `shared-byo` (user-provided NFS), `local` (default, no pool needed)
- FSx OpenZFS: Single-AZ, LZ4 compression, `nconnect=16`, `cache=none,io=native` for VM disks
- Per-second billing, no minimum commitment (~$53/month for 128 GB/160 MBps)
- Hosts without a `storage_pool_id` operate in local mode (backward compatible)
- **Download coordination**: `SharedCacheEntry` tracks what's cached on shared storage — one download serves all hosts in the pool
- **Migration**: `virsh migrate --persistent --undefinesource` via troshkad `vm/migrate` endpoint; `--live` added for running VMs, omitted for stopped VMs (cold migration)
- Migration uses **private IP** for intra-VPC traffic (Host.private_ip field)
- Migration orchestration: set up networks/BMC on target → migrate VMs in start order → tear down source
- **Host evacuation**: moves all projects off a host to other hosts in the same pool
- **Path resolution**: troshkad `_storage_path()` routes to `/var/lib/troshka/shared/` or `/var/lib/troshka/local/` based on `storage_mode` config
- **Pool-level GC**: cache eviction uses `SharedCacheEntry` table, checks all projects in pool before evicting
- BYO NFS pools don't require an AZ or provider — user manages their own NFS infrastructure
- Security group rules: NFS (TCP 2049) for FSx, libvirt TLS (TCP 16514) + migration data (TCP 49152-49215) for all shared pools
- **PKI**: pool-level CA (10-year, stored on StoragePool.ca_cert/ca_key), host certs signed with both public+private IPs as SANs (1-year, re-signed hourly by health poller)
- Libvirt TLS: mutual TLS with pool CA verification, no `tls_no_verify_certificate`
- Auto-renewal: health poller checks CA expiry (renews at 90 days), re-signs and pushes host certs hourly via troshkad `tls/update-certs` endpoint
- Provider credentials mapping: use `_boto_client()` helper — `get_credentials()` returns `access_key_id` which must be mapped to `aws_access_key_id` for boto3
- **Placement**: auto-selects pool with most free RAM, syncs capacity before placing, sorts by least-loaded host. Admins can override pool at deploy time.

### Dev Database
- PostgreSQL runs in a podman container (`troshka-postgres`) with persistent volume (`troshka-pgdata`)
- `--restart=always` ensures container restarts after crashes
- Never `podman rm` the container — data persists on the named volume but rm destroys the link
- To fully reset: `podman volume rm troshka-pgdata` (intentional data loss)

## Database Migrations

```bash
cd src/backend
./venv/bin/python3 -m alembic revision -m "description"
./venv/bin/python3 -m alembic upgrade head
```

Head revision chain is in `src/backend/alembic/versions/`. FK columns must use `postgresql.UUID(as_uuid=False)` to match the existing schema (not `String(36)`).
