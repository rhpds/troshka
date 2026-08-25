import type { Edge, Node } from "@xyflow/react";

export interface ShowroomReadiness {
  hasContentRepo: boolean;
  hasGateway: boolean;
  issues: string[];
}

export const SHOWROOM_GATEWAY_SOURCE_HANDLE = "gateway-link-right";
export const SHOWROOM_GATEWAY_TARGET_HANDLE = "showroom-link";

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

function getGatewayNode(nodes: Node[]): Node | undefined {
  return nodes.find((n) => isGatewayNode(n));
}

export function buildShowroomGatewayEdge(showroomId: string, gatewayId: string): Edge {
  return {
    id: `showroom-gw-${showroomId.slice(0, 8)}-${gatewayId.slice(0, 8)}`,
    source: showroomId,
    target: gatewayId,
    sourceHandle: SHOWROOM_GATEWAY_SOURCE_HANDLE,
    targetHandle: SHOWROOM_GATEWAY_TARGET_HANDLE,
    type: "smoothstep",
    data: { cosmetic: true },
    style: {
      stroke: "rgba(74,222,128,0.45)",
      strokeWidth: 2,
      strokeDasharray: "8 6",
    },
    animated: true,
  };
}

/** Add cosmetic showroom→gateway edge when both nodes exist. */
export function ensureShowroomGatewayEdge(nodes: Node[], edges: Edge[]): Edge[] {
  const showroom = getShowroomNode(nodes);
  const gateway = getGatewayNode(nodes);
  if (!showroom || !gateway) return edges;
  const has = edges.some(
    (e) =>
      isShowroomGatewayEdge(e) ||
      (e.source === showroom.id && e.target === gateway.id) ||
      (e.target === showroom.id && e.source === gateway.id),
  );
  if (has) return edges;
  return [...edges, buildShowroomGatewayEdge(showroom.id, gateway.id)];
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
