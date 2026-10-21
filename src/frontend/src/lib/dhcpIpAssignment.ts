import type { Edge, Node } from "@xyflow/react";
import { isShowroomContainer } from "@/lib/showroomValidation";

function textField(data: Record<string, unknown>, key: string): string {
  const value = data[key];
  return typeof value === "string" ? value : "";
}

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
  if (nums.some((n) => Number.isNaN(n) || n < 0 || n > 255)) return null;
  return ((nums[0] << 24) + (nums[1] << 16) + (nums[2] << 8) + nums[3]) >>> 0;
}

/** Host addresses for a CIDR (excludes network and broadcast). */
export function listCidrHosts(cidr: string): string[] {
  const match = /^(\d+)\.(\d+)\.(\d+)\.(\d+)\/(\d+)$/.exec(cidr);
  if (!match) return [];
  const ip =
    (Number.parseInt(match[1], 10) << 24) +
    (Number.parseInt(match[2], 10) << 16) +
    (Number.parseInt(match[3], 10) << 8) +
    Number.parseInt(match[4], 10);
  const bits = Number.parseInt(match[5], 10);
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
      rangeEnd = hosts.at(-1)!;
    }
  }
  return [rangeStart, rangeEnd];
}

/** Mirror deploy_topology._get_dhcp_range. */
export function getDhcpRange(netData: Record<string, unknown>): [number, number] | null {
  let rangeStart = textField(netData, "dhcpRangeStart");
  let rangeEnd = textField(netData, "dhcpRangeEnd");
  const cidr = textField(netData, "cidr");
  if (!rangeStart || !rangeEnd) {
    [rangeStart, rangeEnd] = computeDhcpBounds(cidr, rangeStart, rangeEnd);
  }
  const startNum = ipToNum(rangeStart);
  const endNum = ipToNum(rangeEnd);
  if (startNum === null || endNum === null) return null;
  return [startNum, endNum];
}

function reserveNodeAddresses(node: Node, used: Set<string>): void {
  const data = node.data as Record<string, unknown>;
  if (node.type === "networkNode") {
    const cidr = textField(data, "cidr");
    if (cidr) {
      const hosts = listCidrHosts(cidr);
      if (hosts.length > 0) used.add(hosts[0]);
    }
  }
  // Reserve VIPs so member NICs and other clusters cannot reuse them.
  if (node.type === "clusterNode") {
    const apiVip = textField(data, "apiVip").trim();
    const ingressVip = textField(data, "ingressVip").trim();
    if (apiVip) used.add(apiVip);
    if (ingressVip) used.add(ingressVip);
  }
  // Ceph mon and OSD addresses are static on the Multus interface.
  if (node.type === "cephClusterNode") {
    const labIp = textField(data, "labIp").trim();
    if (labIp) used.add(labIp);
    addNonemptyIps(used, (data.osdIps as string[]) || []);
  }
}

function addNonemptyIps(used: Set<string>, ips: Array<string | undefined>): void {
  for (const ip of ips) {
    if (ip) used.add(ip);
  }
}

/** Mirror deploy_topology._collect_used_ips. */
export function collectUsedIps(nodes: Node[]): Set<string> {
  const used = new Set<string>();
  for (const node of nodes) {
    const nics = (node.data.nics || []) as Array<{ ip?: string }>;
    addNonemptyIps(used, nics.map((nic) => nic.ip));
    reserveNodeAddresses(node, used);
  }
  return used;
}

/** If `ip` is a Ceph mon or OSD static, return a human label, else null. */
export function cephIpConflict(nodes: Node[], ip: string): string | null {
  if (!ip) return null;
  for (const node of nodes) {
    if (node.type !== "cephClusterNode") continue;
    const data = node.data as Record<string, unknown>;
    if (textField(data, "labIp") === ip) return "Ceph mon";
    if (((data.osdIps as string[]) || []).includes(ip)) return "Ceph OSD";
  }
  return null;
}

/** Highest `count` free hosts in `cidr`, top-down, skipping gateway + used. */
export function pickHighFreeIps(
  cidr: string,
  count: number,
  usedIps: Set<string>,
): string[] {
  if (count <= 0) return [];
  const hosts = listCidrHosts(cidr);
  if (hosts.length === 0) return [];
  const gateway = hosts[0];
  const picked: string[] = [];
  for (let i = hosts.length - 1; i >= 0; i -= 1) {
    const ip = hosts[i];
    if (ip === gateway || usedIps.has(ip)) continue;
    picked.push(ip);
    if (picked.length >= count) break;
  }
  return picked;
}

/**
 * Allocate stable, collision-free static OSD IPs for a cephClusterNode,
 * persisting them on `data.osdIps`. Mirrors backend
 * deploy_topology._auto_assign_ceph_osd_ips: OSD IPs are drawn from the top of
 * the Ceph network's range downward so they never land on node/VM NICs, VIPs,
 * or the mon. Existing valid entries are preserved (a running OSD keeps its
 * address); the list is grown or trimmed to match osdCount. Returns the same
 * array reference when nothing changes.
 */
export function assignCephOsdIps(nodes: Node[]): Node[] {
  const ceph = nodes.find((n) => n.type === "cephClusterNode");
  if (!ceph) return nodes;
  const data = ceph.data as Record<string, unknown>;
  const networkRef = textField(data, "networkRef");
  const netNode = nodes.find(
    (n) =>
      n.type === "networkNode" &&
      (textField(n.data, "id") === networkRef ||
        n.id === networkRef),
  );
  const cidr = netNode ? textField(netNode.data, "cidr") : "";
  if (!cidr) return nodes;
  const hosts = new Set(listCidrHosts(cidr));
  if (hosts.size === 0) return nodes;
  const osdCount = Math.max(1, Math.min(6, Number(data.osdCount) || 3));

  const existing = ((data.osdIps as string[]) || []).filter(Boolean);
  // Everything an OSD IP must avoid — collectUsedIps already covers NICs,
  // gateway, VIPs, the mon, and other ceph nodes' OSD IPs. Drop THIS node's own
  // OSD IPs so we can preserve them below; add the dnsmasq address (.2).
  const reserved = collectUsedIps(nodes);
  for (const ip of existing) reserved.delete(ip);
  const dnsmasq = listCidrHosts(cidr)[1];
  if (dnsmasq) reserved.add(dnsmasq);

  const kept = keepExistingOsdIps(existing, hosts, reserved, osdCount);
  const need = osdCount - kept.length;
  if (need > 0) {
    const excl = new Set(reserved);
    for (const ip of kept) excl.add(ip);
    kept.push(...pickHighFreeIps(cidr, need, excl));
  }

  const unchanged =
    existing.length === kept.length && existing.every((v, i) => v === kept[i]);
  if (unchanged) return nodes;
  return nodes.map((n) =>
    n.id === ceph.id ? { ...n, data: { ...data, osdIps: kept } } : n,
  );
}

function keepExistingOsdIps(existing: string[], hosts: Set<string>, reserved: Set<string>, count: number): string[] {
  const kept: string[] = [];
  for (const ip of existing) {
    if (hosts.has(ip) && !reserved.has(ip) && !kept.includes(ip)) kept.push(ip);
    if (kept.length >= count) break;
  }
  return kept;
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

/** High host in a CIDR (Ceph OSD / multi-node VIP helpers — gateway reserved). */
export function pickHighEndNicIp(cidr: string, usedIps: Set<string>): string | null {
  const hosts = listCidrHosts(cidr);
  if (hosts.length === 0) return null;
  const reserved = new Set(usedIps);
  reserved.add(hosts[0]); // gateway
  for (let i = hosts.length - 1; i >= 0; i -= 1) {
    if (!reserved.has(hosts[i])) return hosts[i];
  }
  return null;
}

/**
 * Template-style machine-network host IP: prefer `.10` and upward (matches
 * `ocp-sno.yaml` / SNO pattern). Gateway (first host, typically `.1`) is reserved.
 * Falls back to `.2`–`.9` when `.10+` is exhausted.
 */
export function pickTemplateStyleNicIp(
  cidr: string,
  usedIps: Set<string>,
): string | null {
  const hosts = listCidrHosts(cidr);
  if (hosts.length === 0) return null;
  const reserved = new Set(usedIps);
  reserved.add(hosts[0]); // gateway
  // hosts[0]=.1 … hosts[9]=.10 on a typical /24
  const preferFrom = Math.min(9, hosts.length - 1);
  for (let i = preferFrom; i < hosts.length; i += 1) {
    if (!reserved.has(hosts[i])) return hosts[i];
  }
  for (let i = 1; i < preferFrom; i += 1) {
    if (!reserved.has(hosts[i])) return hosts[i];
  }
  return null;
}

/** BMC CIDR allocation from .11 upward (.1 gateway). */
export function pickBmcNicIp(cidr: string, usedIps: Set<string>): string | null {
  const base = cidr.split("/")[0].split(".").slice(0, 3).join(".");
  if (base?.split(".").length !== 3) return null;
  for (let i = 11; i < 250; i += 1) {
    const candidate = `${base}.${i}`;
    if (!usedIps.has(candidate)) return candidate;
  }
  return null;
}

/** Pick a static member NIC IP for a cluster-attached network. */
export function pickClusterMemberNicIp(
  netData: Record<string, unknown>,
  usedIps: Set<string>,
): string | null {
  const cidr = textField(netData, "cidr");
  if (!cidr) return null;
  if (netData.networkType === "bmc") {
    return pickBmcNicIp(cidr, usedIps);
  }
  // Machine networks: .10-style (SNO template parity). Multi-node VIPs still use
  // high-end addresses via suggestClusterVips — not this picker.
  const preferred = pickTemplateStyleNicIp(cidr, usedIps);
  if (preferred) return preferred;
  return pickIpForNetwork(netData, usedIps);
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
    if (node.type !== "containerNode" || isShowroomContainer(node)) return node;
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
