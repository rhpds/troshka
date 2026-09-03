import type { Node, Edge } from "@xyflow/react";
import type { ClusterConfig, VMDiskController, DiskSpec, VMNic } from "@/stores/canvasStore";
import { generateDiskControllerId, generateNicId, generateMac } from "@/stores/canvasStore";
import { collectUsedIps, listCidrHosts } from "@/lib/dhcpIpAssignment";

/**
 * Count-driven, existence-aware materialization of a cluster's member VMs on
 * the canvas — the frontend mirror of Plan 1's backend `materialize_cluster_vms`
 * (see `src/backend/app/services/template_loader.py`).
 *
 * A cluster created from a template (backend materialize) and one edited on the
 * canvas (this function) MUST produce identical VM node shapes so members are
 * interchangeable on load/save. The backend VM node `data` carries `vcpus`
 * (int) and `ram` (GB) — NOT `cpu`/`memory` — plus `os: "rhcos"`, `clusterId`,
 * `clusterRole`, `tags.AnsibleGroup` and `parentId` at the node level. Disk
 * sizing has no dedicated VM-node.data field on the backend (disks are separate
 * storageNodes); we carry it as `data.disk` (GB) to preserve the sizing intent.
 *
 * Only VMs THIS function created (`data.generated === true`) are ever
 * auto-removed when a count shrinks — a hand-drawn or user-customized member is
 * never touched.
 */

// Grid layout constants for member positioning and cluster boundary sizing.
// A VM node card is 180px wide (.vm-node-card) and ~260px tall with all rows,
// so cells must exceed those (card + gap) or member cards overlap.
const CELL_W = 210;
const CELL_H = 300;
const PAD = 30;
const HEADER_H = 48;
const COLS_MAX = 4;
const CARD_W = 180; // VM card width (from .vm-node-card)
const CARD_H = 260; // VM card height (approximate with all content)

// Legacy constants (used in roleSpecs for backward compat)
const CP_ROW_Y = 70;
const WORKER_ROW_Y = 200;
const MB_PER_GB = 1024;

/**
 * Calculate the bounding box size for a cluster with N members, positioned on a grid.
 * Returns width and height (in pixels) to fit members in a capped grid layout.
 * CPs placed first, then workers, wrapping at COLS_MAX per row.
 */
export function clusterBoxSize(count: number): { width: number; height: number } {
  const cols = Math.max(1, Math.min(COLS_MAX, count));
  const rows = Math.max(1, Math.ceil(count / cols));
  return { width: 2 * PAD + cols * CELL_W, height: HEADER_H + PAD + rows * CELL_H };
}

interface RoleSpec {
  role: "control-plane" | "worker";
  prefix: string;
  ansibleGroup: string;
  rowY: number;
  want: number;
  cpu: number;
  memoryMb: number;
  disk: number;
}

function roleSpecs(cluster: ClusterConfig): RoleSpec[] {
  return [
    {
      role: "control-plane",
      prefix: "cp",
      ansibleGroup: "controllers",
      rowY: CP_ROW_Y,
      want: cluster.controlPlane ?? 0,
      cpu: cluster.controlPlaneCpu ?? 8,
      memoryMb: cluster.controlPlaneMemory ?? 16384,
      disk: cluster.controlPlaneDisk ?? 120,
    },
    {
      role: "worker",
      prefix: "worker",
      ansibleGroup: "workers",
      rowY: WORKER_ROW_Y,
      want: cluster.workers ?? 0,
      cpu: cluster.workerCpu ?? 4,
      memoryMb: cluster.workerMemory ?? 8192,
      disk: cluster.workerDisk ?? 100,
    },
  ];
}

/**
 * Resolve a node's cluster role, tolerating backend-created members that carry
 * `tags.AnsibleGroup` (controllers/workers) but NO `data.clusterRole`. The
 * explicit `clusterRole` always wins; the AnsibleGroup tag is the fallback
 * source of truth so template/migration-created members are recognized. Returns
 * `null` when neither is present (not a cluster member by role).
 */
export function memberRole(
  n: Node,
): "control-plane" | "worker" | null {
  const d = n.data as Record<string, unknown>;
  const role = d?.clusterRole;
  if (role === "control-plane" || role === "worker") return role;
  const group = (d?.tags as Record<string, unknown> | undefined)?.AnsibleGroup;
  if (typeof group === "string") {
    if (group.includes("controllers")) return "control-plane";
    if (group.includes("workers")) return "worker";
  }
  return null;
}

/**
 * Data patch for a VM whose cluster membership changed on drag. Always sets the
 * new `clusterId`. When a VM is newly ASSIGNED (none -> a cluster) and has no
 * role yet, it defaults to worker (control-plane count is fixed by cluster type,
 * so a hand-dragged member is naturally a worker) — preserving any existing tags
 * and never overriding a role on re-assignment.
 */
export function assignmentDataPatch(
  node: Node,
  newClusterId: string | null,
  prevClusterId: string | null,
): Record<string, unknown> {
  const patch: Record<string, unknown> = { clusterId: newClusterId };
  const isNewAssignment = newClusterId !== null && prevClusterId === null;
  if (isNewAssignment && memberRole(node) === null) {
    const existingTags = (node.data as Record<string, unknown>)?.tags as
      | Record<string, unknown>
      | undefined;
    patch.clusterRole = "worker";
    patch.tags = { ...(existingTags || {}), AnsibleGroup: "workers" };
  }
  return patch;
}

function isMember(n: Node, cluster: ClusterConfig, role: string): boolean {
  const d = n.data as Record<string, unknown>;
  return (
    n.type === "vmNode" &&
    d?.clusterId === cluster.id &&
    memberRole(n) === role
  );
}

/** Lowest `<clusterId>-<prefix>-<n>` name not already used as a node id. */
function nextFreeName(
  clusterId: string,
  prefix: string,
  used: Set<string>,
): string {
  let i = 0;
  while (used.has(`${clusterId}-${prefix}-${i}`)) {
    i += 1;
  }
  return `${clusterId}-${prefix}-${i}`;
}

/**
 * Build disk storageNodes + disk controllers + edges for a cluster member.
 * Returns nodes, controllers, edges, and bootDevice IDs to be merged into the member.
 */
/**
 * Build NICs + NIC edges for a cluster member (one NIC per cluster network).
 * Returns nics array and edges wiring each NIC to its network node.
 * First NIC uses VM handle "top" with sourceHandle "bottom"; rest use "bottom" → "top".
 * NIC edges are hidden: the cluster→network anchor edge is the visible representation.
 */
function buildMemberNics(
  cluster: ClusterConfig,
  memberId: string,
): {
  nics: VMNic[];
  nicEdges: Edge[];
} {
  const nets = cluster.networkIds ?? [];
  const nics: VMNic[] = [];
  const nicEdges: Edge[] = [];

  nets.forEach((netId, i) => {
    const nic: VMNic = {
      id: generateNicId(),
      name: `eth${i}`,
      mac: generateMac(),
      model: "virtio",
    };
    nics.push(nic);

    const isFirstNic = i === 0;
    const vmHandle = isFirstNic ? "top" : "bottom";
    const sourceHandle = isFirstNic ? "bottom" : "top";

    nicEdges.push({
      id: `edge-${netId}-to-${memberId}-nic${i}`,
      source: netId,
      target: memberId,
      sourceHandle,
      targetHandle: `nic-${nic.id}-${vmHandle}`,
      type: "smoothstep",
      animated: true,
      hidden: true,
      style: {
        stroke: "rgba(34,211,238,0.5)",
        strokeWidth: 2,
        strokeDasharray: "6 4",
      },
    } as Edge);
  });

  return { nics, nicEdges };
}

function buildMemberDisks(
  role: "control-plane" | "worker",
  cluster: ClusterConfig,
  memberId: string,
  baseX: number,
  baseY: number,
): {
  diskNodes: Node[];
  diskControllers: VMDiskController[];
  diskEdges: Edge[];
  bootDevices: string[];
} {
  const specs = (role === "control-plane" ? cluster.controlPlaneDisks : cluster.workerDisks) ?? [
    { sizeGb: role === "control-plane" ? 120 : 100, bootable: true },
  ];
  const diskNodes: Node[] = [];
  const diskControllers: VMDiskController[] = [];
  const diskEdges: Edge[] = [];
  const bootDevices: string[] = [];

  specs.forEach((spec: DiskSpec, i: number) => {
    const diskId = `${memberId}-disk-${i}`;
    const dcId = generateDiskControllerId();
    diskControllers.push({ id: dcId, name: `disk${i}`, bus: spec.bus ?? "virtio" });
    diskNodes.push({
      id: diskId,
      type: "storageNode",
      position: { x: baseX, y: baseY + 60 + i * 40 },
      parentId: cluster.nodeId,
      hidden: true, // Collapse member disk sub-nodes — keep in data for deploy but hide rendering
      data: { label: `${memberId}-d${i}`, name: `${memberId}-d${i}`, size: spec.sizeGb, format: "qcow2", icon: "🛢" },
    } as Node);
    diskEdges.push({
      id: `edge-${diskId}-to-${memberId}`,
      source: diskId,
      target: memberId,
      sourceHandle: "right",
      targetHandle: `dp-${dcId}-left`,
      type: "smoothstep",
      animated: false,
      className: "edge-storage-pulse",
      style: { stroke: "rgba(251,191,36,0.6)", strokeWidth: 2, strokeDasharray: "4 4" },
    } as Edge);
    if (spec.bootable) bootDevices.push(diskId);
  });

  if (bootDevices.length === 0 && diskNodes.length > 0) {
    bootDevices.push(diskNodes[0].id);
  }

  return { diskNodes, diskControllers, diskEdges, bootDevices };
}

function makeMemberNode(
  cluster: ClusterConfig,
  spec: RoleSpec,
  name: string,
  gridCol: number,
  gridRow: number,
): { node: Node; extraNodes: Node[]; extraEdges: Edge[] } {
  // Position member on grid within cluster boundary
  const x = PAD + gridCol * CELL_W;
  const y = HEADER_H + gridRow * CELL_H;
  const { diskNodes, diskControllers, diskEdges, bootDevices } = buildMemberDisks(spec.role, cluster, name, x, y);
  const { nics, nicEdges } = buildMemberNics(cluster, name);

  const node: Node = {
    id: name,
    type: "vmNode",
    parentId: cluster.nodeId,
    position: { x, y },
    extent: "parent", // Constrain to cluster boundary
    draggable: false, // Members are cluster-managed (count/editor) — not hand-movable
    data: {
      label: name,
      name,
      os: "rhcos",
      vcpus: spec.cpu,
      ram: Math.max(1, Math.round(spec.memoryMb / MB_PER_GB)),
      disk: spec.disk,
      firmware: "uefi",
      status: "stopped",
      icon: "\u{1F5A5}",
      nics,
      diskControllers,
      bootDevices,
      clusterId: cluster.id,
      clusterRole: spec.role,
      generated: true,
      tags: { AnsibleGroup: spec.ansibleGroup },
    },
  } as Node;

  return { node, extraNodes: diskNodes, extraEdges: [...diskEdges, ...nicEdges] };
}

function addMembers(
  cluster: ClusterConfig,
  spec: RoleSpec,
  nodes: Node[],
  count: number,
): { nodes: Node[]; edges: Edge[] } {
  // FIX 1: Build used names from BOTH node IDs and existing cluster members' data.name.
  // Backend members have random UUID node IDs but their names stored in data.name.
  const nodeIds = new Set(nodes.map((n) => n.id));
  const memberNames = new Set(
    nodes
      .filter((n) => n.type === "vmNode" && (n.data as Record<string, unknown>).clusterId === cluster.id)
      .map((n) => String((n.data as Record<string, unknown>).name))
      .filter(Boolean)
  );
  const usedNames = new Set([...nodeIds, ...memberNames]);
  const allMembers = nodes.filter((n) => n.type === "vmNode" && (n.data as Record<string, unknown>).clusterId === cluster.id);
  const cpMembers = allMembers.filter((n) => memberRole(n) === "control-plane").length;
  const addedNodes: Node[] = [];
  const addedEdges: Edge[] = [];

  for (let k = 0; k < count; k += 1) {
    const name = nextFreeName(cluster.id, spec.prefix, usedNames);
    usedNames.add(name);

    // Compute grid position: CPs fill first, then workers wrap on next rows
    const memberIndex = (spec.role === "control-plane" ? k : cpMembers + k);
    const gridCol = memberIndex % COLS_MAX;
    const gridRow = Math.floor(memberIndex / COLS_MAX);

    const { node, extraNodes, extraEdges } = makeMemberNode(cluster, spec, name, gridCol, gridRow);
    addedNodes.push(node);
    addedNodes.push(...extraNodes);
    addedEdges.push(...extraEdges);
  }
  return { nodes: [...nodes, ...addedNodes], edges: addedEdges };
}

/**
 * Reposition ALL of a cluster's member VM cards onto a clean grid by GLOBAL
 * index (control-plane first, then workers; each ordered by the trailing index
 * in its name), wrapping at COLS_MAX per row. This is authoritative over the
 * incremental placement in `addMembers` — it guarantees non-overlapping layout
 * and correct row-wrapping regardless of how/when members were added (the
 * incremental path could pile members in one column). Cells are CELL_W×CELL_H,
 * which exceed the 180×260 card, so cards never overlap.
 */
function reflowMembers(cluster: ClusterConfig, nodes: Node[]): Node[] {
  const members = nodes.filter(
    (n) =>
      n.type === "vmNode" &&
      (n.data as Record<string, unknown>).clusterId === cluster.id,
  );
  const idxOf = (n: Node): number => {
    const m = String((n.data as Record<string, unknown>).name || "").match(/-(\d+)$/);
    return m ? parseInt(m[1], 10) : 0;
  };
  // CPs and workers occupy SEPARATE rows: control-plane fills its own row(s)
  // first, then workers START on a fresh row below (never sharing a CP row).
  const byIdx = (a: Node, b: Node) => idxOf(a) - idxOf(b);
  const cps = members.filter((n) => memberRole(n) === "control-plane").sort(byIdx);
  const workers = members.filter((n) => memberRole(n) === "worker").sort(byIdx);
  const cpRows = Math.ceil(cps.length / COLS_MAX); // 0 when there are no CPs
  const posById = new Map<string, { x: number; y: number }>();
  cps.forEach((n, i) => {
    posById.set(n.id, {
      x: PAD + (i % COLS_MAX) * CELL_W,
      y: HEADER_H + Math.floor(i / COLS_MAX) * CELL_H,
    });
  });
  workers.forEach((n, j) => {
    posById.set(n.id, {
      x: PAD + (j % COLS_MAX) * CELL_W,
      y: HEADER_H + (cpRows + Math.floor(j / COLS_MAX)) * CELL_H,
    });
  });
  return nodes.map((n) => {
    const pos = posById.get(n.id);
    return pos ? { ...n, position: pos } : n;
  });
}

function removeSurplus(
  cluster: ClusterConfig,
  spec: RoleSpec,
  nodes: Node[],
  members: Node[],
  edges: Edge[],
  surplus: number,
): { nodes: Node[]; edges: Edge[] } {
  const generated = members.filter(
    (n) => (n.data as Record<string, unknown>)?.generated === true,
  );
  // Remove the highest-index generated members first; never a user VM.
  const toRemoveMemberIds = new Set(generated.slice(-surplus).map((n) => n.id));

  // Also remove all disk nodes that belong to removed members
  const remainingNodes = nodes.filter((n) => !toRemoveMemberIds.has(n.id));
  const toRemoveDiskNodeIds = new Set<string>();
  toRemoveMemberIds.forEach((memberId) => {
    nodes.forEach((n) => {
      if (n.type === "storageNode" && n.parentId === cluster.nodeId && n.id.startsWith(`${memberId}-disk-`)) {
        toRemoveDiskNodeIds.add(n.id);
      }
    });
  });
  const finalNodes = remainingNodes.filter((n) => !toRemoveDiskNodeIds.has(n.id));

  // Remove edges that reference removed nodes
  const finalEdges = edges.filter(
    (e) =>
      !toRemoveMemberIds.has(e.source) &&
      !toRemoveMemberIds.has(e.target) &&
      !toRemoveDiskNodeIds.has(e.source) &&
      !toRemoveDiskNodeIds.has(e.target),
  );

  return { nodes: finalNodes, edges: finalEdges };
}

function reconcileRole(
  cluster: ClusterConfig,
  spec: RoleSpec,
  nodes: Node[],
  edges: Edge[],
): { nodes: Node[]; edges: Edge[] } {
  const members = nodes.filter((n) => isMember(n, cluster, spec.role));
  if (members.length < spec.want) {
    const { nodes: newNodes, edges: newEdges } = addMembers(cluster, spec, nodes, spec.want - members.length);
    return { nodes: newNodes, edges: [...edges, ...newEdges] };
  }
  if (members.length > spec.want) {
    const { nodes: newNodes, edges: newEdges } = removeSurplus(
      cluster,
      spec,
      nodes,
      members,
      edges,
      members.length - spec.want,
    );
    return { nodes: newNodes, edges: newEdges };
  }
  return { nodes, edges };
}

/**
 * Extract the cluster's network IDs from existing NIC edges wired to members.
 * For each member (in order), finds NIC edges whose target is that member and
 * targetHandle starts with "nic-", collects the source (network node id).
 * Returns distinct network ids, preserving member order (primary first).
 * Pure function, useful as a fallback when cluster.networkIds is unset.
 */
export function clusterNetworkIdsFromEdges(
  clusterNodeId: string,
  memberIds: string[],
  edges: Edge[],
): string[] {
  const seen = new Set<string>();
  const result: string[] = [];

  for (const memberId of memberIds) {
    const memberNicEdges = edges.filter(
      (e) => e.target === memberId && (e.targetHandle?.startsWith("nic-") ?? false),
    );
    for (const edge of memberNicEdges) {
      const netId = edge.source;
      if (!seen.has(netId)) {
        seen.add(netId);
        result.push(netId);
      }
    }
  }

  return result;
}

/**
 * Compute the content bounding box from visible member VM cards (excluding hidden disk nodes).
 * Returns { contentW, contentH } in pixels, or defaults to { 280, 180 } if no members.
 * Visible members are those with type "vmNode" and parentId matching the cluster.
 */
function computeContentBbox(
  cluster: ClusterConfig,
  visibleMembers: Node[],
): { contentW: number; contentH: number } {
  if (visibleMembers.length === 0) {
    return { contentW: 280, contentH: 180 };
  }

  let maxX = 0;
  let maxY = 0;

  for (const member of visibleMembers) {
    const x = member.position.x;
    const y = member.position.y;
    maxX = Math.max(maxX, x + CARD_W);
    maxY = Math.max(maxY, y + CARD_H);
  }

  return {
    contentW: maxX + PAD,
    contentH: maxY + PAD,
  };
}

/**
 * Rebuild NICs for every existing member of the cluster to match cluster.networkIds.
 * Diff-based and MAC-stable: reuses existing NIC ids and MACs for networks that
 * remain in the list, drops NICs for networks no longer needed, adds new NICs only
 * for new networks. Rebuild nic edges with stable ids: `edge-${networkId}-to-${memberId}-nic`.
 * Disk edges and all other edges remain intact. Returns updated {nodes, edges}.
 * Idempotent: re-running with unchanged networkIds yields byte-identical nodes/edges
 * and preserves MACs (critical for OCP BMH boot). Adding/removing a network only
 * affects that network's NIC + edge; existing NICs keep their ids and MACs.
 */
export function applyClusterNetworks(
  cluster: ClusterConfig,
  nodes: Node[],
  edges: Edge[],
): { nodes: Node[]; edges: Edge[] } {
  let resultNodes = nodes;
  let resultEdges = edges;

  // Get all members of this cluster
  const allMembers = resultNodes.filter((n) => {
    const d = n.data as Record<string, unknown>;
    return n.type === "vmNode" && d?.clusterId === cluster.id;
  });

  // For each member, diff-apply networks to preserve existing NIC ids/MACs
  for (const member of allMembers) {
    const memberData = member.data as Record<string, unknown>;
    const currentNics = (memberData.nics as VMNic[]) || [];

    // Extract existing network→NIC mapping from current nic edges.
    // For each nic edge where target===memberId, match the targetHandle's nicId to a NIC in currentNics.
    const existingNetworkToNic = new Map<string, VMNic>();
    for (const edge of edges) {
      if (
        edge.target === member.id &&
        (edge.targetHandle?.startsWith("nic-") ?? false)
      ) {
        // targetHandle is "nic-<nicId>-top" or "nic-<nicId>-bottom"
        // Extract nicId by removing "nic-" prefix and last suffix (-top/-bottom)
        const match = edge.targetHandle!.match(/^nic-(.+)-(top|bottom)$/);
        if (match) {
          const nicId = match[1];
          const nic = currentNics.find((n) => n.id === nicId);
          if (nic) {
            existingNetworkToNic.set(edge.source, nic);
          }
        }
      }
    }

    const targetNetworkIds = cluster.networkIds ?? [];
    const newNics: VMNic[] = [];

    // For each network in the target list, reuse existing NIC or create new
    for (let i = 0; i < targetNetworkIds.length; i += 1) {
      const netId = targetNetworkIds[i];
      const existingNic = existingNetworkToNic.get(netId);

      if (existingNic) {
        // Reuse: update name to reflect new position (eth0, eth1, ...)
        newNics.push({ ...existingNic, name: `eth${i}` });
      } else {
        // Create new NIC for this network
        const nic: VMNic = {
          id: generateNicId(),
          name: `eth${i}`,
          mac: generateMac(),
          model: "virtio",
        };
        newNics.push(nic);
      }
    }

    // Update the member node's nics
    resultNodes = resultNodes.map((n) =>
      n.id === member.id
        ? { ...n, data: { ...n.data, nics: newNics } }
        : n,
    );

    // Remove old NIC edges for this member
    resultEdges = resultEdges.filter(
      (e) =>
        !(e.target === member.id && (e.targetHandle?.startsWith("nic-") ?? false)),
    );

    // Add fresh NIC edges with stable ids (not positional)
    // Hidden: the cluster→network anchor edge is the visible representation;
    // per-member NIC edges kept in topology data for deploy but not rendered
    const nicEdges: Edge[] = [];
    for (let i = 0; i < newNics.length; i += 1) {
      const nic = newNics[i];
      const netId = targetNetworkIds[i];
      const isFirstNic = i === 0;
      const vmHandle = isFirstNic ? "top" : "bottom";
      const sourceHandle = isFirstNic ? "bottom" : "top";

      nicEdges.push({
        id: `edge-${netId}-to-${member.id}-nic`,
        source: netId,
        target: member.id,
        sourceHandle,
        targetHandle: `nic-${nic.id}-${vmHandle}`,
        type: "smoothstep",
        animated: true,
        hidden: true,
        style: {
          stroke: "rgba(34,211,238,0.5)",
          strokeWidth: 2,
          strokeDasharray: "6 4",
        },
      } as Edge);
    }

    resultEdges = [...resultEdges, ...nicEdges];
  }

  return { nodes: resultNodes, edges: resultEdges };
}

/**
 * Return new nodes and edges where `cluster` has exactly `controlPlane` control-
 * plane and `workers` worker member VMs, each with their disk storageNodes + edges.
 * Pure and idempotent: missing members are added, generated surplus is removed,
 * user-customized members are kept. Also returns disk edges needed to wire disks.
 * Updates the cluster boundary node's style.width/height to auto-fit members on grid,
 * and sets data.minWidth/data.minHeight for NodeResizer constraints.
 */
export function reconcileClusterVms(
  cluster: ClusterConfig,
  nodes: Node[],
): { nodes: Node[]; edges: Edge[] } {
  let resultNodes = nodes;
  let resultEdges: Edge[] = [];
  for (const spec of roleSpecs(cluster)) {
    const { nodes: nextNodes, edges: nextEdges } = reconcileRole(cluster, spec, resultNodes, resultEdges);
    resultNodes = nextNodes;
    resultEdges = nextEdges;
  }

  // Authoritative grid layout: reposition every member by global index so rows
  // wrap and cards never overlap (fixes incrementally-added members piling up).
  resultNodes = reflowMembers(cluster, resultNodes);

  // Collect visible member VMs (not hidden, not disks) for content bbox calculation
  const visibleMembers = resultNodes.filter(
    (n) => n.type === "vmNode" && (n.data as Record<string, unknown>).clusterId === cluster.id,
  );

  // Compute content bounding box from actual member positions and card dimensions
  const { contentW, contentH } = computeContentBbox(cluster, visibleMembers);

  // Auto-fit EXACTLY to content — grow AND shrink. Removing members (e.g.
  // workers → 0) shrinks the box back to just its remaining cards. NodeResizer
  // min is set to the content bbox below so the user still can't clip members.
  const newWidth = contentW;
  const newHeight = contentH;

  // Update cluster boundary node size and set min constraints for NodeResizer
  resultNodes = resultNodes.map((n) =>
    n.id === cluster.nodeId
      ? {
          ...n,
          style: { ...n.style, width: newWidth, height: newHeight },
          data: {
            ...n.data,
            minWidth: contentW,
            minHeight: contentH,
          },
        }
      : n,
  );

  return { nodes: resultNodes, edges: resultEdges };
}

/**
 * Appends the cluster's default members (via `reconcileClusterVms`) to `nodes`.
 * Used by both the drop handler (to materialize on first creation) and
 * PropertiesPanel (when count/type edits change).
 */
export function materializeClusterInto(
  cluster: ClusterConfig,
  nodes: Node[],
): { nodes: Node[]; edges: Edge[] } {
  return reconcileClusterVms(cluster, nodes);
}

/**
 * Push the cluster's per-role sizing (cpu/memory/disk) onto its EXISTING
 * generated member VMs so a sizing edit in the properties panel actually reaches
 * already-materialized nodes. Uses the same field mapping as `makeMemberNode`
 * (cpu→vcpus, memoryMb→ram in GB, disk→disk). Only `generated:true` members of
 * this cluster are touched — hand-customized members are left alone. Pure and
 * idempotent: returns the same array reference when nothing changes.
 */
export function applyClusterSizing(
  cluster: ClusterConfig,
  nodes: Node[],
): Node[] {
  const specByRole = new Map(roleSpecs(cluster).map((s) => [s.role, s]));
  let changed = false;
  const out = nodes.map((n) => {
    const d = n.data as Record<string, unknown>;
    if (n.type !== "vmNode" || d?.clusterId !== cluster.id || d?.generated !== true) {
      return n;
    }
    const role = memberRole(n);
    const spec = role ? specByRole.get(role) : undefined;
    if (!spec) return n;
    const vcpus = spec.cpu;
    const ram = Math.max(1, Math.round(spec.memoryMb / MB_PER_GB));
    const disk = spec.disk;
    if (d.vcpus === vcpus && d.ram === ram && d.disk === disk) return n;
    changed = true;
    return { ...n, data: { ...d, vcpus, ram, disk } };
  });
  return changed ? out : nodes;
}

/**
 * Rebuild disk storageNodes + edges for all EXISTING members of the cluster,
 * matching the role-specific disk lists (controlPlaneDisks / workerDisks).
 * Diff-based and ID-stable: reuses existing disk node IDs for disk indices that
 * still exist, removes stale disk nodes, adds new ones for new disks.
 * Rebuilds diskControllers + bootDevices to match the new disk specs.
 * Idempotent: re-running with unchanged disk specs yields byte-identical nodes/edges.
 * Pure: returns same array reference when nothing changes.
 */
export function applyClusterDisks(
  cluster: ClusterConfig,
  nodes: Node[],
  edges: Edge[],
): { nodes: Node[]; edges: Edge[] } {
  let resultNodes = nodes;
  let resultEdges = edges;

  // Get all members of this cluster
  const allMembers = resultNodes.filter((n) => {
    const d = n.data as Record<string, unknown>;
    return n.type === "vmNode" && d?.clusterId === cluster.id;
  });

  // For each member, rebuild disks to match cluster disk specs
  for (const member of allMembers) {
    const role = memberRole(member);
    if (!role) continue;

    // Get the disk specs for this role
    const diskSpecs = role === "control-plane" ? cluster.controlPlaneDisks : cluster.workerDisks;
    const targetSpecs = diskSpecs ?? [{ sizeGb: role === "control-plane" ? 120 : 100, bootable: true }];

    // Collect existing disk nodes for this member
    const existingDiskNodeIds = new Set<string>();
    const existingDiskNodes = new Map<number, Node>();
    for (const n of resultNodes) {
      if (
        n.type === "storageNode" &&
        n.id.startsWith(`${member.id}-disk-`)
      ) {
        const match = n.id.match(/disk-(\d+)$/);
        if (match) {
          const idx = parseInt(match[1], 10);
          existingDiskNodeIds.add(n.id);
          existingDiskNodes.set(idx, n);
        }
      }
    }

    // Build new disk nodes, controllers, and edges
    const newDiskNodes: Node[] = [];
    const newDiskControllers: VMDiskController[] = [];
    const newDiskEdges: Edge[] = [];
    const newBootDevices: string[] = [];

    const baseX = member.position.x;
    const baseY = member.position.y;

    targetSpecs.forEach((spec, i) => {
      const existingNode = existingDiskNodes.get(i);
      const diskId = existingNode?.id ?? `${member.id}-disk-${i}`;
      const dcId = generateDiskControllerId();

      newDiskControllers.push({ id: dcId, name: `disk${i}`, bus: spec.bus ?? "virtio" });
      newDiskNodes.push({
        id: diskId,
        type: "storageNode",
        position: { x: baseX, y: baseY + 60 + i * 40 },
        parentId: cluster.nodeId,
        hidden: true, // Collapse member disk sub-nodes
        data: { label: `${member.id}-d${i}`, name: `${member.id}-d${i}`, size: spec.sizeGb, format: "qcow2", icon: "🛢" },
      } as Node);

      newDiskEdges.push({
        id: `edge-${diskId}-to-${member.id}`,
        source: diskId,
        target: member.id,
        sourceHandle: "right",
        targetHandle: `dp-${dcId}-left`,
        type: "smoothstep",
        animated: false,
        className: "edge-storage-pulse",
        style: { stroke: "rgba(251,191,36,0.6)", strokeWidth: 2, strokeDasharray: "4 4" },
      } as Edge);

      if (spec.bootable) newBootDevices.push(diskId);
    });

    // If no bootable disk specified, default to first
    if (newBootDevices.length === 0 && newDiskNodes.length > 0) {
      newBootDevices.push(newDiskNodes[0].id);
    }

    // Identify stale disk nodes to remove
    const staleDiskNodeIds = new Set<string>();
    for (const [idx, node] of existingDiskNodes) {
      if (idx >= targetSpecs.length) {
        staleDiskNodeIds.add(node.id);
      }
    }

    // Remove stale disk nodes
    resultNodes = resultNodes.filter((n) => !staleDiskNodeIds.has(n.id));

    // Remove stale disk edges + old member disk edges for this member
    resultEdges = resultEdges.filter(
      (e) =>
        !(e.target === member.id && (e.targetHandle?.startsWith("dp-") ?? false)) &&
        !staleDiskNodeIds.has(e.source) &&
        !staleDiskNodeIds.has(e.target),
    );

    // Merge new disk nodes into result (replace existing ones if updating)
    const nodeIdSet = new Set(resultNodes.map((n) => n.id));
    for (const diskNode of newDiskNodes) {
      if (nodeIdSet.has(diskNode.id)) {
        // Replace existing
        resultNodes = resultNodes.map((n) => (n.id === diskNode.id ? diskNode : n));
      } else {
        // Add new
        resultNodes = [...resultNodes, diskNode];
      }
    }

    // Update the member node's diskControllers and bootDevices
    resultNodes = resultNodes.map((n) =>
      n.id === member.id
        ? { ...n, data: { ...n.data, diskControllers: newDiskControllers, bootDevices: newBootDevices } }
        : n,
    );

    // Add new disk edges
    resultEdges = [...resultEdges, ...newDiskEdges];
  }

  return { nodes: resultNodes, edges: resultEdges };
}

/**
 * Suggest unused VIPs for the cluster's machine network (first network in cluster.networkIds).
 * Scans from the top of the CIDR (reverse/high IPs) excluding the gateway, used IPs,
 * and VIPs from other clusters. Returns { apiVip, ingressVip } or both empty if no CIDR
 * or single-node cluster (SNO uses member IP).
 */
export function suggestClusterVips(
  cluster: ClusterConfig,
  nodes: Node[],
): { apiVip: string | null; ingressVip: string | null } {
  // SNO and single-node clusters use the member IP; no VIP needed
  if ((cluster.controlPlane ?? 0) + (cluster.workers ?? 0) <= 1) {
    return { apiVip: null, ingressVip: null };
  }

  // Get the machine network CIDR (first network)
  const netId = (cluster.networkIds ?? [])[0];
  if (!netId) {
    return { apiVip: null, ingressVip: null };
  }

  const netNode = nodes.find((n) => n.id === netId && n.type === "networkNode");
  if (!netNode) {
    return { apiVip: null, ingressVip: null };
  }

  const netNodeData = netNode.data as Record<string, string | undefined>;
  const cidr = netNodeData.cidr;
  if (!cidr) {
    return { apiVip: null, ingressVip: null };
  }

  // Get all used IPs in the topology
  const usedIps = collectUsedIps(nodes);

  // Add gateway IP (first host in subnet)
  const hosts = listCidrHosts(cidr);
  if (hosts.length > 0) {
    usedIps.add(hosts[0]);
  }

  // Add VIPs from other clusters
  const otherClusters = nodes
    .filter((n) => n.type === "clusterNode" && (n.data as Record<string, string | undefined>).clusterId !== cluster.id)
    .map((n) => n.data as Record<string, string | undefined>);

  for (const other of otherClusters) {
    const apiVip = other.apiVip as string | undefined;
    const ingressVip = other.ingressVip as string | undefined;
    if (apiVip) usedIps.add(apiVip);
    if (ingressVip) usedIps.add(ingressVip);
  }

  // Scan from top of CIDR downward to find unused IPs
  let apiVipCandidate: string | null = null;
  let ingressVipCandidate: string | null = null;

  if (hosts.length >= 2) {
    // Reverse scan: start from high IPs
    for (let i = hosts.length - 1; i >= 0 && (!apiVipCandidate || !ingressVipCandidate); i -= 1) {
      const ip = hosts[i];
      if (!usedIps.has(ip)) {
        if (!apiVipCandidate) {
          apiVipCandidate = ip;
        } else if (!ingressVipCandidate) {
          ingressVipCandidate = ip;
          break;
        }
      }
    }
  }

  return { apiVip: apiVipCandidate, ingressVip: ingressVipCandidate };
}

/**
 * Check if an IP address is in use (by a member NIC, gateway, or another cluster's VIP).
 * Returns true if collision detected.
 */
export function vipCollision(
  ip: string,
  cluster: ClusterConfig,
  nodes: Node[],
): boolean {
  if (!ip || !ip.trim()) return false;

  const usedIps = collectUsedIps(nodes);

  // Also check other clusters' VIPs
  const otherClusters = nodes
    .filter((n) => n.type === "clusterNode" && (n.data as Record<string, string | undefined>).clusterId !== cluster.id)
    .map((n) => n.data as Record<string, string | undefined>);

  for (const other of otherClusters) {
    if (ip === other.apiVip) return true;
    if (ip === other.ingressVip) return true;
  }

  // Check gateway
  const netId = (cluster.networkIds ?? [])[0];
  if (netId) {
    const netNode = nodes.find((n) => n.id === netId && n.type === "networkNode");
    if (netNode) {
      const netNodeData = netNode.data as Record<string, string | undefined>;
      const cidr = netNodeData.cidr;
      if (cidr) {
        const hosts = listCidrHosts(cidr);
        if (hosts.length > 0 && ip === hosts[0]) return true; // gateway
      }
    }
  }

  return usedIps.has(ip);
}
