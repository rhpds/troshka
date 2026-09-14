# CCLM Catalog Item — Design

**Date:** 2026-09-14
**Status:** Approved (design)
**Scope:** Troshka + demo_workloads + agnosticv catalog item for a Cross-Cluster Live Migration (CCLM) demo lab.

---

## 1. Motivation

Reproduce the RHDP CCLM environment (CNV decentralized live migration + MTV/Forklift) as a Babylon/AgnosticD catalog item on Troshka. Students get two nested OCP clusters, post-install operators, Forklift providers/maps, and sample VMs — then run an MTV live migration during the demo.

This replaces a manually built `zf4ws` / `bqc4v` pair with a repeatable catalog deploy.

## 2. Topology (differs from production notes)

| | Production notes | Catalog item |
|---|------------------|--------------|
| Source | 3 CP + 3 workers | **SNO + 2 workers** (1 CP, 2 workers) |
| Destination | 3 CP + 3 workers | **SNO** (1 CP, 0 workers) |
| Provider | Bare metal | **KubeVirt native** (Troshka `kubevirt` provider) |
| Storage | External Ceph (shared) | Same — **pre-provisioned external Ceph** (not created by Troshka) |

## 3. Networks

| Network | CIDR | Attached to | Purpose |
|---------|------|-------------|---------|
| `cluster` | `10.0.0.0/24` | **both** clusters NIC0 | Shared machine network; per-cluster API/Ingress VIPs + DNS |
| `migration` | `172.16.100.0/24` | **both** clusters NIC1 | Shared L2 for node + lm-network traffic |
| `bmc` | `192.168.100.0/24` | BMC (Troshka auto) | Redfish / agent install |

Machine-network IPs: source SNO `.10`, workers `.20`–`.21`; destination SNO `.110`. Both `base_domain`s share one L2; dnsmasq carries FQDN records for each.

### Migration L2 address plan (explicit in template — no auto-assign)

| Address / range | Owner |
|-----------------|-------|
| `172.16.100.10` | source `cp-0` migration NIC |
| `172.16.100.20`–`.21` | source `wrk-0`, `wrk-1` |
| `172.16.100.110` | dest `cp-0` migration NIC |
| `172.16.100.100`–`.109` | source MetalLB pool (`virt-sync-lb`, etc.) |
| `172.16.100.200`–`.209` | dest MetalLB pool |
| `172.16.100.224/28` | source whereabouts (lm-network NAD) |
| `172.16.100.240/28` | dest whereabouts (lm-network NAD) |

Cluster machine networks use high-end static IPs (SNO VIP = node IP). Migration NICs use the low `.10`/`.20` slice so MetalLB/whereabouts upper blocks never collide.

## 4. Clusters

```yaml
ocp:
  - name: source
    type: sno
    workers: 2
    base_domain: source.cclm.local
    ocp_version: "4.21"
    role: source                    # catalog metadata → workload vars
    vm_namespace: source-cluster
  - name: destination
    type: sno
    workers: 0
    base_domain: dest.cclm.local
    ocp_version: "4.21"
    role: destination
    vm_namespace: destination-cluster
```

- **Source** runs migrated-from VMs (`source-cluster` namespace).
- **Destination** receives migrated VMs (`destination-cluster` namespace).
- Both clusters install in parallel via ops pod (`install_via: pod`).

## 5. Provider constraints

- **KubeVirt native only** — OVN secondary networks for shared migration L2; MetalLB EIP tracking; ops-pod install path.
- Every RHCOS member gets `nestedVirt: true` (host-passthrough CPU).
- Placement must target `host_type=kubevirt-cluster` (new `requiresKubevirt` template flag).

## 6. Post-install (demo_workloads — not Troshka core)

Workloads run after both clusters reach control-plane-usable (recert gate). Chain:

1. **Operators** — CNV, MTV, ODF (external), Submariner, nmstate, cert-manager, MetalLB (OLM subscriptions on both clusters).
2. **HCO** — `decentralizedLiveMigration: true`, `liveMigrationConfig.network: lm-network`, timeouts from notes.
3. **Forklift** — `feature_ocp_live_migration: true`, cross-cluster providers, NetworkMap (pod→pod), StorageMap → `ocs-external-storagecluster-ceph-rbd`.
4. **Migration network** — lm-network NAD (macvlan), NNCPs for migration NIC static IPs, MetalLB pools.
5. **Submariner** — broker on source, join both clusters.
6. **Seed VMs** (optional) — sample VMs in `source-cluster` namespace.

External Ceph connection secret is supplied via catalog/workload vars (not provisioned by Troshka).

## 7. Showroom

- Content repo: `showroom-troshka-cclm` (new) or module in existing OCP base repo.
- Tabs: dual-cluster console proxy, cluster terminal (`target: clusters`), MTV UI link, lab instructions.
- Documents known issues: `sync-role=leader` LB bug, stale VMI after failed migration.

## 8. Development workflow — `demo_workloads` branch

All CCLM Ansible roles live on a feature branch in `github.com/rhpds/demo_workloads` (e.g. `feat/cclm-workloads`), **not** `main`, until the lab is validated.

Troshka workload runs pin that branch via `requirements_content` on the `WorkloadRun` (ad-hoc) or in the agnosticv catalog item's merged vars:

```yaml
requirements_content:
  collections:
    - name: https://github.com/rhpds/demo_workloads.git
      type: git
      version: feat/cclm-workloads
```

Example ad-hoc API call while iterating:

```bash
curl -sS -X POST "http://localhost:8200/api/v1/projects/${PROJECT_ID}/workloads" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "kind": "ad_hoc",
    "role_fqcn": "rhpds.demo_workloads.troshka_workload_cclm_operators",
    "requirements_content": {
      "collections": [{
        "name": "https://github.com/rhpds/demo_workloads.git",
        "type": "git",
        "version": "feat/cclm-workloads"
      }]
    },
    "target_map": {"mode": "cluster", "clusters": ["source", "destination"]}
  }'
```

Push early and often to the remote branch so the runner pod (which clones from GitHub, not the laptop) sees changes. No PRs needed during dev — merge `feat/cclm-workloads` → `main` only after live validation.

**Troshka** infra changes land on **`main`** directly so the local dev environment (backend restart, template hot-load) picks them up without a feature branch.

## 9. Catalog item shape (agnosticv)

```
troshka/
  cclm/
    infra_template.yaml      # #include → troshka ocp-cclm template
    config.yaml              # troshka_deploy_mode: template
    software_workloads.yml   # ordered demo_workloads roles
```

Deploy mode: `template` (infra + workloads in one shot) for first iteration; `pattern` + `pattern_workloads` as follow-up after bake.

## 10. Out of scope (v1)

- Provisioning external Ceph from Troshka.
- Automating the MTV migration click-path (student-driven in MTV UI).
- Multi-host Troshka mesh (single kubevirt-cluster host).
- `requiresKubevirt` enforcement in Babylon placement (catalog pins provider in config).

## 11. Success criteria

- Catalog deploy completes: 2 clusters installed, operators healthy, Forklift providers connected.
- `virt-handler` and `virt-synchronization-controller` pods have migration-network IPs.
- Cross-cluster TCP:9185 between sync controllers.
- Student can create MTV migration plan (source → destination) and live-migrate a seeded VM.
