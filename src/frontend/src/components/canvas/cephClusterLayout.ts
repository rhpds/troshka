import type { Node } from "@xyflow/react";
import { BMC_CLUSTER_GAP, clusterBounds } from "./clusterBmc";

export const CEPH_NODE_W = 200;
export const CEPH_NODE_H = 80;

function rectsOverlap(
  a: { x: number; y: number; w: number; h: number },
  b: { x: number; y: number; w: number; h: number },
): boolean {
  return a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y;
}

/** Place Ceph between cluster boxes (CCLM) or to the right of a single cluster. */
export function cephClusterPosition(nodes: Node[]): { x: number; y: number } {
  const clusters = nodes.filter((n) => n.type === "clusterNode");
  if (clusters.length >= 2) {
    const sorted = [...clusters].sort(
      (a, b) => (a.position?.x ?? 0) - (b.position?.x ?? 0),
    );
    const left = clusterBounds(sorted[0]);
    const right = clusterBounds(sorted[sorted.length - 1]);
    const gapLeft = left.x + left.w;
    const gapRight = right.x;
    const x =
      gapLeft + Math.max(BMC_CLUSTER_GAP, (gapRight - gapLeft - CEPH_NODE_W) / 2);
    const y = left.y + Math.max(0, (left.h - CEPH_NODE_H) / 2);
    return { x, y };
  }
  if (clusters.length === 1) {
    const bounds = clusterBounds(clusters[0]);
    return {
      x: bounds.x + bounds.w + BMC_CLUSTER_GAP,
      y: bounds.y + Math.max(0, (bounds.h - CEPH_NODE_H) / 2),
    };
  }
  return { x: 500, y: 400 };
}

/** Nudge ceph clear of cluster boundaries when overlapping (e.g. after template import). */
export function repositionCephClearOfClusters(nodes: Node[]): Node[] {
  const ceph = nodes.find((n) => n.type === "cephClusterNode");
  if (!ceph) return nodes;

  const clusters = nodes.filter((n) => n.type === "clusterNode");
  if (clusters.length === 0) return nodes;

  const pos = ceph.position ?? { x: 0, y: 0 };
  const cephRect = { x: pos.x, y: pos.y, w: CEPH_NODE_W, h: CEPH_NODE_H };
  const overlaps = clusters.some((c) => rectsOverlap(cephRect, clusterBounds(c)));
  if (!overlaps) return nodes;

  const next = cephClusterPosition(nodes);
  if (next.x === pos.x && next.y === pos.y) return nodes;
  return nodes.map((n) =>
    n.id === ceph.id ? { ...n, position: next } : n,
  );
}
