# Troshka Documentation Suite — Design Spec

**Date:** 2026-06-16
**Audience:** Contributors, operators/admins, end users
**Location:** `docs/` directory, linked from `README.md`

## Deliverables

```
docs/
  architecture.md       # System design deep-dive (all audiences)
  api-guide.md          # Workflow walkthroughs + full 164-endpoint reference
  install-common.md     # Shared setup: backend, frontend, PostgreSQL, config
  install-aws.md        # AWS provider setup (self-contained)
  install-gcp.md        # GCP provider setup (self-contained)
  install-azure.md      # Azure provider setup (self-contained)
  install-ocpvirt.md    # OCP Virt provider setup (self-contained)
README.md               # Updated with project overview + doc links
```

---

## 1. Architecture Guide (`architecture.md`)

Target: all audiences. Explains how the system works end-to-end.

### Sections

1. **System Overview**
   - What Troshka is: nested VM environment builder for lab/demo infrastructure
   - Three-tier model: Next.js frontend → FastAPI backend → troshkad host agents
   - ASCII diagram showing the data flow between tiers
   - Tech stack summary: Python 3.11, FastAPI, SQLAlchemy 2, Alembic, Dynaconf, Next.js 15, PatternFly 6, React Flow, Zustand, PostgreSQL 16

2. **Component Architecture**
   - Backend: FastAPI app structure (`app/api/`, `app/models/`, `app/services/`), 16 API route modules, 164 endpoints
   - Frontend: App Router pages, PatternFly layout, React Flow canvas, Zustand store
   - Database: PostgreSQL with JSONB topology storage, Alembic migrations
   - S3: library storage, pattern/snapshot exports, multipart upload

3. **Provider Abstraction**
   - `ProviderDriver` interface: 17 methods (provision, terminate, resize, console, EIP, etc.)
   - `get_provider_driver(provider)` dispatcher pattern
   - Per-cloud driver files: `ec2.py`, `gcp.py`, `azure.py`, `ocpvirt.py`
   - Comparison table: nested virt mechanism, EIP support, shared storage, console TLS, image format, SSH user, data disk path

4. **Host Agent — troshkad**
   - Design: single-file Python daemon (~4000 lines), stdlib only, no pip dependencies
   - Communication: HTTPS on port 31337, bearer token auth, cert fingerprint pinning
   - Job system: `_create_job()` → worker thread → `_complete_job()` / poll via `GET /jobs/{id}`
   - Handler categories with counts:
     - VM lifecycle (create, destroy, start, stop, reboot, state, list, config, reconfigure, undefine)
     - Disk & storage (disk create, disk resize, seed create, image cache, resize storage)
     - Networking (network setup/teardown, full setup/teardown, bridge list, eip configure, nft reset, LB setup/teardown)
     - Patterns & snapshots (snapshot create/capture, pattern capture direct, pattern export, NBD export/stop/pull-flatten, upload-and-cache)
     - BMC (setup, create bridge, teardown, status)
     - Migration (vm migrate, TLS update certs)
     - Exec (serial exec, SSH exec, file push)
     - GC (discover, clean)
     - PXE (pxe setup)
     - Admin (health endpoint, metadata deploy, files remove/stat)
   - Version stamping: content hash at push time
   - Update mechanism: `update-agent.sh` pushes via API, `reinstall-agent.sh` for broken agents

5. **VNC Console — troshka-vncd**
   - Architecture: Browser → `wss://{instance_id}.{base_domain}/ws/{jwt}` → troshka-vncd → localhost VNC
   - JWT: short-lived (5 min, single-use), signed with host's agent token
   - TLS: Let's Encrypt via certbot DNS-01 challenge
   - Per-provider DNS: Route53 (AWS), Cloud DNS (GCP), Azure DNS, OCP Routes
   - noVNC client with virtual keyboard popup

6. **Networking Model**
   - VXLAN overlays with globally unique VNIs (monotonic, never recycled)
   - Network namespaces: one per project for same-CIDR isolation
   - nftables chains: per-project NAT and port forwarding
   - Gateway node: outbound NAT, optional external access (EIPs)
   - EIPs: secondary private IPs on host ENI/NIC, DNAT via nftables

7. **Storage Architecture**
   - Three modes: `local` (default), `shared-fsx` (AWS FSx OpenZFS), `shared-azure-files`, `shared-byo` (user NFS)
   - Storage pools: group hosts sharing NFS, enable live migration
   - Path resolution: `_storage_path()` routes to `/var/lib/troshka/shared/` or `/var/lib/troshka/local/`
   - Download coordination: `SharedCacheEntry` table — one download serves all hosts in pool
   - Auto-extend: EBS volumes and FSx file systems, configurable threshold/increment/max

8. **Deploy Pipeline**
   - Flow: topology JSONB → placement (auto-select pool, least-loaded host) → provision host (if needed) → parallel per-VM: download images → create disks → cloud-init seed → define VM → setup networks → start VMs in order
   - Progress tracking: byte-level download progress, per-VM status, WebSocket updates
   - Reconfigure: re-deploys changed VMs without full teardown
   - Redeploy: single VM or full project re-creation

9. **Patterns & Snapshots**
   - Pattern save: state machine (creating → capturing → available / error)
   - Capture flow: stop VMs → flatten qcow2 (merge backing chains) → upload to S3
   - Pattern buffer: dedicated storage worker per pool, NBD-based captures from shared storage
   - Deploy from pattern: download → extract → create VMs with remapped IDs
   - Topology remapping: all IDs, MACs, boot devices, start order, EIPs, hidden nodes

10. **Library System**
    - Personal libraries (`type="personal"`)
    - S3 multipart upload (start → part URLs → complete)
    - Import from URL with progress tracking
    - Sharing: per-user grants by email
    - ISO management for PXE boot and cloud-init

11. **Live Migration**
    - Prerequisite: hosts in same storage pool (shared NFS)
    - PKI: pool-level CA (10-year), host certs with public+private IP SANs (1-year, re-signed hourly)
    - Libvirt mutual TLS with pool CA verification
    - Migration flow: setup networks/BMC on target → `virsh migrate --persistent --undefinesource` (--live for running VMs) → teardown source
    - Host evacuation: moves all projects to other hosts in pool

12. **Virtual BMC**
    - Per-VM: sushy-emulator (Redfish) + vbmc (IPMI)
    - BMC bridge: `br-bmc-{project_id[:8]}` inside project namespace
    - Config at `/var/lib/troshka/bmc/{project_id}/`
    - Network type: `networkType: "bmc"` auto-created on first BMC-enabled VM

13. **Health & Garbage Collection**
    - Health poller: periodic checks on connected hosts — partition monitoring, cert renewal (hourly re-sign, CA renewal at 90 days), storage warnings
    - GC pipeline: capacity sync → orphan cleanup → network repair → cache eviction → S3 orphan cleanup → SharedCacheEntry cleanup
    - Dry-run mode for preview
    - Wipe: full host reset preserving image cache

14. **Authentication & Authorization**
    - OIDC via RHSSO (production), dev-mode auto-auth (development)
    - API keys: user-scoped, bearer token auth
    - Roles: admin (host/provider management), user (projects/library)
    - Portal tokens: short-lived, scoped to single project, limited VM actions
    - Encrypted secrets: Fernet encryption for OCP pull secret, RH offline token

15. **Red Hat Image Builder**
    - Flow: offline token → exchange for access token → submit compose → poll → download/register
    - Per-cloud image formats: AMI (AWS), shared GCE image (GCP), managed image (Azure)
    - Auto-sets `default_image` on provider after successful build

---

## 2. API Guide (`api-guide.md`)

### Part 1 — Workflow Walkthroughs

Organized by task, covering the key endpoints with curl examples and expected responses.

1. **Getting Started**
   - Base URL: `http://localhost:8200/api/v1`
   - Authentication: dev token (`GET /auth/dev-token`), OIDC flow, API keys (`POST /api-keys`)
   - Common patterns: async operations (202 → poll progress), WebSocket subscriptions, error format

2. **Setting Up Infrastructure** (admin)
   - Create provider: `POST /providers` with per-cloud credentials format
   - Test connection: `POST /providers/{id}/test`
   - Setup network: `POST /providers/{id}/create-vpc` (AWS) / `create-network-gcp` / `create-network-azure` / `setup-infra` (OCP Virt)
   - Setup console: `POST /providers/{id}/setup-console` + DNS delegation
   - Create S3 bucket: `POST /providers/{id}/create-bucket`
   - Set host image: `POST /providers/{id}/set-image`
   - Provision host: `POST /hosts` with instance type and storage size

3. **Managing Your Library**
   - List items: `GET /library`
   - Upload ISO (multipart): `POST /library` → `POST /{id}/upload-start` → `POST /{id}/upload-part-url` (repeat) → `POST /{id}/upload-complete`
   - Import from URL: `POST /{id}/import-url`
   - Share: `POST /{id}/share`

4. **Building & Deploying Projects**
   - Create project: `POST /projects`
   - Update topology: `PATCH /projects/{id}` with topology JSONB
   - Deploy: `POST /projects/{id}/deploy`
   - Monitor progress: `GET /projects/{id}/deploy-progress` or WebSocket `ws://host/api/v1/projects/{id}/ws`
   - From template: `POST /projects/from-template`

5. **Working with VMs**
   - Power operations: `POST /projects/{id}/vms/{vm_id}/start|stop|restart|forcestop`
   - Batch state: `GET /projects/{id}/vm-states`
   - Console: `GET /projects/{id}/vms/{vm_id}/console` → returns JWT + WebSocket URL
   - Exec: `POST /projects/{id}/vms/{vm_id}/exec` (serial or SSH)
   - Files: `PUT /projects/{id}/vms/{vm_id}/files` (push), `GET .../files` (pull)
   - Snapshots: `POST /projects/{id}/vms/{vm_id}/snapshot`

6. **Patterns**
   - Save: `POST /patterns` (from deployed project)
   - Monitor save: `GET /patterns/{id}/progress` or WebSocket
   - Deploy: `POST /patterns/{id}/deploy`
   - Bulk deploy: `POST /patterns/{id}/bulk-deploy`
   - Share: `POST /patterns/{id}/share`

7. **Networking & External Access**
   - Networks: CRUD under `/projects/{id}/networks`
   - EIPs: `GET /projects/{id}/eips`, `DELETE .../eips/{canvas_eip_id}`
   - DNS providers: CRUD under `/dns-providers`

8. **Administration**
   - Hosts: provision, resize, extend storage, update agent, evacuate, wipe, GC
   - Storage pools: create, extend (FSx/Azure Files), pattern buffer lifecycle, cache management
   - Providers: VPC setup, console setup, image builder
   - Users: admin UI management

9. **Portal Access**
   - Generate token: `POST /projects/{id}/portal-token`
   - Portal endpoints: `GET /portal/{token}`, `GET .../vm-states`, `POST .../vms/{vm_id}/{action}`

### Part 2 — Full Endpoint Reference

All 164 endpoints organized by resource module. Each entry includes:

```
### METHOD /path

Description (one line)

**Auth:** admin | user | portal | dev-only
**Request body:** key fields (not full Pydantic schema)
**Response:** summary of returned data
**Curl example:** (for non-obvious endpoints)
```

Resource sections (in order):
- Auth (13 endpoints)
- Providers (26 endpoints)
- Hosts (22 endpoints)
- Storage Pools (14 endpoints)
- Projects (29 endpoints)
- VMs (6 endpoints)
- Disks (7 endpoints)
- Networks (5 endpoints)
- EIPs (4 endpoints)
- Library (13 endpoints)
- Patterns (10 endpoints)
- DNS Providers (5 endpoints)
- API Keys (3 endpoints)
- Portal (4 endpoints)
- Templates (1 endpoint)
- WebSocket (2 endpoints)

---

## 3. Install Guides

### Common Setup (`install-common.md`)

1. **Prerequisites** — Python 3.11+, Node.js 20+, PostgreSQL 16, podman or docker, git
2. **Clone & Backend Setup** — clone repo, create virtualenv, pip install requirements
3. **Configuration** — `config.yaml` structure, `config.local.yaml` for overrides, `TROSHKA_*` env var pattern (Dynaconf)
4. **Database Setup** — run PostgreSQL container (podman), create database, run Alembic migrations
5. **Frontend Setup** — `npm install`, dev environment variables
6. **Development Mode** — `dev-services.sh` usage, ports (backend 8200, frontend 3100, PostgreSQL 5433), auto-auth
7. **Production Deployment** — systemd service files, reverse proxy configuration, OIDC setup, TLS termination
8. **S3 Storage** — bucket setup for library/pattern/snapshot storage
9. **Troubleshooting** — common issues (port conflicts, migration errors, auth problems)

### Per-Cloud Install Guides

Each follows the same template — self-contained, cross-references `install-common.md` for shared setup.

#### AWS (`install-aws.md`)
1. Prerequisites: AWS account, IAM admin access
2. IAM setup: create `troshka` user, attach `troshka-policy` (full policy JSON from `infra/iam-policy.json`)
3. Create provider in Troshka: credentials format (`access_key_id`, `secret_access_key`, `region`)
4. VPC setup: "Setup VPC" via UI or `POST /providers/{id}/create-vpc` — creates VPC, subnets (all AZs), IGW, route tables, security groups, S3 gateway endpoint
5. S3 bucket: `POST /providers/{id}/create-bucket` creates `troshka-images`
6. Host image: marketplace RHEL or Image Builder custom AMI
7. Console setup: Route53 hosted zone, IAM instance profile for certbot, NS delegation
8. Provision first host: recommended `m5.metal` or `c5.metal`, EBS storage sizing
9. Shared storage (optional): FSx OpenZFS pool — create pool, provision hosts, auto-mount
10. Verify: end-to-end checklist

#### GCP (`install-gcp.md`)
1. Prerequisites: GCP project under org folder, Compute Engine + Cloud DNS APIs enabled
2. Service account: create SA, grant Compute Admin + DNS Admin roles, download JSON key
3. Org policy: check for `custom.denyCostlyMachineTypes` — E2 and N2-standard allowed, N2-highmem may need exception
4. Create provider: credentials format (`service_account_json` containing full SA key)
5. Network setup: `POST /providers/{id}/create-network-gcp` — custom-mode VPC, subnet, firewall rules with `troshka-host` tag
6. Host image: PAYG from `rhel-cloud` (default) or Image Builder BYOS
7. Console setup: Cloud DNS zone, `certbot-dns-google` plugin, NS delegation
8. Provision first host: recommended `n2-highmem-16` or larger, SSH user is `troshka`
9. Shared storage: not yet supported — use local mode with pattern buffer
10. Verify checklist

#### Azure (`install-azure.md`)
1. Prerequisites: Azure subscription, resource group
2. Service principal: create SP, assign Contributor on RG, note tenant/client/secret/subscription IDs
3. Marketplace terms: accept RHEL BYOS offer terms (one-time)
4. Create provider: credentials format (`tenant_id`, `client_id`, `client_secret`, `subscription_id`)
5. Network setup: `POST /providers/{id}/create-network-azure` — VNet, subnet, NSG with rules
6. Host image: BYOS from `redhat` publisher or Image Builder managed image
7. Console setup: Azure DNS zone, `certbot-dns-azure` plugin, NS delegation
8. Image Builder one-time setup: grant Contributor to Image Builder SP on resource group
9. Provision first host: recommended `Standard_E32s_v5`, SSH user is `troshka`
10. Shared storage (optional): Azure Files NFS Premium v2 — create pool, private endpoint auto-created
11. Verify checklist

#### OCP Virt (`install-ocpvirt.md`)
1. Prerequisites: OpenShift 4.x with OpenShift Virtualization, nested virt capable nodes
2. RBAC setup: apply `infra/ocpvirt-rbac.yaml` — creates namespace, SA, ClusterRole, binding
3. Generate token: `oc create token troshka -n troshka --duration=8760h`
4. Create provider: credentials format (`api_url`, `token`, `namespace`)
5. Infrastructure setup: `POST /providers/{id}/setup-infra` — verifies connectivity and storage classes
6. Storage: Ceph-NFS via `ocs-storagecluster-ceph-nfs` storage class
7. Host image: RHEL QCOW2 DataVolume (no Image Builder support yet for OCP Virt)
8. Console setup: OCP Routes (edge TLS), vncd runs with `--no-tls`
9. Provision first host: VM sizing, CPU/memory requests
10. Differences from cloud providers: no EIPs, no resize, OCP Routes instead of DNS
11. Verify checklist

---

## 4. README.md Update

Add to the existing README (or create if minimal):

```markdown
## Documentation

- **[Architecture Guide](docs/architecture.md)** — system design, provider abstraction, networking, storage, agent internals
- **[API Guide](docs/api-guide.md)** — workflow walkthroughs with examples + full 164-endpoint reference

### Installation

Start with [Common Setup](docs/install-common.md), then follow the guide for your cloud:

| Cloud | Guide | Shared Storage | Console TLS | EIPs |
|-------|-------|---------------|-------------|------|
| AWS | [install-aws.md](docs/install-aws.md) | FSx OpenZFS | Route53 + certbot | Yes |
| GCP | [install-gcp.md](docs/install-gcp.md) | Not yet | Cloud DNS + certbot | Yes |
| Azure | [install-azure.md](docs/install-azure.md) | Azure Files NFS | Azure DNS + certbot | Yes |
| OCP Virt | [install-ocpvirt.md](docs/install-ocpvirt.md) | Ceph-NFS | OCP Routes | No |
```

Quick-start section for developers pointing to `dev-services.sh`.

---

## Implementation Notes

- All endpoint documentation will be derived from reading the actual route handlers — no guessing
- Curl examples will use `localhost:8200` and dev-mode auth token
- Cross-references between docs use relative markdown links
- Per-cloud install guides will include actual IAM policy JSON / RBAC YAML (referenced from `infra/`)
- Architecture diagrams will use ASCII art (no external rendering dependencies)
- Estimated total: ~8000-10000 lines across all files
