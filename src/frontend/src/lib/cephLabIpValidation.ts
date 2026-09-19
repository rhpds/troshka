import type { Edge, Node } from "@xyflow/react";
import type { ClusterConfig } from "@/stores/canvasStore";
import { getDhcpRange, ipToNum } from "@/lib/dhcpIpAssignment";

export function defaultCephLabIp(cidr: string): string {
  if (!cidr || !cidr.includes("/")) return "";
  const octets = cidr.split("/")[0].split(".");
  if (octets.length !== 4) return "";
  octets[3] = "4";
  return octets.join(".");
}

function findNicNetwork(
  nodeId: string,
  nicId: string,
  edges: Edge[],
  nodesById: Map<string, Node>,
): string | null {
  const nicTop = `nic-${nicId}-top`;
  const nicBottom = `nic-${nicId}-bottom`;
  for (const edge of edges) {
    const sh = edge.sourceHandle || "";
    const th = edge.targetHandle || "";
    if (edge.source === nodeId && (sh === nicTop || sh === nicBottom)) {
      const net = nodesById.get(edge.target);
      if (net?.type === "networkNode") return net.id;
    } else if (edge.target === nodeId && (th === nicTop || th === nicBottom)) {
      const net = nodesById.get(edge.source);
      if (net?.type === "networkNode") return net.id;
    }
  }
  return null;
}

export function collectIpsOnNetwork(
  nodes: Node[],
  edges: Edge[],
  clusters: ClusterConfig[],
  networkId: string,
): Map<string, string> {
  const claimed = new Map<string, string>();
  const netNode = nodes.find((n) => n.id === networkId && n.type === "networkNode");
  if (!netNode) return claimed;

  const netData = netNode.data as Record<string, unknown>;
  const cidr = String(netData.cidr || "");
  const hosts = cidr.match(/^(\d+)\.(\d+)\.(\d+)\.(\d+)\/(\d+)$/);
  if (hosts) {
    const base =
      (parseInt(hosts[1], 10) << 24) +
      (parseInt(hosts[2], 10) << 16) +
      (parseInt(hosts[3], 10) << 8) +
      parseInt(hosts[4], 10);
    const bits = parseInt(hosts[5], 10);
    const mask = bits === 0 ? 0 : (~0 << (32 - bits)) >>> 0;
    const network = (base & mask) >>> 0;
    claimed.set(
      [(network >>> 24) & 255, (network >>> 16) & 255, (network >>> 8) & 255, (network + 1) & 255].join(
        ".",
      ),
      "gateway",
    );
    claimed.set(
      [(network >>> 24) & 255, (network >>> 16) & 255, (network >>> 8) & 255, (network + 2) & 255].join(
        ".",
      ),
      "dnsmasq",
    );
  }

  for (const rec of (netData.dnsRecords as Array<{ name?: string; ip?: string }>) || []) {
    const ip = String(rec.ip || "").trim();
    if (ip) claimed.set(ip, `DNS record '${rec.name || "record"}'`);
  }

  const lbIp = String(netData.lbIp || "").trim();
  if (lbIp) claimed.set(lbIp, "load balancer");

  const nodesById = new Map(nodes.map((n) => [n.id, n]));
  for (const node of nodes) {
    if (node.type !== "vmNode" && node.type !== "containerNode") continue;
    const data = node.data as Record<string, unknown>;
    const name = String(data.name || data.label || node.id.slice(0, 8));
    for (const nic of (data.nics as Array<{ id: string; ip?: string }>) || []) {
      if (findNicNetwork(node.id, nic.id, edges, nodesById) !== networkId) continue;
      const ip = String(nic.ip || "").trim();
      if (ip) claimed.set(ip, `VM/container '${name}'`);
    }
  }

  for (const cluster of clusters) {
    if (!(cluster.networkIds || []).includes(networkId)) continue;
    const cname = cluster.name || "cluster";
    for (const [key, label] of [
      ["apiVip", "API VIP"],
      ["ingressVip", "ingress VIP"],
    ] as const) {
      const ip = String(cluster[key] || "").trim();
      if (ip) claimed.set(ip, `cluster '${cname}' ${label}`);
    }
  }

  for (const node of nodes) {
    if (node.type !== "clusterNode") continue;
    const data = node.data as Record<string, unknown>;
    const cname = String(data.name || "cluster");
    for (const [key, label] of [
      ["apiVip", "API VIP"],
      ["ingressVip", "ingress VIP"],
    ] as const) {
      const ip = String(data[key] || "").trim();
      if (ip) claimed.set(ip, `cluster '${cname}' ${label}`);
    }
  }

  return claimed;
}

function labIpInDhcpPool(labIp: string, netData: Record<string, unknown>): boolean {
  if (!netData.dhcp) return false;
  const range = getDhcpRange(netData);
  const ipNum = ipToNum(labIp);
  if (!range || ipNum === null) return false;
  return ipNum >= range[0] && ipNum <= range[1];
}

function ipInCidr(ip: string, cidr: string): boolean {
  const ipNum = ipToNum(ip);
  const match = cidr.match(/^(\d+)\.(\d+)\.(\d+)\.(\d+)\/(\d+)$/);
  if (ipNum === null || !match) return false;
  const base =
    (parseInt(match[1], 10) << 24) +
    (parseInt(match[2], 10) << 16) +
    (parseInt(match[3], 10) << 8) +
    parseInt(match[4], 10);
  const bits = parseInt(match[5], 10);
  const mask = bits === 0 ? 0 : (~0 << (32 - bits)) >>> 0;
  return (ipNum & mask) === (base & mask);
}

export function validateCephLabIp(
  nodes: Node[],
  edges: Edge[],
  clusters: ClusterConfig[],
  cephData: { networkRef?: string; labIp?: string; name?: string; label?: string },
): string[] {
  const issues: string[] = [];
  const cephName = cephData.name || cephData.label || "Ceph Storage";
  const networkRef = String(cephData.networkRef || "").trim();
  const netNode = nodes.find((n) => n.id === networkRef && n.type === "networkNode");
  if (!networkRef || !netNode) {
    issues.push("Select a lab network");
    return issues;
  }

  const netData = netNode.data as Record<string, unknown>;
  const netLabel = String(netData.name || networkRef);
  const cidr = String(netData.cidr || "");
  const labIp = String(cephData.labIp || "").trim() || defaultCephLabIp(cidr);
  if (!labIp) {
    issues.push("Lab IP is required");
    return issues;
  }

  if (cidr && !ipInCidr(labIp, cidr)) {
    issues.push(`Lab IP ${labIp} is outside network '${netLabel}' (${cidr})`);
    return issues;
  }

  const claimed = collectIpsOnNetwork(nodes, edges, clusters, networkRef);
  const conflict = claimed.get(labIp);
  if (conflict) {
    issues.push(`Lab IP ${labIp} conflicts with ${conflict} on '${netLabel}'`);
  } else if (labIpInDhcpPool(labIp, netData)) {
    issues.push(`Lab IP ${labIp} falls in the DHCP pool on '${netLabel}'`);
  }

  return issues;
}
