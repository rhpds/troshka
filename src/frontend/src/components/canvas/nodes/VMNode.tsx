"use client";

import React, { memo } from "react";
import { Handle, Position, type NodeProps } from "@xyflow/react";
import type { VMNodeData } from "@/stores/canvasStore";
import { useCanvasStore } from "@/stores/canvasStore";

function VMNodeComponent({ id, data, selected }: NodeProps) {
  const duplicateNode = useCanvasStore((s) => s.duplicateNode);
  const edges = useCanvasStore((s) => s.edges);
  const nodes = useCanvasStore((s) => s.nodes);
  const d = data as unknown as VMNodeData;
  const isRunning = d.status === "running";
  const borderColor = isRunning
    ? "var(--troshka-green)"
    : "var(--troshka-red)";

  const connectedStorageIds = edges
    .filter((e) => e.source === id || e.target === id)
    .map((e) => e.source === id ? e.target : e.source)
    .filter((nid) => nodes.some((n) => n.id === nid && n.type === "storageNode"));

  const hasStorage = connectedStorageIds.length > 0;
  const hasNetwork = edges.some(
    (e) =>
      (e.source === id || e.target === id) &&
      nodes.some((n) => n.id === (e.source === id ? e.target : e.source) && n.type === "networkNode")
  );

  const hasSharedDisk = connectedStorageIds.some((sid) => {
    const storageNode = nodes.find((n) => n.id === sid);
    if (!storageNode) return false;
    const isIso = (storageNode.data as Record<string, unknown>).format === "iso";
    if (isIso) return false;
    return edges.filter((e) =>
      (e.source === sid || e.target === sid) &&
      (e.source !== id && e.target !== id)
    ).some((e) =>
      nodes.some((n) => n.id === (e.source === sid ? e.target : e.source) && n.type === "vmNode")
    );
  });

  return (
    <div
      className="vm-node-card"
      style={{
        borderColor: selected ? "var(--troshka-accent)" : borderColor,
        boxShadow: selected
          ? "0 0 0 3px var(--troshka-accent-glow)"
          : "0 2px 8px rgba(0,0,0,0.2)",
      }}
    >
      {/* Header */}
      <div className="vm-node-header">
        <div className="vm-node-icon">{d.icon || "🖥"}</div>
        <span className="vm-node-title">{d.name}</span>
        <span
          className="vm-node-status-dot"
          style={{
            background: isRunning
              ? "var(--troshka-green)"
              : "var(--troshka-red)",
            boxShadow: isRunning ? "0 0 6px var(--troshka-green)" : "none",
          }}
        />
      </div>

      {/* Specs */}
      <div className="vm-node-body">
        <div className="vm-node-specs">
          <span className="vm-node-spec-label">vCPU</span>
          <span className="vm-node-spec-val">{d.vcpus}</span>
          <span className="vm-node-spec-label">RAM</span>
          <span className="vm-node-spec-val">{d.ram} GB</span>
          <span className="vm-node-spec-label">OS</span>
          <span className="vm-node-spec-val">{d.os}</span>
        </div>

        {/* Boot order badge */}
        {/* Warnings */}
        {(!hasStorage || !hasNetwork || hasSharedDisk) && (
          <div className="vm-node-warnings">
            {!hasStorage && (
              <span className="vm-node-warning" title="No storage attached">⚠ No disk</span>
            )}
            {!hasNetwork && (
              <span className="vm-node-warning" title="No network connected">⚠ No network</span>
            )}
            {hasSharedDisk && (
              <span className="vm-node-warning" title="Disk shared with another VM — requires cluster-aware filesystem">⚠ Shared disk</span>
            )}
          </div>
        )}
      </div>

      {/* Action buttons */}
      <div className="vm-node-footer">
        <button
          className={`vm-node-action ${isRunning ? "power-running" : "power-stopped"}`}
          title={isRunning ? "Stop" : "Start"}
        >
          {isRunning ? "■" : "▶"}
        </button>
        <button className="vm-node-action restart" title="Restart">
          ↻
        </button>
        <button className="vm-node-action duplicate" title="Duplicate" onClick={(e) => { e.stopPropagation(); duplicateNode(id); }}>
          ⧉
        </button>
        <button className="vm-node-action console" title="Console">
          <svg
            width="14"
            height="14"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2.5"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <rect x="2" y="3" width="20" height="14" rx="2" />
            <line x1="8" y1="21" x2="16" y2="21" />
            <line x1="12" y1="17" x2="12" y2="21" />
          </svg>
        </button>
      </div>

      {/* Network handles — one pair (top+bottom) per NIC */}
      {(d.nics || [{ id: "default" }]).map((nic, i, arr) => {
        const pct = arr.length === 1 ? 50 : 20 + (i * 60) / Math.max(arr.length - 1, 1);
        return (
          <React.Fragment key={nic.id}>
            <Handle
              type="source"
              position={Position.Top}
              id={`nic-${nic.id}-top`}
              className="canvas-handle canvas-handle-network"
              style={{ left: `${pct}%` }}
            />
            <Handle
              type="source"
              position={Position.Bottom}
              id={`nic-${nic.id}-bottom`}
              className="canvas-handle canvas-handle-network"
              style={{ left: `${pct}%` }}
            />
          </React.Fragment>
        );
      })}
      {/* Storage handles — one pair (left+right) per disk port */}
      {(d.diskControllers || [{ id: "default" }]).map((port, i, arr) => {
        const pct = arr.length === 1 ? 50 : 20 + (i * 60) / Math.max(arr.length - 1, 1);
        return (
          <React.Fragment key={port.id}>
            <Handle
              type="source"
              position={Position.Left}
              id={`dp-${port.id}-left`}
              className="canvas-handle canvas-handle-storage"
              style={{ top: `${pct}%` }}
            />
            <Handle
              type="source"
              position={Position.Right}
              id={`dp-${port.id}-right`}
              className="canvas-handle canvas-handle-storage"
              style={{ top: `${pct}%` }}
            />
          </React.Fragment>
        );
      })}
    </div>
  );
}

export default memo(VMNodeComponent);
