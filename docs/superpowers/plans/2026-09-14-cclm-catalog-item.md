# CCLM Catalog Item Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a Babylon-deployable CCLM demo: two nested OCP clusters (source SNO+2 workers, destination SNO) on KubeVirt, post-install operators/HCO/Forklift/Submariner, and showroom lab content.

**Architecture:** Troshka provisions infra from `ocp-cclm.yaml` (multi-cluster template + shared migration network). Post-install is a chain of `rhpds.demo_workloads` roles on branch `feat/cclm-workloads`, triggered via Troshka workloads API with `requirements_content` pinning that branch. Agnosticv catalog item wires infra template + workload list for Babylon.

**Tech Stack:** Troshka (YAML template, workloads API), Ansible (`demo_workloads`), OCP 4.21, CNV/MTV/ODF/Submariner, KubeVirt native provider

**Spec:** `docs/superpowers/specs/2026-09-14-cclm-catalog-item-design.md`

## Global Constraints

- Python 3.13 in CI/dev; extra trailing values on `time.time()` mocks in tests.
- OCP version **4.21** (match CCLM notes operator channels: CNV stable, MTV `release-v2.12`, ODF `stable-4.21`, Submariner `stable-0.24`).
- **KubeVirt native provider only** — nested OCP on `host_type=kubevirt-cluster`.
- All RHCOS members: `nestedVirt: true`.
- External Ceph is **pre-provisioned** — catalog supplies connection secret via workload vars; Troshka does not create Ceph.
- Workload roles: branch **`feat/cclm-workloads`** on `github.com/rhpds/demo_workloads` until validated; Troshka runner clones via `requirements_content`.
- No catalog-specific Python in Troshka — lab config in YAML + Ansible only.
- SonarQube cognitive complexity ≤ 15 per function.

---

## Repo map

| Repo | Branch | Deliverables |
|------|--------|--------------|
| `rhpds/demo_workloads` | `feat/cclm-workloads` | CCLM workload roles + playbook (push branch, no PRs during dev) |
| `rhpds/troshka` | **`main`** | `ocp-cclm.yaml`, placement/nestedVirt flags, tests — local dev picks up `main` directly |
| `rhpds/agnosticv` | `feat/cclm-catalog` | Catalog item `troshka/cclm/` (after E2E) |
| `rhpds/showroom-troshka-cclm` (new) | `main` | Antora lab content |

---

## Phase 0 — Branch setup

### Task 0: Create `feat/cclm-workloads` on demo_workloads

**Files:** `~/demo_workloads` (separate repo)

- [ ] **Step 1:** `cd ~/demo_workloads && git checkout main && git pull && git checkout -b feat/cclm-workloads`
- [ ] **Step 2:** Scaffold role directory `roles/troshka_workload_cclm_operators/` (meta, defaults, tasks/main.yml, readme.adoc)
- [ ] **Step 3:** Push empty scaffold: `git push -u origin feat/cclm-workloads`

---

## Phase 1 — Troshka infra template

### Task 1: `ocp-cclm.yaml` template

**Files:**
- Create: `src/backend/templates/ocp-cclm.yaml`
- Create: `example_templates/ocp-cclm.yaml` (symlink or copy)
- Test: `src/backend/tests/test_ocp_clusters.py` or new `test_ocp_cclm_template.py`

**Interfaces:** `template_loader.generate_topology_from_template()` materializes two clusters, three networks, explicit IPs.

- [ ] **Step 1: Write failing test** — load `ocp-cclm` template; assert 2 clusters (`source` SNO+2 workers, `destination` SNO), 4 networks, source `cp-0` has 2 NICs (cluster + migration) with explicit IPs from spec §3.
- [ ] **Step 2: Run test → fail**
- [ ] **Step 3: Implement template** — see skeleton below.
- [ ] **Step 4: Run test → pass**
- [ ] **Step 5: Commit** on `main` in troshka (restart backend after Python changes).

**Template skeleton** (`src/backend/templates/ocp-cclm.yaml`):

```yaml
name: ocp-cclm
display_name: "CCLM — Cross-Cluster Live Migration"
description: "Two nested OCP 4.21 clusters for CNV decentralized live migration + MTV"
category: openshift
install_method: agent
install_via: pod
deploy_time: "~60 min"
versions: ["4.21"]

placement:
  requires_kubevirt: true

ocp:
  - name: source
    type: sno
    workers: 2
    ocp_version: "4.21"
    base_domain: source.cclm.local
    api_vip: 10.1.0.10
    ingress_vip: 10.1.0.10
    network: source-cluster
    migration_network: migration
    control_plane_cpu: 8
    control_plane_memory: 32768
    control_plane_disk: 120
    worker_cpu: 8
    worker_memory: 32768
    worker_disk: 120
    control_plane_disks:
      - { size_gb: 120, bootable: true }
      - { size_gb: 250, ocp_mount: /var/lib/containers }
    worker_disks:
      - { size_gb: 120, bootable: true }
      - { size_gb: 250, ocp_mount: /var/lib/containers }
  - name: destination
    type: sno
    workers: 0
    ocp_version: "4.21"
    base_domain: dest.cclm.local
    api_vip: 10.2.0.10
    ingress_vip: 10.2.0.10
    network: dest-cluster
    migration_network: migration
    control_plane_cpu: 8
    control_plane_memory: 32768
    control_plane_disk: 120
    control_plane_disks:
      - { size_gb: 120, bootable: true }
      - { size_gb: 250, ocp_mount: /var/lib/containers }

networks:
  source-cluster:
    cidr: 10.1.0.0/24
    gateway: true
    domain: source.cclm.local
  dest-cluster:
    cidr: 10.2.0.0/24
    gateway: true
    domain: dest.cclm.local
  migration:
    cidr: 172.16.100.0/24
    gateway: true
    dhcp: true
    dhcpRangeStart: 172.16.100.10
    dhcpRangeEnd: 172.16.100.99
  bmc:
    type: bmc
    cidr: 192.168.100.0/24

# Explicit vms section with migration NIC IPs + nestedVirt on all RHCOS members
vms:
  source-cp-0:
    cluster: source
    role: control-plane
    nested_virt: true
    nics:
      - { network: source-cluster, ip: 10.1.0.10 }
      - { network: migration, ip: 172.16.100.10 }
  source-wrk-0:
    cluster: source
    role: worker
    nested_virt: true
    nics:
      - { network: source-cluster, ip: 10.1.0.20 }
      - { network: migration, ip: 172.16.100.20 }
  source-wrk-1:
    cluster: source
    role: worker
    nested_virt: true
    nics:
      - { network: source-cluster, ip: 10.1.0.21 }
      - { network: migration, ip: 172.16.100.21 }
  dest-cp-0:
    cluster: destination
    role: control-plane
    nested_virt: true
    nics:
      - { network: dest-cluster, ip: 10.2.0.10 }
      - { network: migration, ip: 172.16.100.110 }

showroom:
  enabled: true
  dns_network: source-cluster
  content_repo: https://github.com/rhpds/showroom-troshka-cclm.git
  content_ref: main
  build_content: true
  tabs:
    - { type: proxy, cluster: source, proxy_port: 443, proxy_tls: true }
    - { type: proxy, cluster: destination, proxy_port: 443, proxy_tls: true, name: "Destination Console" }
    - { type: terminal, target: clusters, name: "OpenShift Cluster Terminal" }

workloads:
  - role: rhpds.demo_workloads.troshka_workload_cclm_operators
  - role: rhpds.demo_workloads.troshka_workload_cclm_hco
  - role: rhpds.demo_workloads.troshka_workload_cclm_network
  - role: rhpds.demo_workloads.troshka_workload_cclm_forklift
  - role: rhpds.demo_workloads.troshka_workload_cclm_seed_vms
```

Note: `placement`, `migration_network`, `nested_virt` on vms, and `workloads` list may need template_loader support (Task 2).

---

### Task 2: Template loader — `nested_virt`, `networkIds` from template, `requires_kubevirt`

**Files:**
- Modify: `src/backend/app/services/template_loader.py`
- Modify: `src/frontend/src/components/canvas/clusterMaterialize.ts` (if canvas import path)
- Test: `src/backend/tests/test_ocp_cclm_template.py`

- [ ] **Step 1: Failing test** — `nested_virt: true` in vms YAML → `data.nestedVirt: true` on materialized members.
- [ ] **Step 2: Implement** — map `nested_virt` → `nestedVirt` in `_build_vm_data`.
- [ ] **Step 3: Failing test** — cluster `network` + `migration_network` → `networkIds: [net-source-cluster, net-migration]` on cluster object.
- [ ] **Step 4: Implement** — resolve network names to ids during materialize.
- [ ] **Step 5: Commit**

---

### Task 3: Placement — `requires_kubevirt`

**Files:**
- Modify: `src/backend/app/services/placement.py` (`find_available_host`, `_prepare_hosts`)
- Modify: `src/backend/app/services/template_loader.py` (persist `placement.requires_kubevirt` on topology)
- Test: `src/backend/tests/test_placement_helpers.py`

- [ ] **Step 1: Failing test** — topology with `placement.requires_kubevirt: true` only selects `host_type=kubevirt-cluster` hosts.
- [ ] **Step 2: Implement** — filter in `find_available_host`; clear error in `diagnose_placement_failure` when only troshkad hosts exist.
- [ ] **Step 3: Commit**

---

### Task 4: Auto-`nestedVirt` on RHCOS cluster members (canvas + backend materialize)

**Files:**
- Modify: `src/backend/app/services/template_loader.py`
- Modify: `src/frontend/src/components/canvas/clusterMaterialize.ts`
- Test: materialize tests

- [ ] **Step 1: Failing test** — materialized RHCOS member with `os: rhcos` gets `nestedVirt: true` when topology has `placement.requires_kubevirt`.
- [ ] **Step 2: Implement** (both frontend and backend materialize paths).
- [ ] **Step 3: Commit**

---

## Phase 2 — demo_workloads roles (`feat/cclm-workloads`)

All work on `~/demo_workloads`, branch `feat/cclm-workloads`. Push after each role is runnable.

**Shared defaults** (`roles/troshka_workload_cclm_*/defaults/main.yml`):

```yaml
cclm_source_cluster: source
cclm_dest_cluster: destination
cclm_migration_cidr: 172.16.100.0/24
cclm_lm_network_parent_iface: enp2s0   # nested virtio; verify on first live deploy
cclm_metallb_pools:
  source: 172.16.100.100-172.16.100.109
  destination: 172.16.100.200-172.16.100.209
cclm_whereabouts_ranges:
  source: 172.16.100.224/28
  destination: 172.16.100.240/28
# External Ceph — from catalog vault / workload extra_vars
cclm_ceph_secret_name: rook-ceph-external-cluster-details
```

### Task 5: `troshka_workload_cclm_operators`

Install OLM operators on **both** clusters (loop `clusters` dict from openshift-workloads).

| Operator | Namespace | Channel |
|----------|-----------|---------|
| kubevirt-hyperconverged | openshift-cnv | stable |
| mtv-operator | openshift-mtv | release-v2.12 |
| odf-operator (+ deps) | openshift-storage | stable-4.21 |
| submariner | submariner-operator | stable-0.24 |
| kubernetes-nmstate-operator | openshift-nmstate | stable |
| openshift-cert-manager-operator | cert-manager-operator | stable-v1 |
| metallb-operator | metallb-system | stable |

- [ ] **Step 1:** Role skeleton + `tasks/install_subscriptions.yml` using `kubernetes.core` or `redhat.openshift` collection patterns from existing demo_workloads roles.
- [ ] **Step 2:** Wait for CSV `Succeeded` on each cluster.
- [ ] **Step 3:** Push branch; smoke via Troshka ad-hoc run (see Task 10).
- [ ] **Step 4: Commit** `Add cclm operators workload role.`

---

### Task 6: `troshka_workload_cclm_hco`

- [ ] Patch `HyperConverged` CR per spec §3 (decentralizedLiveMigration, lm-network, timeouts).
- [ ] Wait for KubeVirt / HCO healthy.
- [ ] Commit + push.

---

### Task 7: `troshka_workload_cclm_network`

- [ ] Create `StorageCluster` external Ceph (from secret).
- [ ] NNCPs for migration NIC static IPs (per-node IPs from Troshka topology — pass via workload `extra_vars` from inventory or a generated config).
- [ ] `NetworkAttachmentDefinition` `lm-network` (macvlan + whereabouts per cluster).
- [ ] MetalLB `IPAddressPool` + `L2Advertisement` on migration parent iface.
- [ ] Commit + push.

---

### Task 8: `troshka_workload_cclm_forklift`

- [ ] Patch `ForkliftController` (`feature_ocp_live_migration: "true"`).
- [ ] Submariner `Broker` on source, `Submariner` on both.
- [ ] Forklift `Provider` CRs (each cluster registers the remote API).
- [ ] `NetworkMap` + `StorageMap` (pod→pod, all SCs → `ocs-external-storagecluster-ceph-rbd`).
- [ ] Commit + push.

---

### Task 9: `troshka_workload_cclm_seed_vms` (optional for v1)

- [ ] Create `source-cluster` / `destination-cluster` namespaces.
- [ ] Import 1–2 small RHCOS VMs into `source-cluster` (or document MTV import as student step).
- [ ] Commit + push.

---

### Task 10: Playbook wrapper

**Files:** `playbooks/cclm/main.yml`, `group_vars/all.yml`

- [ ] Ordered play that runs roles 5→9 with `clusters` targeting both clusters.
- [ ] Document in `playbooks/cclm/README.md`.

---

## Phase 3 — Troshka workload integration

### Task 11: Run workloads from template `workloads:` list

**Option A (v1 — manual):** Document ad-hoc API calls per role with shared `requirements_content`.

**Option B (follow-up):** Troshka deploy auto-enqueues workload chain after recert on both clusters.

For v1 use **Option A**:

- [ ] **Step 1:** Add `docs/dev/cclm-lab.md` with curl examples pinning `feat/cclm-workloads`.
- [ ] **Step 2:** Helper script `scripts/run-cclm-workloads.sh` that POSTs each role in order, waiting for `succeeded` between steps.

**Pinning the branch** (every run):

```json
{
  "requirements_content": {
    "collections": [{
      "name": "https://github.com/rhpds/demo_workloads.git",
      "type": "git",
      "version": "feat/cclm-workloads"
    }]
  }
}
```

**Multi-cluster target:**

```json
{
  "target_map": {
    "mode": "cluster",
    "clusters": ["source", "destination"]
  }
}
```

---

## Phase 4 — Agnosticv catalog item

### Task 12: `troshka/cclm/` in agnosticv

**Files** (in agnosticv repo):

```
troshka/cclm/
  infra_template.yaml    # #include ocp-cclm from troshka example_templates
  config.yaml            # env_type: troshka, troshka_deploy_mode: template
  software_workloads.yml # workloads list + requirements_content branch pin
```

`software_workloads.yml` excerpt:

```yaml
requirements_content:
  collections:
    - name: https://github.com/rhpds/demo_workloads.git
      type: git
      version: feat/cclm-workloads

workloads:
  - rhpds.demo_workloads.troshka_workload_cclm_operators
  - rhpds.demo_workloads.troshka_workload_cclm_hco
  - rhpds.demo_workloads.troshka_workload_cclm_network
  - rhpds.demo_workloads.troshka_workload_cclm_forklift
  - rhpds.demo_workloads.troshka_workload_cclm_seed_vms
```

- [ ] Register in `.agnosticv.yaml` `related_files` if needed.
- [ ] PR against agnosticv `feat/cclm-catalog`.

---

## Phase 5 — Showroom content

### Task 13: `showroom-troshka-cclm` repo

- [ ] Antora module with lab sections: environment overview, verify sync controllers, MTV migration walkthrough, known issues from production notes §14.
- [ ] Link to MTV UI plugin on each cluster.
- [ ] Point `ocp-cclm.yaml` showroom `content_repo` at it.

---

## Phase 6 — Live validation

### Task 14: E2E on KubeVirt host

Prerequisites: kubevirt provider registered, external Ceph secret available, `TROSHKA_*` dev env up.

- [ ] Deploy project from `ocp-cclm` template on kubevirt host.
- [ ] Both clusters reach control-plane-usable.
- [ ] Run workload chain (Task 11 script) with `version: feat/cclm-workloads`.
- [ ] Verify: `virt-synchronization-controller` pods on migration IPs; Forklift providers connected; MTV UI loads.
- [ ] Live-migrate one VM source → destination via MTV.
- [ ] Document results in spec or `docs/dev/cclm-lab.md`.

---

## Sequencing summary

```
Phase 0  branch setup
Phase 1  Troshka template + placement (Tasks 1–4)     ← parallel with Phase 2 start
Phase 2  demo_workloads roles (Tasks 5–10)            ← feat/cclm-workloads
Phase 3  workload runner wiring (Task 11)
Phase 4  agnosticv catalog (Task 12)
Phase 5  showroom (Task 13)
Phase 6  live E2E (Task 14)
```

**Parallelism:** Tasks 5–7 can start as soon as Task 0 is done (operators don't need Troshka template). Task 7 (network) needs NNCP IPs — coordinate with Task 1 IP plan. Task 8 (Forklift) depends on Task 6–7.

---

## Self-review

- Spec coverage: topology §2–4 → Task 1; networks §3 → Task 1+7; provider §5 → Tasks 3–4; workloads §6 → Tasks 5–10; branch workflow §8 → Task 11; catalog §9 → Task 12; showroom §7 → Task 13; success §11 → Task 14.
- No Troshka catalog-specific Python — config in YAML + Ansible.
- `feat/cclm-workloads` branch is the integration point until merge.

## Execution handoff

Plan: `docs/superpowers/plans/2026-09-14-cclm-catalog-item.md`
Spec: `docs/superpowers/specs/2026-09-14-cclm-catalog-item-design.md`

Start with **Task 0** (create `feat/cclm-workloads`) and **Task 1** (template) in parallel.
