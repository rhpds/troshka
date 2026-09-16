import type { Node } from "@xyflow/react";
import { clusterBounds } from "./clusterBmc";

export const CEPH_NODE_W = 200;
export const CEPH_NODE_H = 80;
export const CEPH_CLUSTER_GAP = 40;

export function cephGapWidth(): number {
  return CEPH_NODE_W + CEPH_CLUSTER_GAP * 2;
}

function sortedClusterNodes(nodes: Node[]): Node[] {
  return [...nodes]
    .filter((n) => n.type === "clusterNode")
    .sort((a, b) => (a.position?.x ?? 0) - (b.position?.x ?? 0));
}

/** Shift the rightmost cluster rightward when the inter-cluster gap is too narrow for ceph. */
export function spreadClustersForCeph(nodes: Node[]): Node[] {
  const clusters = sortedClusterNodes(nodes);
  const hasCeph = nodes.some((n) => n.type === "cephClusterNode");
  if (!hasCeph || clusters.length < 2) return nodes;

  const left = clusterBounds(clusters[0]);
  const right = clusterBounds(clusters[clusters.length - 1]);
  const gap = right.x - (left.x + left.w);
  const needed = cephGapWidth();
  if (gap >= needed) return nodes;

  const shift = needed - gap;
  const rightId = clusters[clusters.length - 1].id;
  return nodes.map((n) => {
    if (n.id === rightId) {
      return {
        ...n,
        position: {
          x: (n.position?.x ?? 0) + shift,
          y: n.position?.y ?? 0,
        },
      };
    }
    return n;
  });
}

/** Center ceph in the gap between cluster boxes (after spreading if needed). */
export function cephClusterPosition(nodes: Node[]): { x: number; y: number } {
  const spread = spreadClustersForCeph(nodes);
  const clusters = sortedClusterNodes(spread);
  if (clusters.length === 0) return { x: 500, y: 400 };

  if (clusters.length >= 2) {
    const left = clusterBounds(clusters[0]);
    const right = clusterBounds(clusters[clusters.length - 1]);
    const gap = right.x - (left.x + left.w);
    return {
      x: left.x + left.w + Math.max(CEPH_CLUSTER_GAP, (gap - CEPH_NODE_W) / 2),
      y: left.y + Math.max(0, (left.h - CEPH_NODE_H) / 2),
    };
  }

  const b = clusterBounds(clusters[0]);
  return {
    x: b.x + b.w + CEPH_CLUSTER_GAP,
    y: b.y + Math.max(0, (b.h - CEPH_NODE_H) / 2),
  };
}

/** Widen cluster gap (if needed) and place ceph centered between the boxes. */
export function layoutCephBetweenClusters(nodes: Node[]): Node[] {
  const ceph = nodes.find((n) => n.type === "cephClusterNode");
  if (!ceph) return nodes;
  if (!nodes.some((n) => n.type === "clusterNode")) return nodes;

  const next = spreadClustersForCeph(nodes);
  const pos = cephClusterPosition(next);
  return next.map((n) => (n.id === ceph.id ? { ...n, position: pos } : n));
}

/** Spread clusters and center ceph in the gap (auto-layout, load, drop). */
export function repositionCephClearOfClusters(nodes: Node[]): Node[] {
  if (
    nodes.some((n) => n.type === "cephClusterNode") &&
    nodes.some((n) => n.type === "clusterNode")
  ) {
    return layoutCephBetweenClusters(nodes);
  }
  return nodes;
}
