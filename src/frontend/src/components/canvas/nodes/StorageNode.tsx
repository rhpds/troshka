"use client";

import React, { memo } from "react";
import { Handle, Position, type NodeProps } from "@xyflow/react";
import { useCanvasStore, type StorageNodeData } from "@/stores/canvasStore";

function StorageNodeComponent({ id, data, selected }: NodeProps) {
  const d = data as unknown as StorageNodeData;
  const projectState = useCanvasStore((s) => s.projectState);
  const deployedNodeData = useCanvasStore((s) => s.deployedNodeData);
  const isDirty = React.useMemo(() => {
    const deployed = deployedNodeData[id];
    if (!deployed) return false;
    const transient = ["resolvedS3Path", "presignedUrl"];
    const clean = (obj: Record<string, unknown>) => {
      const copy = { ...obj };
      for (const k of transient) delete copy[k];
      return JSON.stringify(copy);
    };
    return clean(d as Record<string, unknown>) !== clean(JSON.parse(deployed));
  }, [id, d, deployedNodeData]);

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
        opacity: projectState === "draft" ? 0.55 : 1,
        transition: "opacity 0.3s",
      }}
    >
      <div className="storage-node-icon">{d.format === "iso" ? "💿" : "🛢"}{isDirty && <span title="Unsaved changes" style={{ fontSize: 9, position: "absolute", top: 2, right: 2 }}>💾</span>}</div>
      <div>
        <div className="storage-node-name">{d.name}</div>
        <div className="storage-node-size">
          {d.format === "iso"
            ? (d as unknown as Record<string, any>).libraryItemName as string || ""
            : `${d.size} GB`
          }
        </div>
        {(() => {
          const extra = d as unknown as Record<string, any>;
          return extra.source === "library" && extra.libraryItemName && d.format !== "iso" ? (
            <div style={{ fontSize: 10, color: "var(--troshka-green)", marginTop: 1 }}>
              {String(extra.libraryItemName)}
            </div>
          ) : null;
        })()}
        <div style={{ fontSize: 10, color: "var(--troshka-text-dim)", marginTop: 1 }}>
          {d.format === "iso" ? "ISO" : formatLabel.toUpperCase()}
          {(d as unknown as Record<string, any>).source === "library" ? " · library" : (d as unknown as Record<string, any>).source === "pattern" ? " · pattern" : (d as unknown as Record<string, any>).source === "snapshot" ? " · snapshot" : d.format !== "iso" ? " · blank" : ""}
        </div>
        {d.format === "iso" && !(d as unknown as Record<string, any>).libraryItemName && (
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
