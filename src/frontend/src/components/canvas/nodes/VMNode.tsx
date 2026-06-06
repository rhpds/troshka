"use client";

import React, { memo, useState, useEffect } from "react";
import { Handle, Position, useUpdateNodeInternals, type NodeProps } from "@xyflow/react";
import type { VMNodeData } from "@/stores/canvasStore";
import { useCanvasStore } from "@/stores/canvasStore";

function VMNodeComponent({ id, data, selected }: NodeProps) {
  const duplicateNode = useCanvasStore((s) => s.duplicateNode);
  const edges = useCanvasStore((s) => s.edges);
  const nodes = useCanvasStore((s) => s.nodes);
  const projectId = useCanvasStore((s) => s.currentProjectId);
  const projectState = useCanvasStore((s) => s.projectState);
  const updateNodeData = useCanvasStore((s) => s.updateNodeData);
  const updateNodeInternals = useUpdateNodeInternals();
  const d = data as unknown as VMNodeData;
  const isRunning = d.status === "running";
  const isRedeploying = d.status === "redeploying";

  const nicCount = (d.nics || []).length;
  const dcCount = (d.diskControllers || []).length;
  useEffect(() => {
    // Two-pass update: first let DOM render new handles, then re-measure
    const t1 = setTimeout(() => updateNodeInternals(id), 0);
    const t2 = setTimeout(() => updateNodeInternals(id), 200);
    return () => { clearTimeout(t1); clearTimeout(t2); };
  }, [id, nicCount, dcCount, updateNodeInternals]);
  const deployedVmIds = useCanvasStore((s) => s.deployedVmIds);
  const isDeployed = (projectState === "active" || projectState === "stopped") && deployedVmIds.has(id);

  const [actionPending, setActionPending] = useState<string | null>(null);

  const pollVmStatus = async (): Promise<string> => {
    const resp = await fetch(`/api/v1/projects/${projectId}/vms/${d.name}/status`);
    const data = await resp.json();
    return data.state || "";
  };

  const waitForShutdown = async (maxWaitMs: number): Promise<boolean> => {
    const start = Date.now();
    while (Date.now() - start < maxWaitMs) {
      await new Promise((r) => setTimeout(r, 2000));
      const state = await pollVmStatus();
      if (state === "shut off") return true;
    }
    return false;
  };

  const vmAction = async (action: "start" | "stop" | "forcestop" | "restart") => {
    if (!projectId || actionPending) return;
    setActionPending(action);
    try {
      const resp = await fetch(`/api/v1/projects/${projectId}/vms/${d.name}/${action}`, { method: "POST" });
      const result = await resp.json();
      if (action === "stop") {
        if (result.success) {
          const off = await waitForShutdown(10000);
          if (off) {
            updateNodeData(id, { status: "stopped" });
          } else {
            alert("Graceful shutdown sent but VM is still running. Use Force Power Off if needed.");
          }
        } else {
          alert(`Shutdown failed: ${result.output?.slice(-200) || "unknown error"}`);
        }
      } else if (action === "forcestop") {
        if (result.success || result.output?.includes("domain is not running")) {
          updateNodeData(id, { status: "stopped" });
        } else {
          alert(`Force stop failed: ${result.output?.slice(-200) || "unknown error"}`);
        }
      } else if (action === "start") {
        if (result.success || result.output?.includes("already active")) {
          updateNodeData(id, { status: "running" });
        } else {
          alert(`Start failed: ${result.output?.slice(-200) || "unknown error"}`);
        }
      } else if (action === "restart") {
        if (result.success) {
          const off = await waitForShutdown(10000);
          if (off) {
            updateNodeData(id, { status: "stopped" });
            // Wait for it to come back up
            const start = Date.now();
            while (Date.now() - start < 10000) {
              await new Promise((r) => setTimeout(r, 2000));
              const state = await pollVmStatus();
              if (state === "running") {
                updateNodeData(id, { status: "running" });
                break;
              }
            }
          } else {
            alert("Restart signal sent but VM did not shut down within 10 seconds. Use Force Power Off, then Start.");
          }
        } else {
          alert(`Restart failed: ${result.output?.slice(-200) || "unknown error"}`);
        }
      }
    } catch {
      alert("Failed to connect to server");
    }
    setActionPending(null);
  };

  const openConsole = async () => {
    if (!projectId) return;
    const resp = await fetch(`/api/v1/projects/${projectId}/vms/${d.name}/console`);
    const info = await resp.json();
    window.open(
      `/console?vm=${encodeURIComponent(d.name)}&project=${projectId}`,
      `console-${d.name}`,
      "width=1024,height=768,menubar=no,toolbar=no,location=no",
    );
  };
  const borderColor = isRunning
    ? "var(--troshka-green)"
    : "var(--troshka-red)";

  const connectedStorageIds = edges
    .filter((e) => e.source === id || e.target === id)
    .map((e) => e.source === id ? e.target : e.source)
    .filter((nid) => nodes.some((n) => n.id === nid && n.type === "storageNode"));

  const hasStorage = connectedStorageIds.length > 0;
  const hasWritableDisk = connectedStorageIds.some((sid) => {
    const sn = nodes.find((n) => n.id === sid);
    return sn && (sn.data as Record<string, unknown>).format !== "iso";
  });
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
        {(actionPending || d.status === "redeploying") ? (
          <span title={(d as Record<string, unknown>).redeployStep as string || ""} className="vm-btn-spinner" style={{ width: 8, height: 8 }} />
        ) : (
        <span
          className="vm-node-status-dot"
          style={{
            background: isRunning
              ? "var(--troshka-green)"
              : "var(--troshka-red)",
            boxShadow: isRunning ? "0 0 6px var(--troshka-green)" : "none",
          }}
        />
        )}
      </div>

      {/* Redeploy progress */}
      {d.status === "redeploying" && (d as Record<string, unknown>).redeployStep && (
        <div style={{ fontSize: 9, color: "#fbbf24", textAlign: "center", padding: "2px 0" }}>
          {(d as Record<string, unknown>).redeployStep as string}{(d as Record<string, unknown>).redeployDetail ? `: ${(d as Record<string, unknown>).redeployDetail}` : ""}
        </div>
      )}

      {/* Specs */}
      <div className="vm-node-body">
        <div className="vm-node-specs">
          <span className="vm-node-spec-label">vCPU</span>
          <span className="vm-node-spec-val">{d.vcpus}</span>
          <span className="vm-node-spec-label">RAM</span>
          <span className="vm-node-spec-val">{d.ram} GB</span>
          <span className="vm-node-spec-label">OS</span>
          <span className="vm-node-spec-val">{
            { rhel10: "RHEL 10", rhel9: "RHEL 9", rhel8: "RHEL 8", rhel7: "RHEL 7",
              "centos-stream10": "CentOS 10", "centos-stream9": "CentOS 9",
              almalinux9: "Alma 9", rocky9: "Rocky 9",
              fedora42: "Fedora 42", fedora41: "Fedora 41", fedora40: "Fedora 40",
              ubuntu2404: "Ubuntu 24.04", ubuntu2204: "Ubuntu 22.04",
              debian12: "Debian 12", win11: "Win 11", win10: "Win 10",
              win2022: "WinSrv 2022", win2019: "WinSrv 2019",
            }[d.os] || d.os
          }</span>
        </div>

        {/* Boot order badge */}
        {/* Warnings */}
        {(!hasStorage || !hasWritableDisk || !hasNetwork || hasSharedDisk) && (
          <div className="vm-node-warnings">
            {!hasStorage && (
              <span className="vm-node-warning" title="No storage attached">⚠ No disk</span>
            )}
            {hasStorage && !hasWritableDisk && (
              <span className="vm-node-warning" title="Only ISO attached — no writable disk to install onto">⚠ No install disk</span>
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
      <div className="vm-node-footer nopan nodrag">
        {isDeployed && !isRunning && (
          <button
            className="vm-node-action power-stopped"
            title="Start"
            onClick={(e) => { e.stopPropagation(); vmAction("start"); }}
            disabled={!!actionPending || isRedeploying}
          >
            {actionPending === "start" ? <span className="vm-btn-spinner" /> : "▶"}
          </button>
        )}
        {isDeployed && isRunning && (
          <>
            <button
              className="vm-node-action power-running"
              title="Graceful Shutdown"
              onClick={(e) => { e.stopPropagation(); vmAction("stop"); }}
              disabled={!!actionPending || isRedeploying}
            >
              {actionPending === "stop" ? <span className="vm-btn-spinner" /> : "■"}
            </button>
            <button
              className="vm-node-action power-running"
              title="Force Power Off"
              onClick={(e) => { e.stopPropagation(); vmAction("forcestop"); }}
              disabled={!!actionPending || isRedeploying}
              style={{ color: "#ef4444" }}
            >
              {actionPending === "forcestop" ? <span className="vm-btn-spinner" /> : "⏻"}
            </button>
            <button className="vm-node-action restart" title="Restart" onClick={(e) => { e.stopPropagation(); vmAction("restart"); }} disabled={!!actionPending || isRedeploying}>
              {actionPending === "restart" ? <span className="vm-btn-spinner" /> : "↻"}
            </button>
          </>
        )}
        <button className="vm-node-action duplicate" title="Duplicate" onClick={(e) => { e.stopPropagation(); duplicateNode(id); }}>
          ⧉
        </button>
        <button className="vm-node-action console" title="Console" onClick={(e) => { e.stopPropagation(); if (isDeployed) openConsole(); }} disabled={!isDeployed}>
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
