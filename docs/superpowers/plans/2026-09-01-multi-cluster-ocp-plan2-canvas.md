# Multi-Cluster OCP — Plan 2: Canvas & Frontend

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make an OCP cluster a first-class visual object on the canvas: a resizable `clusterNode` group boundary whose member VMs render inside it, created from the palette, populated by setting control-plane/worker counts (which materialize real editable VMs) or by dragging existing VMs in, and configured (name, type, VIPs, per-role sizing, base domain, version) in the Properties panel.

**Architecture:** Frontend-only (Next.js 15 App Router, `@xyflow/react` v12, Zustand v5), plus one small backend rename. Cluster membership uses React Flow v12 parent/child nesting via `parentId` (introduced here for the first time in this codebase). The persisted topology already carries `topology.clusters[]` and per-VM `data.clusterId` (Plan 1); this plan makes the Zustand store persist/load those, renders the boundary, and adds the editing UX. A new Vitest + Testing Library stack is stood up first so every task is TDD.

**Tech Stack:** TypeScript, `@xyflow/react` ^12.11.0, `zustand` ^5, Vitest, @testing-library/react, jsdom. Backend: Python (one rename).

**Spec:** `docs/superpowers/specs/2026-09-01-multi-cluster-ocp-and-bastionless-install-design.md` (§5 Canvas & frontend; decisions #2, #3, #4, #5, #11)
**Predecessor:** Plan 1 (`docs/superpowers/plans/2026-09-01-multi-cluster-ocp-plan1-data-model.md`) — data model landed on this branch.

## Global Constraints

- **React Flow v12 uses `parentId`, NOT `parentNode`.** Verified in `node_modules/@xyflow/system/dist/esm/types/nodes.d.ts:50`. All parent/child nesting uses `node.parentId` + `extent: "parent"`.
- **Control-plane count is 1 or 3 only** (sno→1, compact/standard→3); worker count is free. Mirror Plan 1's backend semantics exactly.
- **Materialization is existence-aware** — never overwrite an existing/enumerated VM; add only the shortfall using the next free `<clusterId>-<prefix>-<i>` name. (Mirror Plan 1's `_topup`.)
- **RHCOS-only** cluster members (`os: "rhcos"`).
- Per-role sizing defaults (must match Plan 1 exactly): control-plane cpu 8 / memory 16384 / disk 120; worker cpu 4 / memory 8192 / disk 100.
- **Follow existing frontend patterns** (map below). New node component mirrors `VMNode.tsx`; store edits go through the existing `updateNodeData`/`addNode` actions; the Properties editor uses the existing `update(field,value)` helper.
- Do NOT restructure `canvasStore.ts` / `PropertiesPanel.tsx` — they are large but this plan adds focused blocks, not refactors.
- Run tests: `cd /Users/prutledg/troshka/src/frontend && npm run test` (added in Task 2). Backend rename tests: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_ocp_clusters.py -v`.
- `npm run lint` and `npm run build` must pass before the plan is done (Task 11).
- Git via absolute paths / `cd /Users/prutledg/troshka && git ...`. NO Co-Authored-By lines. Run system `black` on any touched Python.

## Codebase map (verified)

- `nodeTypes` registry: `src/frontend/src/components/canvas/Canvas.tsx:52-57` (module scope).
- Node component pattern: `src/frontend/src/components/canvas/nodes/VMNode.tsx` (`({id,data,selected}: NodeProps)`, `data as unknown as VMNodeData`, `export default memo(...)`). No `NodeResizer` used anywhere yet.
- Store: `src/frontend/src/stores/canvasStore.ts`. Types at `:82-200` (`VMNodeData:88-105` has `[key:string]: any`; union `CanvasNodeData:180`). Actions: `addNode:1099`, `updateNodeData:1121`, `deleteNode:1176`, `onNodesChange:668`. Serializer `_saveTopologyToApi:1825-1857` (writes only nodes/edges/hiddenNodeIds/startOrder/externalIps/showroom; `cleanNodes:1836`). Debounce+`topoKey`: `:1932-1961`. Loader `loadProject` parse block `:1309-1399`.
- Palette: `src/frontend/src/components/canvas/Palette.tsx` (`PaletteItemDef:32`, `sections:46`, dragStart sets `application/troshka-node`:205). Drop handler `Canvas.tsx:195-449` (if/else on `item.type`, ends `addNode(newNode):446`).
- PropertiesPanel: `src/frontend/src/components/canvas/PropertiesPanel.tsx`. Selection/write `:373-425` (`update(field,value)→updateNodeData:420`). Node-type guard blocks: vmNode `:477-1587`, containerNode `:1590`, networkNode `:3041`, storageNode `:3901`. OCP section `:1472-1520`; tags editor `:1523-1585`; header icon/subtitle `:430-471`.
- Create-project OCP form: `src/frontend/src/app/projects/page.tsx:447-501`; state `:106-110`; payload `:206-223`.

---

### Task 1: Backend — rename `parentNode` → `parentId` (React Flow v12 alignment)

**Files:**
- Modify: `src/backend/app/services/template_loader.py` (generator: `_stamp_cluster_membership`, `_build_cluster_boundary_nodes` — wherever `parentNode` is set)
- Modify: `src/backend/app/services/ocp/cluster_migration.py` (`migrate_topology_clusters`)
- Modify: `src/backend/app/api/patterns.py` (`_remap_clusters` — the `parentNode` remap line)
- Modify: `src/backend/tests/test_ocp_clusters.py` (all `parentNode` assertions)

**Interfaces:**
- Produces: persisted topology uses `node["parentId"]` (string, = the cluster node id) for cluster members instead of `node["parentNode"]`. `clusterId`/`nodeId`/`clusters[]` unchanged.

- [ ] **Step 1: Find every occurrence**

Run: `cd /Users/prutledg/troshka && grep -rn "parentNode" src/backend/app src/backend/tests`
Expected: matches in template_loader.py, cluster_migration.py, patterns.py, test_ocp_clusters.py.

- [ ] **Step 2: Update the tests first (they encode the contract)**

In `src/backend/tests/test_ocp_clusters.py`, change every `parentNode` to `parentId` (assertions and any test fixtures that set it). Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_ocp_clusters.py -v` — expect FAILURES where the implementation still writes `parentNode`.

- [ ] **Step 3: Rename in implementation**

Use the Edit tool (NOT sed) to change `parentNode` → `parentId` in the three backend modules. In `patterns.py._remap_clusters`, the line remapping `node.get("parentNode")` becomes `node.get("parentId")` and `node["parentId"] = id_map[...]`. Keep the value semantics identical (still `f"cluster-{id}"` at generation/migration time).

- [ ] **Step 4: Green**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_ocp_clusters.py tests/test_template_loader.py -v` — all pass. Then `cd /Users/prutledg/troshka && grep -rn "parentNode" src/backend/app src/backend/tests` returns nothing.

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && black src/backend/app/services/template_loader.py src/backend/app/services/ocp/cluster_migration.py src/backend/app/api/patterns.py && git add src/backend/app/services/template_loader.py src/backend/app/services/ocp/cluster_migration.py src/backend/app/api/patterns.py src/backend/tests/test_ocp_clusters.py && git commit -m "refactor(ocp): use parentId (React Flow v12) for cluster membership"
```

---

### Task 2: Stand up the frontend test stack (Vitest + Testing Library)

**Files:**
- Modify: `src/frontend/package.json` (devDeps + `test` scripts)
- Create: `src/frontend/vitest.config.ts`
- Create: `src/frontend/vitest.setup.ts`
- Create: `src/frontend/src/stores/__tests__/smoke.test.ts`

**Interfaces:**
- Produces: `npm run test` (once) and `npm run test:watch` run Vitest with jsdom + Testing Library matchers. A passing smoke test proves the harness works. Later tasks add `*.test.ts`/`*.test.tsx` beside the code they cover.

- [ ] **Step 1: Add dev dependencies**

Run:
```bash
cd /Users/prutledg/troshka/src/frontend && npm install -D vitest@^2 @testing-library/react@^16 @testing-library/jest-dom@^6 @testing-library/user-event@^14 jsdom@^25 @vitejs/plugin-react@^4
```
(If the registry pins differ, accept the resolved compatible versions.)

- [ ] **Step 2: Write the config**

Create `src/frontend/vitest.config.ts`:
```ts
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "path";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./vitest.setup.ts"],
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
  },
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
});
```
Create `src/frontend/vitest.setup.ts`:
```ts
import "@testing-library/jest-dom/vitest";
```
Confirm the `@/...` alias matches `tsconfig.json` `paths` (read it; if the project uses a different alias, mirror it here).

- [ ] **Step 3: Add scripts**

In `src/frontend/package.json` `"scripts"`, add:
```json
"test": "vitest run",
"test:watch": "vitest"
```

- [ ] **Step 4: Smoke test (write, then run)**

Create `src/frontend/src/stores/__tests__/smoke.test.ts`:
```ts
import { describe, it, expect } from "vitest";

describe("vitest harness", () => {
  it("runs", () => {
    expect(1 + 1).toBe(2);
  });
});
```
Run: `cd /Users/prutledg/troshka/src/frontend && npm run test`
Expected: 1 passing test; jsdom environment loads without error.

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && git add src/frontend/package.json src/frontend/package-lock.json src/frontend/vitest.config.ts src/frontend/vitest.setup.ts src/frontend/src/stores/__tests__/smoke.test.ts && git commit -m "test(frontend): add vitest + testing-library harness"
```

---

### Task 3: Types + cluster persistence in the Zustand store

**Files:**
- Modify: `src/frontend/src/stores/canvasStore.ts` (types `:82-200`; `_saveTopologyToApi:1825`; `loadProject` parse `:1309-1399`; `topoKey` `:1951`)
- Test: `src/frontend/src/stores/__tests__/clusterPersistence.test.ts`

**Interfaces:**
- Produces:
  - `ClusterNodeData` interface + `ClusterConfig` type (the `topology.clusters[]` element shape from Plan 1: `id,name,nodeId,type,controlPlane,workers,controlPlaneCpu,controlPlaneMemory,controlPlaneDisk,workerCpu,workerMemory,workerDisk,baseDomain,apiVip,ingressVip,ocpVersion,pullThroughRegistry`). `VMNodeData` gains explicit `clusterId?: string` and `clusterRole?: "control-plane" | "worker"`.
  - Store state gains `clusters: ClusterConfig[]` with actions `setClusters`, `updateCluster(id, patch)`, `addCluster`, `removeCluster`.
  - `_saveTopologyToApi` includes `topology.clusters`; `loadProject` reads `t.clusters` into state; `topoKey` includes a serialization of `clusters` so cluster edits trigger auto-save.

- [ ] **Step 1: Write failing tests**

Create `src/frontend/src/stores/__tests__/clusterPersistence.test.ts`:
```ts
import { describe, it, expect, beforeEach, vi } from "vitest";
import { useCanvasStore } from "@/stores/canvasStore";

// helper: reset store between tests
beforeEach(() => {
  useCanvasStore.setState({ nodes: [], edges: [], clusters: [] } as any);
});

describe("cluster persistence in store", () => {
  it("addCluster / updateCluster / removeCluster mutate state", () => {
    const s = useCanvasStore.getState();
    s.addCluster({ id: "prod", name: "prod", nodeId: "cluster-prod", type: "standard", controlPlane: 3, workers: 2 } as any);
    expect(useCanvasStore.getState().clusters).toHaveLength(1);
    useCanvasStore.getState().updateCluster("prod", { workers: 3 });
    expect(useCanvasStore.getState().clusters[0].workers).toBe(3);
    useCanvasStore.getState().removeCluster("prod");
    expect(useCanvasStore.getState().clusters).toHaveLength(0);
  });

  it("_saveTopologyToApi includes clusters and node clusterId/parentId", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
    vi.stubGlobal("fetch", fetchMock);
    useCanvasStore.setState({
      nodes: [{ id: "n1", type: "vmNode", position: { x: 0, y: 0 }, parentId: "cluster-prod", data: { os: "rhcos", clusterId: "prod" } }],
      edges: [], hiddenNodeIds: [], startOrder: [], externalIps: [],
      clusters: [{ id: "prod", name: "prod", nodeId: "cluster-prod", type: "standard", controlPlane: 3, workers: 2 }],
    } as any);
    await (useCanvasStore.getState() as any)._saveTopologyToApi("proj1", useCanvasStore.getState());
    const body = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(body.topology.clusters).toHaveLength(1);
    const savedNode = body.topology.nodes[0];
    expect(savedNode.parentId).toBe("cluster-prod");
    expect(savedNode.data.clusterId).toBe("prod");
    vi.unstubAllGlobals();
  });
});
```
(Read the real `_saveTopologyToApi` signature first; adapt the call to match how it is exposed. If it is not on the store object, export a testable wrapper or call the store method that triggers it.)

- [ ] **Step 2: Run to confirm failure**

Run: `cd /Users/prutledg/troshka/src/frontend && npm run test -- clusterPersistence`
Expected: FAIL (`addCluster` not a function; `clusters` undefined; body has no `clusters`).

- [ ] **Step 3: Implement**

- Add `ClusterConfig` + `ClusterNodeData` types near `:180`; add to the `CanvasNodeData` union. Add `clusterId?`/`clusterRole?` to `VMNodeData`.
- Add `clusters: ClusterConfig[]` to `CanvasState` (default `[]`), plus `setClusters/addCluster/updateCluster/removeCluster` actions following the existing action style (immutable update, `pushHistory()` where the other mutators do, recompute `topologyDirty`).
- In `_saveTopologyToApi`, add `topology.clusters = state.clusters;` alongside the existing keys. Ensure `cleanNodes` preserves `parentId` (it spreads the node, so it does — verify it doesn't strip it).
- In `loadProject` parse block, read `clusters: Array.isArray(t.clusters) ? t.clusters : []` into state.
- In the `topoKey` builder (`:1951`), append `JSON.stringify(state.clusters)` (or a stable subset) so cluster edits trigger the debounced save.

- [ ] **Step 4: Green**

Run: `cd /Users/prutledg/troshka/src/frontend && npm run test -- clusterPersistence`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && git add src/frontend/src/stores/canvasStore.ts src/frontend/src/stores/__tests__/clusterPersistence.test.ts && git commit -m "feat(canvas): persist topology.clusters + clusterId/parentId in store"
```

---

### Task 4: `ClusterNode` component + registration

**Files:**
- Create: `src/frontend/src/components/canvas/nodes/ClusterNode.tsx`
- Modify: `src/frontend/src/components/canvas/Canvas.tsx:52-57` (register `clusterNode`)
- Test: `src/frontend/src/components/canvas/nodes/__tests__/ClusterNode.test.tsx`

**Interfaces:**
- Consumes: `ClusterNodeData` (Task 3).
- Produces: a `clusterNode` React Flow node type — a resizable boundary (via `NodeResizer`) rendering a header ("`<name>` · `<type>` · `<cp>cp/<workers>wrk`") and a transparent body children render into. Registered in `nodeTypes`.

- [ ] **Step 1: Write failing render test**

Create `src/frontend/src/components/canvas/nodes/__tests__/ClusterNode.test.tsx`:
```tsx
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { ReactFlowProvider } from "@xyflow/react";
import ClusterNode from "../ClusterNode";

function renderNode(data: any) {
  return render(
    <ReactFlowProvider>
      {/* @ts-expect-error minimal NodeProps for test */}
      <ClusterNode id="cluster-prod" selected={false} data={data} />
    </ReactFlowProvider>,
  );
}

describe("ClusterNode", () => {
  it("shows the cluster name and a type/count badge", () => {
    renderNode({ name: "prod", type: "standard", controlPlane: 3, workers: 2 });
    expect(screen.getByText(/prod/)).toBeInTheDocument();
    expect(screen.getByText(/standard/)).toBeInTheDocument();
    expect(screen.getByText(/3cp/)).toBeInTheDocument();
    expect(screen.getByText(/2\s*wrk/)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Confirm failure**

Run: `cd /Users/prutledg/troshka/src/frontend && npm run test -- ClusterNode`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement the component**

Create `ClusterNode.tsx` mirroring `VMNode.tsx`'s shape: `function ClusterNodeComponent({ id, data, selected }: NodeProps)`, `const d = data as unknown as ClusterNodeData;`, render:
- `<NodeResizer isVisible={selected} minWidth={280} minHeight={180} />` (import from `@xyflow/react`).
- a root `div` styled as a translucent boundary with a labeled header (name + `${d.type}` + `${d.controlPlane}cp/${d.workers}wrk`), selection styling keyed on `selected` (mirror VMNode's border/boxShadow approach). Give the body low z-index / `pointer-events` so children remain interactive.
- `export default memo(ClusterNodeComponent);`
Register in `Canvas.tsx` `nodeTypes`: add `clusterNode: ClusterNode,` and import it.

- [ ] **Step 4: Green**

Run: `cd /Users/prutledg/troshka/src/frontend && npm run test -- ClusterNode`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && git add src/frontend/src/components/canvas/nodes/ClusterNode.tsx src/frontend/src/components/canvas/Canvas.tsx src/frontend/src/components/canvas/nodes/__tests__/ClusterNode.test.tsx && git commit -m "feat(canvas): ClusterNode resizable boundary component"
```

---

### Task 5: Palette item + drop creates an empty cluster

**Files:**
- Modify: `src/frontend/src/components/canvas/Palette.tsx` (`sections`)
- Modify: `src/frontend/src/components/canvas/Canvas.tsx` (onDrop branch ~`:232-446`)
- Test: `src/frontend/src/components/canvas/__tests__/clusterDrop.test.ts`

**Interfaces:**
- Consumes: `addNode` (store), `addCluster` (Task 3).
- Produces: dropping the "OCP Cluster" palette item creates a `clusterNode` node (id `cluster-<slug>-<short>`) AND a matching `ClusterConfig` in `state.clusters` with defaults (type `standard`, controlPlane 3, workers 0, sizing defaults, empty VIPs, base domain `ocp.local`). The node and the cluster share `nodeId`.

- [ ] **Step 1: Write failing test**

Extract the cluster-creation logic into a pure helper so it can be unit-tested without a DOM drag. Create `src/frontend/src/components/canvas/clusterFactory.ts` exporting `makeCluster(name: string, position: {x:number;y:number}): { node: Node; cluster: ClusterConfig }`. Test `clusterDrop.test.ts`:
```ts
import { describe, it, expect } from "vitest";
import { makeCluster } from "@/components/canvas/clusterFactory";

describe("makeCluster", () => {
  it("creates a clusterNode + matching ClusterConfig with defaults", () => {
    const { node, cluster } = makeCluster("prod", { x: 10, y: 20 });
    expect(node.type).toBe("clusterNode");
    expect(node.id).toBe(cluster.nodeId);
    expect(cluster.type).toBe("standard");
    expect(cluster.controlPlane).toBe(3);
    expect(cluster.workers).toBe(0);
    expect(cluster.controlPlaneCpu).toBe(8);
    expect(cluster.workerCpu).toBe(4);
    expect(cluster.baseDomain).toBe("ocp.local");
  });
});
```

- [ ] **Step 2: Confirm failure** — `npm run test -- clusterDrop` → FAIL (module missing).

- [ ] **Step 3: Implement**

- `clusterFactory.ts`: `makeCluster` builds the node (`type:"clusterNode"`, `position`, `style:{width:520,height:320}`, `data` = summary) and the `ClusterConfig` (nodeId = node.id), sharing a slug id. Use the Plan-1 default constants (define them here as TS consts matching Plan 1's values exactly).
- `Palette.tsx`: add an "OCP Cluster" item to a suitable section (e.g. "Compute" or a new "OpenShift" section) with `type: "cluster"`.
- `Canvas.tsx` onDrop: add `else if (item.type === "cluster")` → `const { node, cluster } = makeCluster("ocp", position); addNode(node); addCluster(cluster);`. (Read the drop handler to reuse its id/position conventions.)

- [ ] **Step 4: Green** — `npm run test -- clusterDrop` → PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && git add src/frontend/src/components/canvas/clusterFactory.ts src/frontend/src/components/canvas/Palette.tsx src/frontend/src/components/canvas/Canvas.tsx src/frontend/src/components/canvas/__tests__/clusterDrop.test.ts && git commit -m "feat(canvas): OCP Cluster palette item + drop creates boundary"
```

---

### Task 6: Count → materialize member VMs (frontend, existence-aware)

**Files:**
- Create: `src/frontend/src/components/canvas/clusterMaterialize.ts`
- Test: `src/frontend/src/components/canvas/__tests__/clusterMaterialize.test.ts`

**Interfaces:**
- Produces: `reconcileClusterVms(cluster: ClusterConfig, nodes: Node[]): Node[]` — a PURE function returning the new nodes array so that the cluster has exactly `controlPlane` control-plane VMs and `workers` worker VMs. Adds missing ones (RHCOS, `parentId`=cluster.nodeId, `data.clusterId`=cluster.id, `data.clusterRole`, sizing from the cluster's per-role fields, next-free `<id>-<prefix>-<n>` name, position laid out inside the boundary) and removes surplus AUTO-generated ones (never removes a VM the user customized — treat a VM as removable only if its name matches the generated pattern AND it has no user edits flag; simplest: only auto-remove nodes whose id was generated by this function, tracked via `data.generated === true`). Mirrors Plan 1's existence-aware top-up.

- [ ] **Step 1: Write failing tests**
```ts
import { describe, it, expect } from "vitest";
import { reconcileClusterVms } from "@/components/canvas/clusterMaterialize";

const cluster = { id: "prod", nodeId: "cluster-prod", type: "standard", controlPlane: 3, workers: 2, controlPlaneCpu: 8, controlPlaneMemory: 16384, controlPlaneDisk: 120, workerCpu: 4, workerMemory: 8192, workerDisk: 100 } as any;

describe("reconcileClusterVms", () => {
  it("adds missing cp/workers with rhcos + parentId + sizing", () => {
    const out = reconcileClusterVms(cluster, []);
    const cps = out.filter((n) => n.data.clusterRole === "control-plane");
    const wks = out.filter((n) => n.data.clusterRole === "worker");
    expect(cps).toHaveLength(3);
    expect(wks).toHaveLength(2);
    expect(cps[0].parentId).toBe("cluster-prod");
    expect(cps[0].data.os).toBe("rhcos");
    expect(cps[0].data.clusterId).toBe("prod");
    expect(cps[0].data.vcpus ?? cps[0].data.cpu).toBe(8);
  });
  it("does not remove a user-customized member when count shrinks", () => {
    const custom = { id: "prod-cp-2", type: "vmNode", position: { x: 0, y: 0 }, parentId: "cluster-prod", data: { os: "rhcos", clusterId: "prod", clusterRole: "control-plane", generated: false, vcpus: 32 } };
    const out = reconcileClusterVms({ ...cluster, controlPlane: 1 } as any, [custom as any]);
    expect(out.find((n) => n.id === "prod-cp-2")).toBeTruthy(); // preserved
  });
  it("is idempotent", () => {
    const once = reconcileClusterVms(cluster, []);
    const twice = reconcileClusterVms(cluster, once);
    expect(twice.filter((n) => n.data.clusterRole === "control-plane")).toHaveLength(3);
  });
});
```

- [ ] **Step 2: Confirm failure** — `npm run test -- clusterMaterialize` → FAIL.

- [ ] **Step 3: Implement** `reconcileClusterVms` per the interface. Match Plan 1's field names for a VM node's `data` (read `VMNodeData` — use `vcpus`/`ram` if that's what VMNode expects, mapping cpu→vcpus, memory→ram; confirm against `VMNodeData:88-105`). Only auto-remove nodes with `data.generated === true`; mark generated nodes with `generated: true`.

- [ ] **Step 4: Green** — `npm run test -- clusterMaterialize` → PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && git add src/frontend/src/components/canvas/clusterMaterialize.ts src/frontend/src/components/canvas/__tests__/clusterMaterialize.test.ts && git commit -m "feat(canvas): count-driven cluster VM materialization (existence-aware)"
```

---

### Task 7: Drag membership — assign/clear clusterId + parentId on drop

**Files:**
- Create: `src/frontend/src/components/canvas/clusterMembership.ts`
- Modify: `src/frontend/src/components/canvas/Canvas.tsx` (wire the `onNodeDragStop` React Flow handler)
- Test: `src/frontend/src/components/canvas/__tests__/clusterMembership.test.ts`

**Interfaces:**
- Produces: `resolveMembership(draggedNode: Node, clusterNodes: Node[]): { parentId: string | null; clusterId: string | null }` — a PURE function: given a dragged vmNode's absolute position and the cluster boundary nodes (with position+measured width/height), returns the cluster it now sits inside (topmost containing boundary) or `{null,null}` if outside all. Canvas wires `onNodeDragStop` to call this for `vmNode`s and, on change, `updateNodeData(id,{clusterId})` + set `node.parentId` (via an onNodesChange/replace) — only for RHCOS vmNodes.

- [ ] **Step 1: Write failing tests**
```ts
import { describe, it, expect } from "vitest";
import { resolveMembership } from "@/components/canvas/clusterMembership";

const boundary = { id: "cluster-prod", type: "clusterNode", position: { x: 0, y: 0 }, width: 500, height: 300, data: { clusterId: "prod" } } as any;

describe("resolveMembership", () => {
  it("assigns when the node center is inside a boundary", () => {
    const vm = { id: "n1", type: "vmNode", position: { x: 100, y: 100 }, data: { os: "rhcos" } } as any;
    expect(resolveMembership(vm, [boundary])).toEqual({ parentId: "cluster-prod", clusterId: "prod" });
  });
  it("clears when the node is outside all boundaries", () => {
    const vm = { id: "n1", type: "vmNode", position: { x: 900, y: 900 }, data: { os: "rhcos" } } as any;
    expect(resolveMembership(vm, [boundary])).toEqual({ parentId: null, clusterId: null });
  });
});
```
(Note the boundary width/height source: in @xyflow/react v12 measured size is on `node.measured?.width/height`; the helper should accept explicit width/height and the Canvas wiring passes `node.measured ?? node.width/height`. Encode that in the helper signature.)

- [ ] **Step 2: Confirm failure** — `npm run test -- clusterMembership` → FAIL.

- [ ] **Step 3: Implement** `resolveMembership` (point-in-rect against each boundary; pick the smallest/topmost containing one). Wire `onNodeDragStop` in `Canvas.tsx`: for a dragged `vmNode` with `os==="rhcos"`, compute membership, and if changed, update the node's `parentId` and `data.clusterId` (respect React Flow's relative-position rule when reparenting — convert absolute↔relative position so the node doesn't jump; if that is complex, keep absolute extent by not using `extent:"parent"` on assignment and store parentId only for grouping). Keep the handler under complexity 15 (delegate to the pure helper).

- [ ] **Step 4: Green** — `npm run test -- clusterMembership` → PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && git add src/frontend/src/components/canvas/clusterMembership.ts src/frontend/src/components/canvas/Canvas.tsx src/frontend/src/components/canvas/__tests__/clusterMembership.test.ts && git commit -m "feat(canvas): drag a VM into/out of a cluster boundary to set membership"
```

---

### Task 8: PropertiesPanel — cluster config editor

**Files:**
- Modify: `src/frontend/src/components/canvas/PropertiesPanel.tsx` (new `nodeType === "clusterNode"` guard block; header case `:430-471`)
- Test: `src/frontend/src/components/canvas/__tests__/PropertiesPanelCluster.test.tsx`

**Interfaces:**
- Consumes: `updateCluster` (Task 3), `reconcileClusterVms` (Task 6), the selected `clusterNode`.
- Produces: when a `clusterNode` is selected, the panel edits: name, type (sno/compact/standard — changing type sets controlPlane 1 or 3), worker count (number), per-role cpu/mem/disk, base domain, api_vip, ingress_vip, OCP version. Edits call `updateCluster` AND update the clusterNode's `data` summary; changing type/worker count triggers `reconcileClusterVms`. VIP fields validated for cross-cluster uniqueness (show inline error, don't block typing).

- [ ] **Step 1: Write failing component test**
```tsx
import { describe, it, expect, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useCanvasStore } from "@/stores/canvasStore";
import PropertiesPanel from "@/components/canvas/PropertiesPanel";

beforeEach(() => {
  useCanvasStore.setState({
    nodes: [{ id: "cluster-prod", type: "clusterNode", position: { x: 0, y: 0 }, data: { name: "prod", type: "standard", controlPlane: 3, workers: 2, baseDomain: "ocp.local", apiVip: "", ingressVip: "" } }],
    edges: [], selectedNodeId: "cluster-prod",
    clusters: [{ id: "prod", nodeId: "cluster-prod", name: "prod", type: "standard", controlPlane: 3, workers: 2, baseDomain: "ocp.local" }],
  } as any);
});

describe("PropertiesPanel cluster editor", () => {
  it("edits worker count into the cluster", async () => {
    render(<PropertiesPanel />);
    const workers = screen.getByLabelText(/workers/i);
    await userEvent.clear(workers);
    await userEvent.type(workers, "4");
    // blur to commit
    workers.blur();
    expect(useCanvasStore.getState().clusters[0].workers).toBe(4);
  });
});
```
(Adapt to the real component's props/how it reads selection — it uses `selectedNodeId` from the store, so rendering `<PropertiesPanel/>` with store state set should work. Confirm the component is a default export with no required props.)

- [ ] **Step 2: Confirm failure** — `npm run test -- PropertiesPanelCluster` → FAIL (no cluster editor; no workers field).

- [ ] **Step 3: Implement** the `nodeType === "clusterNode"` block as a sibling to the existing guards (`:1590` etc.), plus a header icon/subtitle case. Reuse the existing collapsible-section + `update(...)` patterns; for cluster edits also call `updateCluster(clusterId, patch)` (resolve clusterId from `node.data` or by matching `nodeId`). Control-plane shown read-only (derived from type). On type/worker change, run `reconcileClusterVms` and apply the returned nodes via the store.

- [ ] **Step 4: Green** — `npm run test -- PropertiesPanelCluster` → PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && git add src/frontend/src/components/canvas/PropertiesPanel.tsx src/frontend/src/components/canvas/__tests__/PropertiesPanelCluster.test.tsx && git commit -m "feat(canvas): cluster config editor in PropertiesPanel"
```

---

### Task 9: Role dropdown for VMs inside a cluster

**Files:**
- Modify: `src/frontend/src/components/canvas/PropertiesPanel.tsx` (vmNode block `:477-1587`)
- Test: `src/frontend/src/components/canvas/__tests__/PropertiesPanelRole.test.tsx`

**Interfaces:**
- Produces: when a selected `vmNode` has a `clusterId`, the panel shows a "Cluster role" dropdown (control-plane / worker) that writes `data.clusterRole` and keeps `data.tags.AnsibleGroup` in sync (`controllers`/`workers`) so the backend (which reads AnsibleGroup) stays correct. Hidden for VMs not in a cluster.

- [ ] **Step 1: Write failing test** — render `PropertiesPanel` with a selected vmNode carrying `data.clusterId:"prod"`; assert a role `<select>`/dropdown exists; selecting "worker" sets `data.clusterRole==="worker"` and `data.tags.AnsibleGroup` contains `workers`. (Model on the Task 8 test setup.)

- [ ] **Step 2: Confirm failure** — `npm run test -- PropertiesPanelRole` → FAIL.

- [ ] **Step 3: Implement** the dropdown inside the vmNode block, gated on `data.clusterId`. On change: `updateNodeData(id, { clusterRole, tags: { ...data.tags, AnsibleGroup: role === "control-plane" ? "controllers" : "workers" } })`.

- [ ] **Step 4: Green** — `npm run test -- PropertiesPanelRole` → PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && git add src/frontend/src/components/canvas/PropertiesPanel.tsx src/frontend/src/components/canvas/__tests__/PropertiesPanelRole.test.tsx && git commit -m "feat(canvas): cluster role dropdown for member VMs"
```

---

### Task 10: Relocate create-project OCP fields (seed default cluster)

**Files:**
- Modify: `src/frontend/src/app/projects/page.tsx:447-501` (OCP form), `:106-110` (state), `:206-223` (payload)
- Test: `src/frontend/src/app/__tests__/createProjectOcp.test.tsx` (light — assert the openshift template path no longer requires the inline cluster fields)

**Interfaces:**
- Produces: the create-project dialog no longer collects cluster name/base domain/VIPs inline for openshift templates (those now live on the canvas via the backend-generated cluster). Keep `ocp_version`/`auto_install_ocp`/`external_access` if still needed by the backend `from-template` path (they are project-level). The generated project already contains `topology.clusters` (Plan 1), so the canvas shows the cluster immediately.

- [ ] **Step 1: Decide minimal change & write test** — Confirm from Plan 1 that `from-template` still accepts (and needs) `cluster_name`/`base_domain` as back-compat defaults. If the backend still consumes them, KEEP them but relabel as optional defaults; if not, remove the inputs. Write a test asserting the dialog renders for an openshift template without throwing and that submitting posts `auto_install_ocp`/`ocp_version` (the project-level fields). Keep this test light (the page is large; render a focused subtree if full render is impractical, or assert on the payload-builder function if extractable).

- [ ] **Step 2: Confirm failure / baseline** — run the new test; adjust to the real structure.

- [ ] **Step 3: Implement** the minimal relocation: remove the now-canvas-owned inputs (cluster name/base domain) OR mark them optional advanced defaults; keep project-level OCP fields. Do not break the non-openshift path.

- [ ] **Step 4: Green** — `npm run test -- createProjectOcp` → PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && git add src/frontend/src/app/projects/page.tsx src/frontend/src/app/__tests__/createProjectOcp.test.tsx && git commit -m "feat(canvas): move OCP cluster config from create dialog onto the canvas"
```

---

### Task 11: Full frontend verification (lint, typecheck, build, test) + manual smoke

**Files:** none (verification), unless fixes needed.

- [ ] **Step 1: Run the whole frontend test suite** — `cd /Users/prutledg/troshka/src/frontend && npm run test` → all green.
- [ ] **Step 2: Lint** — `cd /Users/prutledg/troshka/src/frontend && npm run lint` → clean (fix any new issues).
- [ ] **Step 3: Build (typecheck)** — `cd /Users/prutledg/troshka/src/frontend && npm run build` → succeeds (this is the real TS gate; there is no separate tsc script). Fix type errors.
- [ ] **Step 4: Backend regression** — `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_ocp_clusters.py tests/test_template_loader.py -q` → green (confirms Task 1 rename didn't drift).
- [ ] **Step 5: Manual smoke checklist (document results in the commit message or PR):** with `./dev-services.sh` running, on the canvas: drop an OCP Cluster; set type=standard, workers=2 → 3 CP + 2 worker RHCOS VMs appear inside the boundary; edit a worker's CPU; drag an existing RHCOS VM into the boundary → it gains the cluster; set VIPs; reload the project → clusters + membership persist. Note any gaps as follow-ups.
- [ ] **Step 6: Commit any fixes**

```bash
cd /Users/prutledg/troshka && git add -A && git commit -m "test(canvas): frontend lint/build/test green for multi-cluster canvas"
```

---

## Self-Review

**Spec coverage (§5):**
- clusterNode boundary node (visual group) → Task 4. ✓
- Membership by drag → Task 7; membership persisted (`clusterId`/`parentId`) → Tasks 3, 7. ✓
- Cluster config moved onto canvas (name/type/workers/sizing/VIPs/domain/version) → Task 8; removed from create dialog → Task 10. ✓
- Count → materialize real editable VMs → Task 6. ✓
- Palette "OCP Cluster" item → Task 5. ✓
- Role dropdown for member VMs (replaces freeform AnsibleGroup) → Task 9. ✓
- CP locked to type (1/3), workers free → Tasks 6, 8. ✓
- Explicit VIPs per cluster + uniqueness validation → Task 8. ✓
- React Flow v12 `parentId` alignment → Task 1. ✓
- Persistence round-trip (save/load `clusters` + node refs) → Task 3. ✓
- Test infrastructure (user chose Vitest + Testing Library) → Task 2. ✓

**Placeholder scan:** Test code is concrete; component tasks give the file to mirror (`VMNode.tsx`) and the exact store actions/line anchors rather than full 400-line component bodies — the plan directs the implementer to follow the mapped patterns. Several steps explicitly say "read the real signature/structure first and adapt" — these are verification-against-source steps (the frontend map gives line numbers), not TBDs.

**Type consistency:** `ClusterConfig` fields match Plan 1's `topology.clusters[]` element exactly; `parentId` (not `parentNode`) used everywhere per Task 1; sizing defaults (8/16384/120, 4/8192/100) consistent across Tasks 5, 6; `clusterRole`/`AnsibleGroup` sync consistent between Tasks 6, 9.

**Known follow-ups carried forward:** the export round-trip gap (Plan 1 Task 9) is still open — schedule before OCP export is used. Backend per-cluster deploy is Plan 3.

## Roadmap (subsequent plans)
- **Plan 3** — per-cluster deploy (`agent_template.py` loop; per-cluster install-config/DNS/port-forwards; VIP consumption; SNO `platform:none`) + the deferred export round-trip (Task 9).
- **Plan 4** — ops pod (install-from-pod). **Plan 5** — console pod + showroom terminal. **Plan 6** — inventory plugin per-cluster groups + day-2.
