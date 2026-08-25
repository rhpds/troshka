import type { Edge, Node } from "@xyflow/react";

export function numToIp(num: number): string {
  return [
    (num >>> 24) & 255,
    (num >>> 16) & 255,
    (num >>> 8) & 255,
    num & 255,
  ].join(".");
}

export function ipToNum(ip: string): number | null {
  const parts = ip.split(".");
  if (parts.length !== 4) return null;
  const nums = parts.map(Number);
  if (nums.some((n) => isNaN(n) || n < 0 || n > 255)) return null;
  return ((nums[0] << 24) + (nums[1] << 16) + (nums[2] << 8) + nums[3]) >>> 0;
}

/** Host addresses for a CIDR (excludes network and broadcast). */
export function listCidrHosts(cidr: string): string[] {
  const match = cidr.match(/^(\d+)\.(\d+)\.(\d+)\.(\d+)\/(\d+)$/);
  if (!match) return [];
  const ip =
    (parseInt(match[1], 10) << 24) +
    (parseInt(match[2], 10) << 16) +
    (parseInt(match[3], 10) << 8) +
    parseInt(match[4], 10);
  const bits = parseInt(match[5], 10);
  if (bits < 0 || bits > 32) return [];
  const mask = bits === 0 ? 0 : (~0 << (32 - bits)) >>> 0;
  const network = (ip & mask) >>> 0;
  const broadcast = (network | (~mask >>> 0)) >>> 0;
  const hosts: string[] = [];
  for (let n = network + 1; n < broadcast; n++) {
    hosts.push(numToIp(n));
  }
  return hosts;
}

/** Mirror deploy_topology._compute_dhcp_bounds — hosts[9] through last host. */
export function computeDhcpBounds(
  cidr: string,
  rangeStart: string,
  rangeEnd: string,
): [string, string] {
  if (!cidr) return [rangeStart, rangeEnd];
  const hosts = listCidrHosts(cidr);
  if (hosts.length > 10) {
    if (!rangeStart) {
      rangeStart = hosts[Math.min(9, hosts.length - 2)];
    }
    if (!rangeEnd) {
      rangeEnd = hosts[hosts.length - 1];
    }
  }
  return [rangeStart, rangeEnd];
}

/** Mirror deploy_topology._get_dhcp_range. */
export function getDhcpRange(netData: Record<string, unknown>): [number, number] | null {
  let rangeStart = String(netData.dhcpRangeStart || "");
  let rangeEnd = String(netData.dhcpRangeEnd || "");
  const cidr = String(netData.cidr || "");
  if (!rangeStart || !rangeEnd) {
    [rangeStart, rangeEnd] = computeDhcpBounds(cidr, rangeStart, rangeEnd);
  }
  const startNum = ipToNum(rangeStart);
  const endNum = ipToNum(rangeEnd);
  if (startNum === null || endNum === null) return null;
  return [startNum, endNum];
}

/** Mirror deploy_topology._collect_used_ips. */
export function collectUsedIps(nodes: Node[]): Set<string> {
  const used = new Set<string>();
  for (const node of nodes) {
    const data = node.data as Record<string, unknown>;
    const nics = (data.nics || []) as Array<{ ip?: string }>;
    for (const nic of nics) {
      if (nic.ip) used.add(nic.ip);
    }
    if (node.type === "networkNode") {
      const cidr = String(data.cidr || "");
      if (cidr) {
        const hosts = listCidrHosts(cidr);
        if (hosts.length > 0) used.add(hosts[0]);
      }
    }
  }
  return used;
}

export function pickAvailableIp(
  dhcpRange: [number, number],
  usedIps: Set<string>,
): string | null {
  const [start, end] = dhcpRange;
  for (let addr = start; addr <= end; addr++) {
    const candidate = numToIp(addr);
    if (!usedIps.has(candidate)) return candidate;
  }
  return null;
}

export function pickIpForNetwork(
  netData: Record<string, unknown>,
  usedIps: Set<string>,
): string | null {
  const range = getDhcpRange(netData);
  if (!range) return null;
  return pickAvailableIp(range, usedIps);
}

function findNicNetwork(
  nodeId: string,
  nicId: string,
  edges: Edge[],
  nodesById: Map<string, Node>,
): Node | null {
  const nicHandleTop = `nic-${nicId}-top`;
  const nicHandleBottom = `nic-${nicId}-bottom`;
  for (const edge of edges) {
    const sh = edge.sourceHandle || "";
    const th = edge.targetHandle || "";
    if (edge.source === nodeId && (sh === nicHandleTop || sh === nicHandleBottom)) {
      const net = nodesById.get(edge.target);
      if (net?.type === "networkNode") return net;
    } else if (edge.target === nodeId && (th === nicHandleTop || th === nicHandleBottom)) {
      const net = nodesById.get(edge.source);
      if (net?.type === "networkNode") return net;
    }
  }
  return null;
}

/**
 * Assign IPs to container NICs connected to a network but missing static IPs.
 * Mirrors deploy_topology._auto_assign_container_ips.
 */
export function assignMissingContainerNicIps(nodes: Node[], edges: Edge[]): Node[] {
  const nodesById = new Map(nodes.map((n) => [n.id, n]));
  const usedIps = collectUsedIps(nodes);
  let changed = false;
  const nextNodes = nodes.map((node) => {
    if (node.type !== "containerNode") return node;
    const data = node.data as Record<string, unknown>;
    const nics = ((data.nics || []) as Array<{ id: string; ip?: string }>).map((nic) => ({
      ...nic,
    }));
    let nodeChanged = false;
    for (const nic of nics) {
      if (nic.ip) continue;
      const netNode = findNicNetwork(node.id, nic.id, edges, nodesById);
      if (!netNode) continue;
      const candidate = pickIpForNetwork(netNode.data as Record<string, unknown>, usedIps);
      if (!candidate) continue;
      nic.ip = candidate;
      usedIps.add(candidate);
      nodeChanged = true;
    }
    if (!nodeChanged) return node;
    changed = true;
    return { ...node, data: { ...data, nics } };
  });
  return changed ? nextNodes : nodes;
}
