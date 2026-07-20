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
- Fully automated from admin UI: Providers page → "Setup Console" → enter domain → done
- Console config stored on Provider model (`console_zone_id`, `console_base_domain`, `console_nameservers`)
- `POST /providers/{id}/setup-console` creates Route53 hosted zone + IAM role/instance profile
- `DELETE /providers/{id}/console` removes hosted zone, clears DNS records and host `console_domain` fields
- Each host gets an A record: `{instance_id}.{base_domain}` → public IP (created during provisioning, deleted on removal)
- NS delegation: UI shows nameservers in a collapsible section — admin adds NS records in parent zone
- IAM: `troshka-certbot-role` + `troshka-certbot-profile` created by setup-console endpoint (idempotent)
- Instance profile attached to EC2 instances — allows certbot DNS-01 without storing AWS creds on hosts
- certbot installed in `/opt/troshka/venv/`, certs at `/etc/letsencrypt/live/{fqdn}/`
- Auto-renewal via cron: `certbot renew --quiet`
- `console_domain` stored on Host model, set during provisioning
- **No config.yaml** — console config lives on the Provider, not in config files
- **IAM policy note**: `route53:GetChange` requires `Resource: "*"` (not scoped to hosted zone)

### Canvas
- Topology stored as JSONB in `Project.topology` (source of truth)
- Zustand store: `useCanvasStore` for nodes, edges, selections
- Node types: `vmNode`, `networkNode`, `storageNode`, `containerNode` (single containers AND pods)
- Auto-save: debounced 1s after changes via `_saveTopologyToApi`
- Empty canvas (draft, no nodes) shows "Import Template YAML" overlay — palette still interactive behind it

### Container Nodes
- Single containers: `containerNode` with `isPod: false` (default) — one podman container per node
- Template YAML: `containers:` section with `type: container`, `image`, `command`, `ports`, `env`, `volumes`
- Troshkad endpoints: `/containers/create`, `/containers/start`, `/containers/stop`, `/containers/restart`, `/containers/destroy`
- Batch state polling: `POST /containers/states` returns all container states in one call
- Container logs: `GET /containers/{id}/logs` via troshkad
- Veth networking: container gets a veth pair connected to the project bridge (same as VMs)
- Canvas: uses same `ContainerNode.tsx` component as pods, distinguished by `isPod` flag
- Deploy service routes to container vs pod endpoints based on `isPod`

### Template Import/Export
- **Import**: `POST /projects/{id}/import-template` — takes `template_yaml` dict, generates topology via `resolve_inline_template` + `generate_topology_from_template` (includes auto-layout), patches project in-place. Frontend validates YAML syntax and required sections (`vms`, `networks`) before sending. Only works on `draft` projects.
- **Export**: `GET /projects/{id}/export-template` and `GET /patterns/{id}/export-template` — reverse-maps canvas topology JSONB to simple `infra_template.yaml` format via `export_topology_to_template()`. Returns YAML with `text/yaml` content type. Includes OCP metadata, disconnected config, bastion services, and DNS records if present on the topology.
- **Inline templates**: `resolve_inline_template()` accepts template YAML from external sources (e.g. agnosticv `#include`) without needing files on disk
- **Round-trip**: import → edit on canvas → export produces valid template YAML that can be re-imported or used in agnosticv
- **Library item references**: disks can include `library_item_id` / `library_item_name` to reference a library image; VMs can include `pxe_boot_iso_id` / `pxe_boot_iso_name` for PXE boot ISOs. Import validates all referenced items exist in the DB. Blank disks (no `library_item_id`) create empty qcow2 at the specified `size_gb`.
- **Frontend UI**: "Import Template YAML" button on blank canvas opens paste/upload modal. "Export Template" button in action bar (next to MegaConsole/Save as Pattern) opens confirmation modal noting only infra topology is exported (not disk images — use Save as Pattern for that).

### Pod Nodes (Container Groups)
- Pods are `containerNode` with `isPod: true` — not a separate node type
- Sub-containers stored in `initContainers` and `podContainers` arrays on topology JSONB
- TypeScript field is `podContainers` (not `containers`) to avoid name collision with YAML section
- Template YAML uses `type: pod` in the `containers:` section, with `init_containers` and `containers` sub-keys
- Troshkad endpoints: `/pods/create`, `/pods/start`, `/pods/destroy`
- Veth networking shared via pod infra container (same pattern as single containers)
- Init containers run sequentially, fail fast on non-zero exit
- Pod-level `cpus`/`memory` hidden — each sub-container has its own resources
- Canvas: collapsible sub-container list with ▸/▾ toggle, 🫛 icon
- Deploy service detects `isPod` and routes to pod endpoints instead of container endpoints

### Registry Credentials
- Per-user CRUD for container registry credentials (OCP installs, mirrors, etc.)
- API: `GET/POST /auth/registry-credentials`, `PUT/DELETE /auth/registry-credentials/{id}`
- Passwords encrypted via Fernet before storage, omitted from list response
- Model: `RegistryCredential` — `registry_url`, `username`, `password` (encrypted), `user_id` FK

### Project Timers
- Background daemon (`project_timer.py`) enforces auto-stop and auto-delete on projects
- Polls every 30s, spawns daemon threads for stop/destroy operations
- Skips projects in transitional states (deploying, stopping, starting, reconfiguring, migrating)
- Sends 5-minute warning notifications via WebSocket before auto-stop and auto-delete
- Project model fields: `run_timer_hours`, `lifetime_expires_at`, `poweroff_mode`

### WebSocket PubSub
- In-memory pub/sub (`ws_pubsub.py`) for real-time project/pattern state updates
- API: `subscribe(project_id, ws)`, `unsubscribe()`, `notify_project(project_id, message)`
- State poller: daemon thread polls every 5s, batch-fetches VM states per host (one call per host, not per VM)
- Pushes `project-state`, `deploy-progress`, `vm-state` messages; tracks `_last_states` to only send diffs
- Thread-safe sync-to-async bridge via `run_coroutine_threadsafe`
- Also supports `subscribe_pattern`/`notify_pattern` for pattern capture progress

### Offline Filesystem Modification
- Troshkad endpoint: `POST /vms/modify-fs` — runs commands against a stopped VM's disk using `guestfish`
- Requires `libguestfs-tools` (installed by agent installer)
- Used for kubelet cert cleanup on pattern OCP deploys (removes stale certs before VM start)
- VM must be stopped (not running) — `guestfish` needs exclusive disk access

### Exec API
- `POST /projects/{id}/vms/{vm_id}/exec` — execute commands on VMs
- `method` parameter: `guest-agent` (structured, no creds), `ssh` (requires network + credentials), `console` (OCR/pexpect), `serial` (PTY pexpect), `auto` (tries all in order)
- **Auto priority**: guest-agent → SSH → console → serial
- **Guest-agent exec**: `virsh qemu-agent-command` with `guest-exec` + poll `guest-exec-status`. Returns structured stdout/stderr/exit_code. No network, no credentials needed. Requires `qemu-guest-agent` with exec enabled.
- **Cloud-init**: automatically unblocks `guest-exec` in `/etc/sysconfig/qemu-ga` (handles both RHEL blocklist and allowlist formats). Controlled by `Project.guest_exec_enabled` (default true), toggle in Palette UI.
- **KubeVirt native exec**: guest-agent via virt-launcher pod exec (`_pod_exec_raw` helper for raw JSON), SSH via dnsmasq pod exec, console via WebSocket to KubeVirt console subresource. KubeVirt auto order omits serial (same as console).
- **Virt-launcher pod exec**: requires custom `troshka-virt-exec` SCC (in `infra/ocpvirt-rbac.yaml`) — standard RBAC `pods/exec` is not enough on OpenShift
- **k8s_stream gotcha**: `_preload_content=True` returns Python repr, not raw JSON — use `_preload_content=False` with manual read loop for JSON responses
- SSH key auth preferred over password when `ssh_key_id` is provided
- `from-template` API accepts `ssh_pub_key` directly for agnosticv key injection

### OCP Route External Access (OCP Virt)
- OCP Virt hosts use OCP Routes instead of EIPs for external access to VMs
- Deploy creates edge-terminated Routes for port 443/80 forwards: `{vm_name}-{port}.apps.{cluster_domain}`
- Route annotation: `haproxy.router.openshift.io/timeout: 3600s` (required for WebSocket consoles)
- Routes are cleaned up during project destroy
- EIP allocation is skipped when all port forwards are routable via Routes

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
- **SELinux** (GCP, Azure, OCP Virt): RHEL images have SELinux enforcing — cloud-init must run `semanage fcontext -a -t virt_image_t '/var/lib/troshka(/.*)?' && restorecon -R /var/lib/troshka` so QEMU can access disk images and symlinks. Without this, VMs fail to start with "Permission denied" on ISOs. AWS Amazon Linux does not have SELinux enforcing.
- **Firewalld** (GCP, Azure only): RHEL images on GCP/Azure have firewalld enabled — cloud-init must open ports 31337 (agent) and 443 (console) with `firewall-cmd --add-port=31337/tcp --add-port=443/tcp --permanent && firewall-cmd --reload`. AWS uses security groups (no host firewall), OCP Virt uses OCP Routes — neither needs firewalld rules.
- **Chrony NTP**: when `gateway_ip` is set on VM data, cloud-init writes `/etc/chrony.conf` pointing at the gateway and restarts chronyd. VMs never use public NTP pools — the gateway namespace runs chrony as the authoritative time source.

### Troshkad (Host Agent Daemon)
- Single-file Python daemon at `src/troshkad/troshkad.py` — stdlib only, no pip
- Backend client: `src/backend/app/services/troshkad_client.py` — urllib3 connection pooling with cert fingerprint pinning
- HTTPS on port 31337, mTLS + bearer token auth (two-layer authentication)
- All host operations go through troshkad — SSH only for initial install
- **mTLS**: Global CA (`agent_ca_cert` in `system_config` table) signs a backend client cert. Troshkad requires client certs signed by this CA — unauthenticated connections (scanners, probes) are rejected at TLS handshake before any HTTP processing. CA + client cert generated on first backend startup via `agent_ca_service.py`. CA cert deployed to hosts during agent install at `/opt/troshka/tls/ca.crt`, referenced by `client_ca` in `troshkad.conf`. Backward-compatible: hosts without the CA cert file run without mTLS (token-only auth). Requires **Install Agent** (not Update Agent) to enable on a host.
- **Rate limiting**: Per-IP auto-ban — 10 auth failures in 60s → 5-min ban, 3 temp bans in 1 hour → permaban (until process restart). Banned IPs rejected in `verify_request()` before spawning handler threads. TLS handshake timeout (10s) in `get_request()` prevents stuck handshakes from blocking the accept loop. Backend has matching middleware (`core/rate_limit.py`).
- **NFS resilience**: NFS mounts use `soft,timeo=50,retrans=3` (fail with EIO instead of D-state). Watchdog probes NFS mount health with 5s timeout thread, auto-recovers via lazy unmount + remount after 60s stale. `/health` reports `nfs_stale` status. `_get_capacity` and `_get_partitions` skip NFS paths when stale.
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
- **NIC models**: `virtio`, `e1000`, `e1000e`, `igb` (Intel 82576 SR-IOV emulation), `rtl8139` — set via `model` field in topology NIC data and template YAML
- **powerOnAtDeploy**: per-VM flag in topology — when `false`, VM is defined but not started during deploy (used for blank target VMs like SNOs that boot via BMC/ACM later)
- Agent install restarts `virtqemud` so hook changes take effect
- **Clock offset**: `--clock offset=variable,adjustment=N` added to virt-install when `clock_offset` is in params — sets guest clock to target datetime at the hypervisor level
- **Gateway chronyd**: per-project chronyd runs in the gateway namespace via `ip netns exec` (same pattern as dnsmasq) — config at `/var/lib/troshka/chrony/{pid}.conf`, pidfile at `/run/troshka-chronyd-{pid}.pid`. Killed during `/networks/full-teardown`. Non-fatal if chrony isn't installed on host.
- **`/vms/set-clock` endpoint**: updates `<clock>` element in libvirt XML via `virsh dumpxml` → parse → `virsh define`, then pushes time to running VMs via `virsh domtime` (guest agent) with `virsh qemu-agent-command` fallback
- **Update drain fix**: `_SKIP_DRAIN` set (module-level) lists commands that don't cancel drain or block updates — includes `vm/ssh-exec` and `containers/states` to prevent health poller traffic from cancelling agent updates indefinitely

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
- **Clock target capture**: optional checkbox in SavePatternModal — when checked, copies `project.clock_target` to `pattern.clock_target`. On deploy, the pattern's clock_target is restored to the new project.

### Deploy Pipeline
- Parallel VM deployment: disk creation, VM definition, and start run concurrently per VM
- Progress: byte-level download tracking with active transfer detail
- External access toggle: `externalAccess` on gateway node — when off, no EIPs or port forwards are provisioned (gateway stays for outbound NAT)
- Topology templates: predefined OCP templates with version dropdown, deploy time estimates, auto-sizing from install results
- **Pattern deploy `common_password`**: `PatternDeployRequest` accepts `common_password` to override BMC and cloud-init credentials baked in the pattern's topology. Without this, pattern-deployed projects get the original builder's password instead of the current deployment's. Overrides `bmcPassword` on BMC networks and `ciCloudUserPassword` on cloud-init VMs.

### Clock Backdating
- **Project-level setting**: `Project.clock_target` (DateTime, nullable) — all VMs in a project share one target datetime
- **Hypervisor offset**: `--clock offset=variable,adjustment=N` in virt-install — guest sees target time from BIOS/UEFI, ticks forward in real time
- **Offset calculation**: `int((clock_target - now_utc).total_seconds())` — negative for past dates
- **Gateway NTP**: chronyd runs per-project in the gateway namespace (`ip netns exec`), serves `local stratum 3` — VMs sync from gateway only
- **Cloud-init**: all VMs get chrony pointing at gateway IP with `makestep 1 -1` (immediate step on any offset)
- **Template YAML**: top-level `clock_target: "2025-01-15T00:00:00Z"` — imported to Project model, exported back
- **Live adjustment**: PATCH `clock_target` on active project triggers `adjust_clocks_async()` — updates libvirt XML + pushes time via guest-agent/exec fallback
- **`/vms/set-clock` endpoint**: troshkad handler for live clock updates (XML + time push)
- **Pattern integration**: optional "Capture clock target" checkbox in SavePatternModal — saves `clock_target` on Pattern model, restored on deploy
- **Frontend**: Clock toggle + datetime picker in Palette (Project section) — toggle shows/hides picker, explicit "Set" button to apply
- **Service**: `src/backend/app/services/clock_service.py` — `compute_clock_offset()`, `adjust_clocks_async()`

### Pull-Through Registry
- **Settings toggle**: User model `pull_through_registry` bool + `pull_through_registry_url`, `_user`, `_password` columns
- **Frontend**: Switch on settings page under OCP Pull Secret — when on, replaces pull secret textarea with URL/username/password fields
- **Pull secret construction**: backend builds `{"auths":{"<url>":{"auth":"<base64(user:pass)>"}}}` from the three fields
- **OCP deploy injection**: in `/from-template`, if user has toggle enabled and template doesn't already have `pull_through_registry`, backend injects the config via `_build_pull_through_config()`
- **Priority**: agnosticv template `pull_through_registry` > user toggle > no config (direct pulls)
- **Config dict shape**: `{"enabled": True, "url": str, "orgs": {"registry.redhat.io": "registry_redhat_io", "quay.io": "quay_io"}}`
- **Org convention**: Quay proxy-cache standard — source registry dots replaced with underscores
- **What it enables**: `imageDigestSources` in install-config, `registries.conf` on bastions, podman mirror config — all handled by existing `agent_template.py` code
- **API**: `GET/PUT/DELETE /auth/ocp-pull-secret` (extended), `PATCH /auth/ocp-pull-secret` (new, toggle only)

### AgnosticD-v2 Integration
- **Architecture**: Babylon → AAP2 → agnosticd-v2 (with `troshka` cloud provider + bastion service roles) → Troshka API
- **Catalog items**: defined in agnosticv repo (`troshka/` directory), infrastructure topology in `infra_template.yaml` included via `#include`
- **Config**: `env_type: troshka` — agnosticd-v2 config at `ansible/configs/troshka/`
- **Three deploy modes** (`troshka_deploy_mode`):
  - `template` — full build: infra + bastion services (pre_software_workloads) + OCP + workloads (software_workloads)
  - `pattern` — deploy from saved snapshot, skip all workloads
  - `pattern_workloads` — deploy from snapshot, skip pre-software, run software workloads on top
- **`auto_install_ocp`**: boolean (default true) — when false, `software.yml` skips both `host_ocp4_agent_installer` and `host_ocp4_ibi_installer` roles. Used by IBI lab (students install manually) and non-OCP templates.
- **Pattern deploy `common_password`**: `infrastructure_deployment.yml` passes `common_password` to `project_deploy` module so baked credentials are overridden with the current GUID's password
- **Bastion service roles** (agnosticd-v2): `disconnected_registry`, `disconnected_mirror`, `bastion_gitea`, `bastion_minio`
- **Ansible collection**: `agnosticd.cloud_provider_troshka` — deploy role assembles `template_yaml` from agnosticv merged vars, calls `POST /projects/from-template` then `POST /projects/{id}/deploy`
- **agnosticv `#include`**: files inside catalog item dirs are treated as catalog items by default (causes recursion). Register non-catalog files like `infra_template.yaml` in `.agnosticv.yaml` `related_files` list to prevent this.
- **No catalog-item-specific Python** in Troshka — all lab config comes from YAML templates. Troshka engine is generic; catalog items live in agnosticv.

### Health Poller & Storage Monitoring
- `health_poller.py` runs periodic checks on all connected hosts
- Reports all mounted partitions via troshkad `/health` endpoint (not just root)
- Evaluates partition thresholds, stores `storage_warnings` JSONB on Host model
- Frontend shows warning badges on hosts admin page when partitions exceed thresholds
- Re-signs host TLS certs hourly, checks CA expiry (renews at 90 days)
- Auto-recovery: when a host reconnects (disconnected → connected), `recover_host_services()` in `gc_service.py` restores networking (namespaces, VXLAN, bridges, dnsmasq, nftables) and BMC (sushy, vbmc) for all active projects via background thread. Deduplicates by host ID.

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

### OCP Virt Provider Setup
- Provider type `ocpvirt` — creates nested-virt RHEL VMs on OpenShift Virtualization
- Troshkad runs identically inside KubeVirt VMs as in EC2 instances
- **Provider driver abstraction**: `src/backend/app/services/providers/` — `base.py` interface (16 methods), `ec2.py`, `ocpvirt.py`, `gcp.py`, `azure.py`
- All provisioner calls go through `get_provider_driver(provider)` dispatcher
- **Dev cluster**: `ocpvdev01.dal13.infra.demo.redhat.com` (AMD EPYC 7763, 256 vCPU / 1TB RAM per worker, nested virt enabled)
- **Service account**: `troshka` SA in `troshka` namespace with `troshka-provider` ClusterRole
- **Token**: `oc create token troshka -n troshka --duration=8760h` (1 year)
- **ClusterRole permissions** (least-privilege):
  - `kubevirt.io`: virtualmachines (CRUD + patch), virtualmachineinstances (get, list)
  - `cdi.kubevirt.io`: datavolumes (CRUD)
  - Core: services, PVCs (CRUD + patch), PVs (get, list), namespaces (get, create), nodes (get, list)
  - `route.openshift.io`: routes (CRUD + patch)
- **Storage**: Ceph-NFS via `ocs-storagecluster-ceph-nfs` storage class, ~2.7 TiB available on CephFS
- **Console**: OCP edge Routes (TLS terminated by OCP router), vncd runs with `--no-tls` flag on port 8080
- **Console Route annotation**: `haproxy.router.openshift.io/timeout: 3600s` required for WebSocket — without it HAProxy sends `Connection: Close` and consoles fail
- **Networking**: identical to AWS (VXLAN, nftables, netns) — all inside the host VM
- **EIPs**: not supported on OCP Virt — `externalAccess` toggle disabled for ocpvirt hosts
- **Resize**: not supported (KubeVirt requires stop → modify → start, disabled for now)

### KubeVirt Native Provider Setup
- Provider type `kubevirt` — creates KubeVirt VMs directly on OCP (no nested virt, no troshkad)
- **Architecture**: kopf-based operator manages CRDs (`TroshkaProject`, `TroshkaNetwork`, `TroshkaVM`) that reconcile into KubeVirt VMs, OVN networks, and helper pods
- **Provider driver**: `src/backend/app/services/providers/kubevirt.py` — thin layer creating/watching CRDs
- **Operator**: `src/operator/` — Python/kopf, deployed to `troshka-operator` namespace (configurable)
- **CRDs**: `src/operator/crds/` — TroshkaProject, TroshkaNetwork, TroshkaVM
- **Container images**: operator, dnsmasq, gateway, troshka-tools, sushy, vnc-proxy — built by CI, pushed to `quay.io/redhat-gpte/troshka-*`
- **Prerequisites**: OCP 4.14+, OpenShift Virtualization (KubeVirt + CDI), ODF (Ceph RBD + CephFS), OVN-Kubernetes secondary networks
- **RBAC**: uses same `troshka` SA as nested ocpvirt — `infra/ocpvirt-rbac.yaml` has all permissions for both provider types
- **RBAC escalation**: K8s prevents the SA from creating ClusterRoles with permissions it doesn't hold. An OCP admin must pre-apply the operator RBAC:
  ```bash
  oc apply -f src/operator/deploy/clusterrole.yaml
  oc apply -f src/operator/deploy/clusterrolebinding.yaml
  ```
- **Setup flow**: Create provider in admin UI → auto-creates virtual host → background thread deploys operator + CRDs → "Install Operator" button for repair/retry
- **Virtual host**: one Host record per provider with `host_type="kubevirt-cluster"` — represents cluster capacity, no SSH/agent
- **Networking**: OVN layer2 secondary networks (NADs) + dnsmasq Pod (DHCP/DNS) + gateway Pod (NAT) per project
- **NAD config**: must include `netAttachDefName: "namespace/name"` — OVN-K rejects pods without it
- **SCC**: custom `troshka-network-pods` SCC for NET_ADMIN/NET_RAW, `troshka-network` SA created per project namespace and patched into SCC
- **BMC**: sushy emulator Pod with custom KubeVirt driver (Redfish only, no IPMI), `troshka-bmc` SA created per project namespace
- **VNC console**: `vnc-proxy-{project}` Pod per project, relays noVNC WebSocket to KubeVirt VNC subresource API (`subresources.kubevirt.io/v1/.../vnc`)
- **VNC Route**: OCP edge-terminated Route with `haproxy.router.openshift.io/timeout: 3600s`, auto-generated hostname stored in TroshkaProject CR `status.consoleRoute`
- **VNC RBAC**: `troshka-vnc` SA per project namespace with Role granting `get` on `kubevirt.io` VMIs and `subresources.kubevirt.io` VMIs/vnc
- **VNC proxy image**: reads SA token from `/var/run/secrets/kubernetes.io/serviceaccount/token`, K8S_HOST from env vars
- **Patterns**: fully portable across providers — same topology JSONB, same S3 disk images (qcow2), CDI import → golden PVC → Ceph RBD clone
- **Golden PVC sizing**: reads qcow2 header (bytes 24-31) via S3 Range request for virtual size, headroom `max(size+10, size*1.2)`
- **Pattern capture**: VolumeSnapshot → export Job (qemu-img convert + S3 upload) — untested
- **UEFI SecureBoot**: must explicitly set `secureBoot: false` for plain UEFI — KubeVirt defaults to `true`, which requires SMM
- **Boot order**: `bootOrder` is a sibling of `disk:` on the disk entry, NOT nested inside `disk.disk`
- **VM state polling**: WS poller reads `vmStates` from TroshkaProject CR, maps by VM node UUID (not domain name), normalizes `Running`→`running`
- **Not supported**: clock backdating (no `virsh domtime` equivalent in KubeVirt), gateway NAT pod (TODO)

### GCP Provider Setup
- Provider type `gcp` — creates nested-virt RHEL VMs on Google Compute Engine
- **Driver**: `src/backend/app/services/providers/gcp.py` (~800 lines, self-contained)
- **Dev project**: `troshka-rhdp` under `rhpds-apps` folder (809829662025), billing on RHPDS Master
- **Prerequisites**: pre-create a GCP project, enable Compute Engine + Cloud DNS APIs, create SA with Compute Admin + DNS Admin roles
- **Credentials**: `{"service_account_json": {...}}` — full service account key JSON
- **Instance types**: N2-highmem for hosts (nested virt), E2-standard for pattern buffers (no nested virt needed)
- **Org policy constraint**: `custom.denyCostlyMachineTypes` blocks "exotic" types — E2 and N2-standard work, N2-highmem may need an exception for host provisioning
- **Nested virt**: enabled via `advancedMachineFeatures.enableNestedVirtualization=True`, disabled for pattern buffer hosts
- **Network tags**: instances MUST have `troshka-host` tag for firewall rules to apply (SSH, console, agent, VXLAN)
- **Images**: currently using PAYG from `rhel-cloud` (repos work out of the box). BYOS from `rhel-byos-cloud` available but needs RHSM registration for package installs. Future: Red Hat Image Builder API for custom images with packages pre-installed.
- **Network setup**: "Setup Network" creates custom-mode VPC, subnet (`10.100.1.0/24`), firewall rules targeting `troshka-host` tag
- **Console**: Cloud DNS zone + `certbot-dns-google` plugin for Let's Encrypt TLS
- **EIPs**: GCP static external IPs, associated via access config on nic0
- **Shared storage**: not supported yet (Filestore/NetApp blocked by org policy). Use `local` pool mode with pattern buffer for pattern save.
- **SSH user**: `troshka` (set via instance metadata `ssh-keys`)
- **Data disk**: `/dev/sdb` (second attached persistent SSD)
- **Resize**: requires stop → `setMachineType()` → start (GCP limitation)
- **Pattern buffer**: uses `e2-standard-2` (allowed by org policy, no nested virt)

### Azure Provider Setup
- Provider type `azure` — creates nested-virt RHEL VMs on Azure
- **Driver**: `src/backend/app/services/providers/azure.py` (~880 lines, self-contained)
- **Prerequisites**: create service principal in Azure subscription, assign Contributor role on resource group
- **Credentials**: `{"tenant_id": "...", "client_id": "...", "client_secret": "...", "subscription_id": "..."}`
- **Instance types**: Esv5 series (8 GiB/vCPU, Intel, nested virt supported). Default: `Standard_E32s_v5` (32 vCPU / 256 GiB)
- **Nested virt**: supported natively on Esv5 series (no extra flag)
- **Images**: RHEL BYOS from `redhat` publisher, `rhel-byos` offer (marketplace terms acceptance required on first use), PAYG fallback from `RHEL` offer. Same BYOS repos issue as GCP — future: Red Hat Image Builder for custom images.
- **Network setup**: "Setup Network" creates Resource Group, VNet (`10.100.0.0/16`), subnet, NSG with rules
- **Console**: Azure DNS zone + `certbot-dns-azure` plugin for Let's Encrypt TLS
- **EIPs**: Azure public IPs (Standard SKU, static), associated via NIC IP config
- **Shared storage**: Azure Files NFS Premium v2 (`shared-azure-files` pool mode), ~$0.10/GiB/month, online resize, network ACL deny-all + mandatory private endpoint
- **SSH user**: `troshka` (set via `admin_username`)
- **Data disk**: `/dev/disk/azure/scsi1/lun0` (stable symlink for LUN 0)
- **Terminate cleanup**: must delete VM → OS disk → data disk → NIC → public IP in order (Azure doesn't auto-delete dependents)
- **Stop vs deallocate**: always use `begin_deallocate()` not `begin_power_off()` — deallocate releases compute billing

### Red Hat Image Builder Integration
- Builds custom RHEL host images with all packages pre-installed (qemu-kvm, libvirt, etc.) via Red Hat Insights Image Builder API
- Eliminates RHSM registration at boot and PAYG image premium
- **User flow**: Settings page → save Red Hat offline token → Provider page → "Build Host Image" → wait ~15 min → image auto-set as `default_image`
- Offline token: get from https://access.redhat.com/management/api, stored encrypted on User model (Fernet, same as OCP pull secret)
- Service: `src/backend/app/services/image_builder_service.py` — token exchange, compose submission, polling, progress tracking
- API: `POST /providers/{id}/build-image`, `GET .../status`, `DELETE .../status`
- Background thread polls Red Hat API every 30s, auto-refreshes access token on 401
- Progress tracked in module-level `_build_progress` dict (lost on restart)
- **Azure one-time setup**: Image Builder's service principal (`b94bb246-b02c-4985-9c22-d44e66f657f4`) needs Contributor on the target resource group:
  ```bash
  az ad sp create --id b94bb246-b02c-4985-9c22-d44e66f657f4
  az role assignment create --assignee b94bb246-b02c-4985-9c22-d44e66f657f4 \
    --role Contributor --scope /subscriptions/{SUB_ID}/resourceGroups/{RG_NAME}
  ```
- **Azure image format**: managed image resource ID (`/subscriptions/.../images/...`), NOT marketplace URN — `_parse_image_urn()` handles both
- **GCP setup**: no manual steps — image built in Red Hat's project, shared with service account. `share_with_accounts` must use `serviceAccount:` prefix
- **GCP image format**: `projects/{red-hat-project}/global/images/{name}` — GCP driver handles cross-project image paths
- Pattern buffer hosts also use `default_image` — extra packages are harmless

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
