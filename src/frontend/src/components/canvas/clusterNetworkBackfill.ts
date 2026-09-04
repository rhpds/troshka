import type { Node, Edge } from "@xyflow/react";
import type { ClusterConfig } from "@/stores/canvasStore";

/**
 * Backfill each cluster's `networkIds` from its members' NIC edges when unset.
 *
 * The member network is also wired as a canvas edge (the line); a deployed
 * project's canvas topology can arrive with `networkIds` empty (it's resolved to
 * the live network only in deployed_topology), which would wrongly trip the
 * "Select a member network" validation even though the line is connected.
 * Deriving from the edges keeps the checkbox + validation in sync with the
 * connection and persists on the next save. Clusters that already have
 * networkIds are untouched.
 *
 * This lives in its own module (type-only imports) so the canvas store can call
 * it without a store<->clusterMaterialize import cycle (clusterMaterialize
 * imports store *values*; importing it back into the store blanked the canvas).
 */
export function backfillClusterNetworkIds(
  clusters: ClusterConfig[],
  nodes: Node[],
  edges: Edge[],
): ClusterConfig[] {
  return clusters.map((cluster) => {
    if (cluster.networkIds && cluster.networkIds.length > 0) return cluster;
    const memberIds = new Set(
      nodes
        .filter(
          (n) =>
            n.type === "vmNode" &&
            (n.data as Record<string, unknown>).clusterId === cluster.id,
        )
        .map((n) => n.id),
    );
    const seen = new Set<string>();
    const derived: string[] = [];
    for (const e of edges) {
      if (
        memberIds.has(e.target) &&
        (e.targetHandle?.startsWith("nic-") ?? false) &&
        !seen.has(e.source)
      ) {
        seen.add(e.source);
        derived.push(e.source);
      }
    }
    return derived.length > 0 ? { ...cluster, networkIds: derived } : cluster;
  });
}
