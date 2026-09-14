import type { Node, Edge } from "@xyflow/react";
import type { ClusterConfig } from "@/stores/canvasStore";

export const BMC_EDGE_STYLE = {
  stroke: "rgba(168,85,247,0.5)",
  strokeWidth: 2,
  strokeDasharray: "6 4",
};

const DEFAULT_BMC_CIDR = "192.168.100.0/24";

export function generateBmcPassword(): string {
  const chars = "abcdefghijklmnopqrstuvwxyz0123456789";
  const randomBytes = new Uint8Array(16);
  crypto.getRandomValues(randomBytes);
  return Array.from(randomBytes, (b) => chars[b % chars.length]).join("");
}

export function findBmcNetwork(nodes: Node[]): Node | undefined {
  return nodes.find(
    (n) => n.type === "networkNode" && (n.data as Record<string, unknown>).networkType === "bmc",
  );
}

export function collectUsedBmcIps(nodes: Node[]): Set<string> {
  const used = new Set<string>();
  for (const n of nodes) {
    if (n.type !== "vmNode") continue;
    const ip = (n.data as Record<string, unknown>).bmcIp;
    if (typeof ip === "string" && ip) used.add(ip);
  }
  return used;
}

/** Next host in the BMC network CIDR from .11 upward (.1 is gateway). */
export function nextFreeBmcIp(nodes: Node[], bmcNet?: Node | null, usedOverride?: Set<string>): string {
  const net = bmcNet ?? findBmcNetwork(nodes);
  const cidr = String((net?.data as Record<string, unknown>)?.cidr || DEFAULT_BMC_CIDR);
  const base = cidr.split("/")[0].split(".").slice(0, 3).join(".");
  const usedIps = usedOverride ?? collectUsedBmcIps(nodes);

  for (let i = 11; i < 250; i += 1) {
    const candidate = `${base}.${i}`;
    if (!usedIps.has(candidate)) return candidate;
  }
  return `${base}.11`;
}

export type BmcCredentials = { username: string; password: string };

export function bmcNetworkCredentials(bmcNet: Node): BmcCredentials {
  const d = bmcNet.data as Record<string, unknown>;
  return {
    username: typeof d.bmcUsername === "string" && d.bmcUsername ? d.bmcUsername : "admin",
    password: typeof d.bmcPassword === "string" ? d.bmcPassword : "",
  };
}

/** Ensure the BMC network node carries username/password (generate if missing). */
export function ensureBmcNetworkCredentials(bmcNet: Node): Node {
  const creds = bmcNetworkCredentials(bmcNet);
  if (creds.password) return bmcNet;
  const password = generateBmcPassword();
  const d = bmcNet.data as Record<string, unknown>;
  return {
    ...bmcNet,
    data: { ...d, bmcUsername: creds.username, bmcPassword: password },
  };
}

export function createBmcNetworkNode(
  position: { x: number; y: number },
  id?: string,
  creds?: Partial<BmcCredentials>,
): Node {
  return {
    id: id ?? `bmc-network-${Date.now()}`,
    type: "networkNode",
    position,
    data: {
      label: "BMC Network",
      name: "BMC Network",
      subtype: "network",
      networkType: "bmc",
      cidr: DEFAULT_BMC_CIDR,
      dhcp: true,
      dns: false,
      bmcUsername: creds?.username || "admin",
      bmcPassword: creds?.password || generateBmcPassword(),
    },
  } as Node;
}

export function bmcEdgeId(bmcNetId: string, vmId: string): string {
  return `edge-bmc-${bmcNetId}-to-${vmId}`;
}

export function makeBmcEdge(bmcNetId: string, vmId: string): Edge {
  return {
    id: bmcEdgeId(bmcNetId, vmId),
    source: bmcNetId,
    target: vmId,
    type: "smoothstep",
    animated: true,
    style: BMC_EDGE_STYLE,
  } as Edge;
}

function bmcNetworkPosition(cluster: ClusterConfig, nodes: Node[]): { x: number; y: number } {
  const clusterNode = nodes.find((n) => n.id === cluster.nodeId);
  if (clusterNode?.position) {
    const w = Number((clusterNode.style as Record<string, unknown> | undefined)?.width) || 400;
    return { x: clusterNode.position.x + w + 80, y: clusterNode.position.y };
  }
  const members = nodes.filter(
    (n) => n.type === "vmNode" && (n.data as Record<string, unknown>).clusterId === cluster.id,
  );
  const avgX = members.reduce((sum, n) => sum + (n.position?.x || 0), 0) / Math.max(members.length, 1);
  const avgY = members.reduce((sum, n) => sum + (n.position?.y || 0), 0) / Math.max(members.length, 1);
  return { x: avgX + 300, y: avgY };
}

function clusterMemberIds(cluster: ClusterConfig, nodes: Node[]): string[] {
  return nodes
    .filter((n) => n.type === "vmNode" && (n.data as Record<string, unknown>).clusterId === cluster.id)
    .map((n) => n.id);
}

/**
 * Enable BMC on cluster member VMs, ensure a BMC network exists (with creds),
 * allocate free BMC IPs from its CIDR, and wire purple dashed BMC edges.
 */
export function applyClusterBmc(
  cluster: ClusterConfig,
  nodes: Node[],
  edges: Edge[],
): { nodes: Node[]; edges: Edge[] } {
  const memberIds = new Set(clusterMemberIds(cluster, nodes));
  if (memberIds.size === 0) return { nodes, edges };

  let nextNodes = [...nodes];
  let bmcNet = findBmcNetwork(nextNodes);
  let nodesChanged = false;
  if (!bmcNet) {
    bmcNet = createBmcNetworkNode(bmcNetworkPosition(cluster, nextNodes));
    nextNodes.push(bmcNet);
    nodesChanged = true;
  }
  const ensuredNet = ensureBmcNetworkCredentials(bmcNet);
  if (ensuredNet !== bmcNet) {
    bmcNet = ensuredNet;
    nextNodes = nextNodes.map((n) => (n.id === bmcNet!.id ? bmcNet! : n));
    nodesChanged = true;
  }
  const { username: bmcUsername, password: bmcPassword } = bmcNetworkCredentials(bmcNet);

  const usedIps = collectUsedBmcIps(nextNodes);
  nextNodes = nextNodes.map((n) => {
    if (!memberIds.has(n.id)) return n;
    const d = n.data as Record<string, unknown>;
    const bmcIp = typeof d.bmcIp === "string" && d.bmcIp ? d.bmcIp : nextFreeBmcIp(nextNodes, bmcNet, usedIps);
    if (!usedIps.has(bmcIp)) usedIps.add(bmcIp);
    if (
      d.bmcEnabled === true &&
      d.bmcIp === bmcIp &&
      d.bmcUsername === bmcUsername &&
      d.bmcPassword === bmcPassword
    ) {
      return n;
    }
    nodesChanged = true;
    return {
      ...n,
      data: { ...d, bmcEnabled: true, bmcIp, bmcUsername, bmcPassword },
    };
  });

  const edgeIds = new Set(edges.map((e) => e.id));
  const hasEdge = (vmId: string) =>
    edges.some(
      (e) =>
        (e.source === bmcNet!.id && e.target === vmId) ||
        (e.target === bmcNet!.id && e.source === vmId),
    );

  const addedEdges: Edge[] = [];
  for (const vmId of memberIds) {
    const edgeId = bmcEdgeId(bmcNet.id, vmId);
    if (!edgeIds.has(edgeId) && !hasEdge(vmId)) {
      addedEdges.push(makeBmcEdge(bmcNet.id, vmId));
      edgeIds.add(edgeId);
    }
  }

  if (!nodesChanged && addedEdges.length === 0) {
    return { nodes, edges };
  }
  return { nodes: nextNodes, edges: [...edges, ...addedEdges] };
}
