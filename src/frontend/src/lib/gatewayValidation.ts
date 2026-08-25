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
