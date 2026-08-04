# Network Mesh: Multi-Host & Hybrid Provider Networking

## Problem

A single project's VMs are currently confined to one host (troshkad) or one cluster (KubeVirt native). Large labs that exceed a single host's capacity cannot be deployed, and hybrid labs spanning troshkad hosts and KubeVirt clusters are not possible.

## Solution

Project-scoped WireGuard mesh with VXLAN overlay. When a project spans multiple hosts or providers, the backend generates per-project WireGuard tunnels between all participating hosts/clusters. VXLAN runs inside the WireGuard tunnels, providing full L2 adjacency — VMs on different hosts behave as if they're on the same switch. ARP, broadcast, DHCP, PXE all work transparently.

**Traffic path:** VM → bridge → VXLAN → WireGuard → wire → WireGuard → VXLAN → bridge → VM

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Tunnel technology | WireGuard | Works across any boundary (same VPC, cross-cloud, cross-provider). Encrypted by default — required for public SaaS. |
| Mesh scope | Per-project | Clean lifecycle (create/destroy with project). Full isolation between projects. No stale state. |
| L2 overlay | VXLAN over WireGuard | VXLAN already implemented for single-host. Provides broadcast/multicast needed by PXE, ARP, DHCP. |
| Network services location | Pinned to one host | dnsmasq, chronyd, nftables NAT stay on one "network host." Remote hosts have VXLAN+bridge only. Avoids split-brain DHCP. |
| Placement strategy | Hybrid (auto-fit + affinity) | Bin-packing by default, optional affinity groups for co-location constraints. |
| KubeVirt integration | mesh-bridge Deployment | Privileged pod running userspace WireGuard + VXLAN, bridges to OVN NADs via Multus. |
| Key management | Backend-orchestrated | Backend generates keypairs, stores encrypted, pushes to hosts/operators. Consistent with existing architecture. |

## Data Model

### New Model: `ProjectMeshPeer`

Stores WireGuard state for each host/cluster participating in a multi-host project.

```
project_mesh_peers
├── id (UUID PK)
├── project_id (FK → projects)
├── host_id (FK → hosts, nullable — null for KubeVirt gateway pods)
├── provider_id (FK → providers, nullable — set for KubeVirt peers)
├── peer_type ("troshkad" | "kubevirt")
├── wg_public_key (String)
├── wg_private_key (String, Fernet-encrypted)
├── wg_endpoint (String — "ip:port")
├── wg_address (String — "10.252.x.y/24")
├── wg_port (Integer — unique per host per project)
├── is_network_host (Boolean)
├── created_at (DateTime)
```

### Project Model Changes

```python
mesh_subnet_id: Mapped[int | None]       # /24 from 10.252.0.0/16, null = single-host
mesh_network_host_id: Mapped[str | None]  # FK → hosts, the pinned network host
host_assignments: Mapped[dict | None]     # JSONB: {vm_node_id: host_id}
```

### Transit Subnet Allocation

Each multi-host project gets a `/24` from `10.252.0.0/16`. Third octet from monotonic counter (`mesh_subnet_id`). 254 hosts per project, 256 concurrent multi-host projects. Expandable to `/14` (1024 projects) if needed.

### WireGuard Port Allocation

Starting at UDP `51820`, incrementing per active project on each host. Backend tracks via `ProjectMeshPeer` table.

### Key Lifecycle

- **Generated** by the backend at deploy time (Curve25519 via `wg genkey`/`wg pubkey`)
- **Stored** Fernet-encrypted in DB (same pattern as `RegistryCredential.password`)
- **Pushed** to hosts via troshkad `/mesh/setup` or Kubernetes Secret (operator)
- **Destroyed** on project destroy (cascade delete)

## Placement & Deploy Flow

### Placement Changes

1. **Resource calculation** — sum vCPU + RAM from topology
2. **Single-host attempt** — if everything fits on one host, deploy as today (no mesh)
3. **Multi-host bin packing** — first-fit decreasing by RAM across hosts in the same storage pool
   - Respect `affinityGroup` annotations: VMs with the same group land on the same host
   - Return `Dict[host_id, List[vm_node_id]]`
4. **Network host selection** — host with the most VMs (or the gateway VM's host)

### Affinity Groups

Optional field on VM topology nodes:

```json
{
  "data": {
    "affinityGroup": "ocp-workers"
  }
}
```

VMs with the same group are co-located. VMs without the field are placed freely.

### Template YAML Support

The `from-template` API (used by agnosticd-v2) supports multi-host via topology:

```yaml
vms:
  hub:
    cpus: 16
    memory_gb: 64
    affinity_group: hub
  worker-1:
    cpus: 8
    memory_gb: 32
    affinity_group: ocp-workers
  worker-2:
    cpus: 8
    memory_gb: 32
    affinity_group: ocp-workers
```

No explicit "multi-host: true" flag needed — placement automatically goes multi-host when the total resource requirements exceed a single host. `affinity_group` in the template YAML maps to `affinityGroup` on the topology node data. Templates without `affinity_group` get unconstrained bin-packing.

### Multi-Host Deploy Orchestration

1. **Placement** → `{host_a: [vm1, vm2], host_b: [vm3, vm4]}`, network_host = host_a
2. **Mesh setup** — generate keypairs, push WireGuard config to all hosts in parallel via `POST /mesh/setup`, wait for tunnel confirmation
3. **Network setup on network host** — existing `_setup_networks_via_troshkad()`, VXLAN remote IPs are WireGuard tunnel IPs (e.g., `10.252.3.2`) instead of real host IPs
4. **VXLAN-only setup on remote hosts** — `POST /mesh/join-network`: namespace + VXLAN + bridge only, no dnsmasq/nftables/chronyd
5. **VM deploy** — each host deploys only its assigned VMs in parallel
6. **Progress tracking** — per-host progress merged via Redis pub/sub

### Deploy Failure

If mesh setup fails on any host, tear down WireGuard on all successful hosts, set project to error state. No partial deploys.

## Troshkad Endpoints

### `POST /mesh/setup`

Create a WireGuard interface for a project.

```json
{
  "project_id": "abc-123",
  "wg_private_key": "...",
  "wg_address": "10.252.3.1/24",
  "wg_port": 51820,
  "peers": [
    {
      "public_key": "...",
      "endpoint": "10.0.1.52:51821",
      "allowed_ips": "10.252.3.2/32"
    }
  ]
}
```

Handler:
- `ip link add wg-{pid[:8]} type wireguard`
- Write config to `/var/lib/troshka/mesh/{project_id}.conf`
- `wg setconf wg-{pid[:8]} ...`
- `ip addr add`, `ip link set up`
- Verify connectivity by pinging each peer

### `POST /mesh/join-network`

Set up VXLAN + bridge on a remote host (non-network host).

```json
{
  "project_id": "abc-123",
  "networks": [
    {
      "vni": 1042,
      "bridge": "br-1042",
      "wg_peer_ips": ["10.252.3.1", "10.252.3.3"]
    }
  ]
}
```

Handler:
- Create project network namespace `troshka-{pid}`
- Per network: VXLAN (local = this host's WG IP, FDB entries → peer WG IPs), bridge, attach VXLAN
- Dummy bridge in host namespace (for libvirt XML validation)
- No dnsmasq, nftables, chronyd, or gateway

### `DELETE /mesh/teardown`

Remove WireGuard interface and config for a project.

### `GET /mesh/status`

Return WireGuard handshake timestamps and peer status for health monitoring.

### Existing Endpoint Changes

`POST /networks/full-setup` — unchanged. VXLAN remote IPs are WireGuard tunnel IPs instead of real host IPs, transparent to the handler.

### Host Requirements

- `wireguard-tools` package added to agent install script (available in RHEL 8.6+ and Amazon Linux 2023 base repos)
- Mesh config directory: `/var/lib/troshka/mesh/`
- `/mesh/setup` and `/mesh/join-network` added to `_SKIP_DRAIN`

## KubeVirt Hybrid Integration

### mesh-bridge Deployment

New pod type for hybrid projects — bridges WireGuard tunnels to OVN layer2 NADs.

- **Userspace WireGuard** (`boringtun` or `wireguard-go`) — pods can't load kernel modules
- Creates VXLAN interfaces over the WireGuard tunnel
- Bridges each VXLAN to the corresponding OVN NAD interface
- Attached to all project NADs via Multus annotations
- Needs `NET_ADMIN` + `privileged: true` (same as gateway pod)
- One Deployment per hybrid project

Container image: `quay.io/redhat-gpte/troshka-mesh-bridge`
Containerfile: `src/operator/images/mesh-bridge/`

### TroshkaProject CRD Changes

New `spec.mesh` section:

```yaml
spec:
  mesh:
    enabled: true
    role: "remote"  # or "network-host"
    wireguard:
      privateKeySecretRef: "mesh-wg-{project}"
      address: "10.252.3.4/24"
      port: 51820
      peers:
        - publicKey: "..."
          endpoint: "10.0.1.52:51820"
          allowedIPs: "10.252.3.1/32"
    networks:
      - vni: 1042
        nadName: "troshka-net-1042"
        peerIPs: ["10.252.3.1", "10.252.3.2"]
```

Private key stored in a Kubernetes Secret, referenced by name.

### Operator Handler Changes

When `spec.mesh.enabled`:
1. Create WireGuard Secret
2. Deploy mesh-bridge Deployment
3. Wait for tunnel connectivity
4. Proceed with dnsmasq/gateway/VM creation

When `spec.mesh.role == "remote"`:
- Skip dnsmasq, chronyd, NAT setup (network host handles these)

When `spec.mesh.role == "network-host"`:
- KubeVirt side runs dnsmasq/gateway as today, plus mesh-bridge for tunnels to troshkad peers

### Backend Hybrid Detection

Deploy service detects hybrid when placement returns hosts of mixed `host_type`. Flow:
1. Generate keypairs for all participants
2. Push WireGuard to troshkad hosts via `/mesh/setup`
3. Update TroshkaProject CR with `spec.mesh`
4. Wait for all tunnels up
5. Network setup → VM deploy

Network host preference: troshkad host when available (more mature network namespace stack).

## State Polling, Console Routing, Recovery

### State Polling

WS poller (`ws_pubsub.py`) already iterates per-host. For multi-host projects, it queries each host in `host_assignments` and merges VM states into one `project-state` message. No fundamental change.

### VNC Console Routing

`GET /projects/{id}/console-token` takes `vm_id`. Backend looks up `host_assignments[vm_id]` to find the correct host, signs JWT with that host's agent token. No change to troshka-vncd or frontend noVNC.

### WireGuard Health Monitoring

Health poller gains a new check: `GET /mesh/status` on hosts with active mesh peers. Flags degraded if no WireGuard handshake in >3 minutes. Warnings stored on Host model (same as `storage_warnings`).

### Host Disconnect Recovery

`recover_host_services()` restores WireGuard interfaces from stored configs at `/var/lib/troshka/mesh/`, then re-establishes VXLAN tunnels.

### Teardown

1. Stop VMs on all hosts in parallel
2. Tear down VXLAN on remote hosts
3. Tear down networks on network host (existing `/networks/full-teardown`)
4. Tear down WireGuard on all hosts (`DELETE /mesh/teardown`)
5. Cascade delete `ProjectMeshPeer` records

### Live Migration Interaction

Both source and target hosts already have VXLAN + WireGuard interfaces. VM migration moves the TAP to the target host's bridge. Update `host_assignments` after migration completes. No WireGuard reconfiguration needed.

## Security

### Firewall Rules for WireGuard UDP

Added during pool setup (not per-project):

| Provider | Rule |
|----------|------|
| AWS | Security group: UDP 51820-51850, source = pool SG |
| GCP | Firewall: UDP 51820-51850, target tag `troshka-host`, source tag `troshka-host` |
| Azure | NSG: UDP 51820-51850, source = VNet |
| KubeVirt | `hostPort` on mesh-bridge pod |

### Security Properties

- **Per-project isolation** — separate WireGuard interfaces and keys per project, `AllowedIPs` restricts each peer to its tunnel subnet
- **Encrypted in transit** — ChaCha20-Poly1305 (WireGuard), even within same VPC
- **Encrypted at rest** — private keys Fernet-encrypted in DB, Kubernetes Secrets for operator
- **No long-lived tunnels** — mesh created/destroyed with project lifecycle
- **Forward secrecy** — WireGuard Noise protocol
- **Private keys never logged** — never in API responses, deleted from host FS on teardown

## Phasing

### Phase 1: Multi-Host Within a Pool (troshkad only)

- `ProjectMeshPeer` model + migration
- Project model fields (`mesh_subnet_id`, `mesh_network_host_id`, `host_assignments`)
- WireGuard key generation
- Placement bin-packing with affinity groups
- Troshkad endpoints: `/mesh/setup`, `/mesh/join-network`, `/mesh/teardown`, `/mesh/status`
- Deploy service multi-host orchestration
- Multi-host teardown
- State poller aggregation across hosts
- VNC console routing by VM
- Health poller WireGuard checks
- Host disconnect recovery for mesh
- `wireguard-tools` in agent install
- Security group rules in pool setup
- Affinity group field on VM topology + canvas UI dropdown

### Phase 2: KubeVirt Hybrid

Depends on Phase 1.

- mesh-bridge container image + Containerfile
- TroshkaProject CRD `spec.mesh` schema
- Operator handler for mesh-bridge Deployment + Secret
- Backend hybrid detection in placement
- Deploy service hybrid flow
- Hybrid teardown

### Phase 3: Cross-Cluster KubeVirt

Depends on Phase 2.

- Placement across multiple KubeVirt providers
- mesh-bridge to mesh-bridge tunneling
- Network host selection for KubeVirt-only multi-cluster

### Phase 4 (Future): Inter-Project Networking

Out of scope. Would require a "peering" concept linking projects' networks with policy controls. The WireGuard mesh from Phases 1-3 provides the transport layer.

## What Stays Unchanged

- Single-host projects — no mesh, no WireGuard, existing flow untouched
- Single-cluster KubeVirt projects — OVN NADs, no mesh
- Network namespace architecture on the network host
- dnsmasq, chronyd, nftables — unchanged, serving remote VMs over VXLAN
- VXLAN — same kernel feature, different remote endpoint IPs
- VNI allocation — already globally unique
- Pattern save/restore — topology JSONB unchanged, `host_assignments` is runtime state
