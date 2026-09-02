# Cluster Member Fidelity + Canvas UX — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cluster members materialize on drop, carry per-role multiple disks (IBI-ready) and uniform NICs on the cluster's networks, get availability-checked auto-VIPs, and the boundary auto-sizes to fit them — with frontend (canvas) and backend (template/deploy) emitting identical member shapes.

**Architecture:** Frontend `clusterMaterialize.ts` and backend `template_loader.py` both materialize members; they must emit the SAME node/edge/data shapes (this plan's dominant invariant). VIP logic lives in `agent_template.py` (backend) mirrored by `dhcpIpAssignment.ts` primitives (frontend). Data model is `ClusterConfig` (frontend) + the `ocp:`/`clusters[]` topology objects (backend).

**Tech Stack:** Next.js 15 + React Flow v12 (`parentId`, handles) + Zustand + Vitest (frontend); Python 3.11 + pytest (backend). No new deps.

**Spec:** `docs/superpowers/specs/2026-09-02-cluster-member-fidelity-design.md`

## Global Constraints
- **Frontend/backend parity:** a member materialized on the canvas and one materialized by the backend must be interchangeable — same `vmNode.data` keys, same storageNode + disk-edge + NIC-edge shapes. Every materialize task asserts parity.
- **Exact edge shapes (reproduce verbatim):**
  - Disk edge: `storageNode` is the SOURCE — `sourceHandle:"right"`, target VM `targetHandle:"dp-<dcId>-left"`, `type:"smoothstep"`, style stroke `rgba(251,191,36,0.6)` strokeWidth 2 strokeDasharray `"4 4"`, `className:"edge-storage-pulse"`, `animated:false`.
  - NIC edge: `networkNode` is the SOURCE — `sourceHandle:"bottom"` (first NIC) / `"top"` (rest), target VM `targetHandle:"nic-<nicId>-<top|bottom>"` (first NIC uses `top`), `type:"smoothstep"`, style stroke `rgba(34,211,238,0.5)` strokeWidth 2 strokeDasharray `"6 4"`, `animated:true`.
- **IDs/MACs:** frontend `generateNicId()`=`nic-<uuid>`, `generateMac()`=`52:54:00:xx:xx:xx`, `generateDiskControllerId()`=`dp-<uuid>` (all in `canvasStore.ts`). Backend `_id()`=uuid4, `_mac()`=`52:54:00:%02x:%02x:%02x`; disk controller id `dp-<_id()>`.
- **Member node identity:** `id = f"{clusterId}-{prefix}-{i}"` (prefix `cp`/`worker`, lowest free index); `parentId = cluster.nodeId` (= `cluster-<clusterId>`); `data.generated=true`; `data.clusterId`, `data.clusterRole` (`control-plane`/`worker`), `data.tags.AnsibleGroup` (`controllers`/`workers`).
- **Back-compat:** legacy single `controlPlaneDisk`/`workerDisk` (GB number) → one-disk list `[{sizeGb:<n>,bootable:true}]`; clusters without `networkIds` → derive from member NIC→network edges, else the single project network; explicit VIPs and SNO node-IP behavior unchanged; single-cluster/legacy deploys byte-identical where VIPs are explicit.
- **PROCESS GUARD:** NEVER `git stash`/`pop`/`apply` (2 pre-existing `main` stashes must remain). Absolute git paths or `git -C /Users/prutledg/troshka`. One commit per task, no amend. STOP+report on unexpected `git status`.
- **Run:** frontend `cd src/frontend && npx vitest run …`, `npx tsc --noEmit`, `npx eslint <changed>`; backend `cd src/backend && ./venv/bin/python3 -m pytest …`, system `black`, `pyright`. `pyright`/`tsc` clean; no NEW eslint errors (repo has a baseline).

---

### Task 1: Data model — `DiskSpec`, `networkIds`, per-role disks (both sides) + back-compat normalize

**Files:**
- Modify: `src/frontend/src/stores/canvasStore.ts` (`ClusterConfig` interface + a `DiskSpec` type)
- Modify: `src/frontend/src/components/canvas/clusterFactory.ts` (defaults)
- Create: `src/backend/app/services/cluster_normalize.py` (legacy→new normalizer) OR add to `template_loader.py`
- Test: `src/frontend/src/stores/__tests__/clusterConfig.test.ts`, `src/backend/tests/test_ocp_clusters.py`

**Interfaces produced:**
- TS `DiskSpec = { sizeGb: number; bus?: "virtio"|"sata"|"scsi"; bootable?: boolean }`; `ClusterConfig` gains `networkIds?: string[]`, `controlPlaneDisks?: DiskSpec[]`, `workerDisks?: DiskSpec[]`.
- Python `normalize_cluster_disks(cluster: dict) -> dict` returns a cluster dict with `controlPlaneDisks`/`workerDisks` lists (upgrading legacy single values) and `networkIds` defaulted to `[]`.

- [ ] **Step 1: Frontend failing test** — `clusterConfig.test.ts`

```ts
import { describe, it, expect } from "vitest";
import { makeCluster } from "@/components/canvas/clusterFactory";

describe("ClusterConfig defaults", () => {
  it("gives CP and worker two disks (IBI-ready) and empty networkIds", () => {
    const { cluster } = makeCluster("ocp", { x: 0, y: 0 });
    expect(cluster.controlPlaneDisks).toEqual([
      { sizeGb: 120, bootable: true },
      { sizeGb: 100 },
    ]);
    expect(cluster.workerDisks).toEqual([
      { sizeGb: 120, bootable: true },
      { sizeGb: 100 },
    ]);
    expect(cluster.networkIds).toEqual([]);
  });
});
```

- [ ] **Step 2: Run → fail** (`cd src/frontend && npx vitest run src/stores/__tests__/clusterConfig.test.ts`).

- [ ] **Step 3: Add `DiskSpec` + fields to `ClusterConfig`** (`canvasStore.ts:187-205`): add after `pullThroughRegistry`:
```ts
  networkIds?: string[];
  controlPlaneDisks?: DiskSpec[];
  workerDisks?: DiskSpec[];
```
and above the interface:
```ts
export interface DiskSpec {
  sizeGb: number;
  bus?: "virtio" | "sata" | "scsi";
  bootable?: boolean;
}
```

- [ ] **Step 4: `clusterFactory.ts` defaults** — in `CLUSTER_DEFAULTS` add `controlPlaneDisks: [{ sizeGb: 120, bootable: true }, { sizeGb: 100 }]`, `workerDisks: [{ sizeGb: 120, bootable: true }, { sizeGb: 100 }]`; in both the node `data` and the returned `cluster`, set `networkIds: []`, `controlPlaneDisks: CLUSTER_DEFAULTS.controlPlaneDisks`, `workerDisks: CLUSTER_DEFAULTS.workerDisks`. Keep legacy `controlPlaneDisk`/`workerDisk` for now (unused going forward but preserved for old topologies).

- [ ] **Step 5: Run → pass.**

- [ ] **Step 6: Backend failing test** — `test_ocp_clusters.py`:
```python
def test_normalize_cluster_disks_upgrades_legacy_single():
    from app.services.template_loader import normalize_cluster_disks
    c = normalize_cluster_disks({"controlPlaneDisk": 120, "workerDisk": 100})
    assert c["controlPlaneDisks"] == [{"sizeGb": 120, "bootable": True}]
    assert c["workerDisks"] == [{"sizeGb": 100, "bootable": True}]
    assert c["networkIds"] == []

def test_normalize_cluster_disks_keeps_explicit_list():
    from app.services.template_loader import normalize_cluster_disks
    c = normalize_cluster_disks(
        {"controlPlaneDisks": [{"sizeGb": 120, "bootable": True}, {"sizeGb": 100}], "networkIds": ["net1"]}
    )
    assert len(c["controlPlaneDisks"]) == 2 and c["networkIds"] == ["net1"]
```

- [ ] **Step 7: Run → fail.**

- [ ] **Step 8: Implement `normalize_cluster_disks`** in `template_loader.py`:
```python
def normalize_cluster_disks(cluster: dict) -> dict:
    """Upgrade legacy single-disk clusters to per-role disk lists; default networkIds.

    Legacy ``controlPlaneDisk``/``workerDisk`` (GB int) become a one-element
    bootable list. Explicit ``controlPlaneDisks``/``workerDisks`` pass through.
    """
    out = dict(cluster)
    for role_list, legacy, default_gb in (
        ("controlPlaneDisks", "controlPlaneDisk", 120),
        ("workerDisks", "workerDisk", 100),
    ):
        if not out.get(role_list):
            gb = out.get(legacy) or default_gb
            out[role_list] = [{"sizeGb": gb, "bootable": True}]
    out.setdefault("networkIds", out.get("networkIds") or [])
    return out
```

- [ ] **Step 9: Run → pass.** black + pyright + tsc clean.

- [ ] **Step 10: Commit** — `git -C /Users/prutledg/troshka add src/frontend/src/stores/canvasStore.ts src/frontend/src/components/canvas/clusterFactory.ts src/frontend/src/stores/__tests__/clusterConfig.test.ts src/backend/app/services/template_loader.py src/backend/tests/test_ocp_clusters.py && git -C /Users/prutledg/troshka commit -m "feat(cluster): DiskSpec + networkIds + per-role disks with legacy upgrade (Task 1)"`

---

### Task 2: Materialize members on drop (fix empty cluster)

**Files:** Modify `src/frontend/src/components/canvas/clusterMaterialize.ts` (export a shared helper), `src/frontend/src/components/canvas/Canvas.tsx` (drop handler `:492`); Test `src/frontend/src/components/canvas/__tests__/clusterMaterialize.test.ts`.

**Interfaces produced:** `materializeClusterInto(cluster: ClusterConfig, nodes: Node[]): Node[]` — appends the cluster's default members (via `reconcileClusterVms`) to `nodes`. Used by both the drop handler and PropertiesPanel.

- [ ] **Step 1: Failing test** (extend clusterMaterialize.test.ts):
```ts
it("materializeClusterInto creates 3 CP members for a fresh standard cluster", () => {
  const { node, cluster } = makeCluster("ocp", { x: 0, y: 0 });
  const nodes = materializeClusterInto(cluster, [node]);
  const cps = nodes.filter(
    (n) => n.type === "vmNode" && n.data.clusterId === cluster.id && n.data.clusterRole === "control-plane",
  );
  expect(cps).toHaveLength(3);
  expect(cps.every((n) => n.parentId === cluster.nodeId)).toBe(true);
});
```

- [ ] **Step 2: Run → fail.**

- [ ] **Step 3: Implement** in `clusterMaterialize.ts`:
```ts
export function materializeClusterInto(cluster: ClusterConfig, nodes: Node[]): Node[] {
  return reconcileClusterVms(cluster, nodes);
}
```

- [ ] **Step 4: Wire into drop** — `Canvas.tsx:492` after `addCluster(cluster)`:
```ts
const { node, cluster } = makeCluster("ocp", position);
addNode(node);
addCluster(cluster);
const withMembers = materializeClusterInto(cluster, useCanvasStore.getState().nodes);
useCanvasStore.getState().setNodes(withMembers);
```
(Use the store's existing setter — confirm the action name; `setNodes`/`replaceNodes`. If members must be added individually, loop `addNode` over the new members instead.)

- [ ] **Step 5: Run → pass.** tsc clean.

- [ ] **Step 6: Commit** — `feat(cluster): materialize members on drop (Task 2)`.

---

### Task 3: Frontend — per-role multiple disks (storage nodes + controllers + edges + bootDevices)

**Files:** Modify `src/frontend/src/components/canvas/clusterMaterialize.ts`; Test `clusterMaterialize.test.ts`.

**Interfaces produced:** `makeMemberNode` now returns `{ node, extraNodes: Node[], extraEdges: Edge[] }`; `reconcileClusterVms` collects extras into the returned nodes/edges. A helper `buildMemberDisks(cluster, role, memberId): { diskNodes, diskControllers, diskEdges, bootDevices }` mirroring the backend disk shape.

- [ ] **Step 1: Failing test:**
```ts
it("materializes two disks per CP member with correct edge + bootDevices", () => {
  const { node, cluster } = makeCluster("ocp", { x: 0, y: 0 });
  const { nodes, edges } = reconcileClusterVms(cluster, [node]); // now returns {nodes,edges}
  const cp = nodes.find((n) => n.data.clusterRole === "control-plane")!;
  const disks = nodes.filter((n) => n.type === "storageNode" && edges.some(
    (e) => e.source === n.id && e.target === cp.id && e.sourceHandle === "right"));
  expect(disks).toHaveLength(2);
  const dcId = cp.data.diskControllers[0].id;
  const e0 = edges.find((e) => e.target === cp.id && e.targetHandle === `dp-${dcId}-left`);
  expect(e0).toBeTruthy();
  expect(cp.data.bootDevices).toContain(disks.find((d) => d.data.bootable)?.id ?? disks[0].id);
});
```
> NOTE: `reconcileClusterVms` currently returns `Node[]`. This task changes it to return `{ nodes: Node[]; edges: Edge[] }`. Update its two callers (`PropertiesPanel.tsx:4326`, and the Task-2 drop wiring) to consume `.nodes`/`.edges` and merge edges into the store (add/replace edges). Reflect this in Step 4.

- [ ] **Step 2: Run → fail.**

- [ ] **Step 3: Implement `buildMemberDisks`** — for each `DiskSpec` in the role's disk list (`cluster.controlPlaneDisks`/`workerDisks`, default from Task 1), create a `storageNode` + a disk controller on the member + the storage→VM edge:
```ts
function buildMemberDisks(role: "control-plane" | "worker", cluster: ClusterConfig, memberId: string, baseX: number, baseY: number) {
  const specs = (role === "control-plane" ? cluster.controlPlaneDisks : cluster.workerDisks)
    ?? [{ sizeGb: role === "control-plane" ? 120 : 100, bootable: true }];
  const diskNodes: Node[] = [];
  const diskControllers: VMDiskController[] = [];
  const diskEdges: Edge[] = [];
  const bootDevices: string[] = [];
  specs.forEach((spec, i) => {
    const diskId = `${memberId}-disk-${i}`;
    const dcId = generateDiskControllerId();
    diskControllers.push({ id: dcId, name: `disk${i}`, bus: spec.bus ?? "virtio" });
    diskNodes.push({
      id: diskId, type: "storageNode",
      position: { x: baseX, y: baseY + 60 + i * 40 },
      parentId: cluster.nodeId,
      data: { label: `${memberId}-d${i}`, name: `${memberId}-d${i}`, size: spec.sizeGb, format: "qcow2", icon: "🛢" },
    } as Node);
    diskEdges.push({
      id: generateEdgeId(), source: diskId, target: memberId,
      sourceHandle: "right", targetHandle: `dp-${dcId}-left`,
      type: "smoothstep", animated: false, className: "edge-storage-pulse",
      style: { stroke: "rgba(251,191,36,0.6)", strokeWidth: 2, strokeDasharray: "4 4" },
    } as Edge);
    if (spec.bootable) bootDevices.push(diskId);
  });
  if (bootDevices.length === 0 && diskNodes.length) bootDevices.push(diskNodes[0].id);
  return { diskNodes, diskControllers, diskEdges, bootDevices };
}
```
(Use the existing edge-id generator — confirm name, e.g. `generateEdgeId`/`crypto.randomUUID()` prefixed `edge-`; match how `canvasStore` creates edge ids.) In `makeMemberNode`, call `buildMemberDisks`, set `data.diskControllers` + `data.bootDevices` from it, drop the bare `data.disk` number (keep it too for display back-compat if cheap), and return the disk nodes/edges as extras. In `reconcileClusterVms`/`addMembers`, accumulate extras; `removeSurplus` must also remove a removed member's disk storageNodes + their edges.

- [ ] **Step 4: Update callers** — `PropertiesPanel.tsx:4326` and the drop wiring: consume `{nodes, edges}`, push edges into the store (dedupe by id). `applyClusterSizing` unaffected (sizing patch) but should NOT clobber disks.

- [ ] **Step 5: Run → pass.** tsc clean.

- [ ] **Step 6: Commit** — `feat(cluster): per-role multi-disk materialization on canvas (Task 3)`.

---

### Task 4: Frontend — uniform NICs on cluster networks (members + edges)

**Files:** Modify `clusterMaterialize.ts`; Test `clusterMaterialize.test.ts`.

**Interfaces produced:** `buildMemberNics(cluster, memberId, networkNodes): { nics, nicEdges }` — one NIC per `cluster.networkIds`, wired network→VM. Reconcile includes them.

- [ ] **Step 1: Failing test:**
```ts
it("gives each member one NIC per cluster network, wired to the network node", () => {
  const net = { id: "net1", type: "networkNode", data: { subtype: "network", cidr: "10.0.0.0/24" } } as Node;
  const { node, cluster } = makeCluster("ocp", { x: 0, y: 0 });
  cluster.networkIds = ["net1"];
  const { nodes, edges } = reconcileClusterVms(cluster, [node, net]);
  const cp = nodes.find((n) => n.data.clusterRole === "control-plane")!;
  expect(cp.data.nics).toHaveLength(1);
  const nicId = cp.data.nics[0].id;
  const e = edges.find((x) => x.source === "net1" && x.target === cp.id && x.targetHandle === `nic-${nicId}-top`);
  expect(e).toBeTruthy();
  expect(e!.sourceHandle).toBe("bottom");
});
```

- [ ] **Step 2: Run → fail.**

- [ ] **Step 3: Implement `buildMemberNics`:**
```ts
function buildMemberNics(cluster: ClusterConfig, memberId: string) {
  const nets = cluster.networkIds ?? [];
  const nics: VMNic[] = [];
  const nicEdges: Edge[] = [];
  nets.forEach((netId, i) => {
    const nic = { id: generateNicId(), name: `eth${i}`, mac: generateMac(), model: "virtio" };
    nics.push(nic);
    const vmHandle = i === 0 ? "top" : "bottom";
    nicEdges.push({
      id: generateEdgeId(), source: netId, target: memberId,
      sourceHandle: vmHandle === "top" ? "bottom" : "top",
      targetHandle: `nic-${nic.id}-${vmHandle}`,
      type: "smoothstep", animated: true,
      style: { stroke: "rgba(34,211,238,0.5)", strokeWidth: 2, strokeDasharray: "6 4" },
    } as Edge);
  });
  return { nics, nicEdges };
}
```
Wire into `makeMemberNode` (set `data.nics`) + reconcile extras; `removeSurplus` removes NIC edges for removed members. When `networkIds` is empty, members get `nics: []` (current behavior) — no error.

- [ ] **Step 4: Run → pass.** tsc clean. Commit — `feat(cluster): uniform member NICs on cluster networks (Task 4)`.

---

### Task 5: Canvas — attach network(s) to a cluster sets `networkIds`

**Files:** `Canvas.tsx` (edge-connect handling), `PropertiesPanel.tsx` (network multiselect); Test (vitest for the resolver + a store test).

**Interfaces produced:** a pure `clusterNetworkIdsFromEdges(clusterNodeId, memberIds, edges): string[]` fallback; store action `updateCluster` already exists for the explicit selector.

- [ ] **Step 1:** Failing test for `clusterNetworkIdsFromEdges` (given member→network NIC edges, returns the distinct network ids, primary first by member order).
- [ ] **Step 2–3:** Implement the helper; on `onConnect` when a `clusterNode`↔`networkNode` edge is made (or a member NIC connects), call `updateCluster(clusterId, { networkIds })` then re-materialize NICs (Task 4). Add a network multiselect to the cluster editor writing `networkIds`. When `networkIds` is unset at materialize, derive via the helper; else use explicit.
- [ ] **Step 4:** Run → pass; tsc. Commit — `feat(cluster): attach cluster networks (edge + editor) driving member NICs (Task 5)`.

---

### Task 6: Backend — materialize members with per-role disks + uniform NICs (parity)

**Files:** `src/backend/app/services/template_loader.py` (`_make_node`, `materialize_cluster_vms`/`_topup`, and the `_build_vm_data` inputs); Test `test_deploy_template.py`/`test_ocp_clusters.py`.

**Interfaces produced:** `_make_node` (vms_def entry) now emits `disks` (list of `{size_gb, bus, bootable}` from the role's disk list) and `nics` (one per `cluster["networkIds"]`), so the existing `_build_vm_data` creates N storageNodes + disk edges + NIC edges. Members from the backend match the frontend shape.

- [ ] **Step 1: Failing test** — materialize a cluster with `controlPlaneDisks=[{sizeGb:120,bootable:True},{sizeGb:100}]`, `networkIds=["net1"]`; assert the generated topology has, per CP member: 2 storageNodes + 2 disk edges (`sourceHandle:"right"`, `targetHandle` `dp-*-left`), `bootDevices=[<first disk id>]`, and 1 NIC + 1 nic edge (`source:"net1"`, `targetHandle` `nic-*-top`).

- [ ] **Step 2: Run → fail.**

- [ ] **Step 3: Implement** — in `_make_node` (`template_loader.py:220`), replace the inert `disk` with a real `disks` list and add `nics` from the cluster's `networkIds`:
```python
def _make_node(cluster, role, cpu, memory, disks, network_ids):
    return {
        "role": role, "os": "rhcos", "cluster": cluster["name"],
        "vcpus": cpu, "ram_gb": round(memory / 1024),
        "disks": [
            {"size_gb": d["sizeGb"], "bus": d.get("bus", "virtio"), "bootable": d.get("bootable", False)}
            for d in disks
        ],
        "nics": [{"network": nid} for nid in (network_ids or [])],
        "generated": True,
    }
```
Update `_topup`/`materialize_cluster_vms` to pass `normalize_cluster_disks(cluster)`'s `controlPlaneDisks`/`workerDisks` + `networkIds`. Ensure `_build_vm_data` honors `disks[].bootable` for `bootDevices` (today it uses `di==0`; make the FIRST bootable disk the boot device, falling back to disk 0) and maps `nics[].network` → the right network node for `_workload_net_edge`. Keep single-disk/no-network members working (defaults).

- [ ] **Step 4: Run → pass.** black + pyright.

- [ ] **Step 5: Parity test** — a small test asserting the backend member `vmNode.data` keys (`nics`, `diskControllers`, `bootDevices`, `firmware:"uefi"`, `os:"rhcos"`, `clusterRole`, `tags.AnsibleGroup`, `generated`) match the frontend `makeMemberNode` output shape (documented). Commit — `feat(cluster): backend member per-role disks + uniform NICs, parity (Task 6)`.

---

### Task 7: Backend — availability-checked auto-VIP

**Files:** `src/backend/app/services/ocp/agent_template.py`; Test `test_agent_template*.py`.

**Interfaces produced:** `_network_used_ips(topology, cluster, members) -> set[str]`; `pick_unused_ips(cidr, used, count) -> list[str]`. `_derive_cluster_vips` uses them for multi-node; explicit-collision warning.

- [ ] **Step 1: Failing tests:**
```python
def test_pick_unused_ips_top_down_excludes_used():
    from app.services.ocp.agent_template import pick_unused_ips
    used = {"10.0.0.0", "10.0.0.255", "10.0.0.1", "10.0.0.254"}
    assert pick_unused_ips("10.0.0.0/24", used, 2) == ["10.0.0.253", "10.0.0.252"]

def test_derive_vips_avoid_member_ip_collision():
    # a multi-node cluster whose members occupy .2/.3 must NOT get .2/.3 as VIPs
    ...  # build topology with net 10.0.0.0/24, members with nics ip .2 .3 .4; assert VIPs are high + unused
```

- [ ] **Step 2: Run → fail.**

- [ ] **Step 3: Implement:**
```python
def pick_unused_ips(cidr: str, used: set[str], count: int) -> list[str]:
    net = ipaddress.ip_network(cidr, strict=False)
    picked: list[str] = []
    for host in reversed(list(net.hosts())):
        ip = str(host)
        if ip in used:
            continue
        picked.append(ip)
        if len(picked) == count:
            return picked
    raise ValueError(f"no {count} free IPs in {cidr} (used={len(used)})")


def _network_used_ips(topology, cluster, members) -> set[str]:
    node = _cluster_network_node(cluster, topology)  # networkIds[0] -> node, else _cidr_for_members' node
    net = ipaddress.ip_network(node["data"]["cidr"], strict=False)
    used = {str(net.network_address), str(net.broadcast_address)}
    used.add(node["data"].get("gateway") or str(net.network_address + 1))
    # DHCP range
    if node["data"].get("dhcp"):
        # mirror computeDhcpBounds / deploy_topology
        ...
    for n in topology.get("nodes", []):
        for nic in (n.get("data", {}).get("nics") or []):
            if nic.get("ip"):
                used.add(nic["ip"])
    for other in _ocp_clusters(topology):
        if other.get("id") != cluster.get("id"):
            used.update(v for v in (other.get("apiVip"), other.get("ingressVip")) if v)
    return used
```
`_derive_cluster_vips` multi-node branch: `api, ing = pick_unused_ips(cidr, _network_used_ips(...), 2)`. `resolve_cluster_vips` unchanged shape (explicit per-side wins; SNO node-IP). Add `_warn_vip_collision(cluster, used)` logging when an explicit VIP ∈ used (never raise).

- [ ] **Step 4: Run → pass** (+ existing agent_template golden/SNO tests stay green — SNO + explicit paths untouched). black + pyright. Commit — `feat(ocp): availability-checked auto-VIP with collision warning (Task 7)`.

---

### Task 8: Frontend cluster editor — per-role disk-list editor + VIP auto-suggest + inline collision warning

**Files:** `PropertiesPanel.tsx` (cluster editor), reuse `src/frontend/src/lib/dhcpIpAssignment.ts`; Test `PropertiesPanelCluster.test.tsx`.

**Interfaces produced:** `suggestClusterVips(cluster, nodes): { apiVip, ingressVip }` using `collectUsedIps` + `listCidrHosts` (top-down) + the cluster's machine-network CIDR; `vipCollision(ip, cluster, nodes): boolean`.

**Also in this task — per-role disk-list editor:** add a control-plane and a worker **disk list** editor (add / remove / size-GB / bootable toggle / bus) writing `cluster.controlPlaneDisks` / `cluster.workerDisks` via `updateCluster`, and re-running `reconcileClusterVms` (Task 3) on change so members' storage nodes/edges update live. Include a vitest asserting: adding a disk to `controlPlaneDisks` and reconciling yields an extra storageNode + disk edge per CP member; removing one prunes them.

- [ ] **Step 1: Failing test** — given a network `10.0.0.0/24` with members using `.2/.3`, `suggestClusterVips` returns two high unused IPs (e.g. `.253/.252`); a typed VIP equal to a member IP flags `vipCollision`.
- [ ] **Step 2–3: Implement** — `suggestClusterVips` mirrors backend `pick_unused_ips` (reverse host scan) using `collectUsedIps(nodes)` ∪ gateway ∪ other-cluster VIPs; when the editor's `apiVip`/`ingressVip` is empty and the cluster is multi-node, auto-fill (editable); clearing re-suggests; write to `clusters[]` via `updateCluster`. Extend the existing inline VIP warning (Plan 2) to also flag `vipCollision`.
- [ ] **Step 4: Run → pass; tsc; no new eslint.** Commit — `feat(cluster): editor auto-suggests unused VIPs + inline collision warning (Task 8)`.

---

### Task 9: Frontend — auto-size boundary + grid reflow

**Files:** `clusterMaterialize.ts` (compute size), `ClusterNode.tsx` (consume), the reconcile path; Test `clusterMaterialize.test.ts`.

**Interfaces produced:** `clusterBoxSize(memberCount): { width, height }` and reflow of member `position` on a capped grid; reconcile sets the clusterNode `style.width/height`.

- [ ] **Step 1: Failing test** — a cluster reconciled to 6 workers yields a clusterNode whose `style.height` > the fixed 320 and members laid out on ≤4-per-row grid inside (no member y beyond height).
- [ ] **Step 2–3: Implement:**
```ts
const CELL_W = 130, CELL_H = 130, PAD = 30, HEADER_H = 48, COLS_MAX = 4;
export function clusterBoxSize(count: number) {
  const cols = Math.max(1, Math.min(COLS_MAX, count));
  const rows = Math.max(1, Math.ceil(count / cols));
  return { width: 2 * PAD + cols * CELL_W, height: HEADER_H + PAD + rows * CELL_H };
}
```
Reflow members (CPs first, then workers) into `(col,row)` positions; set the clusterNode style size in `reconcileClusterVms` after add/remove. Recompute on drag-in/out (Task 5/Plan-2 reparent path) and disk/nic changes. `ClusterNode` keeps `width/height:100%`.
- [ ] **Step 4: Run → pass; tsc.** Commit — `feat(cluster): auto-size boundary + grid reflow (Task 9)`.

---

### Task 10: Export / round-trip + parity + regression

**Files:** `src/backend/app/api/projects.py` + `patterns.py` export synth (carry `networkIds` as network **names** + `*Disks`); `template_loader` import (resolve names→ids); Tests `test_api_projects`/`test_patterns` + a cross-side parity test; full regression.

- [ ] **Step 1:** Failing round-trip test — export a project whose cluster has `networkIds`+`controlPlaneDisks`, re-import, assert the cluster + a materialized member's disks/NICs survive (names resolve back to ids).
- [ ] **Step 2–3:** Extend export to include `network` (name of `networkIds[0]`, or a list) + per-role `disks` in the `ocp:` entry; import resolves network names→node ids and runs `normalize_cluster_disks`. Non-OCP export unchanged.
- [ ] **Step 4:** Parity test — assert backend-materialized and frontend-materialized member `data` keys match for the same cluster config (document the canonical key set).
- [ ] **Step 5:** Full regression — `cd src/backend && ./venv/bin/python3 -m pytest -q` green; `cd src/frontend && npx vitest run` green; `npx tsc --noEmit` + `pyright` clean. Commit — `feat(cluster): export round-trip networkIds+disks + parity + regression (Task 10)`.

---

## Self-Review

**Spec coverage:** §5A materialize-on-drop → T2. §5B uniform networks (anchor) → T4 (member NICs) + T5 (attach) + T1 (`networkIds`). §5C per-role multi-disk → T1 (model) + T3 (frontend) + T6 (backend) + T8-editor(disks handled in T3/T6; a disk-list editor UI — FOLD into T8's editor work or add here). §5D auto-VIP → T7 (backend) + T8 (frontend). §5E auto-size → T9. §9 back-compat → T1 (legacy disk upgrade, networkIds derive) + T6/T7 defaults. §10 tests per task. Export §7 → T10.

**Gap found in self-review:** the per-role **disk-list editor UI** (add/remove/size in the cluster properties panel) isn't its own task — FOLD it into Task 8 (rename Task 8 to "editor: disk-list + VIP suggest + collision") or add Task 8b. Implementer: add the disk-list editor alongside the VIP suggest in `PropertiesPanel` cluster editor, writing `controlPlaneDisks`/`workerDisks` and re-materializing (Task 3) on change.

**Placeholder scan:** the `_network_used_ips` DHCP-range block and the `clusterNetworkIdsFromEdges`/edge-id-generator names are marked "confirm/mirror" — implementers must read the referenced `dhcpIpAssignment.computeDhcpBounds` / `canvasStore` edge-id creation and reproduce exactly; not free-form.

**Type consistency:** `DiskSpec` (T1) used in T3/T6/T8. `reconcileClusterVms` return type changes to `{nodes,edges}` in T3 — T2/T5/T9 and the two existing callers must all use the new shape (called out in T3 Step 1 NOTE + T3 Step 4). `networkIds` (T1) used in T4/T5/T6/T7/T8/T10. Edge/NIC/disk shapes identical across T3/T4 (frontend) and T6 (backend) per Global Constraints.
