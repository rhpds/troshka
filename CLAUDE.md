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
- SSH to hosts: `run_ssh_script(host_ip, private_key, script, timeout)`

### Frontend Pages
- `"use client"` directive on all pages
- Raw `fetch()` for API calls (no TanStack Query)
- `useState` + `useEffect` for state management
- PatternFly components: `PageSection`, `Toolbar`, `Card`, `Button`

### Console
- VNC console at `/console?vm=&project=&name=` — bare layout (no app header)
- noVNC (`@novnc/novnc`) over WebSocket, `focusOnClick=true`
- Virtual keyboard at `/console/keyboard?name=` — opens as popup window via `window.open()`
- Keyboard communicates via `postMessage` with same-origin restriction (never `"*"`)
- Key macros: Linux/Windows dropdowns send X11 keysyms via `sendCombo()`
- `sendCombo()`: press all keys down in order, release in reverse — standard VNC key combo pattern

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
- VNIs are globally unique across all projects (for future multi-host VXLAN peering)
- Allocated by scanning all `Project.vni_map` JSONB fields
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
- Backend client: `src/backend/app/services/troshkad_client.py`
- HTTPS on port 31337, bearer token auth, cert fingerprint pinning
- All host operations go through troshkad — SSH only for initial install + VNC console
- **Qemu hook** (`/etc/libvirt/hooks/qemu`): lives ONLY in agent install script (`agent_deployer.py`), must NOT call `virsh` (deadlocks virtqemud), parses XML from stdin
- **Python string escaping**: backslashes in install script heredocs must be doubled (`\\(`, `\\1`, `\\K`)
- **Shared ISOs**: hard-linked into VM dirs (not symlinked) — prevents qemu permission denied and survives `virsh undefine --remove-all-storage`
- **File ownership**: chown to `qemu:qemu` after creating disks, seeds, and hard links
- **Download locking**: `fcntl.flock()` prevents concurrent downloads of same file
- **Wipe preserves cache**: never deletes `/var/lib/troshka/images/` or `/var/lib/troshka/cache/`
- **Version**: `VERSION = "dev"` in source, stamped with SHA-256 content hash at push time
- Agent install restarts `virtqemud` so hook changes take effect

### Host Operations
- Disk paths: `/var/lib/troshka/vms/{project_id}/{vm_id[:8]}-{disk_id[:8]}.{format}`
- Image cache: `/var/lib/troshka/images/{item_id}.{format}`
- Pattern cache: `/var/lib/troshka/cache/patterns/{pattern_id}/`
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
- Steps: capacity sync → orphan cleanup → network repair → cache eviction
- Cache eviction configurable per type in `config.yaml` (`gc.cache_stale_hours_*`)

### Pattern Save State
- Backend `Pattern.state`: "creating" → "capturing" → "available" or "error"
- Frontend patterns page shows read-only cards during save (buttons disabled, delete hidden)
- Auto-polls every 3s while any pattern is in creating/capturing state

### Duplicate Name Prevention
- Projects, patterns, library items, and snapshots enforce unique names per user
- Frontend pre-checks before destructive operations (e.g., check before VM shutdown for snapshot)

### AWS Provider Setup
- IAM user: `troshka` with inline policy `troshka-policy`
- Credentials stored in `~/secrets/troshka-aws.env`
- Required IAM permissions:
  - **EC2**: RunInstances, TerminateInstances, StopInstances, StartInstances, RebootInstances, Describe{Instances,InstanceTypes,Images,Vpcs,Subnets,AvailabilityZones}, CreateKeyPair, DeleteKeyPair, CreateTags
  - **VPC**: Create/Delete/Modify{Vpc,Subnet,VpcAttribute,SubnetAttribute}, Create/Delete/Attach/Detach InternetGateway, Create/DeleteRoute, Describe{RouteTables,InternetGateways}, AssociateRouteTable
  - **Security Groups**: Create/Delete, Describe{SecurityGroups,SecurityGroupRules}, Authorize/RevokeSecurityGroupIngress
  - **Elastic IPs**: Allocate/Release/Associate/Disassociate Address, DescribeAddresses
  - **S3**: PutObject, GetObject, DeleteObject, HeadObject, ListBucket on `troshka-images`
- VPC setup creates subnets in all AZs — provisioner retries across AZs if instance type not supported
- Provisioner never falls back to default VPC — requires explicit VPC setup
- `Setup VPC` auto-creates a troshka-managed VPC if none exists (tagged `ManagedBy: troshka`)
- VPC discovery only lists troshka-managed VPCs, not all VPCs in the account

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
