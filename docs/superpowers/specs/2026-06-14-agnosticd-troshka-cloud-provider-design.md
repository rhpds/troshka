# AgnosticD Troshka Cloud Provider — Design Spec

## Overview

A new `troshka` cloud provider for agnosticd-v2 that deploys Troshka patterns into projects, replacing bare-metal/cloud VM provisioning with pre-built nested VM environments. Includes an Ansible collection, dynamic inventory plugin, student portal, and template YAML refactor.

## Deployment Modes

### Mode 1: Deploy Pattern (`pattern`)

Deploy a pre-built pattern to a project. The pattern contains fully-configured VMs — no post-deploy workloads needed. Used for production labs served via Babylon/RHPDS.

**Flow:** Troshka API deploy → wait for active → create portal token → return URLs to Babylon

### Mode 2: Pattern + Workloads (`pattern_workloads`)

Deploy a base pattern (e.g., bare OCP cluster), then run agnosticd workloads on top (install operators, configure apps, etc.). Used for production labs that need dynamic configuration at deploy time.

**Flow:** Troshka API deploy → wait for active → build inventory → run agnosticd workloads → create portal token → return URLs

### Mode 3: Template Build (`template`)

Deploy a fresh OCP cluster from a template, run workloads, capture the result as a new pattern. This is a developer/build tool for creating golden images. **Restricted to `dev.yaml` in agnosticv** — schema validation will enforce this once available.

**Flow:** Troshka API deploy-template → wait for active → build inventory → run workloads → capture pattern → optionally delete source project

## Architecture Decision

**Thin collection, smart API.** The Ansible collection is a lightweight API client. Business logic stays in Troshka where it's testable with Python, not scattered across Ansible YAML.

---

## Component 1: Ansible Collection — `agnosticd.cloud_provider_troshka`

Repository: `https://github.com/agnosticd/cloud_provider_troshka.git`

### Roles

#### `deploy`

Handles all three modes via `troshka_deploy_mode` variable.

| Mode | Variable Value | API Call |
|------|---------------|----------|
| 1 | `pattern` (default) | `POST /patterns/{id}/deploy` with `auto_deploy=true, auto_start=true` |
| 2 | `pattern_workloads` | Same as mode 1, agnosticd proceeds to run workloads after |
| 3 | `template` | `POST /deploy-template` with YAML template definition |

All modes:
- Pattern lookup: use `troshka_pattern_name` to find pattern via `GET /patterns/?name=...`. If `troshka_pattern_id` is also set, it takes precedence (skip lookup).
- Poll `GET /projects/{id}/deploy-progress` until project state is `active`
- Inject per-user variables (GUID, password) via `troshka_inject_vars` dict
- Create student portal token via `POST /projects/{id}/portal-token`
- EIP/gateway setup for showroom (port 443 on bastion) handled by Troshka during deploy
- Register output variables as job_vars

#### `destroy`

- `DELETE /projects/{id}`
- Portal token invalidated automatically (scoped to project)

#### `lifecycle`

- `ACTION: start` → `POST /projects/{id}/start`, poll until active
- `ACTION: stop` → `POST /projects/{id}/stop`, poll until stopped
- `ACTION: status` → `GET /projects/{id}`, return `current_state`

#### `capture` (mode 3 only)

- Triggered by a final play in `infrastructure_deployment.yml` that runs after all workloads complete, gated by `troshka_capture_on_complete: true`
- Calls pattern creation API from running project
- Polls until pattern state is `available`
- Registers `troshka_captured_pattern_id` and `troshka_captured_pattern_name`
- Optionally deletes source project (`troshka_delete_after_capture`)

#### `create_inventory`

- Calls `GET /projects/{id}` to read topology
- Parses VM node tags for `AnsibleGroup` values
- Identifies bastion (tagged with `bastions` group)
- Bastion gets `ansible_host` = external IP
- Inner VMs get `ansible_ssh_common_args` with ProxyJump through bastion
- Exposes VM metadata as host vars (VM ID, console URL)
- SSH credentials: the deploy role injects an SSH public key into the bastion via `troshka_inject_vars` (cloud-init `authorized_keys`). The corresponding private key is generated per-deploy and used for inventory SSH access. Inner VMs reached via ProxyJump inherit the same key (baked into pattern or injected via cloud-init).

### Dynamic Inventory Plugin — `troshka_inventory`

For modes 2 and 3 where agnosticd workloads need to SSH into VMs.

```yaml
# inventory file
plugin: agnosticd.cloud_provider_troshka.troshka_inventory
api_url: "{{ troshka_api_url }}"
api_key: "{{ troshka_api_key }}"
project_id: "{{ troshka_project_id }}"
```

- Fetches project topology from API
- Groups hosts by `AnsibleGroup` tag values (comma-separated → multiple groups)
- Bastion = jump host, inner VMs reached via SSH ProxyJump
- Exposes VM metadata as host vars

### Default Variables

```yaml
troshka_deploy_mode: pattern          # pattern, pattern_workloads, template
troshka_portal_access_level: console  # readonly, power, console, manage
troshka_auto_start: true
troshka_auto_deploy: true
troshka_capture_on_complete: false    # mode 3: capture after workloads
troshka_delete_after_capture: false   # mode 3: delete source project
```

### Output Variables (job_vars)

```yaml
troshka_project_id: "uuid"
troshka_portal_url: "https://troshka.example.com/portal/{token}"
troshka_showroom_url: "https://{bastion_eip}/"
troshka_bastion_ip: "1.2.3.4"
# Mode 3 only:
troshka_captured_pattern_id: "uuid"
troshka_captured_pattern_name: "OCP 4.16 Lab Name"
```

---

## Component 2: AgnosticD-v2 Cloud Provider

### Playbooks

#### `ansible/cloud_providers/troshka/infrastructure_deployment.yml`

Three plays following the AWS provider pattern:

| Play | Hosts | Purpose |
|------|-------|---------|
| Step 001.1: Deploy Infrastructure | `localhost` | Call Troshka API (deploy pattern or deploy template), poll until `active`, create portal token |
| Step 001.2: Create Inventory | `localhost` | Fetch project topology, parse `AnsibleGroup` tags, build inventory with bastion as jump host |
| Step 001.3: Wait for Hosts | `bastions` | Wait for SSH connectivity through bastion (modes 2 & 3 only; mode 1 skips) |

#### `ansible/cloud_providers/troshka/destroy_env.yml`

Single play on `localhost`: call `DELETE /projects/{id}`.

#### `ansible/cloud_providers/troshka/default_vars.yml`

Default variables for the provider (see Default Variables section above).

### Framework Changes

- Add `troshka` to `agnosticd_cloud_providers` list in `ansible/setup_runtime.yml`

---

## Component 3: Agnosticv Configuration

### Account

```yaml
# troshka/account.yaml
cloud_provider: troshka
```

### Secrets Include

```yaml
# /includes/secrets/troshka-prod.yaml
troshka_api_url: https://troshka-prod.example.com
troshka_api_key: !vault |
  $ANSIBLE_VAULT;1.2;AES256;gpte_vault_0
  ...

# /includes/secrets/troshka-dev.yaml
troshka_api_url: https://troshka-dev.example.com
troshka_api_key: !vault |
  ...
```

Multiple Troshka instances supported — each gets its own secret include. AWS/cloud credentials are internal to each Troshka instance, not exposed in agnosticv.

### Mode 1 Example — Deploy Pattern

```yaml
# troshka/ocp_416_networking_lab/common.yaml
cloud_provider: troshka
troshka_deploy_mode: pattern
troshka_pattern_name: "OCP 4.16 Advanced Networking Lab"
troshka_portal_access_level: console

troshka_inject_vars:
  guid: "{{ guid }}"
  student_password: "{{ common_password }}"

#include /includes/secrets/troshka-prod.yaml
```

### Mode 2 Example — Pattern + Workloads

```yaml
# troshka/ocp_416_dynamic_lab/common.yaml
cloud_provider: troshka
troshka_deploy_mode: pattern_workloads
troshka_pattern_name: "OCP 4.16 Base"
troshka_portal_access_level: power

troshka_inject_vars:
  guid: "{{ guid }}"
  student_password: "{{ common_password }}"

workloads:
  - agnosticd.core_workloads.ocp4_workload_authentication
  - agnosticd.core_workloads.ocp4_workload_pipelines

post_software_final_workloads:
  bastions:
    - showroom

showroom_git_repo: "https://github.com/rhpds/showroom-dynamic-lab.git"

#include /includes/secrets/troshka-prod.yaml
```

### Mode 3 Example — Template Build (dev.yaml only)

```yaml
# troshka/ocp_416_networking_lab/dev.yaml
cloud_provider: troshka
troshka_deploy_mode: template
troshka_template: ocp-compact
troshka_template_version: "4.16"
troshka_template_overrides:
  control_ram_gb: 32
troshka_capture_on_complete: true
troshka_capture_pattern_name: "OCP 4.16 Advanced Networking Lab"
troshka_delete_after_capture: true

workloads:
  - agnosticd.core_workloads.ocp4_workload_authentication
  - agnosticd.core_workloads.ocp4_workload_advanced_networking

post_software_final_workloads:
  bastions:
    - showroom

showroom_git_repo: "https://github.com/rhpds/showroom-networking-lab.git"

#include /includes/secrets/troshka-dev.yaml
```

### Constraint: `troshka_deploy_mode: template` Only in dev.yaml

Schema validation will enforce this once available. Until then, this is a documented convention. Risk: if placed in `prod.yaml`, every student order deploys OCP from scratch instead of using a pre-built pattern.

### Info Message Template

```adoc
Your lab environment is ready.

Lab Portal: {{ troshka_portal_url }}
Showroom: {{ troshka_showroom_url }}
GUID: {{ guid }}
```

---

## Component 4: Troshka API Additions

### 4a. VM Node Tags

Add `tags` dict to VM node data in topology JSONB.

Convention: `AnsibleGroup` key with comma-separated group names as the value (e.g., `"bastions,showroom"`).

Tags:
- Editable in the canvas UI (tag editor on VM node sidebar)
- Persist through pattern capture and deploy (cloned with topology)
- Read by the inventory plugin to build Ansible groups

### 4b. Student Portal

**New model:** `ProjectPortalToken`
- `token` (random, URL-safe)
- `project_id` (FK)
- `access_level` (readonly, power, console, manage)
- `expires_at` (optional)
- Deleted when project is deleted (cascade)

**New endpoints (outside SSO/OAuth protection):**
- `POST /projects/{id}/portal-token` — create token, returns portal URL. Requires authenticated user (project owner or admin).
- `GET /portal/{token}` — serves stripped-down canvas view. No authentication required — token is the auth.

**Access levels:**

| Level | Capabilities |
|-------|-------------|
| `readonly` | View topology (respects `hiddenNodeIds`) |
| `power` | readonly + start/stop/restart VMs |
| `console` | power + VNC console access |
| `manage` | console + add/remove hosts (future) |

### 4c. Pattern Lookup by Name

Extend `GET /patterns/` to support exact name lookup:
- `GET /patterns/?name=OCP%204.16%20Base` → returns single matching pattern or 404
- Used by the Ansible collection's deploy role when `troshka_pattern_id` is not set

### 4d. Deploy Template Endpoint

`POST /api/v1/deploy-template` — accepts template definition and creates a project from it.

Request body:
```json
{
  "template": "ocp-compact",
  "version": "4.16",
  "overrides": {
    "control_ram_gb": 32,
    "worker_count": 2
  },
  "name": "build-ocp416-networking",
  "auto_deploy": true,
  "auto_start": true
}
```

Validates overrides against the template's declared parameters (unknown keys or below-minimum values rejected).

**This endpoint must be outside SSO protection** — AAP2 authenticates via API key.

### 4e. Deploy-Time Variable Injection

Extend `POST /patterns/{id}/deploy` to accept `inject_vars` dict.

Values (GUID, user password, domain) injected via cloud-init user-data on the bastion VM. Showroom config updated with these values on boot.

### 4f. API Key Authentication — Outside SSO

The existing API key auth (`trk_` prefix, `Authorization: Bearer trk_...`) must work without SSO/OAuth proxy protection. AAP2 uses API keys to call Troshka. Ensure the reverse proxy (if present) passes through API requests with `Authorization: Bearer trk_*` headers without requiring SSO session.

---

## Component 5: Template YAML Refactor

### Base Template Definition

```yaml
# templates/ocp-cluster.yaml
name: ocp-cluster
description: OpenShift cluster (configurable topology)
versions: ["4.14", "4.15", "4.16", "4.17"]
parameters:
  control_count:
    default: 3
    min: 1
    description: Number of control plane nodes
  control_vcpus:
    default: 4
    min: 4
  control_ram_gb:
    default: 16
    min: 16
  control_disk_gb:
    default: 120
    min: 100
  control_schedulable:
    default: true
    description: Whether control plane nodes accept workload scheduling
  worker_count:
    default: 0
    min: 0
  worker_vcpus:
    default: 4
    min: 4
  worker_ram_gb:
    default: 16
    min: 16
  worker_disk_gb:
    default: 120
    min: 100
bastion:
  vcpus: 2
  ram_gb: 4
  disk_gb: 50
  image: rhel-10
networks:
  cluster:
    cidr: 10.0.0.0/24
    dhcp: true
  bmc:
    cidr: 192.168.100.0/24
```

### Named Presets

Presets are named configurations of the base template, giving users clear starting points.

```yaml
# templates/ocp-sno.yaml
name: ocp-sno
description: Single Node OpenShift
extends: ocp-cluster
defaults:
  control_count: 1
  control_vcpus: 16
  control_ram_gb: 64
  control_disk_gb: 120
  control_schedulable: true
  worker_count: 0

# templates/ocp-compact.yaml
name: ocp-compact
description: Compact cluster (3 schedulable control plane nodes)
extends: ocp-cluster
defaults:
  control_count: 3
  control_vcpus: 4
  control_ram_gb: 16
  control_disk_gb: 120
  control_schedulable: true
  worker_count: 0

# templates/ocp-standard.yaml
name: ocp-standard
description: Standard cluster (3 CP + 2 workers)
extends: ocp-cluster
defaults:
  control_count: 3
  control_vcpus: 4
  control_ram_gb: 16
  control_disk_gb: 120
  control_schedulable: false
  worker_count: 2
  worker_vcpus: 4
  worker_ram_gb: 16
  worker_disk_gb: 120
```

### Storage Location

- Default templates ship in the Troshka repo: `src/backend/templates/`
- Agnosticv can override template defaults via `troshka_template_overrides`
- The `deploy-template` API endpoint reads template definitions from disk, applies overrides, validates, and generates the topology

### Migration from Python

Replace the hardcoded topology generation in `src/backend/app/services/topology_templates.py` with a YAML-driven generator that reads template definitions and produces the same topology JSONB structure.

---

## Component 6: Troshka UI Additions

### 6a. VM Node Tag Editor

- Tag editor on the VM node sidebar in the canvas
- Key-value pairs (e.g., `AnsibleGroup: bastions,showroom`)
- Tags stored in VM node data within topology JSONB
- Tags visible in the node details panel

### 6b. Student Portal View

- New route: `/portal/{token}`
- Stripped-down canvas: no header, no sidebar, no editing controls
- Shows topology with `hiddenNodeIds` respected
- VM controls based on access level (power buttons, console launch)
- VNC console opens in popup (reuses existing console infrastructure)
- No Troshka authentication required — token is auth

---

## Student Access Model

Students do NOT get SSH access to any VM. Their access:

1. **Student portal URL** — token-based, stripped-down canvas view with power controls and VNC console
2. **Showroom URL** — HTTPS on bastion via EIP/gateway (port 443). Showroom SSHs to localhost for its embedded terminal. Student never SSHs directly.
3. **VNC console** — available through the portal for VMs the student is allowed to see
4. **OCP stays internal** — `.local` domain, accessed through bastion/showroom only

Access level and VM visibility configured in agnosticv per catalog item.

---

## Networking

- Bastion VM gets external IP with port 443 (showroom HTTPS) forwarded through the gateway
- No port 22 exposed to students
- OCP API/console stays internal (`.local` domain)
- Gateway handles outbound NAT for all VMs
- EIP/gateway setup is part of the pattern topology — carried over on deploy

---

## Babylon/AAP2 Integration

- Babylon fires one AAP2 job per student with unique GUID
- AAP2 authenticates to Troshka via API key from agnosticv secrets
- Provider returns job_vars: `troshka_project_id`, `troshka_portal_url`, `troshka_showroom_url`
- Babylon surfaces these to the student via info-message-template
- Lifecycle: Babylon calls start/stop/status/destroy via agnosticd lifecycle entry point
- Bulk deploy is a Troshka-native feature, not used by Babylon

---

## Scope Summary

### New Repos

- `agnosticd/cloud_provider_troshka` — Ansible collection (roles + inventory plugin)

### Changes to Existing Repos

**agnosticd-v2:**
- `ansible/cloud_providers/troshka/infrastructure_deployment.yml`
- `ansible/cloud_providers/troshka/destroy_env.yml`
- `ansible/cloud_providers/troshka/default_vars.yml`
- `ansible/setup_runtime.yml` — add `troshka` to supported providers

**agnosticv:**
- `troshka/account.yaml` — new account
- `troshka/` — catalog items using snake_case names
- `/includes/secrets/troshka-*.yaml` — per-instance secrets

**troshka:**
- VM node tags in topology JSONB
- Student portal (model, API endpoints, frontend view)
- Pattern lookup by name
- Deploy template endpoint
- Deploy-time variable injection
- Template YAML definitions (replace hardcoded Python)
- Canvas tag editor UI
- SSO bypass for API key auth and portal routes
