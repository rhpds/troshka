import type { Node, Edge } from "@xyflow/react";
import type { ClusterConfig } from "@/stores/canvasStore";
import { orderChildAfterParent } from "@/components/canvas/clusterMembership";
import { healClusterBoundaryWidths } from "@/components/canvas/clusterMaterialize";

/** Legacy lazy-migration artifact — must not coexist with modern multi-cluster boxes. */
export const LEGACY_GHOST_CLUSTER_ID = "ocp";
export const LEGACY_GHOST_NODE_ID = "cluster-ocp";

const DEPLOY_ONLY_CLUSTER_FIELDS = [
  "_generatedInstallConfig",
  "_generatedAgentConfig",
  "controlPlaneDisks",
  "workerDisks",
] as const;

export function isLegacyMigrationGhost(c: ClusterConfig): boolean {
  return (
    c.id === LEGACY_GHOST_CLUSTER_ID &&
    c.nodeId === LEGACY_GHOST_NODE_ID &&
    (c.baseDomain === "ocp.local" || !c.baseDomain)
  );
}

function stripDeployOnlyClusterFields(dc: ClusterConfig): ClusterConfig {
  const c = { ...dc } as Record<string, unknown>;
  for (const f of DEPLOY_ONLY_CLUSTER_FIELDS) delete c[f];
  return c as unknown as ClusterConfig;
}

/** Infer cluster id from a generated member VM id (`<clusterId>-cp-0`, etc.). */
export function inferClusterIdFromMemberId(vmId: string): string | null {
  const m = vmId.match(/^(.+)-(cp|worker)-\d+$/);
  return m ? m[1] : null;
}

function clusterConfigFromBoundaryNode(
  node: Node,
  deployed?: ClusterConfig,
): ClusterConfig {
  const d = node.data as Record<string, unknown>;
  const id =
    (d.clusterId as string) ||
    deployed?.id ||
    node.id.replace(/^cluster-/, "");
  return {
    id,
    name: (d.name as string) || deployed?.name || id,
    nodeId: node.id,
    type: (d.type as string) || deployed?.type || "sno",
    controlPlane: (d.controlPlane as number) ?? deployed?.controlPlane ?? 1,
    workers: (d.workers as number) ?? deployed?.workers ?? 0,
    controlPlaneCpu: deployed?.controlPlaneCpu ?? 8,
    controlPlaneMemory: deployed?.controlPlaneMemory ?? 16384,
    controlPlaneDisk: deployed?.controlPlaneDisk ?? 120,
    workerCpu: deployed?.workerCpu ?? 4,
    workerMemory: deployed?.workerMemory ?? 8192,
    workerDisk: deployed?.workerDisk ?? 100,
    baseDomain: (d.baseDomain as string) || deployed?.baseDomain || "local",
    apiVip: (d.apiVip as string) || deployed?.apiVip || "",
    ingressVip: (d.ingressVip as string) || deployed?.ingressVip || "",
    ocpVersion: deployed?.ocpVersion || "",
    pullThroughRegistry: deployed?.pullThroughRegistry,
    usePullThroughRegistry: deployed?.usePullThroughRegistry,
    networkIds: deployed?.networkIds || (d.networkIds as string[]) || [],
    recert: deployed?.recert,
    monitorHealth: deployed?.monitorHealth,
    configureBastionBrowser: deployed?.configureBastionBrowser,
    installOnDeploy: deployed?.installOnDeploy,
  };
}

/**
 * Rebuild `clusters[]` from deployed_topology, boundary nodes, and the canvas
 * list — dropping the legacy migration ghost when real clusters exist.
 */
export function reconcileCanvasClusters(
  canvasClusters: ClusterConfig[],
  deployedClusters: ClusterConfig[] | undefined,
  nodes: Node[],
): ClusterConfig[] {
  const strippedDeployed = (deployedClusters || []).map(stripDeployOnlyClusterFields);
  const hasGhost = canvasClusters.some(isLegacyMigrationGhost);
  const hasRealCanvas = canvasClusters.some((c) => !isLegacyMigrationGhost(c));

  // The legacy ghost (cluster-ocp) is a synthetic migration artifact only when a
  // REAL cluster exists to replace it (another canvas cluster or a deployed
  // cluster) — that is the case it was created to heal (it "stole" members from
  // real boxes). When "ocp"/cluster-ocp is the SOLE cluster it is a legitimate
  // single-cluster OCP project (every fresh sno/compact/standard template makes
  // exactly this id), not a ghost — dropping it deletes the real cluster box and
  // its member VM. Only drop the ghost when there is something to replace it.
  const hasReplacement = hasRealCanvas || strippedDeployed.length > 0;
  const dropGhost = hasGhost && hasReplacement;

  let base: ClusterConfig[] = dropGhost
    ? canvasClusters.filter((c) => !isLegacyMigrationGhost(c))
    : [...canvasClusters];

  if ((hasGhost || base.length === 0) && strippedDeployed.length > 0) {
    for (const dc of strippedDeployed) {
      if (!base.some((c) => c.id === dc.id)) {
        base.push(dc);
      }
    }
  }

  const realBoundaryNodes = nodes.filter(
    (n) =>
      n.type === "clusterNode" &&
      n.id !== LEGACY_GHOST_NODE_ID &&
      (n.data as Record<string, unknown>)?.clusterId,
  );

  for (const boundary of realBoundaryNodes) {
    const d = boundary.data as Record<string, unknown>;
    const cid = d.clusterId as string;
    if (base.some((c) => c.id === cid || c.nodeId === boundary.id)) continue;
    const dep = strippedDeployed.find(
      (c) => c.id === cid || c.nodeId === boundary.id,
    );
    base.push(clusterConfigFromBoundaryNode(boundary, dep));
  }

  if (!hasRealCanvas && base.length === 0 && !hasGhost) {
    return canvasClusters;
  }

  const filtered = dropGhost
    ? base.filter((c) => !isLegacyMigrationGhost(c))
    : base;
  for (const boundary of realBoundaryNodes) {
    const d = boundary.data as Record<string, unknown>;
    const cid = d.clusterId as string;
    const cluster = filtered.find((c) => c.id === cid);
    if (!cluster) continue;
    if (d.controlPlane !== undefined && d.controlPlane !== null) {
      cluster.controlPlane = d.controlPlane as number;
    }
    if (d.workers !== undefined && d.workers !== null) {
      cluster.workers = d.workers as number;
    }
  }
  return filtered;
}

function resolveMemberClusterId(
  node: Node,
  clusters: ClusterConfig[],
  deployedIds: Set<string>,
): string | null {
  const d = node.data as Record<string, unknown>;
  const byId = new Map(clusters.map((c) => [c.id, c]));
  const cid = d.clusterId as string | undefined;

  const inferred = inferClusterIdFromMemberId(node.id);
  if (inferred && byId.has(inferred)) {
    return inferred;
  }

  if (
    cid === LEGACY_GHOST_CLUSTER_ID &&
    clusters.some((c) => !isLegacyMigrationGhost(c))
  ) {
    const name = String(d.name || "");
    if (/^cp-\d+$/.test(name)) {
      const sno = clusters.filter(
        (c) => c.type === "sno" && !isLegacyMigrationGhost(c),
      );
      const deployedSno = sno.filter((c) => deployedIds.has(c.id));
      if (deployedSno.length === 1) return deployedSno[0].id;
      if (sno.length === 1) return sno[0].id;
    }
    if (inferred) return inferred;
  }

  if (cid && byId.has(cid)) return cid;
  return inferred && byId.has(inferred) ? inferred : null;
}

/** Point RHCOS members (and their hidden disks) at the right cluster boundary. */
export function healClusterMembership(
  nodes: Node[],
  clusters: ClusterConfig[],
  deployedClusters?: ClusterConfig[],
): Node[] {
  const byId = new Map(clusters.map((c) => [c.id, c]));
  const deployedIds = new Set(
    (deployedClusters || []).map((c) => String(c.id || "")).filter(Boolean),
  );

  return nodes.map((n) => {
    const d = n.data as Record<string, unknown>;

    if (n.type === "vmNode" && (d.os === "rhcos" || d.clusterId)) {
      const cid = resolveMemberClusterId(n, clusters, deployedIds);
      const cluster = cid ? byId.get(cid) : undefined;
      if (!cluster) return n;
      return {
        ...n,
        parentId: cluster.nodeId,
        extent: "parent" as const,
        draggable: false,
        data: { ...d, clusterId: cluster.id },
      };
    }

    if (
      n.type === "storageNode" &&
      n.parentId === LEGACY_GHOST_NODE_ID &&
      clusters.some((c) => !isLegacyMigrationGhost(c))
    ) {
      const diskOwner = n.id.replace(/-disk-\d+$/, "");
      const owner = nodes.find((x) => x.id === diskOwner);
      if (!owner) return n;
      const cid = resolveMemberClusterId(owner, clusters, deployedIds);
      const cluster = cid ? byId.get(cid) : undefined;
      if (!cluster) return n;
      return { ...n, parentId: cluster.nodeId };
    }

    return n;
  });
}

export function dedupeNodeIds(nodes: Node[]): Node[] {
  const seen = new Set<string>();
  return nodes.filter((n) => {
    if (seen.has(n.id)) return false;
    seen.add(n.id);
    return true;
  });
}

/** React Flow requires each parent boundary to appear before its children. */
export function orderClusterParentsBeforeChildren(nodes: Node[]): Node[] {
  let ordered = [...nodes];
  for (const n of nodes) {
    if (!n.parentId) continue;
    ordered = orderChildAfterParent(ordered, n.id, n.parentId);
  }
  return ordered;
}

function removeLegacyGhostBoundary(
  nodes: Node[],
  clusters: ClusterConfig[],
): Node[] {
  if (!clusters.some((c) => !isLegacyMigrationGhost(c))) return nodes;
  return nodes.filter((n) => n.id !== LEGACY_GHOST_NODE_ID);
}

export interface HealClusterTopologyInput {
  nodes: Node[];
  edges: Edge[];
  clusters: ClusterConfig[];
  deployedClusters?: ClusterConfig[];
}

export interface HealClusterTopologyResult {
  nodes: Node[];
  edges: Edge[];
  clusters: ClusterConfig[];
}

/**
 * Repair cluster metadata drift: legacy migration ghosts, orphaned boundary
 * boxes, wrong member parentId/clusterId, duplicate ids, and parent ordering.
 */
export function healClusterTopology(
  input: HealClusterTopologyInput,
): HealClusterTopologyResult {
  const clusters = reconcileCanvasClusters(
    input.clusters,
    input.deployedClusters,
    input.nodes,
  );
  let nodes = dedupeNodeIds(input.nodes);
  nodes = removeLegacyGhostBoundary(nodes, clusters);
  nodes = healClusterMembership(nodes, clusters, input.deployedClusters);
  nodes = healClusterBoundaryWidths(nodes);
  nodes = orderClusterParentsBeforeChildren(nodes);
  return { nodes, edges: input.edges, clusters };
}
