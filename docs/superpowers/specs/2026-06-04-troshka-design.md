# Troshka — Nested VM Environment Builder

**Date:** 2026-06-04
**Status:** Draft
**Author:** prutledg + Claude

## Overview

Troshka is a self-service web platform for building, managing, and sharing nested VM environments on AWS EC2 and OCP Virtualization. Users design multi-VM topologies through a drag-and-drop visual editor (Visio-like canvas), manage VM lifecycle through the GUI or API, and share environments as time-limited demo labs with public URLs.

The name "troshka" evokes nesting — VMs inside VMs inside cloud infrastructure.

## Target Users

- **End users** (SEs, QA, developers) — create and manage their own VM environments through a self-service portal
- **Guests** (customers, partners) — access shared demo environments via public URLs with console-only permissions
- **Operators** — manage hosts assigned to them, monitor capacity
- **Admins** — provision infrastructure, manage users, maintain the public image library

## Architecture

### Approach: Centralized Monolith with Remote Agents

One FastAPI API server + PostgreSQL deployed on OpenShift. Lightweight Python agents run on each EC2 host and communicate back to the API server via WebSocket. OCP Virt hosts are managed directly via the Kubernetes API.

```
User Browser
  |
  +-- HTTPS --> Next.js Frontend (static + SSR)
  |                 |
  |                 +-- /api/* proxy --> FastAPI API Server
  |                                        |
  |                                        +-- PostgreSQL (state)
  |                                        +-- Redis (queue + cache)
  |                                        +-- WebSocket --> Host Agents (EC2)
  |                                        +-- Kubernetes API --> OCP Virt
  |
  +-- WSS (console) --> API Server --> Agent --> libvirt VNC/SPICE
```

### Components

| Component | Where | Tech | Purpose |
|-----------|-------|------|---------|
| API Server | OpenShift | FastAPI + Uvicorn | REST API, WebSocket hub, business logic, RBAC, host provisioning |
| Frontend | OpenShift | Next.js 15 + PatternFly 6 + React Flow | Drag-and-drop canvas, console viewer, project management |
| Database | OpenShift | PostgreSQL 16 | Projects, VMs, templates, users, host registry, audit log |
| Task Queue | OpenShift | Redis + Celery | Async: host provisioning, bulk power ops, image uploads |
| Host Agent | EC2 (RHEL) | Python + libvirt | VM lifecycle, console proxy (noVNC/SPICE), health reporting |

### Key Design Decisions

- API server is the single gateway to all host agents — no direct browser-to-host traffic
- Agents maintain a persistent WebSocket to the API server, reconnecting on failure
- Console streams are multiplexed over the agent WebSocket connection
- Redis serves double duty: Celery broker + ephemeral cache (host metrics, console session tokens)
- All components are stateless except PostgreSQL — API server and agents can be scaled horizontally
- No credentials or hostnames hardcoded in Python — everything via Dynaconf config + env vars

## Tech Stack

- **Backend:** Python 3.11+, FastAPI, SQLAlchemy 2.0, Alembic, Celery, Dynaconf
- **Frontend:** Next.js 15 (App Router), PatternFly 6, React Flow, Zustand, TanStack Query
- **Console:** noVNC (Linux VMs), SPICE HTML5 (Windows VMs), auto-selected
- **Agent:** Python 3.11+, libvirt-python, dnsmasq (DHCP/DNS/PXE), nftables
- **Database:** PostgreSQL 16
- **Queue/Cache:** Redis
- **Image Storage:** S3 (or MinIO on OCP)
- **Deployment:** Ansible playbook + Jinja2 manifests on OpenShift
- **CI:** GitHub Actions (ruff, mypy, pytest, next build, playwright)
- **Host OS:** RHEL or compatible derivative (Alma, Rocky, CentOS Stream)

## RBAC

Three-tier role model:

| Role | Capabilities |
|------|-------------|
| admin | Manage hosts (provision/deprovision EC2), manage users, set quotas, manage providers, manage public library, see all projects |
| operator | Manage hosts assigned to them, view host metrics, reassign projects between hosts |
| user | Create/manage own projects and VMs, share templates, no host visibility |

Authentication: pluggable — local accounts for quick setup, OAuth proxy (SSO/OIDC) for production. Same pattern as parsec/labagator.

## Data Model

### Core Entities

**User**
- id (guid), email, display_name, role (admin/operator/user), auth_source (local/sso), quota_overrides (jsonb), created_at

**Provider**
- id (guid), name, type (ec2/ocp_virt), config (encrypted JSON — kubeconfig or AWS creds reference), region, state (active/unreachable/disabled), created_by

**Host**
- id (guid), provider_id (FK), instance_id, instance_type, region, state (provisioning/active/draining/terminated), host_type (shared/dedicated/bare_metal), total_vcpus, total_ram_mb, used_vcpus, used_ram_mb, ip_address, agent_status (connected/disconnected), last_health_at

**HostAssignment**
- host_id (FK), operator_id (FK), assigned_at

**Project**
- id (guid), name, description, owner_id (FK), provider_id (FK), host_type (shared/dedicated/bare_metal), host_id (FK, nullable), state (draft/deploying/active/stopping/stopped/deleting), public_token (nullable, unique), guest_permission (console_only/read_power), run_timer_hours (nullable — null=indefinite), run_timer_max_ext_hours, run_timer_started_at (nullable), lifetime_expires_at (nullable — null=indefinite), poweroff_mode (ordered/simultaneous/none), created_at, updated_at

**ProjectShare**
- project_id (FK), user_id (FK), permission (view/use/admin)

**VM**
- id (guid), project_id (FK), host_id (FK), name, description, vcpus, ram_mb, os_template, state (creating/stopped/starting/running/stopping/error/force_stopping), boot_method (template/iso/pxe), boot_iso_id (FK to LibraryItem, nullable), pxe_profile_id (FK, nullable), boot_order (int), console_type (vnc/spice/auto), cloud_init (text, nullable), ip_address, mac_address, created_at

**BootPrereq**
- id, vm_id (FK), depends_on_vm_id (FK), check_type (port/ping/none), check_value (e.g. "3306")

**Network**
- id (guid), project_id (FK), name, cidr, dhcp_enabled (bool), dns_enabled (bool), dns_domain, dns_upstream (bool — whether DNS queries go to internet), pxe_enabled (bool), pxe_profile_id (FK, nullable)

**SecurityRule**
- id, network_id (FK), direction (inbound/outbound), protocol (tcp/udp/icmp/all), port_range_start, port_range_end, source_cidr, action (allow/deny), priority (lower=first), description

Default security posture: outbound allow all, inbound deny all except same-project traffic.

**VMInterface**
- id, vm_id (FK), network_id (FK), ip_mode (dhcp/static), ip_address, mac_address, dns_name

**Disk**
- id (guid), vm_id (FK, nullable), project_id (FK), name, size_gb, format (qcow2/raw), boot_order, attached (bool)

**Tunnel**
- id, src_project_id (FK), dst_project_id (FK), src_network_id (FK), dst_network_id (FK), state (pending_approval/active/rejected/deleted), requested_by, approved_by

**PXEProfile**
- id (guid), project_id (FK), name, type (builtin/byo), network_id (FK), tftp_root

**PXEImage**
- id, profile_id (FK), name, kernel_url, initrd_url, kickstart_url, boot_args

### Library System

All disk images, ISOs, templates, and snapshots are stored in S3 using GUIDs as directory names. No filename collisions possible.

**Library**
- id (guid), type (user/public), owner_id (nullable — null for public), quota_bytes, used_bytes

**LibraryItem**
- id (guid — also the S3 directory name), library_id (FK), name (display name, not unique), description, type (template/snapshot/iso/pxe_kernel/pxe_initrd), format (qcow2/raw/iso), size_bytes, s3_key, checksum_sha256, os_variant, state (uploading/available/deleting/error), source_vm_id (nullable), source_project_id (nullable), tags (jsonb), created_at

**LibraryShare**
- item_id (FK), shared_with_id (FK), permission (use/view)

**ImageCache**
- item_id (FK), host_id (FK), local_path, cached_at, last_used

S3 bucket layout:
```
troshka-images/
  public/{item-guid}/image.qcow2
  users/{user-guid}/{item-guid}/image.qcow2
```

Deduplication: VMs booting from shared images use copy-on-write overlays, not full copies.

## API Design

RESTful, versioned under `/api/v1`. Every GUI action has an API equivalent. WebSocket endpoints for real-time features.

### Authentication
```
POST   /api/v1/auth/login              Local login (returns JWT)
POST   /api/v1/auth/logout             Revoke token
GET    /api/v1/auth/me                 Current user + role
GET    /api/v1/auth/oauth/callback     SSO/OIDC callback
```

### Users (admin only)
```
GET    /api/v1/users                   List users
POST   /api/v1/users                   Create local user
GET    /api/v1/users/{id}              Get user details
PATCH  /api/v1/users/{id}              Update user (role, quotas)
DELETE /api/v1/users/{id}              Remove user
```

### Providers (admin only)
```
GET    /api/v1/providers               List providers
POST   /api/v1/providers               Add provider (EC2 or OCP Virt)
GET    /api/v1/providers/{id}          Provider details + health
PATCH  /api/v1/providers/{id}          Update (kubeconfig rotation, etc.)
DELETE /api/v1/providers/{id}          Remove provider (must drain first)
POST   /api/v1/providers/{id}/test     Test connectivity
```

### Hosts (admin + operator)
```
GET    /api/v1/hosts                   List all hosts
POST   /api/v1/hosts                   Provision new EC2 host
GET    /api/v1/hosts/{id}              Host details + metrics
PATCH  /api/v1/hosts/{id}              Update (assign operator)
DELETE /api/v1/hosts/{id}              Deprovision host
POST   /api/v1/hosts/{id}/drain        Migrate VMs off, prepare for removal
GET    /api/v1/hosts/{id}/vms          List VMs on this host
```

### Projects
```
GET    /api/v1/projects                List own + shared projects
POST   /api/v1/projects                Create project
GET    /api/v1/projects/{id}           Get project (full topology)
PATCH  /api/v1/projects/{id}           Update project metadata
DELETE /api/v1/projects/{id}           Delete project (stops all VMs first)
POST   /api/v1/projects/{id}/clone     Clone entire project
POST   /api/v1/projects/{id}/share     Share with user(s)
DELETE /api/v1/projects/{id}/share/{uid} Revoke share
POST   /api/v1/projects/{id}/export    Export as JSON/YAML
POST   /api/v1/projects/import         Import from JSON/YAML
```

### Project Power Operations
```
POST   /api/v1/projects/{id}/start         Start all VMs (ordered, respects prereqs)
POST   /api/v1/projects/{id}/stop          Graceful stop all (reverse order or simultaneous)
POST   /api/v1/projects/{id}/restart       Graceful restart all
POST   /api/v1/projects/{id}/force-stop    Force stop all
POST   /api/v1/projects/{id}/force-restart Force restart all
GET    /api/v1/projects/{id}/boot-order    Get boot order + prereqs
PUT    /api/v1/projects/{id}/boot-order    Update boot order + prereqs
```

### Project Publishing (Demo Sharing)
```
POST   /api/v1/projects/{id}/publish       Generate public URL + access token
DELETE /api/v1/projects/{id}/publish       Revoke public access
PATCH  /api/v1/projects/{id}/publish       Update guest permissions, extend timer
```

### Public/Guest Access (no auth required, token in URL)
```
GET    /api/v1/public/{token}              Guest: view project topology
GET    /api/v1/public/{token}/vms          Guest: list VMs
POST   /api/v1/public/{token}/vms/{id}/start   Guest: power on VM
POST   /api/v1/public/{token}/vms/{id}/stop    Guest: power off VM
POST   /api/v1/public/{token}/vms/{id}/restart Guest: restart VM
GET    /api/v1/public/{token}/vms/{id}/console Guest: get console session
```

Guest permissions: console_only (view topology + power + console) or read_power (same + see configs). Guests can never create/modify/delete infrastructure.

### VMs
```
GET    /api/v1/projects/{pid}/vms              List VMs
POST   /api/v1/projects/{pid}/vms              Create VM
GET    /api/v1/projects/{pid}/vms/{id}         Get VM details + state
PATCH  /api/v1/projects/{pid}/vms/{id}         Update VM config
DELETE /api/v1/projects/{pid}/vms/{id}         Delete VM
POST   /api/v1/projects/{pid}/vms/{id}/start         Graceful start
POST   /api/v1/projects/{pid}/vms/{id}/stop           Graceful stop
POST   /api/v1/projects/{pid}/vms/{id}/restart        Graceful restart
POST   /api/v1/projects/{pid}/vms/{id}/force-stop     Force stop
POST   /api/v1/projects/{pid}/vms/{id}/force-restart  Force restart
POST   /api/v1/projects/{pid}/vms/{id}/save-template  Save as template to user library
POST   /api/v1/projects/{pid}/vms/{id}/snapshot        Snapshot VM disk to user library
```

### VM Console
```
GET    /api/v1/projects/{pid}/vms/{id}/console   Get console connection info (type, token)
WS     /ws/console/{session_token}                WebSocket: VNC/SPICE binary stream
```

Console auto-selects VNC for Linux, SPICE for Windows. Overridable per VM.

### VM Network Interfaces
```
GET    /api/v1/projects/{pid}/vms/{vid}/interfaces         List NICs
POST   /api/v1/projects/{pid}/vms/{vid}/interfaces         Add NIC
PATCH  /api/v1/projects/{pid}/vms/{vid}/interfaces/{id}    Update (IP mode, address)
DELETE /api/v1/projects/{pid}/vms/{vid}/interfaces/{id}    Remove NIC
```

### Networks
```
GET    /api/v1/projects/{pid}/networks         List networks
POST   /api/v1/projects/{pid}/networks         Create network
PATCH  /api/v1/projects/{pid}/networks/{id}    Update (CIDR, DHCP, DNS)
DELETE /api/v1/projects/{pid}/networks/{id}    Delete network
```

### Network Services (per network)
```
GET    /api/v1/projects/{pid}/networks/{nid}/dhcp      Get DHCP config
PUT    /api/v1/projects/{pid}/networks/{nid}/dhcp      Enable/configure DHCP
DELETE /api/v1/projects/{pid}/networks/{nid}/dhcp      Disable DHCP

GET    /api/v1/projects/{pid}/networks/{nid}/dns       Get DNS config
PUT    /api/v1/projects/{pid}/networks/{nid}/dns       Configure DNS (domain, upstream)
DELETE /api/v1/projects/{pid}/networks/{nid}/dns       Disable DNS

GET    /api/v1/projects/{pid}/networks/{nid}/pxe       Get PXE config
PUT    /api/v1/projects/{pid}/networks/{nid}/pxe       Configure built-in PXE
DELETE /api/v1/projects/{pid}/networks/{nid}/pxe       Disable PXE
```

### Security Rules (per network)
```
GET    /api/v1/projects/{pid}/networks/{nid}/security-rules      List rules
POST   /api/v1/projects/{pid}/networks/{nid}/security-rules      Add rule
PATCH  /api/v1/projects/{pid}/networks/{nid}/security-rules/{id} Update
DELETE /api/v1/projects/{pid}/networks/{nid}/security-rules/{id} Delete
```

### Storage
```
GET    /api/v1/projects/{pid}/disks            List disks
POST   /api/v1/projects/{pid}/disks            Create disk
PATCH  /api/v1/projects/{pid}/disks/{id}       Update (resize, rename)
DELETE /api/v1/projects/{pid}/disks/{id}       Delete disk
POST   /api/v1/projects/{pid}/disks/{id}/attach/{vid}   Attach to VM
POST   /api/v1/projects/{pid}/disks/{id}/detach         Detach from VM
```

### Tunnels (cross-project)
```
GET    /api/v1/tunnels                 List my tunnels
POST   /api/v1/tunnels                 Request tunnel (needs other project owner approval)
PATCH  /api/v1/tunnels/{id}            Approve/reject tunnel request
DELETE /api/v1/tunnels/{id}            Remove tunnel
```

### Libraries
```
GET    /api/v1/libraries               List accessible libraries (own + public)
GET    /api/v1/libraries/{id}          Library details + usage stats
PATCH  /api/v1/libraries/{id}          Update quota (admin only)

GET    /api/v1/libraries/{lid}/items              List items (filter by type, tags, name)
POST   /api/v1/libraries/{lid}/items              Upload item (returns presigned S3 URL)
GET    /api/v1/libraries/{lid}/items/{id}          Item details + cache status
PATCH  /api/v1/libraries/{lid}/items/{id}          Update metadata
DELETE /api/v1/libraries/{lid}/items/{id}          Delete item + purge caches
POST   /api/v1/libraries/{lid}/items/{id}/share    Share with user(s)
POST   /api/v1/libraries/{lid}/items/{id}/prefetch Pre-cache on host(s)
GET    /api/v1/libraries/{lid}/items/{id}/download Presigned download URL

GET    /api/v1/my/library              My library (shortcut)
GET    /api/v1/my/templates            My templates
GET    /api/v1/my/snapshots            My snapshots
GET    /api/v1/my/isos                 My ISOs
GET    /api/v1/public/library          Public library items
POST   /api/v1/public/library/items    Upload to public (admin only)
```

### PXE Profiles
```
GET    /api/v1/projects/{pid}/pxe-profiles         List profiles
POST   /api/v1/projects/{pid}/pxe-profiles         Create profile
PATCH  /api/v1/projects/{pid}/pxe-profiles/{id}    Update
DELETE /api/v1/projects/{pid}/pxe-profiles/{id}    Delete
GET    /api/v1/projects/{pid}/pxe-profiles/{id}/images    List boot images
POST   /api/v1/projects/{pid}/pxe-profiles/{id}/images    Add boot image
DELETE /api/v1/projects/{pid}/pxe-profiles/{id}/images/{iid} Remove image
```

### WebSocket Channels
```
WS     /ws/console/{session_token}     VM console stream (VNC/SPICE binary)
WS     /ws/events                      Project/VM state changes, timer alerts
WS     /ws/agent/{host_id}             Agent <-> API server (internal)
```

## Frontend Architecture

### Tech
- Next.js 15 (App Router), PatternFly 6, React Flow (canvas), Zustand (UI state), TanStack Query (server state)
- noVNC for Linux VM consoles, SPICE HTML5 for Windows VM consoles

### Page Structure
```
/                              Dashboard / project list
/projects                      Project list view
/projects/[id]                 Canvas view (main editor)
/projects/[id]/settings        Project settings (timers, sharing, security)
/projects/[id]/boot-order      Boot order config
/library                       My library (templates, snapshots, ISOs)
/library/public                Public library
/admin/users                   User management (admin)
/admin/providers               Provider management (admin)
/admin/hosts                   Host management (admin/operator)
/demo/[token]                  Public guest view (read-only canvas + consoles)
```

### Canvas (React Flow)
- Custom node types: VMNode, NetworkNode, StorageNode, RouterNode, TunnelNode
- Custom edge types: NetworkEdge (dashed cyan), StorageEdge (dashed yellow)
- Left sidebar: Palette (drag source for components + templates)
- Right sidebar: PropertiesPanel (edit selected node)
- Top: CanvasToolbar (select, pan, connect, zoom, undo/redo)
- Bottom-right: Minimap

### Console Windows
- Multiple simultaneous consoles, each a draggable/resizable React portal
- Focus management: clicking brings to front (z-index tracking)
- Power controls in each console titlebar (stop, restart + force variants)
- Auto-selects VNC (Linux) or SPICE (Windows), overridable per VM
- Ctrl+Alt+Del, paste, screenshot buttons

### State Management
- Server state (projects, VMs, hosts): TanStack Query with WebSocket-driven cache invalidation
- UI state (open consoles, selected node, theme): Zustand
- Canvas state (positions, edges, zoom): React Flow internal store, synced to server on change

### Guest View (/demo/[token])
- Same canvas component with readOnly prop — no palette, no property editing
- Power controls and console buttons remain active
- Timer banner visible ("Environment shuts down in 3h 42m")

### Light/Dark Mode
- Theme toggle in top bar, persisted to localStorage
- PatternFly supports both natively

## Host Agent

Lightweight Python process on each EC2 host (RHEL). Only component that touches libvirt.

### Responsibilities
- Receive commands from API server via WebSocket (create VM, start, stop, snapshot)
- Report host health and VM state changes
- Proxy VNC/SPICE console connections
- Manage local image cache (pull from S3, copy-on-write overlays)
- Manage network services (dnsmasq for DHCP/DNS/PXE)
- Enforce security rules (nftables)

### Lifecycle
1. API server provisions EC2 instance via boto3 with `--cpu-options "NestedVirtualization=enabled"` and cloud-init user data
2. Cloud-init installs libvirt, qemu-kvm, dnsmasq, nftables, python3, troshka-agent
3. Agent starts, opens WebSocket to API server, sends registration (host specs)
4. API server marks host as active
5. Agent enters command loop, sends health metrics every 30s

### Communication Protocol
JSON messages over WebSocket:
- Commands: `{id, type: "vm.create", payload: {...}}`
- Responses: `{id, status: "success", payload: {...}}`
- Events: `{type: "vm.state_changed", payload: {vm_id, old_state, new_state}}`
- Health: `{type: "host.health", payload: {cpu_pct, ram_used_mb, ...}}`

### Console Proxying
```
Browser -> WS /ws/console/{token} -> API Server -> Agent WS -> local VNC/SPICE socket
```
API server relays binary frames without decoding. Agent opens TCP to libvirt's VNC/SPICE socket.

### Security Rule Enforcement
Agent translates SecurityRule records into nftables rules per project network bridge. Default: outbound allow, inbound deny except intra-project.

### Config (Dynaconf)
```yaml
api:
  url: "wss://troshka.example.com/ws/agent"
  # token via TROSHKA_AGENT_API__TOKEN env var
storage:
  image_cache_dir: /var/lib/troshka/images
  image_cache_max_gb: 200
s3:
  bucket: troshka-images
  # credentials via env vars or instance profile
libvirt:
  uri: "qemu:///system"
health:
  interval_seconds: 30
```

## OCP Virtualization Provider

Alternative to EC2 — VMs run as KubeVirt VirtualMachine CRDs on an existing OpenShift cluster.

- Admin adds provider with kubeconfig
- Troshka creates namespace per project (`troshka-{project-id}`)
- VMs become VirtualMachine CRDs, disks become DataVolume CRDs
- Networks map to Multus NetworkAttachmentDefinition resources
- Console via KubeVirt VNC WebSocket API
- Security rules map to NetworkPolicy resources
- No agent needed — API server talks directly to Kubernetes API

## Timers

Two independent clocks per project:

| Timer | Effect | Options |
|-------|--------|---------|
| Run timer | Auto powers off all VMs after N hours of uptime. Resets on manual restart. Guests can extend up to owner-set max. | 1h, 4h, 8h, 24h, 72h, 7d, indefinite |
| Lifetime | Auto deletes entire project after set date. Only owner/admin can extend. | Date/time or indefinite |

When a run timer expires, VMs are stopped using the project's `poweroff_mode`: `ordered` stops in reverse boot order respecting dependencies, `simultaneous` sends stop to all at once, `none` is equivalent to `simultaneous` (exists as a distinct value for explicit "I don't care about order" semantics). When a lifetime expires, all VMs are force-stopped and the project is deleted regardless of poweroff mode.

Alerts delivered via in-app notification bell + banner on canvas (at 7d, 1d, 1h before expiry). No emails. Guests see the banner too.

A CronJob (`timer-check`) runs hourly on OpenShift to enforce expired timers.

## Demo Lab Flow

1. SE builds environment in troshka (drag-and-drop VMs, networks, storage)
2. Sets run timer to 4h, lifetime to 7d, poweroff mode to simultaneous
3. Sets security rules: outbound 443 only, inbound deny all
4. Clicks Publish — gets URL like `https://troshka.example.com/demo/abc123`
5. Sends URL to customer
6. Customer opens link, sees topology, opens VM consoles, can start/stop VMs
7. After 4h of runtime, VMs auto-stop. Customer can restart (timer resets) up to max extension
8. After 7d, entire project self-destructs

## Bulk Deployment (Events / Workshops)

A project can be stamped out into hundreds of isolated environments for large events. Each attendee gets their own segregated copy with its own VMs, networks, storage, and public URL.

### Concepts

**Event** — a grouping of bulk-deployed environments from a single source project.

| Field | Purpose |
|-------|---------|
| id (guid) | Unique identifier |
| name | e.g. "Red Hat Summit 2026 — OpenShift Workshop" |
| source_project_id | The golden project to clone from |
| owner_id | SE who created the event |
| count | Number of environments to deploy (e.g. 200) |
| state | draft / provisioning / ready / running / stopping / teardown / complete |
| naming_pattern | e.g. `summit-2026-{index}` or `{attendee_email}` |
| run_timer_hours | Applied to every environment |
| lifetime_expires_at | Applied to every environment |
| guest_permission | Applied to every environment |
| provider_id | Where to deploy (EC2 / OCP Virt) |
| host_strategy | pack (fill hosts densely) / spread (one env per host) / auto |
| created_at | |

**EventEnvironment** — one attendee's instance of the project.

| Field | Purpose |
|-------|---------|
| id (guid) | Unique identifier |
| event_id (FK) | Parent event |
| project_id (FK) | The cloned project (each env gets its own full project) |
| index | 1..N |
| label | Attendee name, email, or seat number |
| public_token | Unique guest URL for this attendee |
| state | queued / provisioning / ready / running / stopped / error / deleted |
| assigned_host_id | Which host this env landed on |

### How It Works

1. SE builds a golden project in the canvas (e.g. 3 VMs, 1 network, DHCP+DNS)
2. Creates an Event: "Summit Workshop — 200 environments"
3. Troshka calculates resource requirements:
   - Per environment: 14 vCPU, 28 GB RAM (from the golden project)
   - Total: 2,800 vCPU, 5,600 GB RAM
   - Hosts needed: ~22 r8i.4xlarge (128 vCPU, 512 GB each) at ~6 envs per host
4. Shows a deployment plan with cost estimate, asks for confirmation
5. On confirm, Celery task fans out:
   a. Provisions needed EC2 hosts (parallel, waits for agents to connect)
   b. Clones the project N times (parallel, using CoW image overlays — not full copies)
   c. Each clone gets its own isolated network namespace (no cross-env traffic)
   d. Assigns each clone to a host using the placement strategy
   e. Optionally starts all VMs in boot order
   f. Generates N public URLs
6. SE gets a dashboard showing all 200 environments: status, guest URLs, resource usage
7. SE can export a CSV of URLs to hand out (or integrate with an event registration system)
8. At event end: one-click teardown stops all VMs, deletes all projects, deprovisions hosts

### Isolation Guarantees

Each cloned environment is fully segregated:
- **Own project** — separate project record, separate RBAC
- **Own network bridges** — each env gets unique bridge names and non-overlapping internal MACs (CIDRs can overlap since bridges are isolated)
- **Own nftables chains** — no cross-environment traffic, even on the same host
- **Own VNC/SPICE sessions** — console tokens are per-environment
- **Own guest URL** — each attendee sees only their environment

### Host Placement Strategies

| Strategy | Behavior | Best for |
|----------|----------|----------|
| pack | Fill each host to max capacity before provisioning the next | Cost efficiency — fewer hosts |
| spread | One environment per host (or as few as possible per host) | Performance isolation — no noisy neighbors |
| auto | Pack up to 80% capacity, then provision new hosts | Balance of cost and performance |

### Scaling Optimizations

- **Image sharing:** All environments on the same host use CoW overlays off a single base image. 200 RHEL 9 VMs don't mean 200 copies of the base image — just 200 thin overlays.
- **Parallel provisioning:** Hosts are provisioned in parallel. Environments are cloned in parallel per host (agent handles local cloning, which is fast for CoW).
- **Staggered start:** If `start_on_provision` is true, VMs start in batches (e.g. 10 environments at a time) to avoid thundering herd on the host.
- **Pre-warming:** For known events, hosts can be provisioned hours/days ahead with base images pre-cached. Actual environment cloning happens minutes before the event.

### API

```
GET    /api/v1/events                          List events
POST   /api/v1/events                          Create event
GET    /api/v1/events/{id}                     Event details + environment summary
PATCH  /api/v1/events/{id}                     Update event metadata
DELETE /api/v1/events/{id}                     Delete event (tears down all environments)

POST   /api/v1/events/{id}/provision           Start provisioning all environments
POST   /api/v1/events/{id}/start               Start all VMs across all environments
POST   /api/v1/events/{id}/stop                Stop all VMs across all environments
POST   /api/v1/events/{id}/teardown            Tear down: stop VMs, delete projects, deprovision hosts
POST   /api/v1/events/{id}/extend              Extend run timer / lifetime for all environments

GET    /api/v1/events/{id}/environments        List all environments (status, URLs)
GET    /api/v1/events/{id}/environments/{eid}  Single environment details
PATCH  /api/v1/events/{id}/environments/{eid}  Update label (assign attendee name/email)
POST   /api/v1/events/{id}/environments/{eid}/start   Start one environment
POST   /api/v1/events/{id}/environments/{eid}/stop     Stop one environment
POST   /api/v1/events/{id}/environments/{eid}/reset    Reset to golden state (re-clone)

GET    /api/v1/events/{id}/export-urls         Export CSV: index, label, URL
GET    /api/v1/events/{id}/dashboard           Real-time dashboard data (host load, env states)
```

### Event Dashboard (GUI)

A dedicated view at `/events/[id]` showing:
- Grid of all environments as cards (color-coded by state: green=running, red=stopped, yellow=provisioning, gray=queued)
- Aggregate stats: X/200 running, total CPU/RAM used, host count
- Per-host heatmap showing capacity utilization
- Bulk actions: start all, stop all, teardown, extend timers
- Search/filter by attendee label or status
- Click any environment card to open its canvas view

### Ansible Collection Additions

```
troshka.core.event             Create/manage events
troshka.core.event_info        Get event details + environment list
troshka.core.event_environment Manage individual environments within an event
```

Example playbook — deploy a workshop:
```yaml
- name: Deploy Summit Workshop
  hosts: localhost
  tasks:
    - name: Create event from golden project
      troshka.core.event:
        name: "Summit 2026 — OpenShift Workshop"
        source_project: "OpenShift Demo Lab"
        count: 200
        run_timer_hours: 4
        lifetime_days: 3
        guest_permission: console_only
        host_strategy: auto
        provider: ec2-us-east-1
        state: present
      register: event

    - name: Assign attendees from CSV
      troshka.core.event_environment:
        event: "{{ event.id }}"
        index: "{{ item.index }}"
        label: "{{ item.email }}"
      loop: "{{ lookup('file', 'attendees.csv') | from_csv }}"

    - name: Provision and start all environments
      troshka.core.event:
        id: "{{ event.id }}"
        action: provision
        start_on_provision: true

    - name: Export URLs for distribution
      troshka.core.event_info:
        id: "{{ event.id }}"
        export: urls
        dest: workshop-urls.csv
```

## Ansible Collection: troshka.core

Full Ansible collection wrapping the REST API for infrastructure-as-code management of troshka environments.

### Modules
```
troshka.core.project           Create/update/delete projects
troshka.core.project_info      Get project details
troshka.core.vm                Create/update/delete VMs
troshka.core.vm_power          Start/stop/restart (graceful + force), start_all/stop_all
troshka.core.vm_info           Get VM details + state
troshka.core.network           Create/update/delete networks
troshka.core.network_service   DHCP, DNS, PXE config
troshka.core.security_rule     Inbound/outbound rules
troshka.core.disk              Create/attach/detach disks
troshka.core.template          Save/share/list templates
troshka.core.template_info     Get template details
troshka.core.tunnel            Cross-project tunnels
troshka.core.boot_order        Configure boot order + prereqs
troshka.core.publish           Publish/unpublish project URL
troshka.core.host              Provision/deprovision hosts (admin)
troshka.core.host_info         Get host details (admin/operator)
troshka.core.provider          Manage providers (admin)
troshka.core.user              Manage users (admin)
troshka.core.library_item      Upload/manage library items
troshka.core.library_item_info List/query items across libraries
troshka.core.image             Alias for library_item (backward compat)
troshka.core.image_info        Alias for library_item_info
troshka.core.event             Create/manage events (bulk deployment)
troshka.core.event_info        Get event details + environment list + export URLs
troshka.core.event_environment Manage individual environments within an event
```

### Dynamic Inventory Plugin
```yaml
plugin: troshka.core.troshka
url: https://troshka.example.com
token: "{{ lookup('env', 'TROSHKA_TOKEN') }}"
project: "My Project"
groups:
  databases: "'db' in name"
  webservers: "'web' in name"
```

### Example Roles
- `demo_environment` — build a full demo env from variables

## Deployment

### OpenShift Deployment

Ansible playbook + Jinja2 manifests (same pattern as parsec/labagator).

Resources deployed:
- Deployment: frontend (2 replicas), api-server (3 replicas), celery-worker (2 replicas)
- StatefulSet: PostgreSQL (PVC 50Gi)
- Deployment: Redis
- Routes: frontend (`/`), api-server (`/api`, `/ws`)
- OAuth proxy sidecars on frontend and api-server
- CronJob: timer-check (hourly), db-backup (daily)
- Secrets: db credentials, S3 credentials, AWS credentials, OAuth proxy, agent signing key
- ConfigMap: app config YAML, allowed users

### Deployment Commands
```bash
ansible-playbook ansible/deploy.yml -e env=prod
ansible-playbook ansible/deploy.yml -e env=prod --tags update
ansible-playbook ansible/deploy.yml -e env=prod --tags migrate
```

### Configuration

All config via Dynaconf (YAML + env var overrides). No hardcoded credentials or hostnames in Python code.

```yaml
database:
  url: "postgresql+asyncpg://..."
redis:
  url: "redis://troshka-redis:6379/0"
s3:
  bucket: troshka-images
  region: us-east-1
aws:
  default_region: us-east-1
  default_instance_type: r8i.4xlarge
auth:
  oauth_enabled: true
  allowed_groups: [...]
defaults:
  run_timer_hours: 8
  lifetime_days: 30
  max_vms_per_project: 20
  max_projects_per_user: 10
  user_library_quota_gb: 500
```

## Repo Structure

```
troshka/
+-- src/
|   +-- backend/                 FastAPI API server
|   |   +-- app/
|   |   |   +-- api/             Route handlers
|   |   |   +-- core/            Config, database, auth, websocket
|   |   |   +-- models/          SQLAlchemy ORM
|   |   |   +-- schemas/         Pydantic request/response
|   |   |   +-- services/        Business logic
|   |   |   +-- tasks/           Celery tasks
|   |   +-- alembic/             DB migrations
|   |   +-- tests/
|   |   +-- Containerfile
|   +-- frontend/                Next.js 15 + PatternFly 6
|   |   +-- src/
|   |   +-- e2e/
|   |   +-- Containerfile
|   +-- agent/                   Host agent
|       +-- troshka_agent/
|       +-- systemd/
|       +-- cloud-init/
|       +-- tests/
|       +-- pyproject.toml
+-- ansible/                     OCP deployment
|   +-- deploy.yml
|   +-- vars/
|   +-- templates/
+-- collection/                  Ansible collection: troshka.core
|   +-- galaxy.yml
|   +-- plugins/
|   +-- roles/
|   +-- playbooks/
+-- docs/
+-- .github/workflows/
+-- dev-services.sh              Local dev (PostgreSQL + Redis via Podman)
+-- CLAUDE.md
+-- README.md
```

## CI/CD

GitHub Actions, path-filtered:
- ci-backend.yml: ruff, mypy, pytest (src/backend/**)
- ci-frontend.yml: next build, playwright e2e (src/frontend/**)
- ci-agent.yml: ruff, pytest (src/agent/**)

Image builds via OCP BuildConfig + webhook (same as labagator).

## AWS Nested Virtualization Reference

Supported instance types: C8i, M8i, R8i
Supported L1 hypervisors: KVM, Hyper-V
Enable via: `--cpu-options "NestedVirtualization=enabled"`
No additional cost.
Host OS: RHEL or compatible derivative.

## Scale Target

500+ concurrent users, many hundreds of projects, auto-scaling host pool.
Horizontal scaling: multiple API server replicas behind OCP Route, multiple Celery workers, agents scale with hosts.
