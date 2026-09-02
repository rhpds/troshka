import type { Node } from "@xyflow/react";

/**
 * Cluster membership for a VM node: which cluster boundary it sits inside.
 *
 * `parentId` is the React Flow parent node id (the cluster boundary node),
 * `clusterId` is the logical ClusterConfig id stored on the VM as
 * `data.clusterId`. Both are `null` when the VM is outside every boundary.
 */
export interface Membership {
  parentId: string | null;
  clusterId: string | null;
}

interface XY {
  x: number;
  y: number;
}

/**
 * Rendered footprint of a node in px.
 *
 * In @xyflow/react v12 the measured size lives on `node.measured?.width/height`;
 * we fall back to an explicit top-level `width`/`height` (unit tests set these
 * directly) and finally to `style.width/height` (cluster boundaries store their
 * footprint there — see clusterFactory). Missing dimensions resolve to 0 so a
 * VM with no measured size is treated as a point at its `position`.
 */
function nodeDims(n: Node): { width: number; height: number } {
  const measured = (n as { measured?: { width?: number; height?: number } }).measured;
  const explicit = n as { width?: number; height?: number };
  const style = (n.style ?? {}) as { width?: number | string; height?: number | string };
  const width = measured?.width ?? explicit.width ?? style.width;
  const height = measured?.height ?? explicit.height ?? style.height;
  return { width: Number(width) || 0, height: Number(height) || 0 };
}

/** Logical cluster id for a boundary node (from data, else derived from id). */
function clusterIdOf(boundary: Node): string {
  const cid = (boundary.data as { clusterId?: unknown } | undefined)?.clusterId;
  if (typeof cid === "string" && cid) return cid;
  return boundary.id.replace(/^cluster-/, "");
}

function centerOf(node: Node): XY {
  const { width, height } = nodeDims(node);
  return { x: node.position.x + width / 2, y: node.position.y + height / 2 };
}

/**
 * PURE point-in-rect membership resolver.
 *
 * Returns the cluster boundary whose rectangle contains the dragged node's
 * center. If the center falls inside multiple boundaries, the smallest (topmost)
 * one wins. If it is inside none, membership is cleared (`{null, null}`).
 *
 * The dragged node's `position` must be ABSOLUTE (canvas coordinates); the
 * Canvas wiring converts a parented node's relative position before calling.
 */
export function resolveMembership(draggedNode: Node, clusterNodes: Node[]): Membership {
  const center = centerOf(draggedNode);
  let best: Node | null = null;
  let bestArea = Infinity;
  for (const boundary of clusterNodes) {
    const { width, height } = nodeDims(boundary);
    const inside =
      center.x >= boundary.position.x &&
      center.x <= boundary.position.x + width &&
      center.y >= boundary.position.y &&
      center.y <= boundary.position.y + height;
    if (!inside) continue;
    const area = width * height;
    if (area < bestArea) {
      bestArea = area;
      best = boundary;
    }
  }
  if (!best) return { parentId: null, clusterId: null };
  return { parentId: best.id, clusterId: clusterIdOf(best) };
}

/**
 * Absolute canvas position of a node, resolving a single level of parenting.
 *
 * React Flow stores a child node's `position` RELATIVE to its parent; this adds
 * the parent's position back to recover absolute coordinates. Cluster
 * boundaries are never themselves nested, so one level is sufficient.
 */
export function absolutePosition(node: Node, candidateParents: Node[]): XY {
  if (!node.parentId) return node.position;
  const parent = candidateParents.find((n) => n.id === node.parentId);
  if (!parent) return node.position;
  return { x: node.position.x + parent.position.x, y: node.position.y + parent.position.y };
}

/**
 * Stored `position` for a node given a new parent (or none).
 *
 * When assigning a parent, an absolute position must be expressed relative to
 * that parent so the node does not visually jump. With no parent the absolute
 * position is used as-is.
 */
export function relativePosition(absPos: XY, parent: Node | null): XY {
  if (!parent) return absPos;
  return { x: absPos.x - parent.position.x, y: absPos.y - parent.position.y };
}

/**
 * Return `nodes` with `childId` moved to immediately after `parentId`.
 *
 * React Flow v12 requires a child node to appear AFTER its parent in the nodes
 * array or it will not render inside the boundary (and may error). A VM that
 * existed before its cluster was dropped has a lower array index than the
 * cluster, so on reparent it must be reordered. No-op when the child is already
 * after the parent, when either node is missing, or when they are the same node.
 */
export function orderChildAfterParent(nodes: Node[], childId: string, parentId: string): Node[] {
  if (childId === parentId) return nodes;
  const childIndex = nodes.findIndex((n) => n.id === childId);
  const parentIndex = nodes.findIndex((n) => n.id === parentId);
  if (childIndex === -1 || parentIndex === -1) return nodes;
  if (childIndex > parentIndex) return nodes;
  const child = nodes[childIndex];
  const without = nodes.filter((_, i) => i !== childIndex);
  // Parent index shifts down by one once the earlier child is removed.
  const insertAt = without.findIndex((n) => n.id === parentId) + 1;
  return [...without.slice(0, insertAt), child, ...without.slice(insertAt)];
}
