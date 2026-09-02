import type { Node, Edge } from "@xyflow/react";
import type { ClusterConfig, VMDiskController, DiskSpec, VMNic } from "@/stores/canvasStore";
import { generateDiskControllerId, generateNicId, generateMac } from "@/stores/canvasStore";

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

const CHILD_X0 = 30;
const CHILD_GAP_X = 130;
const CP_ROW_Y = 70;
const WORKER_ROW_Y = 200;
const MB_PER_GB = 1024;

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
  col: number,
): { node: Node; extraNodes: Node[]; extraEdges: Edge[] } {
  const x = CHILD_X0 + col * CHILD_GAP_X;
  const y = spec.rowY;
  const { diskNodes, diskControllers, diskEdges, bootDevices } = buildMemberDisks(spec.role, cluster, name, x, y);
  const { nics, nicEdges } = buildMemberNics(cluster, name);

  const node: Node = {
    id: name,
    type: "vmNode",
    parentId: cluster.nodeId,
    position: { x, y },
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
  const usedNames = new Set(nodes.map((n) => n.id));
  const existing = nodes.filter((n) => isMember(n, cluster, spec.role)).length;
  const addedNodes: Node[] = [];
  const addedEdges: Edge[] = [];
  for (let k = 0; k < count; k += 1) {
    const name = nextFreeName(cluster.id, spec.prefix, usedNames);
    usedNames.add(name);
    const { node, extraNodes, extraEdges } = makeMemberNode(cluster, spec, name, existing + k);
    addedNodes.push(node);
    addedNodes.push(...extraNodes);
    addedEdges.push(...extraEdges);
  }
  return { nodes: [...nodes, ...addedNodes], edges: addedEdges };
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
