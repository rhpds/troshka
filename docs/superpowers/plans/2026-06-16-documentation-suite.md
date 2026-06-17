# Documentation Suite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Write comprehensive documentation covering architecture, API (all 164 endpoints), and per-cloud installation for AWS, GCP, Azure, and OCP Virt.

**Architecture:** Eight markdown files in `docs/` plus a README.md update. Each file is self-contained with cross-references via relative links. The API guide has two parts: workflow walkthroughs and full endpoint reference. Install guides are per-cloud with a shared common setup doc.

**Tech Stack:** Markdown, ASCII diagrams, curl examples, YAML/JSON config snippets

## Global Constraints

- All docs go in `docs/` at the project root
- Cross-references use relative markdown links (`[text](other-doc.md#section)`)
- Curl examples use `localhost:8200` with dev-mode auth (`DEV_TOKEN` placeholder)
- ASCII art for diagrams (no external rendering dependencies)
- Endpoint documentation must be derived from actual route handler code — never guess signatures
- Include actual IAM policy / RBAC YAML by reference to `infra/` files
- No emojis in documentation files

---

### Task 1: Architecture Guide

**Files:**
- Create: `docs/architecture.md`

**Interfaces:**
- Consumes: CLAUDE.md (architecture notes), `src/backend/app/services/providers/base.py` (17-method interface), `src/backend/app/main.py` (router registration), `src/troshkad/troshkad.py` (handler list)
- Produces: Architecture reference linked from README.md and cross-referenced by all other docs

**Content sections (15 total):**

1. System Overview — what Troshka is, three-tier ASCII diagram, tech stack table
2. Component Architecture — backend structure (api/models/services), frontend (pages/components/stores), database (PostgreSQL + JSONB topology), S3 storage
3. Provider Abstraction — `ProviderDriver` 17-method interface, `get_provider_driver()` dispatch, per-cloud comparison table (nested virt, EIPs, shared storage, console TLS, image format, SSH user, data disk path)
4. Host Agent (troshkad) — single-file daemon design, HTTPS port 31337, bearer token + cert pinning, job system flow, handler categories with counts (VM: 10, Disk: 5, Network: 8, Pattern: 7, BMC: 4, Migration: 2, Exec: 3, GC: 2, PXE: 1, Admin: 4)
5. VNC Console (troshka-vncd) — direct proxy architecture diagram, JWT auth flow, per-provider DNS/TLS
6. Networking Model — VXLAN + VNI allocation, network namespaces, nftables chains, gateway NAT, EIP secondary IPs
7. Storage Architecture — local vs shared modes (FSx, Azure Files NFS, BYO), storage pools, SharedCacheEntry coordination, path resolution, auto-extend
8. Deploy Pipeline — topology → placement → provision → parallel VM creation → network → cloud-init → start order, progress tracking, reconfigure/redeploy
9. Patterns & Snapshots — state machine, capture flow, pattern buffer (NBD), S3 upload, topology remapping
10. Library System — personal libraries, multipart upload, URL import, sharing, ISO management
11. Live Migration — pool PKI, libvirt mutual TLS, migration flow, host evacuation
12. Virtual BMC — sushy-emulator + vbmc per VM, BMC bridge in namespace, Redfish/IPMI
13. Health & Garbage Collection — health poller, GC pipeline (6 steps), dry-run, wipe
14. Authentication & Authorization — OIDC, dev-mode, API keys, roles, portal tokens, Fernet encryption
15. Red Hat Image Builder — offline token flow, per-cloud image formats, auto-set default_image

**Source files to read for each section:**
- Section 3: `src/backend/app/services/providers/base.py`, `ec2.py`, `gcp.py`, `azure.py`, `ocpvirt.py`
- Section 4: `src/troshkad/troshkad.py` (grep `def _handle_` for all handlers)
- Section 5: `src/troshka-vncd/troshka-vncd.py`
- Section 6: `src/backend/app/services/vxlan.py`, troshkad network handlers
- Section 7: `src/backend/app/services/storage_pool_service.py`, `storage_extend.py`
- Section 8: `src/backend/app/services/deploy_service.py`, `placement.py`, `provisioner.py`
- Section 9: `src/backend/app/services/pattern_service.py`, `pattern_buffer_service.py`, `snapshot_service.py`
- Section 11: `src/backend/app/services/migration_service.py`
- Section 13: `src/backend/app/services/health_poller.py`, `gc_service.py`
- Section 14: `src/backend/app/core/auth.py`
- Section 15: `src/backend/app/services/image_builder_service.py`

- [ ] **Step 1: Read all source files** listed above to extract accurate details for each section
- [ ] **Step 2: Write `docs/architecture.md`** with all 15 sections, ASCII diagrams, comparison tables, and code-level details
- [ ] **Step 3: Verify internal links** — ensure all `#section-slug` anchors match actual heading slugs
- [ ] **Step 4: Move on** — commits deferred to Task 10

---

### Task 2: API Guide — Part 1 (Workflow Walkthroughs)

**Files:**
- Create: `docs/api-guide.md` (Part 1 only — Part 2 added in Task 3)

**Interfaces:**
- Consumes: All files in `src/backend/app/api/` (route handlers), `src/backend/app/schemas/` (Pydantic models)
- Produces: API guide with workflow sections, cross-referenced by install guides

**Content — 9 workflow sections:**

1. Getting Started — base URL (`http://localhost:8200/api/v1`), auth methods (dev token, OIDC, API keys), common patterns (async ops, progress polling, WebSocket), error format
2. Setting Up Infrastructure — create provider (per-cloud credentials), test, setup network (per-cloud), setup console, create bucket, set image, provision host
3. Managing Your Library — list, upload (multipart flow: create → start → part-url × N → complete), import URL, share
4. Building & Deploying Projects — create, topology JSONB structure, deploy, progress (HTTP poll + WebSocket), from-template
5. Working with VMs — power ops, batch state, console (JWT flow), exec (serial/SSH), files (push/pull), snapshots
6. Patterns — save (POST /patterns), progress, deploy, bulk deploy, share
7. Networking & External Access — network CRUD, EIPs, DNS providers
8. Administration — hosts (provision/resize/extend/evacuate/wipe/GC), storage pools (create/extend/pattern-buffer/cache), providers
9. Portal Access — generate token, portal endpoints

**For each workflow section:**
- Read the relevant API route handler to get exact request/response shapes
- Write curl examples with `$DEV_TOKEN` auth header
- Show expected response JSON (abbreviated)
- Note which operations are async (return 202, poll for progress)

- [ ] **Step 1: Read all API route files** in `src/backend/app/api/` and schema files in `src/backend/app/schemas/` to understand exact request/response shapes
- [ ] **Step 2: Write Part 1 of `docs/api-guide.md`** — table of contents, 9 workflow sections with curl examples
- [ ] **Step 3: Verify curl paths** match actual route prefixes from `main.py` registration
- [ ] **Step 4: Move on** — commits deferred to Task 10

---

### Task 3: API Guide — Part 2 (Full Endpoint Reference)

**Files:**
- Modify: `docs/api-guide.md` (append Part 2 after existing Part 1)

**Interfaces:**
- Consumes: All 16 API route modules, Pydantic schemas
- Produces: Complete 164-endpoint reference appended to api-guide.md

**Endpoint reference format for each endpoint:**
```markdown
### METHOD /api/v1/path

One-line description.

**Auth:** admin | user | portal
**Request:** `{ key_field: "type", ... }` (only for POST/PUT/PATCH)
**Response:** `{ field: "type", ... }` or `204 No Content`
```

Add curl examples only for non-obvious endpoints (multipart upload, WebSocket, async operations).

**Resource modules to document (16 total, 164 endpoints):**

| Module | File | Count | Notes |
|--------|------|-------|-------|
| Auth | `auth.py` | 13 | Dev token, OIDC config, SSH keys, secrets |
| Providers | `providers.py` | 26 | CRUD, VPC/network setup, console, image builder |
| Hosts | `hosts.py` | 22 | CRUD, agent, power, resize, GC, evacuate |
| Storage Pools | `storage_pools.py` | 14 | CRUD, extend, cache, pattern buffer, GC |
| Projects | `projects.py` | 29 | CRUD, deploy, VM ops, exec, files, migrate |
| VMs | `vms.py` | 6 | CRUD, snapshot |
| Disks | `disks.py` | 7 | CRUD, attach, detach |
| Networks | `networks.py` | 5 | CRUD |
| EIPs | `eips.py` | 4 | List, delete, sync, provider GC |
| Library | `library.py` | 13 | CRUD, multipart upload, import, share, scan |
| Patterns | `patterns.py` | 10 | CRUD, share, progress, deploy, bulk deploy |
| DNS Providers | `dns_providers.py` | 5 | CRUD |
| API Keys | `api_keys.py` | 3 | List, create, delete |
| Portal | `portal.py` | 4 | Token, project view, VM states, VM action |
| Templates | `templates.py` | 1 | Deploy from template |
| WebSocket | `ws.py` | 2 | Project WS, pattern WS |

Plus 3 endpoints registered directly in `main.py`: `GET /health`, `GET /ocp/versions`, `GET /debug/threads`.

- [ ] **Step 1: Read each API route file** and extract every `@router.*` decorated function — method, path, auth dependency, request body fields, response model
- [ ] **Step 2: Append Part 2 to `docs/api-guide.md`** — "Full Endpoint Reference" header, 16 resource sections + 3 main.py endpoints
- [ ] **Step 3: Cross-check count** — verify documented endpoint count matches 164 + 3 = 167 total
- [ ] **Step 4: Move on** — commits deferred to Task 10

---

### Task 4: Common Install Guide

**Files:**
- Create: `docs/install-common.md`

**Interfaces:**
- Consumes: `src/backend/config/config.yaml`, `dev-services.sh`, `src/backend/requirements.txt` or `pyproject.toml`
- Produces: Shared setup doc cross-referenced by all per-cloud install guides

**Content — 9 sections:**

1. Prerequisites — Python 3.11+, Node.js 20+, PostgreSQL 16, podman/docker, git
2. Clone & Backend Setup — clone, virtualenv, pip install, verify
3. Configuration — `config.yaml` key-by-key reference, `config.local.yaml` overrides, `TROSHKA_*` env vars (Dynaconf `__` separator)
4. Database Setup — podman PostgreSQL container (port 5433, named volume `troshka-pgdata`), create database, Alembic migrations
5. Frontend Setup — `cd src/frontend && npm install`, environment variables
6. Development Mode — `./dev-services.sh start`, ports table (8200/3100/5433), dev-mode auto-auth, Swagger at `/docs`
7. Production Deployment — systemd unit files for backend (uvicorn), reverse proxy (nginx example), OIDC configuration (oauth_enabled, admin_users/groups), TLS termination
8. S3 Storage — bucket creation (AWS S3 or compatible), config.yaml `s3` section
9. Troubleshooting — port conflicts, migration errors, auth issues, PostgreSQL container issues

- [ ] **Step 1: Read config files** — `config.yaml`, `dev-services.sh`, backend requirements
- [ ] **Step 2: Write `docs/install-common.md`** with all 9 sections
- [ ] **Step 3: Verify** — ensure all config keys mentioned match actual `config.yaml`
- [ ] **Step 4: Move on** — commits deferred to Task 10

---

### Task 5: AWS Install Guide

**Files:**
- Create: `docs/install-aws.md`

**Interfaces:**
- Consumes: `infra/iam-policy.json`, `src/backend/app/services/providers/ec2.py`, `src/backend/app/services/storage_pool_service.py`
- Produces: Self-contained AWS setup guide, references `install-common.md`

**Content — 10 sections:**

1. Prerequisites — AWS account, IAM admin, region selection
2. IAM Setup — create `troshka` IAM user, create `troshka-policy` managed policy (reference `infra/iam-policy.json` — list permission categories), attach to user, generate access key
3. Create Provider — UI or `POST /api/v1/providers` with `{"name": "aws-prod", "type": "ec2", "credentials": {"access_key_id": "...", "secret_access_key": "...", "region": "us-east-1"}}`, test with `POST .../test`
4. VPC Setup — "Setup VPC" button or `POST /providers/{id}/create-vpc` — what it creates: VPC (10.0.0.0/16), subnets in all AZs, IGW, route tables, security group (SSH 22, agent 31337, console 443, VXLAN 4789 UDP), S3 gateway endpoint
5. S3 Bucket — `POST /providers/{id}/create-bucket` creates `troshka-images`
6. Host Image — Option A: marketplace RHEL (auto-discovered via `GET /providers/{id}/discover-images`); Option B: Image Builder custom AMI (Settings → RH offline token, then Provider → Build Host Image)
7. Console Setup — `POST /providers/{id}/setup-console` with `{"base_domain": "console.example.com"}` → creates Route53 hosted zone + IAM role/instance profile → UI shows nameservers → admin adds NS records in parent zone
8. Provision First Host — `POST /hosts` with `{"provider_id": "...", "instance_type": "m5.metal", "storage_size_gb": 500}`, recommended instance types table (m5.metal, c5.metal, m8i.xlarge), storage sizing guidance
9. Shared Storage (optional) — create FSx OpenZFS pool: `POST /storage-pools` with `{"name": "...", "mode": "shared-fsx", "provider_id": "...", "availability_zone": "us-east-1a"}`, what it creates (FSx filesystem, security group rules), mount options (`nconnect=16`), pricing (~$53/mo for 128GB/160MBps)
10. Verify — end-to-end checklist: provider test passes, VPC visible, host connected (agent green), deploy a test project

- [ ] **Step 1: Read `ec2.py`** provider driver for VPC setup details, instance provisioning, console setup, EIP allocation
- [ ] **Step 2: Read `infra/iam-policy.json`** to summarize permission categories
- [ ] **Step 3: Write `docs/install-aws.md`** with all 10 sections, API calls, and verification steps
- [ ] **Step 4: Move on** — commits deferred to Task 10

---

### Task 6: GCP Install Guide

**Files:**
- Create: `docs/install-gcp.md`

**Interfaces:**
- Consumes: `src/backend/app/services/providers/gcp.py`
- Produces: Self-contained GCP setup guide, references `install-common.md`

**Content — 10 sections:**

1. Prerequisites — GCP project (ideally under org folder), enable Compute Engine API + Cloud DNS API
2. Service Account — create SA, grant `roles/compute.admin` + `roles/dns.admin`, download JSON key
3. Org Policy — check for `custom.denyCostlyMachineTypes` constraint, E2/N2-standard allowed, N2-highmem may need exception for host provisioning
4. Create Provider — `POST /api/v1/providers` with `{"name": "gcp-prod", "type": "gcp", "credentials": {"service_account_json": {...}}, "default_region": "us-central1"}`, test
5. Network Setup — `POST /providers/{id}/create-network-gcp` — creates custom-mode VPC, subnet (10.100.1.0/24), firewall rules targeting `troshka-host` network tag (SSH 22, agent 31337, console 443, VXLAN 4789 UDP, ICMP)
6. Host Image — PAYG from `rhel-cloud` project (default, repos work out of box) or Image Builder BYOS (needs RHSM registration)
7. Console Setup — `POST /providers/{id}/setup-console` with `{"base_domain": "..."}` → creates Cloud DNS zone → NS delegation, uses `certbot-dns-google` plugin
8. Provision First Host — recommended `n2-highmem-16` or larger (nested virt via `advancedMachineFeatures`), SSH user is `troshka` (set via instance metadata), data disk is `/dev/sdb`
9. Shared Storage — not yet supported (Filestore/NetApp blocked by org policy), use `local` pool mode with pattern buffer for pattern save
10. Verify — checklist

- [ ] **Step 1: Read `gcp.py`** provider driver for network setup, instance provisioning, console DNS, image discovery
- [ ] **Step 2: Write `docs/install-gcp.md`** with all 10 sections
- [ ] **Step 3: Move on** — commits deferred to Task 10

---

### Task 7: Azure Install Guide

**Files:**
- Create: `docs/install-azure.md`

**Interfaces:**
- Consumes: `src/backend/app/services/providers/azure.py`
- Produces: Self-contained Azure setup guide, references `install-common.md`

**Content — 11 sections:**

1. Prerequisites — Azure subscription, resource group (or Troshka creates one)
2. Service Principal — `az ad sp create-for-rbac --name troshka --role Contributor --scopes /subscriptions/{sub}/resourceGroups/{rg}`, note tenant_id, client_id, client_secret, subscription_id
3. Marketplace Terms — accept RHEL BYOS offer: `az vm image terms accept --publisher redhat --offer rhel-byos --plan rhel-lvm94-gen2` (one-time per subscription)
4. Create Provider — `POST /api/v1/providers` with `{"name": "azure-prod", "type": "azure", "credentials": {"tenant_id": "...", "client_id": "...", "client_secret": "...", "subscription_id": "..."}}`, test
5. Network Setup — `POST /providers/{id}/create-network-azure` — creates Resource Group (if needed), VNet (10.100.0.0/16), subnet, NSG with rules (SSH 22, agent 31337, console 443, VXLAN 4789 UDP)
6. Host Image — BYOS from `redhat` publisher (`rhel-byos` offer) or Image Builder managed image
7. Console Setup — `POST /providers/{id}/setup-console` with `{"base_domain": "..."}` → creates Azure DNS zone → NS delegation, uses `certbot-dns-azure` plugin
8. Image Builder One-Time Setup — grant Contributor to Image Builder SP: `az role assignment create --assignee b94bb246-b02c-4985-9c22-d44e66f657f4 --role Contributor --scope /subscriptions/{sub}/resourceGroups/{rg}`
9. Provision First Host — recommended `Standard_E32s_v5` (32 vCPU / 256 GiB, nested virt native), SSH user is `troshka`, data disk at `/dev/disk/azure/scsi1/lun0`
10. Shared Storage (optional) — Azure Files NFS Premium v2 pool: `POST /storage-pools` with `{"name": "...", "mode": "shared-azure-files", "provider_id": "..."}`, private endpoint auto-created, pricing (~$0.10/GiB/month), online resize
11. Verify — checklist (include: stop vs deallocate note — always deallocate to release billing)

- [ ] **Step 1: Read `azure.py`** provider driver for network setup, provisioning, console, storage, cleanup order
- [ ] **Step 2: Write `docs/install-azure.md`** with all 11 sections
- [ ] **Step 3: Move on** — commits deferred to Task 10

---

### Task 8: OCP Virt Install Guide

**Files:**
- Create: `docs/install-ocpvirt.md`

**Interfaces:**
- Consumes: `infra/ocpvirt-rbac.yaml`, `src/backend/app/services/providers/ocpvirt.py`
- Produces: Self-contained OCP Virt setup guide, references `install-common.md`

**Content — 11 sections:**

1. Prerequisites — OpenShift 4.x with OpenShift Virtualization operator, nodes with nested virt capability (AMD EPYC recommended), Ceph-NFS storage class available
2. RBAC Setup — apply `infra/ocpvirt-rbac.yaml` (creates namespace `troshka`, SA `troshka`, ClusterRole `troshka-provider` with exact permissions, ClusterRoleBinding), full YAML included
3. Generate Token — `oc create token troshka -n troshka --duration=8760h` (1-year token)
4. Create Provider — `POST /api/v1/providers` with `{"name": "ocpvirt-prod", "type": "ocpvirt", "credentials": {"api_url": "https://api.cluster:6443", "token": "...", "namespace": "troshka"}}`, test
5. Infrastructure Setup — `POST /providers/{id}/setup-infra` verifies API connectivity and storage classes
6. Storage — Ceph-NFS via `ocs-storagecluster-ceph-nfs` storage class, PVC-based storage for VMs
7. Host Image — RHEL QCOW2 imported as DataVolume (no Image Builder support for OCP Virt yet)
8. Console Setup — OCP Routes (edge TLS terminated by OCP router), vncd runs with `--no-tls` flag, no certbot needed
9. Provision First Host — VM sizing (CPU/memory requests), storage sizing via DataVolume
10. Differences from Cloud Providers — table: no EIPs (externalAccess disabled), no resize (KubeVirt limitation), OCP Routes instead of DNS zones, NodePort services for SSH/agent access, no S3 gateway endpoint
11. Verify — checklist

- [ ] **Step 1: Read `ocpvirt.py`** provider driver for provisioning, console routes, storage, limitations
- [ ] **Step 2: Write `docs/install-ocpvirt.md`** with all 11 sections, include full RBAC YAML
- [ ] **Step 3: Move on** — commits deferred to Task 10

---

### Task 9: README.md Update

**Files:**
- Modify: `README.md` (update existing — add doc links, update provider list, refresh quick start)

**Interfaces:**
- Consumes: All 7 doc files created in Tasks 1-8
- Produces: Updated README with documentation section linking to all guides

**Changes:**

1. Update the description paragraph to mention all 4 cloud providers (currently says "AWS EC2 and OCP Virtualization" — add GCP and Azure)
2. Add a "Documentation" section after the existing content (or update existing Architecture section) with:
   - Link to architecture guide
   - Link to API guide
   - Installation table with all 4 clouds (common setup + per-cloud)
3. Update the "Multi-provider" bullet in Key Features to list all 4 providers
4. Update the Architecture diagram to show GCP and Azure connections
5. Verify all relative links resolve correctly

- [ ] **Step 1: Read current README.md** to understand structure and identify update points
- [ ] **Step 2: Edit README.md** — update description, add Documentation section with links table, update features list
- [ ] **Step 3: Verify all links** — `docs/architecture.md`, `docs/api-guide.md`, `docs/install-common.md`, `docs/install-aws.md`, `docs/install-gcp.md`, `docs/install-azure.md`, `docs/install-ocpvirt.md`
- [ ] **Step 4: Move on** — commits deferred to Task 10

---

### Task 10: Final Verification & Commit

**Files:**
- Verify: all 8 files created/modified in Tasks 1-9

**Checks:**

1. Every `[link text](path.md)` and `[link text](path.md#section)` resolves to an existing file and heading
2. Endpoint counts in api-guide.md match actual route count (167 = 164 from routers + 3 from main.py)
3. Provider comparison table in architecture.md is consistent with per-cloud install guides
4. Config keys mentioned in install-common.md match actual `config.yaml`
5. IAM policy summary in install-aws.md matches `infra/iam-policy.json`
6. RBAC YAML in install-ocpvirt.md matches `infra/ocpvirt-rbac.yaml`
7. No broken markdown (unclosed code blocks, malformed tables, orphaned headers)

- [ ] **Step 1: Run link check** — grep for all `](` patterns, verify each target exists
- [ ] **Step 2: Spot-check 5 random API endpoints** — verify documented method/path/auth matches source code
- [ ] **Step 3: Fix any issues found**
- [ ] **Step 4: Commit all documentation**
```bash
cd /Users/prutledg/troshka && git add docs/architecture.md docs/api-guide.md docs/install-common.md docs/install-aws.md docs/install-gcp.md docs/install-azure.md docs/install-ocpvirt.md README.md && git commit -m "docs: add architecture guide, API guide, and per-cloud install guides"
```
