import type { Edge, Node } from "@xyflow/react";
import { isGatewayNode, isLabNetworkNode } from "@/lib/showroomValidation";

export const GATEWAY_NETWORK_SOURCE_HANDLE = "bottom";
export const GATEWAY_NETWORK_TARGET_HANDLE = "top";

/** Gateway ↔ lab-network — matches VM/network connector color. */
export const GATEWAY_NETWORK_EDGE_STYLE = {
  stroke: "rgba(34, 211, 238, 0.5)",
  strokeWidth: 2,
  strokeDasharray: "6 4",
};

/** Router ↔ lab-network — green infra backbone styling. */
export const ROUTER_NETWORK_EDGE_STYLE = {
  stroke: "rgba(74, 222, 128, 0.5)",
  strokeWidth: 2,
  strokeDasharray: "8 4",
};

function infraEdgeStyle(source: Node, target: Node): typeof GATEWAY_NETWORK_EDGE_STYLE {
  if (
    (isGatewayNode(source) && isLabNetworkNode(target)) ||
    (isGatewayNode(target) && isLabNetworkNode(source))
  ) {
    return GATEWAY_NETWORK_EDGE_STYLE;
  }
  return ROUTER_NETWORK_EDGE_STYLE;
}

export function isRouterNode(node: Node | undefined): boolean {
  return (
    node?.type === "networkNode" &&
    (node.data as Record<string, unknown>).subtype === "router"
  );
}

function isInfraNetworkEdge(source: Node, target: Node): boolean {
  return (
    (isRouterNode(source) && isLabNetworkNode(target)) ||
    (isRouterNode(target) && isLabNetworkNode(source)) ||
    (isGatewayNode(source) && isLabNetworkNode(target)) ||
    (isGatewayNode(target) && isLabNetworkNode(source))
  );
}

function normalizeInfraNetworkEdge(
  edge: Edge,
  source: Node,
  target: Node,
): Edge {
  let next = edge;
  if (isGatewayNode(source) && isLabNetworkNode(target)) {
    if (edge.sourceHandle !== GATEWAY_NETWORK_SOURCE_HANDLE) {
      next = { ...next, sourceHandle: GATEWAY_NETWORK_SOURCE_HANDLE };
    }
    if (edge.targetHandle !== GATEWAY_NETWORK_TARGET_HANDLE) {
      next = { ...next, targetHandle: GATEWAY_NETWORK_TARGET_HANDLE };
    }
  }
  if (isGatewayNode(target) && isLabNetworkNode(source)) {
    if (edge.targetHandle !== GATEWAY_NETWORK_SOURCE_HANDLE) {
      next = { ...next, targetHandle: GATEWAY_NETWORK_SOURCE_HANDLE };
    }
    if (edge.sourceHandle !== GATEWAY_NETWORK_TARGET_HANDLE) {
      next = { ...next, sourceHandle: GATEWAY_NETWORK_TARGET_HANDLE };
    }
  }
  const edgeStyleTarget = infraEdgeStyle(source, target);
  const style = next.style as Record<string, unknown> | undefined;
  if (
    isInfraNetworkEdge(source, target) &&
    (style?.stroke !== edgeStyleTarget.stroke || next.animated !== true)
  ) {
    next = {
      ...next,
      style: edgeStyleTarget,
      animated: true,
    };
  }
  return next;
}

/** Normalize router/gateway ↔ lab-network edges (handles + connector styling). */
export function normalizeInfraNetworkEdges(nodes: Node[], edges: Edge[]): Edge[] {
  let changed = false;
  const next = edges.map((edge) => {
    const source = nodes.find((n) => n.id === edge.source);
    const target = nodes.find((n) => n.id === edge.target);
    if (!source || !target) return edge;
    if (!isInfraNetworkEdge(source, target)) return edge;

    const normalized = normalizeInfraNetworkEdge(edge, source, target);
    if (normalized !== edge) changed = true;
    return normalized;
  });
  return changed ? next : edges;
}

/** @deprecated Use normalizeInfraNetworkEdges */
export const normalizeGatewayNetworkEdges = normalizeInfraNetworkEdges;

export function peerLabNetworkOnGatewayEdge(
  edge: Edge,
  gatewayId: string,
  nodes: Node[],
): Node | undefined {
  const source = nodes.find((n) => n.id === edge.source);
  const target = nodes.find((n) => n.id === edge.target);
  if (edge.source === gatewayId && isLabNetworkNode(target)) return target;
  if (edge.target === gatewayId && isLabNetworkNode(source)) return source;
  return undefined;
}

export function isGatewayConnectedLabNetwork(
  network: Node,
  nodes: Node[],
  edges: Edge[],
): boolean {
  const gateway = nodes.find(isGatewayNode);
  if (!gateway || !isLabNetworkNode(network)) return false;
  return edges.some((edge) => {
    const peer = peerLabNetworkOnGatewayEdge(edge, gateway.id, nodes);
    return peer?.id === network.id;
  });
}

export const SHOWROOM_MANAGED_OUTBOUND_PORTS = ["53", "443"];

function parseOutboundPortEntries(outboundPorts: string): string[] {
  return outboundPorts
    .split(",")
    .map((p) => p.trim())
    .filter(Boolean);
}

function outboundEntriesIncludePort(entries: string[], port: string): boolean {
  return entries.some(
    (entry) => entry === port || (entry.includes("/") && entry.split("/", 1)[0] === port),
  );
}

export function gatewayAllowsDnsUpstream(gatewayData: Record<string, unknown>): boolean {
  const policy = String(gatewayData.outboundPolicy || "allow-all");
  if (policy !== "restrict") return true;
  const entries = parseOutboundPortEntries(String(gatewayData.outboundPorts || ""));
  return outboundEntriesIncludePort(entries, "53");
}

export function injectShowroomOutboundPorts(outboundPorts: string): {
  ports: string;
  added: string[];
} {
  const entries = parseOutboundPortEntries(outboundPorts);
  const added: string[] = [];
  for (const port of SHOWROOM_MANAGED_OUTBOUND_PORTS) {
    if (!outboundEntriesIncludePort(entries, port)) {
      entries.push(port);
      added.push(port);
    }
  }
  return { ports: entries.join(","), added };
}

export function stripShowroomOutboundPorts(
  outboundPorts: string,
  managed: string[],
): string {
  if (!managed.length) return outboundPorts;
  const managedSet = new Set(managed);
  const stripped = parseOutboundPortEntries(outboundPorts).filter(
    (entry) =>
      !managedSet.has(entry) &&
      !(entry.includes("/") && managedSet.has(entry.split("/", 1)[0])),
  );
  return stripped.join(",");
}

/** First gateway-connected lab network with DNS enabled (edge order). */
export function gatewayConnectedDnsNetworkName(nodes: Node[], edges: Edge[]): string {
  const gateway = nodes.find(isGatewayNode);
  if (!gateway) return "";
  if (!gatewayAllowsDnsUpstream(gateway.data as Record<string, unknown>)) return "";
  for (const edge of edges) {
    const net = peerLabNetworkOnGatewayEdge(edge, gateway.id, nodes);
    if (!net) continue;
    const d = net.data as Record<string, unknown>;
    const sub = d.subtype as string | undefined;
    if (sub && !["network", "dhcp", "dns"].includes(sub)) continue;
    if (!d.dns && sub !== "dns") continue;
    return String(d.name || d.label || "");
  }
  return "";
}
