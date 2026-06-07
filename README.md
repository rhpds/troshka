# Troshka

<p align="center">
  <img src="src/frontend/public/images/troshka-logo-dark-200.png" alt="Troshka" height="120" />
</p>

<p align="center">
  <strong>Nested VM Environment Builder</strong><br/>
  Design, deploy, and share multi-VM environments on AWS EC2 and OCP Virtualization
</p>

---

## What is Troshka?

Troshka is a self-service web platform for building nested VM environments. Users design multi-VM topologies through a drag-and-drop visual editor, manage VM lifecycle, and share environments as time-limited demo labs with public URLs.

The name "troshka" evokes nesting — VMs inside VMs inside cloud infrastructure, like a matryoshka doll.

### Key Features

- **Visual topology editor** — Drag-and-drop VMs, networks, routers, gateways, and storage onto a Visio-like canvas
- **Deep VM configuration** — NICs with model selection and static IP reservation, disk controllers, boot device order, cloud-init, OS types matching QEMU/libvirt
- **Network services** — DHCP with static IP reservations (MAC→IP), DNS, PXE boot (legacy, iPXE, UEFI HTTP), security rules per network
- **Routing & NAT** — L3 routers between subnets, NAT gateway with port forwarding and multiple external IPs
- **Project sharing** — Publish environments as time-limited demo labs with guest console access
- **Patterns** — Save entire projects as reusable patterns, stamp out hundreds of identical environments for labs and demos
- **VM snapshots** — Capture individual VMs (config + disks) to the library, drag-and-drop into any project with auto-connected networks
- **Bulk deployment** — Deploy 1-500 projects from a pattern with naming templates
- **Host garbage collector** — Auto-sync capacity counters, clean orphaned VMs/disks/bridges, repair missing networks, evict stale cache (configurable per type)
- **API-first** — Full REST API with API key authentication, plus an Ansible collection for IaC
- **Multi-provider** — Deploy to AWS EC2 (nested virtualization) or OCP Virtualization (KubeVirt)

## Architecture

```
Browser
  |
  +-- Next.js Frontend (PatternFly 6 + React Flow)
  |       |
  |       +-- /api/* proxy --> FastAPI API Server
  |                               |
  |                               +-- PostgreSQL (state)
  |                               +-- Redis (queue + cache)
  |                               +-- WebSocket --> Host Agents (EC2)
  |                               +-- Kubernetes API --> OCP Virt
  |
  +-- WSS (console) --> API Server --> Agent --> libvirt VNC/SPICE
```

| Component | Tech | Purpose |
|-----------|------|---------|
| API Server | FastAPI + Uvicorn | REST API, WebSocket hub, RBAC |
| Frontend | Next.js 15 + PatternFly 6 + React Flow | Canvas editor, project management |
| Database | PostgreSQL 16 (SQLite for dev) | Projects, topology, users |
| Host Agent | Python + libvirt | VM lifecycle, console proxy |
| Deployment | Ansible + Jinja2 manifests | OpenShift deployment |

## Quick Start

### Prerequisites

- Python 3.11+
- Node.js 18+
- Podman (for PostgreSQL, optional — SQLite used by default in dev)

### Start Development

```bash
# Clone the repo
git clone <repo-url> troshka
cd troshka

# Start everything (PostgreSQL + backend + frontend)
./dev-services.sh start

# Or start components individually
./dev-services.sh backend start    # FastAPI on port 8200
./dev-services.sh frontend start   # Next.js on port 3100
./dev-services.sh db start         # PostgreSQL on port 5432
```

Open http://localhost:3100 — dev mode auto-authenticates as admin.

### Services

| Service | URL | Notes |
|---------|-----|-------|
| Frontend | http://localhost:3100 | Next.js dev server |
| Backend API | http://localhost:8200 | FastAPI |
| API Docs | http://localhost:8200/docs | Swagger UI |
| PostgreSQL | localhost:5432 | Podman container (optional) |

### Run Tests

```bash
cd src/backend
source venv/bin/activate
python3 -m pytest tests/ -v
```

## Configuration

All config via Dynaconf (YAML + environment variable overrides). No hardcoded credentials.

```bash
# Base config
src/backend/config/config.yaml

# Local overrides (gitignored)
src/backend/config/config.local.yaml

# Environment variable override
TROSHKA_AUTH__JWT_SECRET=my-secret
```

See `src/backend/config/config.local.yaml.example` for local dev setup.

## Authentication

| Mode | How | When |
|------|-----|------|
| Dev mode | Auto-authenticates as `local-dev` admin | `oauth_enabled: false` |
| SSO | OAuth proxy (ose-oauth-proxy) + OpenShift groups | OCP deployment |
| API keys | `Authorization: Bearer trk_...` | External tools, scripts, Ansible |

Create API keys in the UI: Settings > API Keys.

## Project Structure

```
troshka/
+-- src/
|   +-- backend/              # FastAPI API server
|   |   +-- app/
|   |   |   +-- api/          # Route handlers
|   |   |   +-- core/         # Config, database, auth
|   |   |   +-- models/       # SQLAlchemy ORM
|   |   |   +-- schemas/      # Pydantic request/response
|   |   |   +-- services/     # Business logic (deploy, GC, patterns, S3)
|   |   +-- config/           # Dynaconf YAML config
|   |   +-- alembic/          # Database migrations
|   |   +-- tests/            # pytest
|   +-- frontend/             # Next.js 15 + PatternFly 6
|   |   +-- src/
|   |   |   +-- app/          # Pages (App Router)
|   |   |   +-- components/   # Canvas editor, nodes
|   |   |   +-- stores/       # Zustand state
|   +-- agent/                # Host agent (Phase 5)
+-- ansible/                  # OCP deployment (Phase 8)
+-- collection/               # Ansible collection (Phase 8)
+-- docs/                     # Design specs and plans
+-- dev-services.sh           # Local dev orchestration
```

## Canvas Editor

The drag-and-drop topology editor supports:

| Node | Handles | Description |
|------|---------|-------------|
| VM | Blue (top/bottom) + Yellow (left/right) | Virtual machine with NICs and disk controllers |
| Network | Blue (top/bottom) + Orange (left/right) | Virtual bridge with DHCP, DNS, PXE |
| Router | Orange (all sides) | L3 forwarding between subnets |
| Gateway | Orange (left/right) | NAT with optional port forwarding |
| Storage | Yellow (left/right) | Disk (qcow2/raw) or ISO image |

Connection rules are enforced visually — blue handles connect to networks, yellow to storage, orange to routers/gateways.

## AWS Nested Virtualization

Troshka uses [EC2 nested virtualization](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/amazon-ec2-nested-virtualization.html):

- **Supported instances**: C8i, M8i, R8i
- **Supported hypervisors**: KVM, Hyper-V
- **Enable**: `--cpu-options "NestedVirtualization=enabled"`
- **No additional cost**
- **Host OS**: RHEL or compatible (Alma, Rocky, CentOS Stream)

## Roadmap

- [x] Phase 1: Backend foundation (FastAPI, auth, models, CRUD APIs)
- [x] Phase 2: Backend API sync (topology persistence)
- [x] Phase 3: Frontend foundation (Next.js, PatternFly, auth)
- [x] Phase 4: Canvas editor (React Flow, drag-and-drop, properties)
- [x] Phase 5: Host agent (libvirt, VM lifecycle, deploy, reconfigure)
- [x] Phase 6: Console & power management (noVNC/SPICE proxy)
- [x] Phase 7: Library system (S3 image registry, templates, upload/import)
- [x] Patterns & VM snapshots (capture, deploy, bulk deploy, drag-import)
- [x] Host garbage collector (capacity sync, orphan cleanup, network repair, cache eviction)
- [x] Static IP reservations (NIC IP → dnsmasq dhcp-host, CIDR validation, conflict detection)
- [x] Cloud-init improvements (unique instance-id per deploy, YAML validation, new chpasswd format)
- [ ] Phase 8: Deployment & Ansible collection (OCP manifests, cron GC)

## License

TBD
