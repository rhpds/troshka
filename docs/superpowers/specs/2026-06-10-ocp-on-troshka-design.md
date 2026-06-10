# OpenShift on Troshka — Design Spec

## Overview

Enable running full OpenShift clusters as Troshka projects. Build a golden OCP cluster once, save as a pattern, stamp out pre-installed clusters in minutes instead of hours. Target use case: RHDP labs with 60 users each getting a dedicated OCP cluster.

**Approach:** Troshka provides general-purpose infrastructure (LB node, DNS provider integration). OCP-specific automation (install, certs, domain reconfiguration) stays in external Ansible playbooks.

## OCP Profiles Supported

| Profile | Nodes | Per-Node vCPU | Per-Node RAM | Per-Node Disk | Total vCPU | Total RAM | Total Disk |
|---------|-------|---------------|--------------|---------------|------------|-----------|------------|
| SNO | 1 CP+worker | 8 | 32 GB | 120 GB | 8 | 32 GB | 120 GB |
| Compact 3-node | 3 CP+worker | 8 | 16 GB | 120 GB | 24 | 48 GB | 360 GB |
| Standard 3+2 | 3 CP + 2 workers | 4 CP / 4 worker | 16 GB each | 120 GB each | 20 | 80 GB | 600 GB |

Bootstrap node (temporary, install only): 4 vCPU, 16 GB RAM, 120 GB disk.

**Install method:** Bare metal IPI with Troshka's existing virtual BMC (vbmcd + sushy-emulator).

## 1. Load Balancer Network Node

A new network node type providing per-project HAProxy in the project's network namespace. L4 TCP passthrough — no TLS termination. General-purpose, usable beyond OCP.

### Canvas Representation

A `networkNode` with `networkType: "loadbalancer"`. Distinct icon on the canvas. Users connect VM nodes to it via edges — connected VMs become backends.

### Topology Data (JSONB on the node)

```json
{
  "type": "networkNode",
  "networkType": "loadbalancer",
  "frontends": [
    { "name": "api", "bindPort": 6443, "mode": "tcp", "backendPort": 6443 },
    { "name": "ingress-https", "bindPort": 443, "mode": "tcp", "backendPort": 443 },
    { "name": "ingress-http", "bindPort": 80, "mode": "tcp", "backendPort": 80 },
    { "name": "machine-config", "bindPort": 22623, "mode": "tcp", "backendPort": 22623 }
  ]
}
```

Backends are derived from edges — whichever VM nodes are connected to the LB node become backends. The `backendPort` on each frontend tells HAProxy which port to target on each backend VM. No manual IP entry; the canvas connections are the source of truth.

### HAProxy Config Generation

```
global
    daemon
    maxconn 4096

defaults
    mode tcp
    timeout connect 5s
    timeout client 30s
    timeout server 30s
    option tcplog

frontend api
    bind *:6443
    default_backend api-servers

backend api-servers
    balance roundrobin
    option httpchk GET /readyz HTTP/1.1\r\nHost:\ localhost\r\nConnection:\ close
    http-check expect status 200
    server cp-0 10.0.0.10:6443 check check-ssl verify none
    server cp-1 10.0.0.11:6443 check check-ssl verify none
    server cp-2 10.0.0.12:6443 check check-ssl verify none
```

Health checks are protocol-aware in TCP mode via `option httpchk` with `check-ssl verify none`.

### Runtime Lifecycle (troshkad)

**Prerequisites:** `haproxy` package must be installed on the host. Add to provisioner cloud-init package list alongside qemu-kvm, libvirt, etc.

**Setup** (during `/networks/full-setup`):
1. Generate `haproxy.cfg` from frontend definitions + connected VM IPs
2. HAProxy binds on `0.0.0.0` inside the namespace (safe due to namespace isolation)
3. Write config to `/etc/haproxy/troshka-{project_id[:8]}.cfg`
4. Start HAProxy in namespace: `ip netns exec {ns} haproxy -f {cfg} -D -p {pidfile}`
5. Configure EIP port forwards for each frontend port (reuses existing gateway nftables pattern)

**Teardown** (during `/networks/full-teardown`):
1. Kill HAProxy process
2. Remove config/PID files

**Reconfigure** (on topology change):
1. Regenerate config, reload HAProxy with `-sf` for graceful reload

### Port Forwarding Path

```
EIP:6443 → host nftables DNAT → namespace veth → HAProxy :6443 → CP nodes :6443
EIP:443  → host nftables DNAT → namespace veth → HAProxy :443  → ingress nodes :443
EIP:80   → host nftables DNAT → namespace veth → HAProxy :80   → ingress nodes :80
```

Same two-layer DNAT as existing gateway port forwards.

## 2. DNS Provider Integration

DNS as a provider — admin-configured, credential-bearing, selectable at deploy time.

### DNS Provider Model

```
DNSProvider
  id: UUID
  name: String ("RHDP Production BIND")
  type: String ("nsupdate" | "route53")
  config: JSONB {
    // nsupdate:
    "server": "10.0.0.53",
    "port": 53,
    "key_name": "update-key",
    "key_secret": "encrypted-tsig-secret",
    "key_algorithm": "hmac-sha256",
    "default_zone": "dynamic.redhatworkshops.io"

    // route53:
    "access_key_id": "...",
    "secret_access_key": "...",
    "hosted_zone_id": "..."
  }
```

Admins configure DNS providers in the admin UI (like hosts and storage pools).

### DNS Record Templates

Stored on the LB node in topology JSONB:

```json
{
  "dnsRecords": [
    { "name": "api.{guid}.{domain}", "type": "A", "target": "eip" },
    { "name": "api-int.{guid}.{domain}", "type": "A", "target": "eip" },
    { "name": "*.apps.{guid}.{domain}", "type": "A", "target": "eip" }
  ],
  "dnsTtl": 30
}
```

Tokens `{guid}` and `{domain}` are resolved at deploy time from the Project fields. `eip` resolves to the project's assigned Elastic IP.

### Separation of Concerns

- **Pattern (LB node):** record templates with `{guid}` and `{domain}` tokens
- **DNS Provider (admin config):** BIND server address, TSIG credentials, or Route53 keys
- **Deploy API call:** `guid`, `domain`, and `dns_provider_id` values

Patterns are portable across environments — same topology, different DNS providers.

### Lifecycle

**Deploy** (after networks up, EIP assigned):
1. Resolve `{guid}` and `{domain}` tokens from Project fields
2. Resolve `eip` to assigned Elastic IP
3. Dispatch to provider (NSUPDATE or Route53)
4. Create A records
5. Store created records in project metadata for cleanup

**Teardown** (before EIP release):
1. Delete DNS records via provider
2. Clear stored record metadata

DNS creation failure is non-fatal — project deploys, logs a warning.

### Implementation

Backend-side in `src/backend/app/services/dns_service.py`. The DNS server is external infrastructure reachable from the backend. Troshkad does not need DNS credentials or libraries.

## 3. Project Model Additions

New fields on Project:

- `guid: String` (optional) — unique identifier for DNS templating
- `domain: String` (optional) — base domain for DNS records
- `dns_provider_id: FK → DNSProvider` (optional) — which DNS provider to use

All nullable — projects without DNS config work as today.

## 4. Deploy-from-Pattern API Extension

```
POST /api/projects/deploy-from-pattern
{
  "pattern_id": "ocp-420-compact",
  "name": "OCP Lab - user42",
  "guid": "abc123",
  "domain": "sandbox123.opentlc.com",
  "dns_provider_id": "uuid-of-bind-provider",
  "host_id": "auto"
}
```

## 5. Topology Templates (UI Convenience)

Pre-wired canvas layouts for common OCP profiles. Not patterns (no disk images) — topology skeletons that get a user from "new project" to "ready to install" in one click.

### SNO Template
- 1 VM node: 8 vCPU, 32 GB RAM, 120 GB disk, BMC enabled, PXE boot
- 1 bootstrap VM: 4 vCPU, 16 GB RAM, 120 GB disk, BMC enabled, PXE boot
- 1 BMC network
- 1 cluster network (DHCP)
- 1 LB node (frontends: 6443, 443, 80, 22623)

### Compact 3-Node Template
- 3 VM nodes: 8 vCPU, 16 GB RAM, 120 GB disk each, BMC enabled, PXE boot
- 1 bootstrap VM: 4 vCPU, 16 GB RAM, 120 GB disk, BMC enabled, PXE boot
- 1 BMC network
- 1 cluster network (DHCP)
- 1 LB node (frontends: 6443, 443, 80, 22623)
- DNS record templates on LB node

### Standard 3+2 Template
- 3 CP nodes: 4 vCPU, 16 GB RAM, 120 GB disk, BMC enabled
- 2 worker nodes: 4 vCPU, 16 GB RAM, 120 GB disk, BMC enabled
- 1 bootstrap VM: 4 vCPU, 16 GB RAM, 120 GB disk, BMC enabled
- 1 BMC network
- 1 cluster network (DHCP)
- 1 LB node (frontends: 6443, 443, 80, 22623)
- DNS record templates on LB node

Templates stored as JSON bundled with Troshka or in the DB as system-level entries. "New Project from Template" option in UI.

## 6. Host Sizing Recommendations

### Golden Image Build (includes bootstrap)

| Profile | Minimum EC2 | Recommended EC2 | Storage Volume |
|---------|-------------|-----------------|----------------|
| SNO | `m8i.4xlarge` (16/64) | `m8i.4xlarge` | 300 GB |
| Compact 3 | `m8i.8xlarge` (32/128) | `m8i.8xlarge` | 600 GB |
| Standard 3+2 | `m8i.8xlarge` (32/128) | `m8i.12xlarge` (48/192) | 800 GB |

### Stamp-Out (steady-state, no bootstrap)

| Profile | Minimum EC2 | Storage Volume |
|---------|-------------|----------------|
| SNO | `m8i.2xlarge` (8/32) | 200 GB |
| Compact 3 | `m8i.4xlarge` (16/64) | 500 GB |
| Standard 3+2 | `m8i.8xlarge` (32/128) | 750 GB |

### Storage Efficiency

Pattern stamp-out uses qcow2 backing files. Each clone stores only the delta from the golden image. 60 compact 3-node clusters consume ~3-5 TB actual storage (not 60 × 360 GB = 21.6 TB). With FSx shared storage, backing files are shared across all hosts in the pool.

### Overcommit Guidance

Default 4x CPU / 1.5x RAM overcommit works for general VMs. For OCP, recommend sizing hosts so OCP VMs fit within 1.0x RAM (no memory overcommit) — etcd is highly sensitive to memory pressure. CPU overcommit is fine.

## 7. End-to-End Flows

### Stamp-Out Flow

```
User clicks "Deploy from Pattern"
  → UI collects: pattern, host/pool, DNS provider, GUID, domain
  → POST /api/projects/deploy-from-pattern
  → Backend:
    1. Clone topology from pattern (remap IDs, MACs, disk IDs)
    2. Store guid, domain, dns_provider_id on Project
    3. Placement: select host with enough capacity
    4. Create disks from pattern snapshots (qcow2 backing files)
    5. Setup networks: VXLAN, bridges, dnsmasq
    6. Setup BMC: vbmcd + sushy
    7. Setup LB: generate haproxy.cfg, start HAProxy in namespace
    8. Define + start VMs
    9. Assign EIP, configure port forwards for LB ports
    10. Create DNS records via DNS provider
  → VMs boot with pre-installed OCP
  → External Ansible (post-deploy):
    - Issue LE/ZeroSSL certs for *.apps.{guid}.{domain}
    - Update OCP ingress domain + inject certs
  → Cluster ready at console-openshift-console.apps.{guid}.{domain}
```

### Teardown Flow

```
User clicks "Destroy Project"
  → Backend:
    1. Delete DNS records via DNS provider (before EIP release)
    2. Stop VMs
    3. Teardown BMC
    4. Teardown LB: kill HAProxy, remove config
    5. Teardown networks
    6. Release EIP
    7. Delete disks
```

## 8. New Components

| Component | Location | Purpose |
|-----------|----------|---------|
| LB network node (frontend) | `src/frontend/` | Canvas node type, LB config panel |
| LB network node (backend schemas) | `src/backend/app/schemas/` | Frontend/backend pool definitions |
| HAProxy manager (troshkad) | `src/troshkad/troshkad.py` | Config gen, process lifecycle in namespace |
| DNS Provider model | `src/backend/app/models/dns_provider.py` | Provider credentials + type |
| DNS Provider API | `src/backend/app/api/dns_providers.py` | CRUD for admin UI |
| DNS service | `src/backend/app/services/dns_service.py` | NSUPDATE/Route53 dispatch |
| Topology templates | `src/backend/app/services/templates.py` | SNO/Compact/Standard skeletons |
| Deploy-from-pattern additions | `src/backend/app/services/deploy_service.py` | GUID, domain, DNS provider params |

## 9. What Doesn't Change

- VM definition, disk creation, qcow2 handling
- Pattern save/restore and ID remapping
- BMC endpoints (vbmcd, sushy-emulator)
- Network namespaces, VXLAN, dnsmasq
- EIP allocation/release
- Canvas core (React Flow, Zustand store)
- Existing network node types (standard, gateway, BMC)

## 10. Out of Scope (External Ansible)

- OCP IPI installation on first golden image build
- Post-stamp-out cert issuance (LE/ZeroSSL)
- Post-stamp-out OCP domain reconfiguration
- OCP version-specific logic

## 11. Build Order

1. **LB node (troshkad + backend)** — HAProxy lifecycle, config generation, new endpoints
2. **LB node (frontend)** — canvas node type, config panel, edge-based backend wiring
3. **DNS Provider model + API** — CRUD, admin UI for managing providers
4. **DNS service** — NSUPDATE implementation, deploy/teardown hooks
5. **Deploy-from-pattern additions** — GUID, domain, DNS provider fields on Project, deploy API extension
6. **Topology templates** — SNO/Compact/Standard starter skeletons in UI
7. **Golden image build** — install OCP on Troshka, save as pattern, validate stamp-out

Steps 1-2 are independently useful (general-purpose LB). Steps 3-5 are independently useful (DNS for any project). Step 6 is convenience. Step 7 is the payoff.
