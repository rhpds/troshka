# Project Ceph in a Box — Design

**Date:** 2026-09-15
**Status:** Approved (design)
**Scope:** Troshka operator + backend + frontend for KubeVirt-native projects. Delivers shared block storage (Rook-Ceph) in the project namespace and exports credentials for workloads/Ansible to consume. Does **not** auto-provision nested OpenShift `StorageCluster` CRs (v1).

---

## 1. Motivation

KubeVirt projects often need Ceph-backed storage for nested OpenShift (ODF external mode), Forklift/MTV storage maps, or future VM RBD use — without depending on a pre-provisioned RHDP shared Ceph cluster.

Today demos (e.g. CCLM) stall waiting for a hand-supplied `rook-ceph-external-cluster-details` secret. **Project Ceph in a box** makes shared storage a first-class Troshka infra component (like dnsmasq and gateway): one optional node per project, credentials stamped into topology, consumed by workloads and the ops pod.

**Provider constraint:** KubeVirt-native projects only. troshkad / ocpvirt paths are out of scope for v1.

**Layering honesty:** OSDs are backed by PVCs on the host cluster’s ODF (`ocs-storagecluster-ceph-rbd-virtualization`). This is Ceph-on-Ceph — acceptable for single-user demos; not a production storage tier.

---

## 2. Goals and non-goals

### Goals

- Palette + template support for one **Project Ceph** node per project
- Operator deploys minimal but real Rook-Ceph (configurable OSD count, default 3) in `troshka-<project>`
- Mons reachable on project lab L2 (Multus NAD + user-configurable IP, default `.3`)
- Export ODF-compatible external cluster secret + stamped topology fields for consumers
- Inject credentials into workload runner and ops pod mounts
- Deploy gate: storage-dependent steps wait until Ceph is `Ready`

### Non-goals (v1)

- Auto-creating `StorageCluster` / copying secrets into nested OCP (`autoAttachOcpClusters` — deferred)
- troshkad or ocpvirt provider support
- Multiple Ceph clusters per project
- Direct VM RBD attach (future)
- Replacing host ODF for VM disk backing

---

## 3. User model

### Palette

New item under **Storage** (distinct from VM **Disk** / **ISO**):

| Label | Node type | Visibility |
|-------|-----------|------------|
| Project Ceph | `cephClusterNode` | `providerType === kubevirt` only |

**Limit:** max one `cephClusterNode` per project (same pattern as gateway).

### Canvas properties

| Field | Default | Notes |
|-------|---------|-------|
| `name` | `project-ceph` | Display name |
| `networkRef` | (required) | `networkNode` id — lab L2 mons attach here |
| `labIp` | `<network-cidr>.3` | User overridable; convention `.1` gateway, `.2` dnsmasq, `.3` ceph |
| `capacityGi` | `300` | Total raw OSD backing; split evenly across `osdCount` PVCs |
| `osdCount` | `3` | Number of OSD pods (1–6); drives pool replication (see below) |
| `linkedClusters` | (edges) | `clusterNode` ids that receive credentials in topology stamp |

**Validation:**

- `labIp` must be in `networkRef` CIDR and not collide with gateway (`.1`), dnsmasq (`.2`), or stamped static leases
- `osdCount` integer **1–6** (default **3**)
- `capacityGi` minimum **`osdCount × 50`** Gi (per-OSD floor 50 Gi)
- `networkRef` network must be attached to all `linkedClusters` (or a shared network they can route to)

**Replication:** operator sets block-pool `replicated size = min(osdCount, 3)` — 1 OSD → size 1 (no HA, tiny lab only), 2 → size 2, 3+ → size 3. UI shows effective replica size when `osdCount` changes.

### Templates

```yaml
cephCluster:
  name: project-ceph
  network: cluster          # network node name/id
  labIp: 10.0.0.3           # optional; default .3 on CIDR
  capacityGi: 300
  osdCount: 3               # optional; default 3
  clusters: [source, destination]   # ocp cluster names → linkedClusters
```

Example: `ocp-cclm.yaml` gains a `cephCluster` block; CCLM workloads read stamped credentials instead of manual extra_vars.

---

## 4. Architecture

```
[KubeVirt host OCP — openshift-storage]
    └── ODF backs VM PVCs + Project Ceph OSD PVCs

[troshka-<project> namespace]
    ├── dnsmasq (.2) / gateway (.1) / ops pod / virt-launchers
    └── rook-ceph (TroshkaCeph reconcile)
            ├── mon/mgr on lab NAD → labIp (.3)
            ├── N× OSD pods (PVC-backed, N = osdCount, host failureDomain)
            └── Secret troshka-ceph-external (ODF external format)

[nested OCP cluster(s) on lab L2]
    └── Workloads/Ansible create StorageCluster external mode
        using credentials from topology / mounted secret
```

**Traffic split:**

- **Client/MON** traffic: lab L2 (`labIp`) — nested OCP nodes reach mons here
- **OSD replication**: host pod network — stays off the nested lab overlay for better demo performance

---

## 5. Operator — `TroshkaCeph` CR

New CRD `troshkancephs.troshka.redhat.com` (v1alpha1), one per project max.

### Spec

```yaml
spec:
  cephId: string          # topology node id
  networkNad: string      # cluster NAD name in project ns
  labIp: string           # e.g. 10.0.0.3
  labPrefixLength: int    # from network CIDR
  capacityGi: int         # total; operator divides by osdCount for OSD PVCs
  osdCount: int           # default 3, range 1–6
  replicateSize: int     # computed: min(osdCount, 3); stored for status/UI
  linkedClusterIds: []      # ocp cluster topology ids (metadata only v1)
  storageClassName: string  # default troshka-ceph-rbd
```

### Reconcile (ordered)

1. Create/update `osdCount` OSD backing PVCs (`ocs-storagecluster-ceph-rbd-virtualization`, RWO, `capacityGi/osdCount` each)
2. Deploy Rook `CephCluster` (namespace-scoped) with lab-tuned sizing (`storageClassDeviceSets[0].count = osdCount`)
3. Attach mon (+ mgr) to Multus NAD; init container or rook network config sets `labIp`
4. Create CephBlockPool `replicated size=min(osdCount, 3)`
5. Export external cluster details → Secret `troshka-ceph-external`
6. Optional: dnsmasq `address=/ceph.<domain>/<labIp>` if network has DNS enabled
7. Update status

### Rook tuning (demo-balanced)

| Component | Count | Notes |
|-----------|-------|-------|
| MON | 1 | Lab quorum; saves RAM (independent of osdCount) |
| MGR | 1 | Standard |
| OSD | `osdCount` (default 3) | User-tunable 1–6 |
| Pool | `replicated size=min(osdCount, 3)` | Ceph requires size ≤ osdCount |

**osdCount guidance (UI hints):**

| osdCount | Replication | Use case |
|----------|-------------|----------|
| 1 | 1× | Minimal smoke test; no redundancy |
| 2 | 2× | Tight clusters; mirrored |
| 3+ | 3× | Default; best match for CCLM / Forklift demos |

**Resources (starting point):** mon 1 CPU / 2 Gi; each OSD 2 CPU / 4 Gi — tune in operator constants after first live deploy.

**Placement:** pod anti-affinity prefer spread across KubeVirt worker nodes; tolerate co-location on small clusters.

**Prerequisite:** Rook operator cluster-scoped on host (present when host runs ODF).

### Status

```yaml
status:
  phase: Pending | Progressing | Ready | Failed
  message: string
  monEndpoint: string       # labIp:6789
  secretName: troshka-ceph-external
  storageClassName: troshka-ceph-rbd
  poolName: string
  conditions: []
```

---

## 6. Credential export and topology stamp

When `status.phase == Ready`, backend stamps `project.topology.projectCeph` (and `deployed_topology` mirror):

```yaml
projectCeph:
  cephId: ceph-1
  secretName: troshka-ceph-external
  secretNamespace: troshka-b57b2bab    # project ns on host
  monHost: 10.0.0.3
  monPort: 6789
  storageClassName: troshka-ceph-rbd   # name consumers use in StorageCluster / maps
  poolName: troshka-ceph-pool
  osdCount: 3
  replicateSize: 3
  linkedClusters: [source, destination]
  # Keys for generic workload extra_vars (not CCLM-specific):
  troshka_ceph_secret_name: rook-ceph-external-cluster-details
  troshka_ceph_mon_host: 10.0.0.3
```

Secret `troshka-ceph-external` contains the standard Rook export blob ODF expects. Workloads that create `rook-ceph-external-cluster-details` in nested `openshift-storage` copy from this (Ansible task in consumer role, not Troshka deploy).

### Consumer contract (Option A — credentials only)

Troshka **does not** create nested `StorageCluster` CRs. Consumers:

1. Read stamped `projectCeph` (API topology or workload `extra_vars` merge)
2. Copy/create external secret in nested cluster namespace
3. Apply external `StorageCluster` (demo_workloads, catalog ansible, or custom playbook)

CCLM `troshka_workload_cclm_network` maps `cclm_ceph_secret_name` from `troshka_ceph_secret_name` via workload extra_vars merge in `run_service`.

---

## 7. Credential delivery paths

| Path | Mechanism |
|------|-----------|
| **Topology API** | `project.topology.projectCeph` after Ready |
| **Workload runner** | `run_service` merges `projectCeph` into `extra_vars`; mounts host secret at `/workdir/project-ceph/` (read-only) |
| **Ops pod** | Same secret mount at `/workdir/project-ceph/` for post-install scripts |
| **Workloads gate** | Optional `requiresProjectCeph: true` on run → fail fast if not Ready |

Runner pod copies secret **content** into generated files (e.g. `ceph-external-details.json`) so ansible `kubernetes.core.k8s` can apply to nested API without host-cluster secret RBAC on nested clusters.

---

## 8. Deploy ordering

```
Networks/NADs → dnsmasq → gateway → TroshkaCeph (Ready) → OCP install → workloads
```

- `TroshkaCeph` reconcile starts after project namespace + NADs exist
- OCP install does not require Ceph
- Workloads that need storage should check `projectCeph.phase` (or mount precondition)

Destroy: `TroshkaCeph` finalizer triggers graceful Rook cleanup (mirror `TroshkaNetwork` teardown patterns).

---

## 9. Frontend / backend touchpoints

### Frontend

- `Palette.tsx` — item + KubeVirt gate
- `canvasStore.ts` — node type, validation, one-per-project limit
- `CephClusterNode.tsx` — new node component
- `PropertiesPanel.tsx` — network, labIp, capacityGi, osdCount, linked clusters
- Edge rules: `cephClusterNode` → `networkNode`; `cephClusterNode` → `clusterNode` (consumer link)

### Backend

- `template_loader.py` — import `cephCluster` block
- `deploy_service.py` — create `TroshkaCeph` CR, wait/poll Ready, stamp topology
- `deploy_topology.py` — validation helpers (IP collision, min capacity)
- `run_service.py` — merge `projectCeph` into workload extra_vars; mount secret on runner
- `ops_pod_scaffold.py` — optional secret volume mount

### Operator

- `crds/troshkanceph.yaml`
- `handlers/ceph.py` (or `handlers/project.py` integration)
- `helpers/rook_ceph.py` — manifest builders (CephCluster, pools, multus mon)
- Tests in `test_operator_handlers.py`

---

## 10. Phased implementation

| Phase | Deliverable |
|-------|-------------|
| **1** | CRD + operator reconcile + Ready/Failed status + secret export |
| **2** | Palette, node, validation, template import |
| **3** | Deploy stamp, workload/ops-pod injection, deploy gate |
| **4** | CCLM template + `demo_workloads` credential mapping; docs |
| **5** (future) | `autoAttachOcpClusters`, VM RBD consumer, metrics in UI |

---

## 11. Risks and mitigations

| Risk | Mitigation |
|------|------------|
| Ceph-on-Ceph latency | Document; size for demo; OSD traffic on host network |
| Small KubeVirt clusters (3 nodes) | Anti-affinity prefer; allow co-located OSDs |
| Rook operator missing on host | Pre-flight in deploy; clear error |
| labIp collisions | Validate against gateway/dnsmasq/leases |
| Secret sprawl | One secret name per project; namespace-scoped RBAC |
| Destroy stuck PVCs | Finalizer + documented manual cleanup escape hatch |

---

## 12. Open items (post-v1)

- `autoAttachOcpClusters: true` on node → deploy creates nested `StorageCluster`
- Performance profiling on ocpvdev01; adjust CPU/mem requests
- UI status widget (phase, capacity, mon health)
- Integration test in live-env harness

---

## 13. References

- CCLM design: `docs/superpowers/specs/2026-09-14-cclm-catalog-item-design.md` (§6 external Ceph → superseded by this for KubeVirt labs)
- KubeVirt provider: `docs/superpowers/specs/2026-07-11-kubevirt-native-provider-design.md`
- dnsmasq/gateway lab IP conventions: `src/operator/helpers/k8s.py`, `src/operator/helpers/dnsmasq.py`
