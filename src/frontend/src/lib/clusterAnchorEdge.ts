import type { Edge } from "@xyflow/react";

export const CLUSTER_ANCHOR_EDGE_TYPE = "clusterAnchor";

export function isClusterAnchorEdge(edge: Edge): boolean {
  const th = edge.targetHandle ?? "";
  const sh = edge.sourceHandle ?? "";
  return th.startsWith("cluster-net-") || sh.startsWith("cluster-net-");
}

export function clusterAnchorSide(
  edge: Pick<Edge, "sourceHandle" | "targetHandle">,
): "top" | "bottom" | null {
  const handle = edge.targetHandle?.startsWith("cluster-net-")
    ? edge.targetHandle
    : edge.sourceHandle?.startsWith("cluster-net-")
      ? edge.sourceHandle
      : null;
  if (!handle) return null;
  if (handle.includes("bottom")) return "bottom";
  if (handle.includes("top")) return "top";
  return null;
}

/** Vertical stub above/below the network anchor before the shared bus. */
export const CLUSTER_ANCHOR_BUS_STUB_PX = 28;

/** Orthogonal path: vertical at cluster x, horizontal bus, vertical at network anchor. */
export function buildClusterAnchorPath(
  sourceX: number,
  sourceY: number,
  targetX: number,
  targetY: number,
  side: "top" | "bottom",
): string {
  if (Math.abs(sourceX - targetX) < 6) {
    return `M ${sourceX} ${sourceY} L ${targetX} ${targetY}`;
  }
  if (side === "bottom") {
    // Network below cluster: drop from cluster, bus above network, stub to anchor.
    const busY = sourceY - CLUSTER_ANCHOR_BUS_STUB_PX;
    return `M ${targetX} ${targetY} L ${targetX} ${busY} L ${sourceX} ${busY} L ${sourceX} ${sourceY}`;
  }
  // Network above cluster: stub from anchor, bus, then up to cluster top.
  const busY = sourceY + CLUSTER_ANCHOR_BUS_STUB_PX;
  return `M ${sourceX} ${sourceY} L ${sourceX} ${busY} L ${targetX} ${busY} L ${targetX} ${targetY}`;
}
