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

## Why Troshka?

Today, every lab environment is built from scratch: provision cloud VMs, install an OS, deploy OpenShift, install operators, configure the lab. It works, but it's slow (45-90 minutes per environment), fragile (any step in the chain can fail), and expensive (each deploy burns compute time reproducing identical work).

Troshka changes this with a **build once, deploy many** model:

1. A lab author creates the environment once — either manually through the visual editor or via automation
2. The finished environment is captured as a **pattern** — a snapshot of the entire topology with all VMs, disks, networks, and configuration
3. When a student orders the lab, Troshka deploys the pattern by restoring the snapshot onto nested VMs

The result:

| | Traditional (agnosticd + AWS) | Troshka |
|---|---|---|
| **Deploy time** | 45-90 minutes | 5-10 minutes |
| **Failure surface** | AMIs, CloudFormation, repos, OCP installer, operators | Single API call to restore a tested snapshot |
| **Consistency** | Drift between deploys (package versions, timing issues) | Bit-for-bit identical every time |
| **Iteration speed** | Change → wait 60 min → test | Change → capture → deploy in minutes |
| **Cost per environment** | Dedicated cloud instances per student | Multiple environments share a single host |
| **Infrastructure overhead** | SSL certs, DNS zones, cloud networking per environment | All internal — no certs, no DNS, no cloud networking |
| **Cluster access** | Shared or limited access, managed credentials | Each student gets full admin over their own cluster |

### Why it's cheaper

Traditional labs provision dedicated cloud instances for every student — a 3-node OCP cluster means 3+ EC2 instances per person. Troshka runs lab environments as nested VMs on EC2 instances with nested virtualization enabled. A single large EC2 instance can host multiple complete OCP clusters side by side, each fully isolated with its own networking. Disk images are thin-provisioned and shared across deploys via copy-on-write, so 50 identical OCP labs don't need 50 copies of the base image.

No SSL certificates to provision. No DNS zones to manage. No cloud security groups or load balancers per environment. The nested VMs use internal `.local` domains and private networking — students access everything through the web portal and Showroom.

### Elastic capacity

Troshka hosts are regular EC2 instances with an agent installed. Spinning up a new host takes minutes — provision the instance, run the agent installer, and it connects to Troshka automatically. When demand drops, hosts can be torn down just as quickly. No long-lived infrastructure to babysit. Scale up for an event with 500 students, scale back down when it's over.

Storage scales the same way. Shared storage pools (FSx or NFS) auto-extend when capacity runs low and can be torn down when no longer needed. EBS volumes on hosts grow automatically based on usage thresholds. No manual capacity planning, no pre-provisioned disks sitting idle.

Shared storage pools also enable live migration of running environments between hosts, so you can drain a host before terminating it without disrupting students.

### Cost efficiency

- **No cross-AZ data transfer fees.** OCP nodes in a Troshka environment are nested VMs on the same host — all inter-node traffic is local. Traditional OCP labs spread nodes across availability zones, racking up data transfer charges for etcd replication, image pulls, and pod-to-pod communication.
- **Cheap pattern storage.** Patterns are stored in S3. Data transfer into S3 is free — you only pay for cold storage of the disk images. A typical OCP lab pattern is 30-50 GB, costing pennies per month to keep on the shelf.
- **No EIPs for clusters.** OCP clusters run on internal networks — no Elastic IPs to allocate, associate, or pay for per student. The only EIPs are on the Troshka hosts themselves.
- **No Route53 costs per environment.** No hosted zones per sandbox, no per-domain fees, no DNS record churn. OCP clusters use internal `.local` domains. Route53 is only used for the Troshka infrastructure itself, which rarely changes. No more throttling from thousands of DNS record updates during large events.
- **No idle infrastructure costs.** Hosts spin up and down with demand. When no labs are running, the only cost is S3 storage for patterns.

No more failed destroys leaving orphaned cloud resources. A Troshka project is a single entity — deleting it cleans up everything: VMs, disks, networks, tokens. There are no CloudFormation stacks to get stuck, no dangling EIPs to leak, no security groups left behind. If something does go sideways, the host garbage collector catches it on the next pass.

### What stays the same

Troshka plugs into the existing ecosystem — it's a new cloud provider, not a replacement:

- **Agnosticv** catalog items work the same way. Set `cloud_provider: troshka` instead of `ec2`.
- **Babylon / RHPDS** ordering, lifecycle, and user experience are unchanged.
- **AAP2** runs the same agnosticd playbooks. The Troshka Ansible collection handles the API calls.
- **Showroom** lab guides work as-is. Showroom is baked into the pattern image.
- **Workloads** can still overlay dynamic configuration on top of a base pattern when needed.

### What's different

- **Students get their own cluster.** Each student is a full admin of their own OCP cluster — no shared tenancy, no namespace restrictions, no worrying about stepping on each other.
- **No laptop issues.** Everything runs in the browser over standard HTTPS — no SSH clients, no VPN, no local tools to install. Students just need a web browser. The entire experience from lab guide to terminal to VM console is curated and consistent regardless of what OS or machine the student is using.
- **Patterns are portable.** A pattern built on one Troshka instance can be deployed on any other. The topology, disks, and configuration travel together.

### Security

Traditional lab environments hand students cloud credentials — AWS keys on the bastion, cloud provider secrets inside the OCP cluster, IAM roles that can provision resources. This is a constant source of risk: accidental resource sprawl, credential exfiltration, lateral movement between environments.

Troshka eliminates this entire class of problems:

- **No cloud credentials exist in the environment.** There are no AWS keys on the bastion, no cloud provider secrets in the OCP cluster, no IAM roles attached to the VMs. The nested VMs have no awareness of the underlying cloud — they're just VMs on a hypervisor.
- **The environment is static.** Students can't provision additional resources even with full cluster admin. There's no cloud API to call, no capacity to request. What's in the pattern is what you get.
- **Network isolation is physical, not policy-based.** Each project runs in its own network namespace with VXLAN isolation. There's no way to reach another student's environment or the host network — it's not a firewall rule that can be misconfigured, it's a separate network stack.
- **No SSH access to the host.** Students interact through VNC console and Showroom over HTTPS. There's no path from the lab environment to the underlying EC2 instance.
- **Access is token-scoped.** Student portal tokens are tied to a single project with configurable access levels (read-only, power, console). Deleting the project invalidates the token.
- **No discoverable endpoints.** Today, anyone who guesses or scans for a lab URL can access a student's environment. With Troshka, access requires a cryptographic token — there's nothing to stumble across or brute-force.
- **Controlled outbound access.** Outbound internet access from lab environments can be turned off entirely or restricted to specific ports. OCP templates only allow the ports needed for cluster operation (443, 80, 53, 123) — no general internet access. No more students using lab environments as jump boxes or downloading unauthorized software.
- **No external dependencies at deploy time.** Once a pattern is captured, it has everything baked in — OS packages, container images, operator bundles, OCP itself. No pulling from Quay, no Satellite subscription, no CDN access, no package mirrors. The environment doesn't need the internet to function. External services can go down without affecting running or new lab deployments.
- **No telemetry or registration.** OCP clusters deployed from patterns don't phone home or register in OpenShift Cluster Manager. A pull secret is needed once to build the pattern, but every subsequent deploy from that pattern is invisible — no subscription tracking, no cluster count inflation.

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

### OCP Virt Provider

For setting up Troshka on OpenShift Virtualization (KubeVirt), see [docs/ocp_virt_setup.md](docs/ocp_virt_setup.md).

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

- [ ] OCP deployment automation
- [ ] OCP template auto-set gateway to restrict mode
- [ ] Template mode (mode 3) end-to-end testing
- [ ] Showroom integration testing with Troshka console
- [ ] Multi-instance pattern portability

## License

TBD
