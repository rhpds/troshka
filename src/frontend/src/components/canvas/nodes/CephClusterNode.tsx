"use client";

import React, { memo } from "react";
import { Handle, Position, type NodeProps } from "@xyflow/react";
import { useCanvasStore, stableNodeData, stableStringify } from "@/stores/canvasStore";

const CEPH_HANDLE_STYLE: React.CSSProperties = {
  pointerEvents: "all",
  background: "rgba(255, 255, 255, 0.9)",
  border: "1px solid rgba(255, 255, 255, 1)",
  width: 10,
  height: 10,
  borderRadius: "50%",
  zIndex: 20,
};

export function cephClusterDisplayName(data: { name?: string; label?: string }): string {
  const raw = data.name || data.label || "";
  if (!raw || raw === "project-ceph") return "Ceph Storage";
  return raw;
}

export interface CephClusterNodeData {
  label: string;
  name: string;
  networkRef: string;
  labIp: string;
  capacityGi: number;
  osdCount: number;
  linkedClusters: string[];
  storageClassName?: string;
  [key: string]: unknown;
}

function CephClusterNodeComponent({ id, data, selected }: NodeProps) {
  const d = data as unknown as CephClusterNodeData;
  const deployedNodeData = useCanvasStore((s) => s.deployedNodeData);
  const isDirty = React.useMemo(() => {
    const deployed = deployedNodeData[id];
    if (!deployed) return false;
    return stableStringify(stableNodeData(d as Record<string, unknown>)) !== deployed;
  }, [id, d, deployedNodeData]);

  const replicateSize = Math.min(d.osdCount || 3, 3);

  return (
    <div
      className="storage-node-card"
      style={{
        borderColor: selected ? "var(--troshka-yellow)" : "rgba(96,165,250,0.35)",
        boxShadow: selected ? "0 0 0 3px rgba(96,165,250,0.2)" : "none",
        minWidth: 180,
      }}
    >
      <div className="storage-node-icon" style={{ position: "relative" }}>
        🐙
        {isDirty && (
          <span title="Unsaved changes" style={{ fontSize: 9, position: "absolute", top: 2, right: 2 }}>
            💾
          </span>
        )}
      </div>
      <div>
        <div className="storage-node-name">{cephClusterDisplayName(d)}</div>
        <div className="storage-node-size">{d.capacityGi} Gi · {d.osdCount} OSD</div>
        <div style={{ fontSize: 10, color: "var(--troshka-text-dim)", marginTop: 2 }}>
          {d.labIp || "auto .4"} · {replicateSize}x repl
        </div>
      </div>

      <Handle
        type="source"
        position={Position.Left}
        id="left"
        className="canvas-handle"
        style={CEPH_HANDLE_STYLE}
      />
      <Handle
        type="source"
        position={Position.Right}
        id="right"
        className="canvas-handle"
        style={CEPH_HANDLE_STYLE}
      />
    </div>
  );
}

export default memo(CephClusterNodeComponent);
