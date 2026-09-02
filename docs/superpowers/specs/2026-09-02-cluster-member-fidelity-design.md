# Cluster Member Fidelity + Canvas UX — Design Spec

**Date:** 2026-09-02
**Status:** Approved (design), pending spec review → implementation plan
**Branch context:** builds on the multi-cluster OCP work (Plans 1–4b) on `feat/multi-cluster-ocp-bastionless`.

## 1. Motivation

The multi-cluster OCP canvas (Plans 1–2) gives each cluster a boundary node with count-materialized member VMs, but members are thin and the UX has gaps found in use:

- A freshly-dropped cluster is **empty** — members are only materialized when the properties panel edits type/worker count (`reconcileClusterVms` is called only from `PropertiesPanel.tsx:4326`, never on drop at `Canvas.tsx:492`). A `standard · 3cp/0wrk` cluster shows no VMs.
- Members have a single implicit disk. **IBI (image-based install) requires ≥2 disks per node** (install target + boot/seed disk); there is no way to give members multiple disks.
- Members' networks are inferred from NIC IPs; there's no explicit, uniform network for a cluster, which makes VIP/DNS resolution guessy and blocks canvas-time auto-VIP (before member IPs exist).
- VIPs are chosen by a blind `network+2/+3` offset that can collide with existing VM IPs.
- The boundary is a fixed `520×320` that doesn't grow, so members overflow it as counts rise.

This spec makes cluster members **deploy-faithful** (multi-disk, uniform NICs) and the canvas **self-consistent** (materialize on drop, auto-size, availability-checked VIPs).

## 2. Goals / Non-goals

**Goals**
- Materialize a cluster's members the moment it is created.
- Per-role **multiple disks** on members (IBI-ready); real storage nodes + controller/boot edges.
- **Cluster-level uniform networks**: every member gets a NIC on each of the cluster's networks; the primary is the machine network and the VIP anchor.
- **Availability-checked auto-VIP** on the machine network — unused IPs, overridable, warn-on-collision.
- Boundary **auto-sizes** to fit its members.
- Frontend (canvas materialize) and backend (template/count materialize + deploy resolution) use the **same rules** so canvas-created and backend-created clusters are identical.

**Non-goals**
- Per-VM manual divergence of members (individual members getting different networks or ad-hoc disks) — deferred; per-role templates cover IBI and the common cases.
- Live-network probing for VIP availability (topology-scoped only).
- Changing the deploy/install pipeline itself (Plan 3 member-normalization already consumes disks/NICs once present).

## 3. Settled decisions
- **Storage:** per-role disk templates (`controlPlaneDisks[]`, `workerDisks[]`); legacy single `controlPlaneDisk`/`workerDisk` upgrades to a one-disk list (back-compat).
- **Networks:** cluster-level and **uniform** — all members get a NIC on every cluster network; primary network = machine network = VIP anchor. No per-role NIC config.
- **Availability:** topology-scoped ("used" = network/broadcast, gateway, DHCP range, all VM NIC IPs on the network, other clusters' VIPs). Pick unused from the **top** of the CIDR downward.
- **Auto-VIP UX:** canvas auto-fills the (editable) `apiVip`/`ingressVip` fields; deploy re-resolves if blank; explicit per-side wins.
- **Override:** warn-but-allow (inline on canvas + deploy log); never block.
- **Member layout:** auto grid; boundary hugs members (choice A). Manual member positioning is not preserved across reconcile.
- **Network attach UX:** attach a network to a cluster via a cluster→network edge or the cluster editor's network selector; primary = first attached.

## 4. Current state (verified)
- `clusterFactory.makeCluster` creates only the boundary node + `ClusterConfig` (defaults `type:"standard", controlPlane:3, workers:0`); no members. `CLUSTER_NODE_SIZE={520,320}` fixed.
- `clusterMaterialize.reconcileClusterVms` creates member `vmNode`s with `parentId=cluster.nodeId`, grid-positioned (`CHILD_X0 + col*CHILD_GAP_X`, `rowY`), with `vcpus/ram/disk` + `clusterRole` + `tags.AnsibleGroup`. It does **not** create storage nodes, NIC data, or edges.
- Backend `template_loader.materialize_cluster_vms` / `_generate_topology_from_vms` materialize members for templates/counts; `agent_template` normalizes member OCP fields (Plan 3 Task 7: bmcEnabled/bootDevices/diskControllers/cdrom).
- `agent_template.resolve_cluster_vips` → explicit wins; SNO → CP node IP; else `_derive_cluster_vips` = `net+2/net+3`. Network data carries `cidr`, `gateway`, `dhcpRangeStart/End`, `dhcp`, `dns`. Frontend has `lib/dhcpIpAssignment.ts` for IP tracking.

## 5. Design

### A. Materialize on drop
`Canvas.tsx` drop handler (`:492`), after `addCluster`/`addNode`, runs the same materialize path the editor uses so the default `standard` cluster immediately shows its 3 CP members (grid-laid-out, inside the boundary, auto-sized). Extract a shared `materializeClusterInto(cluster, nodes) -> nodes'` used by both drop and `PropertiesPanel` to avoid divergence.

### B. Cluster-level uniform networks (subsumes the "anchor")
- **Data model:** `ClusterConfig.networkIds: string[]` — network node ids the cluster attaches to; `networkIds[0]` is the machine network (VIP anchor). Back-compat: when absent, derive from members' NIC→network edges, else the single project network.
- **Attach UX:** a cluster→network edge (drag from the cluster boundary to a network node) or a network multiselect in the cluster editor sets `networkIds`. First attached = primary.
- **Materialize:** each member gets one NIC per `networkId` (generated unique MAC; primary NIC on `networkIds[0]`), with the member↔network edges wired. Uniform across CP and worker.
- **Consumers:** VIP resolution, DNS, and gateway all key off `networkIds[0]`'s network node.

### C. Per-role multiple disks
- **Data model:** `ClusterConfig.controlPlaneDisks: DiskSpec[]` and `workerDisks: DiskSpec[]`, `DiskSpec = { sizeGb: number; bus?: "virtio"|"sata"|"scsi"; bootable?: boolean }`. Defaults: CP `[{sizeGb:120,bootable:true},{sizeGb:100}]`, worker `[{sizeGb:120,bootable:true},{sizeGb:100}]` (two disks → IBI-ready). Legacy single `controlPlaneDisk`/`workerDisk` numbers map to `[{sizeGb:<n>,bootable:true}]`.
- **Materialize (frontend + backend, matching):** for each member, create one `storageNode` per disk spec (`parentId` = cluster for visual grouping or free — see layout), wire `vm → storage` edges with disk-controller handles, set `bootDevices=[<first bootable disk node id>]`, and the disk-controller IDs. This yields deploy-ready members that Plan 3 normalization finishes.
- **Editor:** per-role disk-list editor (add/remove/reorder, size, bootable) in the cluster properties panel.

### D. Availability-checked auto-VIP
- **Shared rule:** `used_ips(network)` = network + broadcast, gateway (`data.gateway` or `net+1`), DHCP range (`dhcpRangeStart..End` when `dhcp`), every VM NIC static IP on the network, every other cluster's resolved VIPs. `pick_unused(cidr, used, count)` returns the first `count` free host IPs scanning **top→down**.
- **Backend:** new `_network_used_ips(topology, cluster)` + `pick_unused_ips(...)` in `agent_template` (or `ocp/vip_alloc.py`). `_derive_cluster_vips` for multi-node calls them (keyed on `networkIds[0]` CIDR) instead of `net+2/+3`. `resolve_cluster_vips` shape unchanged: explicit per-side wins; SNO → node IP; two clusters on one network get distinct VIPs. CIDR exhausted → clear error.
- **Frontend:** cluster editor suggests unused VIPs into the (editable) fields using the same rule via `dhcpIpAssignment.ts`; clearing re-suggests; persisted to topology (deploy re-resolves if blank).
- **Warn-but-allow:** explicit VIP in the used set → inline canvas warning + deploy-log warning; never blocks.

### E. Auto-size boundary
- A layout helper computes cluster node `width/height` from member count: `cols = min(membersPerRowCap, count)`, `rows = ceil(count/cols)`, `width = 2*PAD + cols*CELL_W`, `height = HEADER_H + PAD + rows*CELL_H`. Recomputed on every membership/disk/NIC change (drop, reconcile, drag-in/out, role change). Members reflow into the grid (CPs first, workers wrapping). `NodeResizer` min becomes the fitted size (or is removed for clusters).

## 6. Data model summary (`ClusterConfig` additions)
- `networkIds: string[]` (primary = machine network / VIP anchor)
- `controlPlaneDisks: DiskSpec[]`, `workerDisks: DiskSpec[]` (supersede single `controlPlaneDisk`/`workerDisk`; legacy upgraded)
- (unchanged) `controlPlaneCpu/Memory`, `workerCpu/Memory`, `type`, `controlPlane`, `workers`, `apiVip`, `ingressVip`, `baseDomain`, `ocpVersion`, `pullThroughRegistry`

## 7. Backend changes
- `template_loader`: materialize members with per-role disks (storage nodes + edges + bootDevices/controllers) and uniform NICs per `networkIds`; upgrade legacy single-disk; normalize `networkIds`/`*Disks` on load.
- `agent_template`: `_network_used_ips` + `pick_unused_ips`; `_derive_cluster_vips` uses them keyed on `networkIds[0]`; explicit-VIP collision warning.
- Export/round-trip (`projects.py`/`patterns.py`): carry `networkIds` (as network **names** for portability) + `*Disks`; template `ocp:` entries may specify `network:` + per-role `disks:`.

## 8. Frontend changes
- `clusterFactory`: default `controlPlaneDisks`/`workerDisks` (two disks each), `networkIds: []`.
- `clusterMaterialize`: create storage nodes + edges + NIC data per member (matching backend); grid + auto-size; shared `materializeClusterInto`.
- `Canvas.tsx`: materialize on drop; support cluster→network edge to set `networkIds`.
- `ClusterNode`: auto-size; header shows `Ncp/Mwrk` + network + disks summary.
- `PropertiesPanel`: per-role disk-list editor; network selector; auto-VIP suggestion + inline collision warning.

## 9. Back-compat
- Clusters without `networkIds` → derive from member NIC edges, else the single network (current behavior).
- Legacy `controlPlaneDisk`/`workerDisk` numbers → one-disk lists.
- `resolve_cluster_vips` unchanged for explicit VIPs and SNO; only the blind multi-node derivation changes (to unused-IP pick). Single-cluster/legacy deploys stay byte-identical where VIPs are explicit.
- Backend- and canvas-created members must be interchangeable (extend the Plan 2 consistency ruling to disks/NICs).

## 10. Testing
- **Backend units:** legacy-disk upgrade; member materialize creates N storage nodes + edges + bootDevices per role; uniform NICs per networkId; `_network_used_ips` exclusions; `pick_unused_ips` top-down + distinct + multi-cluster non-collision; `resolve_cluster_vips` explicit-wins/SNO/derive; collision warning; CIDR-full error; export round-trip of `networkIds`+`*Disks`.
- **Frontend vitest:** materialize-on-drop (3 CPs appear); per-role disks create storage nodes + edges; uniform NICs; auto-size grows with count; auto-VIP suggestion + inline collision warning; network attach sets `networkIds`; canvas- vs backend-materialized member shape parity (round-trip).

## 11. Sequencing (for the plan)
1. Materialize-on-drop (fix empty cluster) — smallest, unblocks visual verification.
2. Cluster-level networks + uniform member NICs (data model + attach + materialize + back-compat).
3. Per-role multi-disk (data model + materialize storage nodes/edges + editor).
4. Availability-checked auto-VIP (used-IP rule + pick + resolve + suggest + warn).
5. Auto-size boundary + grid reflow.
6. Export/round-trip + parity + regression.

## 12. Out of scope / future
- Per-VM manual member overrides (different network/disks per individual node).
- Live-network VIP probing.
- Deploy-side resolution of the two live-env blockers found earlier (ops-pod EE image registry auth; `_ops_pod_api_url` = external_url misconfig) — tracked separately; not part of this canvas/data-model work.
