import type { Edge, Node } from "@xyflow/react";
import { firstProjectVni, showroomInfraIpFromVni } from "@/lib/showroomPortForwards";
import { getVmNicIpOnNetwork } from "@/lib/showroomTabs";

export interface TopologyDnsRecord {
  name?: string;
  ip?: string;
  target?: string;
  type?: string;
}

/** Resolve template-style target names to IPs for display / deploy. */
export function resolveWorkloadIpByName(
  name: string,
  nodes: Node[],
  edges: Edge[],
  networkId: string,
  vniMap: Record<string, number>,
): string {
  const key = name.trim();
  if (!key) return "";
  if (key === "showroom") {
    const vni = firstProjectVni(vniMap);
    return vni != null ? showroomInfraIpFromVni(vni) : "";
  }
  const workload = nodes.find(
    (n) =>
      (n.type === "vmNode" || n.type === "containerNode") &&
      ((n.data as Record<string, unknown>).name === key ||
        (n.data as Record<string, unknown>).label === key),
  );
  if (!workload) return "";
  if (networkId) {
    const onNet = getVmNicIpOnNetwork(workload.id, networkId, nodes, edges);
    if (onNet) return onNet;
  }
  const nics = ((workload.data as Record<string, unknown>).nics || []) as Array<{
    ip?: string;
  }>;
  return nics.find((n) => n.ip)?.ip || "";
}

export function resolveDnsRecordDisplayIp(
  rec: TopologyDnsRecord,
  nodes: Node[],
  edges: Edge[],
  networkId: string,
  vniMap: Record<string, number>,
): string {
  const ip = String(rec.ip || "").trim();
  if (ip) return ip;
  const target = String(rec.target || "").trim();
  if (!target) return "";
  return resolveWorkloadIpByName(target, nodes, edges, networkId, vniMap);
}
