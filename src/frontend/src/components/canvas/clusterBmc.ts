import type { Node, Edge } from "@xyflow/react";
import type { ClusterConfig } from "@/stores/canvasStore";

export const BMC_EDGE_STYLE = {
  stroke: "rgba(168,85,247,0.5)",
  strokeWidth: 2,
  strokeDasharray: "6 4",
};

const DEFAULT_BMC_CIDR = "192.168.100.0/24";
/** Match auto_layout.py net_w / net_h and NetworkNode card size. */
export const BMC_NET_W = 240;
export const BMC_NET_H = 70;
export const BMC_CLUSTER_GAP = 80;

type Rect = { x: number; y: number; w: number; h: number };

export function clusterBounds(node: Node): Rect {
  const w = Number((node.style as Record<string, unknown> | undefined)?.width) || 520;
  const h = Number((node.style as Record<string, unknown> | undefined)?.height) || 320;
  return { x: node.position.x, y: node.position.y, w, h };
}

function rectsOverlap(a: Rect, b: Rect): boolean {
  return a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y;
}

/** Place BMC to the right of a cluster boundary, vertically centered. */
export function bmcNetworkPosition(
  cluster: ClusterConfig,
  nodes: Node[],
): { x: number; y: number } {
  const clusterNode = nodes.find((n) => n.id === cluster.nodeId);
  if (clusterNode?.position) {
    const bounds = clusterBounds(clusterNode);
    return {
      x: bounds.x + bounds.w + BMC_CLUSTER_GAP,
      y: bounds.y + Math.max(0, (bounds.h - BMC_NET_H) / 2),
    };
  }
  const members = nodes.filter(
    (n) => n.type === "vmNode" && (n.data as Record<string, unknown>).clusterId === cluster.id,
  );
  const avgX = members.reduce((sum, n) => sum + (n.position?.x || 0), 0) / Math.max(members.length, 1);
  const avgY = members.reduce((sum, n) => sum + (n.position?.y || 0), 0) / Math.max(members.length, 1);
  return { x: avgX + 300, y: avgY };
}

/**
 * Nudge the BMC network clear of every cluster boundary (e.g. after the box
 * grows when workers are added and the old x+width+gap lands inside the box).
 */
export function repositionBmcNetworkClearOfClusters(
  bmcNet: Node,
  nodes: Node[],
): Node {
  const clusters = nodes.filter((n) => n.type === "clusterNode");
  if (clusters.length === 0) return bmcNet;

  const boxes = clusters.map(clusterBounds);
  const pos = bmcNet.position ?? { x: 0, y: 0 };
  const bmcRect: Rect = {
    x: pos.x,
    y: pos.y,
    w: BMC_NET_W,
    h: BMC_NET_H,
  };
  if (!boxes.some((b) => rectsOverlap(bmcRect, b))) return bmcNet;

  const maxRight = Math.max(...boxes.map((b) => b.x + b.w));
  const minTop = Math.min(...boxes.map((b) => b.y));
  const maxBottom = Math.max(...boxes.map((b) => b.y + b.h));
  let nx = maxRight + BMC_CLUSTER_GAP;
  let ny = minTop + Math.max(0, (maxBottom - minTop - BMC_NET_H) / 2);

  const candidate: Rect = { x: nx, y: ny, w: BMC_NET_W, h: BMC_NET_H };
  if (boxes.some((b) => rectsOverlap(candidate, b))) {
    nx = Math.min(...boxes.map((b) => b.x));
    ny = maxBottom + BMC_CLUSTER_GAP;
  }

  if (nx === pos.x && ny === pos.y) return bmcNet;
  return { ...bmcNet, position: { x: nx, y: ny } };
}

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
  } else {
    const repositioned = repositionBmcNetworkClearOfClusters(bmcNet, nextNodes);
    if (repositioned !== bmcNet) {
      bmcNet = repositioned;
      nextNodes = nextNodes.map((n) => (n.id === bmcNet!.id ? bmcNet! : n));
      nodesChanged = true;
    }
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
