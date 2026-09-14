"use client";

import { BaseEdge, type EdgeProps } from "@xyflow/react";
import {
  buildClusterAnchorPath,
  clusterAnchorSide,
} from "@/lib/clusterAnchorEdge";

export default function ClusterAnchorEdge({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourceHandleId,
  targetHandleId,
  style,
  markerEnd,
}: EdgeProps) {
  const side =
    clusterAnchorSide({
      sourceHandle: sourceHandleId,
      targetHandle: targetHandleId,
    }) ?? "top";
  const path = buildClusterAnchorPath(sourceX, sourceY, targetX, targetY, side);
  return (
    <BaseEdge
      id={id}
      path={path}
      style={style}
      markerEnd={markerEnd}
      interactionWidth={20}
    />
  );
}
