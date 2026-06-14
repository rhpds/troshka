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
- **Virtual BMC** — IPMI and Redfish endpoints per VM for bare-metal simulation (PXE + BMC workflows)
- **Shared storage & live migration** — NFS/FSx storage pools with zero-downtime VM migration between hosts
- **Storage auto-extend** — Automatic EBS/FSx capacity expansion with threshold monitoring and admin controls
- **DNS integration** — Optional Route53 DNS provider for automated record management per project
- **Host garbage collector** — Auto-sync capacity, clean orphaned VMs/disks/bridges, repair networks, evict stale cache
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
  |                               +-- S3 (disk images, patterns)
  |                               +-- troshkad agents (EC2 hosts)
  |                               +-- Kubernetes API --> OCP Virt
  |
  +-- WSS (console) --> API Server --> troshkad --> libvirt VNC
```

| Component | Tech | Purpose |
|-----------|------|---------|
| API Server | FastAPI + Uvicorn | REST API, WebSocket hub, RBAC |
| Frontend | Next.js 15 + PatternFly 6 + React Flow | Canvas editor, project management |
| Database | PostgreSQL 16 | Projects, topology, users, hosts |
| troshkad | Python daemon + libvirt | Host agent — VM lifecycle, storage, networking |
| S3 | AWS S3 | Disk images, patterns, snapshots |
| Deployment | Ansible + Jinja2 manifests | OpenShift deployment |

## Quick Start

### Prerequisites

- Python 3.11+
- Node.js 18+
- Podman (for PostgreSQL in dev)

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
./dev-services.sh db start         # PostgreSQL on port 5433
```

Open http://localhost:3100 — dev mode auto-authenticates as admin.

### Services

| Service | URL | Notes |
|---------|-----|-------|
| Frontend | http://localhost:3100 | Next.js dev server (hot-reloads) |
| Backend API | http://localhost:8200 | FastAPI (restart required for changes) |
| API Docs | http://localhost:8200/docs | Swagger UI |
| PostgreSQL | localhost:5433 | Podman container |

### Utility Scripts

```bash
./scripts/host-ssh.sh              # SSH into first connected host
./scripts/host-ssh.sh -- <cmd>     # Run command on host
./scripts/host-db.sh               # Interactive Python shell with DB
./scripts/host-db.sh "<code>"      # Run inline DB query
```

### Run Tests

```bash
cd src/backend
./venv/bin/python3 -m pytest tests/ -v
```

Tests use SQLite with type compiler overrides for JSONB/UUID.

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
|   |   |   +-- services/     # Business logic (deploy, GC, patterns, S3, migration)
|   |   +-- config/           # Dynaconf YAML config
|   |   +-- alembic/          # Database migrations
|   |   +-- tests/            # pytest
|   +-- frontend/             # Next.js 15 + PatternFly 6
|   |   +-- src/
|   |   |   +-- app/          # Pages (App Router)
|   |   |   +-- components/   # Canvas editor, nodes, modals
|   |   |   +-- stores/       # Zustand state
|   +-- troshkad/             # Host agent daemon (single-file, stdlib only)
|   +-- agent/                # Agent installer and deployer
+-- ansible/                  # OCP deployment
+-- collection/               # Ansible collection for IaC
+-- infra/                    # IAM policies, infrastructure config
+-- scripts/                  # Dev utilities (host-ssh.sh, host-db.sh)
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

- [x] Backend foundation (FastAPI, auth, models, CRUD APIs)
- [x] Frontend + Canvas editor (Next.js, PatternFly, React Flow)
- [x] Host agent — troshkad daemon (libvirt, HTTPS, job system)
- [x] Console & power management (noVNC proxy, virtual keyboard)
- [x] Library system (S3 image registry, upload/import)
- [x] Patterns & VM snapshots (capture, deploy, bulk deploy, drag-import)
- [x] Host garbage collector (capacity sync, orphan cleanup, network repair, cache eviction)
- [x] Static IP reservations (MAC→IP, CIDR validation, conflict detection)
- [x] Cloud-init (unique instance-id, YAML validation, packages, custom user-data)
- [x] PXE network boot (managed + BYO, BIOS/UEFI, Secure Boot)
- [x] Virtual BMC (IPMI + Redfish per VM, sushy-emulator, virtualbmc)
- [x] Shared storage pools (FSx OpenZFS, BYO NFS, local mode)
- [x] Live migration (shared storage, mutual TLS, pool-level PKI)
- [x] External IPs (secondary ENI IPs, nftables DNAT/SNAT, per-project chains)
- [x] Storage monitoring (partition thresholds, warning badges, health poller)
- [x] Storage auto-extend (EBS + FSx, threshold-based, admin override)
- [x] DNS providers (Route53 integration, per-project DNS records)
- [x] Libvirt events (lifecycle callbacks, block threshold alerts, batch state polling)
- [x] OCP topology templates (version dropdown, deploy estimates, auto-sizing)
- [x] Parallel VM deployment (concurrent disk creation, definition, start)
- [ ] OCP deployment & Ansible collection

## License

TBD
