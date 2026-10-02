/** Helpers for Redeploy when a project has OpenShift clusters. */

export type OcpRedeployMode = "rebuild" | "recert";

export type OcpClusterStatus = {
  id: string;
  name: string;
  status: string | null;
};

export function collectOcpClusters(input: {
  clusters?: Array<{ id?: string; name?: string; ocpInstallStatus?: string | null }> | null;
  nodes?: Array<{ type?: string; data?: { name?: string; label?: string; clusterId?: string } }> | null;
}): OcpClusterStatus[] {
  const fromList = (input.clusters || [])
    .filter((c) => c && (c.id || c.name))
    .map((c) => ({
      id: String(c.id || c.name),
      name: String(c.name || c.id || "cluster"),
      status: c.ocpInstallStatus ?? null,
    }));
  if (fromList.length > 0) return fromList;

  const fromNodes: OcpClusterStatus[] = [];
  for (const n of input.nodes || []) {
    if (n.type !== "clusterNode") continue;
    const id = String(n.data?.clusterId || n.data?.name || n.data?.label || "");
    if (!id) continue;
    fromNodes.push({
      id,
      name: String(n.data?.name || n.data?.label || id),
      status: null,
    });
  }
  return fromNodes;
}

export function allOcpClustersReady(clusters: OcpClusterStatus[]): boolean {
  return clusters.length > 0 && clusters.every((c) => c.status === "ready");
}
