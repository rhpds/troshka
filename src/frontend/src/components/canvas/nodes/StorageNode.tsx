"use client";

import React, { memo } from "react";
import { Handle, Position, type NodeProps } from "@xyflow/react";
import { useCanvasStore, stableNodeData, stableStringify, type StorageNodeData } from "@/stores/canvasStore";
import { useCanvasDisplay } from "@/components/canvas/CanvasDisplayContext";

function storageSourceLabel(source: unknown, isIso: boolean): string {
  if (source === "library" || source === "pattern" || source === "snapshot") return ` · ${source}`;
  return isIso ? "" : " · blank";
}

function StorageNodeComponent({ id, data, selected }: NodeProps) {
  const d = data as unknown as StorageNodeData;
  const projectState = useCanvasStore((s) => s.projectState);
  const { isPreview } = useCanvasDisplay();
  const deployedNodeData = useCanvasStore((s) => s.deployedNodeData);
  const isDirty = React.useMemo(() => {
    const deployed = deployedNodeData[id];
    if (!deployed) return false;
    // Match the deployed baseline's normalization to avoid false 💾.
    return stableStringify(stableNodeData(d as Record<string, unknown>)) !== deployed;
  }, [id, d, deployedNodeData]);

  const isIso = d.format === "iso";
  const formatLabel = ["iso", "raw"].includes(d.format) ? d.format : "qcow2";
  const libraryItemName = typeof d.libraryItemName === "string" ? d.libraryItemName : "";
  const showLibraryName = d.source === "library" && libraryItemName && !isIso;

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
        opacity: projectState === "draft" && !isPreview ? 0.55 : 1,
        transition: "opacity 0.3s",
      }}
    >
      <div className="storage-node-icon">{d.format === "iso" ? "💿" : "🛢"}{isDirty && <span title="Unsaved changes" style={{ fontSize: 9, position: "absolute", top: 2, right: 2 }}>💾</span>}</div>
      <div>
        <div className="storage-node-name">{d.name}</div>
        <div className="storage-node-size">
          {isIso ? libraryItemName : `${d.size} GB`}
        </div>
        {showLibraryName && (
            <div style={{ fontSize: 10, color: "var(--troshka-green)", marginTop: 1 }}>
              {libraryItemName}
            </div>
          )}
        <div style={{ fontSize: 10, color: "var(--troshka-text-dim)", marginTop: 1 }}>
          {d.format === "iso" ? "ISO" : formatLabel.toUpperCase()}
          {storageSourceLabel(d.source, isIso)}
        </div>
        {isIso && !libraryItemName && (
          <div style={{ fontSize: 10, color: "var(--troshka-yellow)", marginTop: 2 }}>
            ⚠ Select ISO from library
          </div>
        )}
      </div>

      <Handle type="source" position={Position.Right} id="right" className="canvas-handle canvas-handle-storage" />
    </div>
  );
}

export default memo(StorageNodeComponent);
