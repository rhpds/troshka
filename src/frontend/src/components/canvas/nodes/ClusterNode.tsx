"use client";

import React, { memo } from "react";
import { Handle, NodeResizer, Position, type NodeProps } from "@xyflow/react";
import type { ClusterNodeData } from "@/stores/canvasStore";

function ClusterNodeComponent({ data, selected }: NodeProps) {
  const d = data as unknown as ClusterNodeData;

  return (
    <div
      style={{
        width: "100%",
        height: "100%",
        minWidth: 280,
        minHeight: 180,
        boxSizing: "border-box",
        borderRadius: 10,
        border: `2px ${selected ? "solid" : "dashed"} ${
          selected ? "var(--troshka-accent)" : "var(--troshka-border, #4b5563)"
        }`,
        boxShadow: selected
          ? "0 0 0 3px var(--troshka-accent-glow)"
          : "none",
        // Translucent boundary — must not capture pointer events over children.
        background: "rgba(59,130,246,0.06)",
        pointerEvents: "none",
        // Sit behind member VM nodes so they stay interactive.
        zIndex: 0,
      }}
    >
      {/* Resizer — draggable handles only visible when selected */}
      <NodeResizer
        isVisible={selected}
        minWidth={d.minWidth ?? 280}
        minHeight={d.minHeight ?? 180}
      />

      {/* Header label — the drag handle for the boundary itself. No `nodrag`:
          the body is pointerEvents:none (members stay interactive), so the
          header is the ONLY grabbable area and must initiate the node drag. */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          padding: "6px 10px",
          fontSize: 12,
          fontWeight: 600,
          color: "var(--troshka-text, #e5e7eb)",
          background: "rgba(59,130,246,0.12)",
          borderTopLeftRadius: 8,
          borderTopRightRadius: 8,
          // Header is interactive (selectable/draggable) even though body is not.
          pointerEvents: "all",
        }}
      >
        <span style={{ fontSize: 13 }}>☸</span>
        <span>{d.name}</span>
        <span
          style={{
            marginLeft: "auto",
            fontSize: 10,
            fontWeight: 500,
            padding: "1px 6px",
            borderRadius: 6,
            background: "rgba(59,130,246,0.25)",
            color: "var(--troshka-text-dim, #cbd5e1)",
            whiteSpace: "nowrap",
          }}
        >
          {d.type} · {d.controlPlane}cp/{d.workers}wrk
        </span>
      </div>

      {/* Network anchor handles (top + bottom, like VMs) — target handles for network connections */}
      <Handle
        type="target"
        position={Position.Top}
        id="cluster-net-top"
        className="canvas-handle canvas-handle-network"
        style={{
          pointerEvents: "all",
          background: "rgba(34, 211, 238, 0.7)",
          border: "1px solid rgba(34, 211, 238, 0.9)",
          width: 10,
          height: 10,
          borderRadius: "50%",
        }}
      />
      <Handle
        type="target"
        position={Position.Bottom}
        id="cluster-net-bottom"
        className="canvas-handle canvas-handle-network"
        style={{
          pointerEvents: "all",
          background: "rgba(34, 211, 238, 0.7)",
          border: "1px solid rgba(34, 211, 238, 0.9)",
          width: 10,
          height: 10,
          borderRadius: "50%",
        }}
      />
    </div>
  );
}

export default memo(ClusterNodeComponent);
