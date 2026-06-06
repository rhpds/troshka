"use client";

import React, { memo } from "react";
import { Handle, Position, type NodeProps } from "@xyflow/react";
import type { StorageNodeData } from "@/stores/canvasStore";

function StorageNodeComponent({ data, selected }: NodeProps) {
  const d = data as unknown as StorageNodeData;

  const formatLabel =
    d.format === "iso" ? "iso" : d.format === "raw" ? "raw" : "qcow2";

  return (
    <div
      className="storage-node-card"
      style={{
        borderColor: selected
          ? "var(--troshka-yellow)"
          : "rgba(251,191,36,0.3)",
        boxShadow: selected
          ? "0 0 0 3px rgba(251,191,36,0.2)"
          : "none",
      }}
    >
      <div className="storage-node-icon">{d.format === "iso" ? "💿" : "🛢"}</div>
      <div>
        <div className="storage-node-name">{d.name}</div>
        <div className="storage-node-size">
          {d.format === "iso"
            ? (d as unknown as Record<string, unknown>).libraryItemName as string || ""
            : `${d.size} GB`
          }
        </div>
        {(d as unknown as Record<string, unknown>).source === "library" && (d as unknown as Record<string, unknown>).libraryItemName && d.format !== "iso" && (
          <div style={{ fontSize: 10, color: "var(--troshka-green)", marginTop: 1 }}>
            {(d as unknown as Record<string, unknown>).libraryItemName as string}
          </div>
        )}
        <div style={{ fontSize: 10, color: "var(--troshka-text-dim)", marginTop: 1 }}>
          {d.format === "iso" ? "ISO" : formatLabel.toUpperCase()}
          {(d as unknown as Record<string, unknown>).source === "library" ? " · library" : d.format !== "iso" ? " · blank" : ""}
        </div>
        {d.format === "iso" && !(d as unknown as Record<string, unknown>).libraryItemName && (
          <div style={{ fontSize: 10, color: "var(--troshka-yellow)", marginTop: 2 }}>
            ⚠ Select ISO from library
          </div>
        )}
      </div>

      <Handle type="source" position={Position.Left} id="left" className="canvas-handle canvas-handle-storage" />
      <Handle type="source" position={Position.Right} id="right" className="canvas-handle canvas-handle-storage" />
    </div>
  );
}

export default memo(StorageNodeComponent);
