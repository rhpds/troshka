import type { Edge, Node } from "@xyflow/react";

export interface ShowroomReadiness {
  hasContentRepo: boolean;
  hasGateway: boolean;
  issues: string[];
}

export const SHOWROOM_GATEWAY_SOURCE_HANDLE = "gateway-link-right";
export const SHOWROOM_GATEWAY_TARGET_HANDLE = "showroom-link";

export const SHOWROOM_GATEWAY_EDGE_STYLE = {
  stroke: "rgba(59, 130, 246, 0.7)",
  strokeWidth: 2,
  strokeDasharray: "8 6",
};

/** Orthogonal routing (sharp 90° corners). */
export const SHOWROOM_GATEWAY_EDGE_TYPE = "step";

export function isShowroomContainer(node: Node | undefined): boolean {
  if (!node || node.type !== "containerNode") return false;
  const d = node.data as Record<string, unknown>;
  return !!(d.isShowroom || d.name === "showroom");
}

export function isGatewayNode(node: Node | undefined): boolean {
  return (
    node?.type === "networkNode" &&
    (node.data as Record<string, unknown>).subtype === "gateway"
  );
}

/** Lab network node (not router/gateway/load balancer). */
export function isLabNetworkNode(node: Node | undefined): boolean {
  if (!node || node.type !== "networkNode") return false;
  const sub = (node.data as Record<string, unknown>).subtype;
  return sub !== "router" && sub !== "gateway" && sub !== "loadbalancer";
}

export function isShowroomLabNetworkEdge(edge: Edge, nodes: Node[]): boolean {
  const source = nodes.find((n) => n.id === edge.source);
  const target = nodes.find((n) => n.id === edge.target);
  if (!source || !target) return false;
  const showroom = isShowroomContainer(source) ? source : isShowroomContainer(target) ? target : undefined;
  if (!showroom) return false;
  const other = source.id === showroom.id ? target : source;
  return isLabNetworkNode(other);
}

function isInvalidShowroomMountEdge(edge: Edge, nodes: Node[]): boolean {
  const source = nodes.find((n) => n.id === edge.source);
  const target = nodes.find((n) => n.id === edge.target);
  if (!source || !target) return false;
  const showroom = isShowroomContainer(source) ? source : isShowroomContainer(target) ? target : undefined;
  if (!showroom) return false;
  const handle =
    source.id === showroom.id ? edge.sourceHandle || "" : edge.targetHandle || "";
  return handle.startsWith("mnt-") && handle.endsWith("-right");
}

/** Drop lab-network edges to showroom and strip accidental NICs (infra networking only). */
export function sanitizeShowroomTopology(
  nodes: Node[],
  edges: Edge[],
): { nodes: Node[]; edges: Edge[] } {
  const filteredEdges = edges.filter(
    (e) => !isShowroomLabNetworkEdge(e, nodes) && !isInvalidShowroomMountEdge(e, nodes),
  );
  const showroom = getShowroomNode(nodes);
  if (!showroom) {
    return filteredEdges.length === edges.length
      ? { nodes, edges }
      : { nodes, edges: filteredEdges };
  }

  const data = showroom.data as Record<string, unknown>;
  const nics = data.nics;
  const needsNicClear = Array.isArray(nics) && nics.length > 0;
  const needsInfraFlag = data.infraNetworking !== true;

  if (!needsNicClear && !needsInfraFlag && filteredEdges.length === edges.length) {
    return { nodes, edges };
  }

  const nextNodes = nodes.map((n) => {
    if (n.id !== showroom.id) return n;
    return {
      ...n,
      data: {
        ...n.data,
        nics: [],
        infraNetworking: true,
      },
    };
  });

  return { nodes: nextNodes, edges: filteredEdges };
}

export function isShowroomGatewayEdge(edge: Edge): boolean {
  const sh = edge.sourceHandle || "";
  const th = edge.targetHandle || "";
  return (
    sh === SHOWROOM_GATEWAY_SOURCE_HANDLE && th === SHOWROOM_GATEWAY_TARGET_HANDLE ||
    th === SHOWROOM_GATEWAY_SOURCE_HANDLE && sh === SHOWROOM_GATEWAY_TARGET_HANDLE
  );
}

export function getShowroomNode(nodes: Node[]): Node | undefined {
  return nodes.find((n) => isShowroomContainer(n));
}

export function getGatewayNode(nodes: Node[]): Node | undefined {
  return nodes.find((n) => isGatewayNode(n));
}

export function buildShowroomGatewayEdge(showroomId: string, gatewayId: string): Edge {
  return {
    id: `showroom-gw-${showroomId.slice(0, 8)}-${gatewayId.slice(0, 8)}`,
    source: showroomId,
    target: gatewayId,
    sourceHandle: SHOWROOM_GATEWAY_SOURCE_HANDLE,
    targetHandle: SHOWROOM_GATEWAY_TARGET_HANDLE,
    type: SHOWROOM_GATEWAY_EDGE_TYPE,
    data: { cosmetic: true },
    style: SHOWROOM_GATEWAY_EDGE_STYLE,
    animated: true,
  };
}

function isShowroomGatewayPair(
  edge: Edge,
  showroomId: string,
  gatewayId: string,
): boolean {
  return (
    (edge.source === showroomId && edge.target === gatewayId) ||
    (edge.source === gatewayId && edge.target === showroomId)
  );
}

/** Add cosmetic showroom→gateway edge when both nodes exist. */
export function ensureShowroomGatewayEdge(nodes: Node[], edges: Edge[]): Edge[] {
  const showroom = getShowroomNode(nodes);
  const gateway = getGatewayNode(nodes);
  if (!showroom || !gateway) return edges;

  const canonical = buildShowroomGatewayEdge(showroom.id, gateway.id);
  let hasCanonical = false;
  const normalized = edges.map((e) => {
    if (!isShowroomGatewayPair(e, showroom.id, gateway.id)) return e;
    if (isShowroomGatewayEdge(e)) {
      hasCanonical = true;
      return { ...canonical, id: e.id };
    }
    hasCanonical = true;
    return { ...canonical, id: e.id };
  });

  if (hasCanonical) return normalized;
  return [...normalized, canonical];
}

export function getShowroomReadiness(nodes: Node[], edges: Edge[]): ShowroomReadiness {
  const showroom = getShowroomNode(nodes);
  const empty: ShowroomReadiness = {
    hasContentRepo: false,
    hasGateway: false,
    issues: ["Add a showroom to the canvas"],
  };
  if (!showroom) return empty;

  const data = showroom.data as Record<string, unknown>;
  const buildContent = data.buildContent !== false;
  const contentRepo = String(data.contentRepo || "").trim();
  const hasContentRepo = !buildContent || contentRepo.length > 0;
  const hasGateway = !!getGatewayNode(nodes);

  const issues: string[] = [];
  if (!hasContentRepo) {
    issues.push("Content repo URL is required when build at deploy is enabled");
  }
  if (!hasGateway) {
    issues.push("Add a gateway to the project for external showroom access");
  }

  return {
    hasContentRepo,
    hasGateway,
    issues,
  };
}

export function isShowroomReady(nodes: Node[], edges: Edge[]): boolean {
  return getShowroomReadiness(nodes, edges).issues.length === 0;
}
