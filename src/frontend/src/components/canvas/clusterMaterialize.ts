import type { Node } from "@xyflow/react";
import type { ClusterConfig } from "@/stores/canvasStore";

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

function isMember(n: Node, cluster: ClusterConfig, role: string): boolean {
  const d = n.data as Record<string, unknown>;
  return (
    n.type === "vmNode" &&
    d?.clusterId === cluster.id &&
    d?.clusterRole === role
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

function makeMemberNode(
  cluster: ClusterConfig,
  spec: RoleSpec,
  name: string,
  col: number,
): Node {
  return {
    id: name,
    type: "vmNode",
    parentId: cluster.nodeId,
    position: { x: CHILD_X0 + col * CHILD_GAP_X, y: spec.rowY },
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
      nics: [],
      diskControllers: [],
      clusterId: cluster.id,
      clusterRole: spec.role,
      generated: true,
      tags: { AnsibleGroup: spec.ansibleGroup },
    },
  } as Node;
}

function addMembers(
  cluster: ClusterConfig,
  spec: RoleSpec,
  nodes: Node[],
  count: number,
): Node[] {
  const usedNames = new Set(nodes.map((n) => n.id));
  const existing = nodes.filter((n) => isMember(n, cluster, spec.role)).length;
  const added: Node[] = [];
  for (let k = 0; k < count; k += 1) {
    const name = nextFreeName(cluster.id, spec.prefix, usedNames);
    usedNames.add(name);
    added.push(makeMemberNode(cluster, spec, name, existing + k));
  }
  return [...nodes, ...added];
}

function removeSurplus(
  cluster: ClusterConfig,
  spec: RoleSpec,
  nodes: Node[],
  members: Node[],
  surplus: number,
): Node[] {
  const generated = members.filter(
    (n) => (n.data as Record<string, unknown>)?.generated === true,
  );
  // Remove the highest-index generated members first; never a user VM.
  const toRemove = new Set(generated.slice(-surplus).map((n) => n.id));
  return nodes.filter((n) => !toRemove.has(n.id));
}

function reconcileRole(
  cluster: ClusterConfig,
  spec: RoleSpec,
  nodes: Node[],
): Node[] {
  const members = nodes.filter((n) => isMember(n, cluster, spec.role));
  if (members.length < spec.want) {
    return addMembers(cluster, spec, nodes, spec.want - members.length);
  }
  if (members.length > spec.want) {
    return removeSurplus(
      cluster,
      spec,
      nodes,
      members,
      members.length - spec.want,
    );
  }
  return nodes;
}

/**
 * Return a new nodes array where `cluster` has exactly `controlPlane` control-
 * plane and `workers` worker member VMs. Pure and idempotent: missing members
 * are added, generated surplus is removed, user-customized members are kept.
 */
export function reconcileClusterVms(
  cluster: ClusterConfig,
  nodes: Node[],
): Node[] {
  let result = nodes;
  for (const spec of roleSpecs(cluster)) {
    result = reconcileRole(cluster, spec, result);
  }
  return result;
}
