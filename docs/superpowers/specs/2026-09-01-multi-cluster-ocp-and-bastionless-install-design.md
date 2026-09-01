# Multi-Cluster OCP + Bastionless Install — Design

**Date:** 2026-09-01
**Status:** Approved (design); implementation plan pending
**Scope:** Combined redesign of (A) how OCP clusters are defined in templates/topology/canvas and (B) how they are installed — moving the install off a bastion VM and into pods.

---

## 1. Motivation

Today an OCP environment in Troshka is a single, *implicit* cluster:

- The template carries one `ocp:` mapping (`cluster_name`, `base_domain`, `api_vip`, `ingress_vip`).
- That mapping is **consumed at topology-generation time** (baked into DNS records, gateway port-forwards, and the bastion's install-config) but is **never persisted as a structured object**. Only `ocpMeta{clusterName, baseDomain}` is read on export, and almost nothing writes it. VIPs are re-derived from the network CIDR each deploy.
- A "cluster" is emergent: a set of `vmNode`s tagged `AnsibleGroup: controllers|workers`, with `os: rhcos` + BMC, plus an `ocp-sno` template-id special case. There is no cluster object, no OCP node type, no role dropdown, no bootstrap concept (the agent-based installer uses a rendezvous node).
- The install is **driven by the bastion VM's cloud-init**: download `openshift-install`, `agent create image`, serve the ISO over HTTP :8080, drive Redfish (sushy) virtual media to boot each node, then `agent wait-for install-complete`. The bastion *also* hosts a GNOME + Firefox desktop as the lab user's workstation.

Two goals:

1. **Multiple clusters per project**, each with a type (SNO / SNO+workers / 3-node compact / standard), RHCOS-only, where nodes are generated from counts *or* drawn explicitly and assigned to a cluster.
2. **Eliminate the bastion.** Run the install from an **execution pod**, and provide the lab user's `oc` shell from a separate pod surfaced in the showroom.

Both ship together as one design.

## 2. Key facts that shape the design (current state)

- **DNS / DHCP / NTP and the api/ingress VIPs are already off-bastion** — served from the per-project network namespace on the troshkad host (dnsmasq / chrony) and by OCP's own keepalived. The bastion's install-time role is really just *ISO build + HTTP serve + Redfish boot loop + wait-for*.
- **The showroom pod is the proven networking model**: a podman pod attached to the project netns transit subnet, with nft forward + masquerade into every lab bridge and a `resolv.conf` pointed at the project dnsmasq — so it already reaches every nested VM *and* resolves `api.<cluster>.<base_domain>`. (`troshkad._attach_pod_to_infra_transit`, `_allow_infra_veth_forward`; `deploy_topology.showroom_infra_network`.)
- **A dynamic inventory plugin already exists** in the `troshka.cloud` collection (`plugins/inventory/troshka.py`): it reads `topology.nodes` and groups hosts by `AnsibleGroup`. It assumes **project == one cluster** — so multi-cluster needs a `clusterId` dimension added to both the topology and this plugin.
- **No mechanism injects credentials into a pod today** — the backend always drives pods (backend → pod via k8s/podman exec), never the reverse. The ops pod's API callback needs a new, scoped credential.
- **`_exec_oc`** (`deploy_service.py`) already runs `oc`/`kubectl` "bastionlessly" but assumes an existing kubeconfig; nothing today *creates* a cluster from a pod.

## 3. Design decisions (settled)

| # | Decision | Choice |
|---|----------|--------|
| 1 | Scope/sequencing | One combined design (A + B together). |
| 2 | Cluster representation | First-class **visual group boundary** (React Flow parent/child); member VMs render inside and move together. |
| 3 | Counts vs VMs | Setting a count **materializes real, editable child VMs**; count is a fast generator. |
| 4 | Type semantics | Type **sets the control-plane count** (SNO=1, compact/standard=3 — OCP allows only 1 or 3); **workers freely editable**. SNO+workers = SNO with workers>0. |
| 5 | Multi-cluster VIPs | **Explicit `api_vip` / `ingress_vip` per cluster** (user-entered, user owns uniqueness; SNO auto-sets to node IP). |
| 6 | Bastion | **Gone entirely** from OCP topologies; the pod installs; lab console moves to showroom. |
| 7 | Install pod | **Persistent per-project "ops pod"** — installs all clusters, then stays for day-2 / monitoring / oc-exec. |
| 8 | Pod auth | **Project-scoped API key** minted at pod creation, injected as a secret, revoked on destroy. |
| 9 | Back-compat | **Accept both** legacy `ocp:` mapping and new `ocp:` list; rewrite shipped templates; migrate persisted topology lazily. |
| 10 | Lab shell | **Separate console pod** — non-root, `oc`-only + merged kubeconfig contexts; no API key, no install tooling. |
| 11 | Node sizing | **Per-role cpu/mem/disk** on the cluster, used as materialization defaults; each VM editable afterward. |

## 4. Data model & schema

### 4.1 Topology

`Project.topology` (JSONB) gains a first-class `clusters[]` array:

```jsonc
topology.clusters = [
  {
    id: "prod",                     // stable slug; used as clusterId ref AND install metadata.name
    name: "prod",
    type: "standard",              // sno | compact | standard  (preset over the two knobs below)
    controlPlane: 3,               // locked by type: sno=1, compact/standard=3
    workers: 2,                    // freely editable
    controlPlaneCpu: 8,  controlPlaneMemory: 16384, controlPlaneDisk: 120,  // seeds cp-* nodes
    workerCpu: 4,        workerMemory: 8192,        workerDisk: 100,         // seeds wrk-* nodes
    baseDomain: "ocp.local",
    apiVip: "10.0.0.10",           // explicit, required (auto = node IP for SNO)
    ingressVip: "10.0.0.11",
    ocpVersion: "4.20",
    pullThroughRegistry: null,
    position: { x, y }, width, height   // React Flow group boundary presentation
  }
]
```

- **VM membership**: new `vmNode.data.clusterId` references `clusters[].id`. **Role stays as `tags.AnsibleGroup: controllers|workers`** (the inventory plugin and install-config counter already key off it); `clusterId` + `AnsibleGroup` together give "which cluster, which role."
- **Canvas nesting**: a member vmNode's `parentNode` = the cluster node id, with `extent: "parent"`.

### 4.2 Topology remapping (CLAUDE.md rule extension)

When cloning topology (patterns / deploy), in addition to the existing remap set, remap:

- `clusters[].id`
- every `vmNode.data.clusterId`
- every member vmNode `parentNode`

Missing this is the classic clone-corruption bug for this feature.

### 4.3 YAML schema (`ocp:` becomes a list)

```yaml
ocp:
  - name: prod
    type: standard
    workers: 2
    control_plane_cpu: 8
    control_plane_memory: 16384
    control_plane_disk: 120
    worker_cpu: 4
    worker_memory: 8192
    worker_disk: 100
    base_domain: ocp.local
    api_vip: 10.0.0.10
    ingress_vip: 10.0.0.11
    ocp_version: "4.20"
  - name: dev
    type: sno
    base_domain: dev.local
```

- `vms:` entries gain an optional `cluster:` field naming which cluster they join (for explicitly-drawn VMs). Legacy templates default all OCP VMs to the single/first cluster.
- **Back-compat**: a legacy `ocp:` *mapping* is wrapped into a one-element list at parse time in `template_loader._copy_template_content_sections`. Shape-detection (mapping vs list) selects the path; no `schemaVersion` field.
- Shipped `ocp-sno.yaml` / `ocp-compact.yaml` / `ocp-standard.yaml` (in `src/backend/templates/` and `example_templates/`) are rewritten to the list form.
- Persisted topology migrates lazily on load (legacy topology without `clusters[]` synthesizes a one-element cluster from tags + description).

## 5. Canvas & frontend

- **New node type `clusterNode`** registered in `Canvas.tsx` — a resizable group boundary titled with name + type badge (e.g. "prod · standard · 3cp/2wrk"). Member VMs render inside via `parentNode`.
- **Membership by drag**: dropping a `vmNode` inside a boundary sets its `clusterId` + `parentNode`; dragging it out clears them. This is the primary new canvas interaction.
- **Cluster config** moves from the create-project dialog into the boundary's PropertiesPanel:
  - name, type dropdown (sno / compact / standard)
  - worker count (control-plane shown read-only, driven by type)
  - **per-role cpu / memory / disk** inputs (seed materialized nodes)
  - base domain, **api_vip / ingress_vip** inputs (new — first time VIPs are user-editable), OCP version
  - VIP collision validation across clusters
- **Count → materialize**: changing the worker count adds/removes real child `vmNode`s (RHCOS `os`, `AnsibleGroup: workers`, `clusterId` set, specs seeded from the cluster's per-role defaults); each is fully editable afterward. Same for the locked CP count when the type changes (1 ↔ 3).
- **Palette**: an "OCP Cluster" item drops an empty boundary; set type/counts to populate it, or drag existing VMs in.
- Per-VM OCP flags in PropertiesPanel (`recertEnabled`, `ocpMonitor`) stay. The freeform `AnsibleGroup` tag editing is replaced by a **role dropdown** (control-plane / worker) for VMs inside a cluster.
- The create-project dialog's OCP fields (cluster name / base domain / version) are removed in favor of the on-canvas cluster config (or seed a default cluster for the openshift template category).

## 6. Deploy pipeline (per-cluster)

`src/backend/app/services/ocp/agent_template.py` becomes **per-cluster in a loop**. `customize_topology` iterates `topology.clusters[]`; for each cluster:

- Select member VMs by `clusterId`; count roles via `_count_ocp_nodes_by_group` scoped to the cluster.
- `metadata.name` = cluster id, `baseDomain` = cluster's, `apiVIPs`/`ingressVIPs` = the explicit VIPs.
  - SNO (controlPlane==1, workers==0) → `platform: none: {}`, VIP = the single node IP.
  - compact/standard → `platform: baremetal` with `apiVIPs`/`ingressVIPs` + per-host BMC entries.
- Per-cluster DNS records (`api.<name>.<domain>`, `api-int...`, `*.apps...`) written to the project dnsmasq, **keyed by cluster** so multiple clusters' records coexist.
- Per-cluster gateway EIP port-forwards (6443 / 443 / 80) — allocate **distinct external ports or distinct EIPs** when multiple clusters are exposed.
- Artifacts written under a per-cluster workdir (`<clusterId>/install-config.yaml`, `agent-config.yaml`, extra-manifests) consumed by the ops pod.

Extra-manifest generation (ocp_mount MachineConfigs, disconnected DNS forwarder) is applied per-cluster from the same per-cluster workdir.

## 7. Ops pod (install driver + day-2 runner)

The bastion's cloud-init install script is reborn as an **execution-environment run inside a persistent per-project ops pod**, modeled on the showroom pod's networking.

- **Launch (libvirt/troshkad)**: a podman pod attached to the project netns transit, reusing `_attach_pod_to_infra_transit` + `_allow_infra_veth_forward`. This gives reach to lab bridges, the BMC network (for Redfish), and dnsmasq DNS.
- **Launch (KubeVirt native/ocpvirt)**: parity via a namespace pod with `dnsConfig` and equivalent reachability (required — 100% troshkad parity).
- **Image**: an execution environment containing ansible + the `troshka.cloud` collection + `oc` / `openshift-install` (fetched per-version at runtime, as the bastion does today) + Redfish/sushy client tooling.
- **Per cluster, the pod runs** (bash or `ansible-navigator` with the EE):
  1. `openshift-install agent create image` for the cluster's per-cluster workdir.
  2. Serve the agent ISO over HTTP from the pod.
  3. Drive Redfish virtual-media insert + `ComputerSystem.Reset` against each node's BMC (sushy-emulator), eject after boot.
  4. `openshift-install agent wait-for install-complete`.
  5. Retrieve the resulting kubeconfig; hand it to the console pod as a named context (§8).
- **Parallelism**: clusters install concurrently (separate plays / async tasks).
- **Trust**: the ops pod holds the scoped API key + cluster-admin kubeconfigs + install tooling. It is **backend-driven only** — no lab-user access.
- **Lifecycle**: created at deploy, persists for day-2 (add-worker, reconfigure), cluster-health monitoring, and `oc`/`kubectl` exec; destroyed with the project. Over time this retires the kubeconfig-juggling `_exec_oc` split.

## 8. Console pod + showroom terminal

- A **second lightweight per-project "console" pod**, also netns-attached, holding **only** `oc` / `kubectl` and the **merged kubeconfigs as contexts** (one per cluster: `oc config get-contexts` → `prod`, `dev`, …). **No Troshka API key, no install tooling.**
- After each cluster installs, the ops pod (or backend) drops that cluster's kubeconfig into the console pod as a named context.
- The web terminal drops into a **non-root, unprivileged user** whose only capability is running `oc` / `kubectl`. **No sudo, no root, no package manager** — a minimal image (oc binary + kubeconfigs + PTY). Even a fully compromised console pod yields only what the lab already grants: `oc` against the lab's own clusters.
- **Showroom "Terminal" tab**: a ttyd/wetty-style web terminal served from the console pod, embedded via the existing app-proxy tab mechanism (same pattern as the console tab). Replaces the bastion's GNOME + Firefox workstation.

## 9. Dynamic inventory & scoped auth

- **Scoped API key**: extend the `ApiKey` model (`src/backend/app/models/api_key.py`) with a scope, e.g. `{ project: <id>, perms: [topology:read, vm:exec] }`. Enforce the scope in `get_current_user` / a dependency so a scoped key can only touch its project. Backend mints one at ops-pod creation, injects `TROSHKA_API_URL` / `TROSHKA_API_KEY` / `TROSHKA_PROJECT_ID` as a pod secret, and revokes it on project destroy.
- **Inventory**: extend `troshka.cloud`'s `plugins/inventory/troshka.py` with a **`clusterId` dimension** — instead of flattening the project into `controllers` / `workers`, emit per-cluster groups (`prod_controllers`, `prod_workers`, `dev_controllers`, …) derived from `vmNode.data.clusterId`. The ops pod runs ansible against these groups.
- **Day-2 & monitoring**: because the ops pod persists with live API access, add-worker / reconfigure re-query the API for current topology, and a cluster-health monitor loop runs in-pod.

## 10. Trust model summary

The bastion's three hats split across pods by trust level:

- **Ops pod** — high trust (API key + admin creds + install tooling); backend-only.
- **Console pod** — low trust (kubeconfig contexts only, non-root, oc-only); the only pod untrusted lab users touch.

This is affordable *because* the showroom already proved a pod can reach every nested VM across netns; both pods instantiate that proven pattern with different secrets rather than inventing new plumbing.

## 11. Out of scope / future

- Non-RHCOS OCP node OSes (design is RHCOS-only).
- Cross-cluster networking/routing helpers (clusters may share or use separate networks; VIP uniqueness is user-owned).
- Auto-allocation of VIPs (explicit per cluster by decision #5).
- Migrating existing *deployed* clusters to the ops-pod model (new deploys only; deployed_topology remains readable).
- Full retirement of `_exec_oc` (incremental; the ops pod supersedes it over time).

## 12. Testing strategy

- **Schema/parse**: unit tests for legacy `ocp:` mapping → one-element list; new `ocp:` list with multiple clusters; `vms[].cluster` association; lazy topology migration.
- **Topology remapping**: tests that cloning remaps `clusters[].id`, `clusterId`, and `parentNode` (extend existing remap tests).
- **Deploy generation**: per-cluster `install-config.yaml` / `agent-config.yaml` for SNO (platform none), compact, standard; distinct DNS records and port-forwards for two clusters; VIP collision validation.
- **Materialization**: count → node create/remove with seeded per-role specs; type change 1↔3 CP.
- **Scoped API key**: a scoped key can read its project topology + exec its VMs and is rejected for other projects; revocation on destroy.
- **Inventory plugin**: multi-cluster grouping output (`<cluster>_controllers` / `_workers`).
- **Pods**: ops-pod and console-pod reachability into nested VM networks + DNS (mirror showroom pod tests); console pod runs as non-root with oc-only.
- Follow project test conventions (SQLite, dev-mode auth, extra trailing values on `time.time()` mocks).
