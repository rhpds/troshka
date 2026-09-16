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

/**
 * Place Ceph between cluster boxes when the gap is wide enough (template import),
 * otherwise centered below the cluster row (auto-layout packs boxes tight).
 */
export function cephClusterPosition(nodes: Node[]): { x: number; y: number } {
  const clusters = nodes.filter((n) => n.type === "clusterNode");
  if (clusters.length === 0) return { x: 500, y: 400 };

  const bounds = clusters.map(clusterBounds);
  const minLeft = Math.min(...bounds.map((b) => b.x));
  const maxRight = Math.max(...bounds.map((b) => b.x + b.w));
  const maxBottom = Math.max(...bounds.map((b) => b.y + b.h));

  if (clusters.length >= 2) {
    const sorted = [...clusters].sort(
      (a, b) => (a.position?.x ?? 0) - (b.position?.x ?? 0),
    );
    const left = clusterBounds(sorted[0]);
    const right = clusterBounds(sorted[sorted.length - 1]);
    const gap = right.x - (left.x + left.w);
    if (gap >= CEPH_NODE_W + BMC_CLUSTER_GAP) {
      return {
        x: left.x + left.w + (gap - CEPH_NODE_W) / 2,
        y: left.y + Math.max(0, (left.h - CEPH_NODE_H) / 2),
      };
    }
    return {
      x: minLeft + Math.max(0, (maxRight - minLeft - CEPH_NODE_W) / 2),
      y: maxBottom + BMC_CLUSTER_GAP,
    };
  }

  const b = bounds[0];
  return {
    x: b.x + b.w + BMC_CLUSTER_GAP,
    y: b.y + Math.max(0, (b.h - CEPH_NODE_H) / 2),
  };
}

/** Nudge ceph clear of cluster boundaries when overlapping (e.g. after auto-layout). */
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
