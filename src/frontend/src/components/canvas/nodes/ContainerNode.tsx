"use client";

import React, { memo, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { Handle, Position, useUpdateNodeInternals } from "@xyflow/react";
import type { NodeProps } from "@xyflow/react";
import { useCanvasStore } from "@/stores/canvasStore";
import type { ContainerNodeData } from "@/stores/canvasStore";

function ContainerNodeComponent({ id, data, selected }: NodeProps) {
  const projectState = useCanvasStore((s) => s.projectState);
  const deployedVmIds = useCanvasStore((s) => s.deployedVmIds);
  const updateNodeData = useCanvasStore((s) => s.updateNodeData);
  const updateNodeInternals = useUpdateNodeInternals();
  const d = data as unknown as ContainerNodeData;
  const isRunning = d.status === "running";
  const isDeployed = (projectState === "active" || projectState === "stopped") && deployedVmIds.has(id);
  const projectId = useCanvasStore((s) => s.currentProjectId);
  const [actionPending, setActionPending] = useState<string | null>(null);
  const [logModalOpen, setLogModalOpen] = useState(false);
  const [logContent, setLogContent] = useState("");

  const nicCount = (d.nics || []).length;
  const mountCount = (d.mounts || []).length;
  useEffect(() => {
    const t1 = setTimeout(() => updateNodeInternals(id), 0);
    const t2 = setTimeout(() => updateNodeInternals(id), 200);
    return () => {
      clearTimeout(t1);
      clearTimeout(t2);
    };
  }, [id, nicCount, mountCount, updateNodeInternals]);

  const displayStatus = d.status || "stopped";

  const statusColor =
    displayStatus === "running"
      ? "var(--troshka-green)"
      : displayStatus === "created"
        ? "var(--troshka-yellow)"
        : "var(--troshka-text-dim)";

  const imageName = d.image
    ? d.image.split("/").pop()?.split(":")[0] || d.image
    : "no image";

  return (
    <div
      className={`canvas-node canvas-node-container ${selected ? "canvas-node-selected" : ""}`}
      style={{
        borderColor: selected
          ? "var(--troshka-blue)"
          : "rgba(56, 189, 248, 0.3)",
      }}
    >
      <div className="canvas-node-header">
        <span className="canvas-node-icon">📦</span>
        <span className="canvas-node-label">{d.name || "container"}</span>
        <span
          className="canvas-node-status-dot"
          style={{ background: statusColor }}
          title={displayStatus}
        />
      </div>

      <div className="canvas-node-body">
        <div
          className="canvas-node-detail"
          style={{ fontFamily: "monospace", fontSize: 10 }}
          title={d.image}
        >
          {imageName}
        </div>
        <div className="canvas-node-detail">
          {d.cpus} CPU · {d.memory >= 1024 ? `${d.memory / 1024}G` : `${d.memory}M`} RAM
        </div>
        {(() => {
          const liveIps = (d as Record<string, unknown>).liveIps as string[] | undefined;
          const staticIps = (d.nics || []).map((nic) => nic.ip).filter(Boolean);
          const ips = liveIps && liveIps.length > 0 ? liveIps : staticIps;
          const portList = (d.ports || []).map((p) => p.containerPort).filter(Boolean);
          if (!ips.length && !portList.length) return null;
          return (
            <div className="canvas-node-detail" style={{ fontFamily: "monospace", fontSize: 10, color: "var(--troshka-green)" }}>
              {ips.length > 0 && ips.join(", ")}
              {ips.length > 0 && portList.length > 0 && ":"}
              {portList.length > 0 && portList.join(",")}
            </div>
          );
        })()}
      </div>

      {isDeployed && (
        <div className="vm-node-footer nopan nodrag">
          {!isRunning && (
            <button
              className="vm-node-action power-stopped"
              title="Start"
              onClick={async (e) => {
                e.stopPropagation();
                if (!projectId || actionPending) return;
                setActionPending("start");
                try {
                  await fetch(`/api/v1/projects/${projectId}/containers/${id}/start`, { method: "POST" });
                  updateNodeData(id, { status: "running" });
                } catch { /* */ }
                setActionPending(null);
              }}
              disabled={!!actionPending}
            >
              {actionPending === "start" ? <span className="vm-btn-spinner" /> : "▶"}
            </button>
          )}
          {isRunning && (
            <>
              <button
                className="vm-node-action power-running"
                title="Stop"
                onClick={async (e) => {
                  e.stopPropagation();
                  if (!projectId || actionPending) return;
                  setActionPending("stop");
                  try {
                    await fetch(`/api/v1/projects/${projectId}/containers/${id}/stop`, { method: "POST" });
                    updateNodeData(id, { status: "stopped" });
                  } catch { /* */ }
                  setActionPending(null);
                }}
                disabled={!!actionPending}
              >
                {actionPending === "stop" ? <span className="vm-btn-spinner" /> : "■"}
              </button>
              <button
                className="vm-node-action restart"
                title="Restart"
                onClick={async (e) => {
                  e.stopPropagation();
                  if (!projectId || actionPending) return;
                  setActionPending("restart");
                  try {
                    await fetch(`/api/v1/projects/${projectId}/containers/${id}/restart`, { method: "POST" });
                  } catch { /* */ }
                  setActionPending(null);
                }}
                disabled={!!actionPending}
              >
                {actionPending === "restart" ? <span className="vm-btn-spinner" /> : "↻"}
              </button>
            </>
          )}
          <button
            className="vm-node-action console"
            title="View Logs"
            onClick={async (e) => {
              e.stopPropagation();
              if (!projectId) return;
              try {
                const resp = await fetch(`/api/v1/projects/${projectId}/containers/${id}/logs?tail=500`);
                if (resp.ok) {
                  const logData = await resp.json();
                  setLogContent(logData.logs || "(no logs)");
                  setLogModalOpen(true);
                }
              } catch { /* */ }
            }}
          >
            📜
          </button>
        </div>
      )}

      {logModalOpen && createPortal(
        <div
          className="nopan nodrag"
          style={{
            position: "fixed",
            top: 0,
            left: 0,
            right: 0,
            bottom: 0,
            background: "rgba(0,0,0,0.6)",
            zIndex: 9999,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
          }}
          onClick={() => setLogModalOpen(false)}
        >
          <div
            style={{
              background: "var(--troshka-surface)",
              borderRadius: 12,
              width: "min(800px, 90vw)",
              maxHeight: "80vh",
              display: "flex",
              flexDirection: "column",
              overflow: "hidden",
              border: "1px solid var(--troshka-border)",
            }}
            onClick={(e) => e.stopPropagation()}
          >
            <div
              style={{
                padding: "12px 16px",
                borderBottom: "1px solid var(--troshka-border)",
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                background: "var(--troshka-surface)",
                borderRadius: "12px 12px 0 0",
              }}
            >
              <div>
                <div style={{ fontSize: 14, fontWeight: 600 }}>📜 Container Logs</div>
                <div style={{ fontSize: 11, color: "var(--troshka-text-dim)", marginTop: 2 }}>
                  {d.name}
                </div>
              </div>
              <div style={{ display: "flex", gap: 8 }}>
                <button
                  style={{
                    background: "var(--troshka-surface2)",
                    border: "1px solid var(--troshka-border)",
                    borderRadius: 6,
                    padding: "4px 10px",
                    color: "var(--troshka-text)",
                    cursor: "pointer",
                    fontSize: 12,
                  }}
                  onClick={() => {
                    navigator.clipboard.writeText(logContent);
                  }}
                >
                  Copy All
                </button>
                <button
                  style={{
                    background: "var(--troshka-surface2)",
                    border: "1px solid var(--troshka-border)",
                    borderRadius: 6,
                    padding: "4px 10px",
                    color: "var(--troshka-text)",
                    cursor: "pointer",
                    fontSize: 12,
                  }}
                  onClick={async () => {
                    if (!projectId) return;
                    try {
                      const resp = await fetch(`/api/v1/projects/${projectId}/containers/${id}/logs?tail=500`);
                      if (resp.ok) {
                        const logData = await resp.json();
                        setLogContent(logData.logs || "(no logs)");
                      }
                    } catch { /* */ }
                  }}
                >
                  ↻ Refresh
                </button>
                <button
                  style={{
                    background: "none",
                    border: "none",
                    color: "var(--troshka-text-dim)",
                    cursor: "pointer",
                    fontSize: 18,
                    padding: "0 4px",
                  }}
                  onClick={() => setLogModalOpen(false)}
                >
                  ✕
                </button>
              </div>
            </div>
            <pre
              style={{
                margin: 0,
                padding: 16,
                overflow: "auto",
                flex: 1,
                fontFamily: "monospace",
                fontSize: 12,
                lineHeight: 1.5,
                whiteSpace: "pre-wrap",
                wordBreak: "break-all",
                color: "var(--troshka-text)",
                background: "var(--troshka-surface2)",
                userSelect: "text",
                cursor: "text",
                maxHeight: "60vh",
              }}
            >
              {logContent}
            </pre>
          </div>
        </div>,
        document.body,
      )}

      {/* Network handles — top/bottom, same pattern as VM */}
      {(d.nics || [{ id: "default" }]).map((nic, i, arr) => {
        const pct =
          arr.length === 1
            ? 50
            : 20 + (i * 60) / Math.max(arr.length - 1, 1);
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

      {/* Mount handles — left/right, same pattern as VM disk controllers */}
      {(d.mounts && d.mounts.length > 0
        ? d.mounts
        : [{ diskNodeId: "default", mountPath: "" }]
      ).map((mount, i, arr) => {
        const pct =
          arr.length === 1
            ? 50
            : 20 + (i * 60) / Math.max(arr.length - 1, 1);
        const handleId = mount.diskNodeId || `mount-${i}`;
        return (
          <React.Fragment key={handleId}>
            <Handle
              type="source"
              position={Position.Left}
              id={`mnt-${handleId}-left`}
              className="canvas-handle canvas-handle-storage"
              style={{ top: `${pct}%` }}
            />
            <Handle
              type="source"
              position={Position.Right}
              id={`mnt-${handleId}-right`}
              className="canvas-handle canvas-handle-storage"
              style={{ top: `${pct}%` }}
            />
          </React.Fragment>
        );
      })}
    </div>
  );
}

export const ContainerNode = memo(ContainerNodeComponent);
