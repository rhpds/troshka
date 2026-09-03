"use client";

import React, { memo } from "react";
import { Handle, NodeResizer, Position, type NodeProps } from "@xyflow/react";
import type { ClusterNodeData } from "@/stores/canvasStore";
import { useCanvasStore } from "@/stores/canvasStore";
import { clusterPrereqIssues } from "../clusterMaterialize";

function ClusterNodeComponent({ id, data, selected }: NodeProps) {
  const d = data as unknown as ClusterNodeData;

  // Surface cluster prerequisite issues (DNS network, gateway outbound) on the
  // box itself so problems are visible without opening the properties panel.
  const clusters = useCanvasStore((s) => s.clusters);
  const nodes = useCanvasStore((s) => s.nodes);
  const projectState = useCanvasStore((s) => s.projectState);
  const openClusterLog = useCanvasStore((s) => s.openClusterLog);
  const ocpHealth = useCanvasStore((s) => s.ocpHealth);
  const clusterId = ((data as Record<string, unknown>).clusterId as string) || id.replace(/^cluster-/, "");
  const cluster = clusters.find((c) => c.id === clusterId);
  // The install log/status is available once the cluster is being (or has been)
  // built. clusterKey mirrors the backend _cluster_key (id, falling back to name).
  const clusterKey = clusterId || d.name;
  const showInstallLog = projectState === "active" || projectState === "stopped";
  // Status button color reflects the cluster outcome: green complete, red
  // failed, else the "installing" cyan.
  const statusColor =
    ocpHealth?.phase === "ready"
      ? { bg: "rgba(34,197,94,0.18)", border: "rgba(34,197,94,0.55)" }
      : ocpHealth?.phase === "error" || ocpHealth?.phase === "timeout"
        ? { bg: "rgba(239,68,68,0.18)", border: "rgba(239,68,68,0.55)" }
        : { bg: "rgba(34,211,238,0.18)", border: "rgba(34,211,238,0.4)" };
  const issues = cluster ? clusterPrereqIssues(cluster, nodes) : [];
  const hasError = issues.some((i) => i.level === "error");
  const issueColor = hasError
    ? "var(--troshka-red, #ef4444)"
    : "var(--troshka-yellow, #f59e0b)";

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
          selected
            ? "var(--troshka-accent)"
            : hasError
              ? "var(--troshka-red, #ef4444)"
              : "var(--troshka-border, #4b5563)"
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
        // Hide the white corner squares (the box auto-fits its contents); the
        // invisible handles remain draggable for manual resize.
        handleStyle={{ opacity: 0, width: 10, height: 10 }}
        lineStyle={{ opacity: 0 }}
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
        <span>{d.baseDomain ? `${d.name}.${d.baseDomain}` : d.name}</span>
        {issues.length > 0 && (
          <span
            title={issues.map((i) => `${i.level === "error" ? "⛔" : "⚠"} ${i.message}`).join("\n")}
            style={{ marginLeft: "auto", fontSize: 13, color: issueColor, lineHeight: 1 }}
          >
            {hasError ? "⛔" : "⚠"}
          </span>
        )}
        <span
          style={{
            marginLeft: issues.length > 0 ? 6 : "auto",
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
        {showInstallLog && (
          <button
            type="button"
            title="View this cluster's install status & log"
            className="nodrag"
            onClick={(e) => {
              e.stopPropagation();
              openClusterLog(clusterKey, d.name);
            }}
            style={{
              marginLeft: 6,
              fontSize: 10,
              fontWeight: 500,
              padding: "1px 6px",
              borderRadius: 6,
              cursor: "pointer",
              background: statusColor.bg,
              border: `1px solid ${statusColor.border}`,
              color: "var(--troshka-text, #e5e7eb)",
              whiteSpace: "nowrap",
            }}
          >
            📋 Status
          </button>
        )}
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
