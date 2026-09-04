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

// Deploy-time-only fields that must NOT be carried into the canvas ClusterConfig
// when seeding from deployed_topology (they bloat the canvas topology and would
// re-appear as install artifacts).
const _DEPLOY_ONLY_CLUSTER_FIELDS = [
  "_generatedInstallConfig",
  "_generatedAgentConfig",
  "controlPlaneDisks",
  "workerDisks",
];

/**
 * Rebuild the canvas `clusters` list from deployed_topology when it is empty but
 * the project is deployed (deployed has clusters). The canvas `clusters` list can
 * drift empty (a save/load race); nothing else recreates it, so it sticks empty
 * and then corrupts metadata (misclassified as bastion -> reconfigure 400s). The
 * deployed clusters are the source of truth for a running project; strip the
 * deploy-only fields so the seeded ClusterConfig is clean.
 */
export function seedClustersFromDeployed(
  canvasClusters: ClusterConfig[],
  deployedClusters: Array<Record<string, unknown>> | undefined,
): ClusterConfig[] {
  if (canvasClusters.length > 0 || !deployedClusters || deployedClusters.length === 0) {
    return canvasClusters;
  }
  return deployedClusters.map((dc) => {
    const c = { ...dc };
    for (const f of _DEPLOY_ONLY_CLUSTER_FIELDS) delete c[f];
    return c as unknown as ClusterConfig;
  });
}

/**
 * Restore fields that are FIXED at install for clusters present in
 * deployed_topology. The canvas ClusterConfig can drift from what was actually
 * deployed (e.g. the OCP-version auto-default overwrote an empty-on-load value to
 * 4.20 while the cluster was really installed at 4.22); the deployed value wins.
 */
export function reconcileDeployedClusters(
  clusters: ClusterConfig[],
  deployedClusters: Array<{ id?: string; ocpVersion?: string; baseDomain?: string }>,
): ClusterConfig[] {
  const deployedById = new Map(
    deployedClusters.filter((c) => c.id).map((c) => [c.id as string, c]),
  );
  return clusters.map((cluster) => {
    const dep = deployedById.get(cluster.id);
    if (!dep) return cluster;
    let out = cluster;
    if (dep.ocpVersion && dep.ocpVersion !== out.ocpVersion) {
      out = { ...out, ocpVersion: dep.ocpVersion };
    }
    // baseDomain is fixed at install (baked into every node's DNS). A canvas copy
    // that drifted (e.g. "ocp.local" vs the deployed "local") both misleads the
    // UI and — via applyClusterDns setting the network dnsDomain = baseDomain —
    // leaves the network node perpetually dirty. Deployed value wins.
    if (dep.baseDomain && dep.baseDomain !== out.baseDomain) {
      out = { ...out, baseDomain: dep.baseDomain };
    }
    return out;
  });
}

/**
 * Tag a network node's cluster-managed DNS records (api / api-int / *.apps for
 * each cluster) with `managed: true` + `clusterId`. Deploy persists these records
 * as plain `{name, ip}`, so on load they fall into the editable DNS list and look
 * user-authored; tagging them routes them into the read-only ☸ "managed by the
 * OpenShift cluster" group instead. Mirrors buildClusterDnsRecords' names without
 * importing clusterMaterialize (which would create a store import cycle).
 */
export function reconcileManagedClusterDns(
  nodes: Node[],
  clusters: ClusterConfig[],
): Node[] {
  const nameToCluster = new Map<string, string>();
  for (const c of clusters) {
    if (!c.name) continue;
    const base = c.baseDomain || "local";
    nameToCluster.set(`api.${c.name}.${base}`, c.id);
    nameToCluster.set(`api-int.${c.name}.${base}`, c.id);
    nameToCluster.set(`.apps.${c.name}.${base}`, c.id);
  }
  if (nameToCluster.size === 0) return nodes;
  let changed = false;
  const out = nodes.map((n) => {
    if (n.type !== "networkNode") return n;
    const data = n.data as Record<string, unknown>;
    const recs = data.dnsRecords as Array<Record<string, unknown>> | undefined;
    if (!Array.isArray(recs)) return n;
    let recChanged = false;
    const tagged = recs.map((r) => {
      const cid = nameToCluster.get(String(r?.name ?? ""));
      if (cid && (r.managed !== true || r.clusterId !== cid)) {
        recChanged = true;
        return { ...r, managed: true, clusterId: cid };
      }
      return r;
    });
    if (!recChanged) return n;
    changed = true;
    return { ...n, data: { ...data, dnsRecords: tagged } };
  });
  return changed ? out : nodes;
}
