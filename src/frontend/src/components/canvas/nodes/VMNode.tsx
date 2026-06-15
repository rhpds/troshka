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
  const deployedNodeData = useCanvasStore((s) => s.deployedNodeData);
  const updateNodeData = useCanvasStore((s) => s.updateNodeData);
  const updateNodeInternals = useUpdateNodeInternals();
  const d = data as unknown as VMNodeData;
  const isRunning = d.status === "running";
  const isRedeploying = d.status === "redeploying";
  const isNotFound = d.status === "not_found";

  const nicCount = (d.nics || []).length;
  const dcCount = (d.diskControllers || []).length;
  useEffect(() => {
    // Two-pass update: first let DOM render new handles, then re-measure
    const t1 = setTimeout(() => updateNodeInternals(id), 0);
    const t2 = setTimeout(() => updateNodeInternals(id), 200);
    return () => { clearTimeout(t1); clearTimeout(t2); };
  }, [id, nicCount, dcCount, updateNodeInternals]);
  const deployedVmIds = useCanvasStore((s) => s.deployedVmIds);
  const isDeployed = (projectState === "active" || projectState === "stopped" || projectState === "starting") && deployedVmIds.has(id);
  const startOrder = useCanvasStore((s) => s.startOrder);
  const autoStart = (() => { const e = startOrder.find((o) => o.vmId === id); return e ? e.autoStart : true; })();

  const isDirty = React.useMemo(() => {
    const deployed = deployedNodeData[id];
    if (!deployed) return false;
    const { status, redeployStep, redeployDetail, liveBootDevs, ...stable } = d as Record<string, unknown>;
    return JSON.stringify(stable) !== deployed;
  }, [id, d, deployedNodeData]);

  const [actionPending, setActionPending] = useState<string | null>(null);
  const [nicsExpanded, setNicsExpanded] = useState(false);

  const pollVmStatus = async (): Promise<string> => {
    const resp = await fetch(`/api/v1/projects/${projectId}/vms/${id}/status`);
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
      const resp = await fetch(`/api/v1/projects/${projectId}/vms/${id}/${action}`, { method: "POST" });
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
          const start = Date.now();
          while (Date.now() - start < 120000) {
            await new Promise((r) => setTimeout(r, 3000));
            const state = await pollVmStatus();
            if (state === "running") {
              updateNodeData(id, { status: "running" });
              break;
            }
          }
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
    const resp = await fetch(`/api/v1/projects/${projectId}/vms/${id}/console`);
    const info = await resp.json();
    window.open(
      `/console?vm=${encodeURIComponent(id)}&project=${projectId}&name=${encodeURIComponent(d.name)}`,
      `console_${projectId?.replace(/-/g, "")}_${id.replace(/-/g, "")}`,
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
    return sn && (sn.data as Record<string, any>).format !== "iso";
  });
  const hasNetwork = edges.some(
    (e) =>
      (e.source === id || e.target === id) &&
      nodes.some((n) => n.id === (e.source === id ? e.target : e.source) && n.type === "networkNode")
  );

  const hasSharedDisk = connectedStorageIds.some((sid) => {
    const storageNode = nodes.find((n) => n.id === sid);
    if (!storageNode) return false;
    const isIso = (storageNode.data as Record<string, any>).format === "iso";
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
        opacity: projectState === "draft" ? 0.55 : 1,
        transition: "opacity 0.3s",
      }}
    >
      {/* Header */}
      <div className="vm-node-header">
        <div className="vm-node-icon">{String(d.icon || "🖥")}</div>
        <span className="vm-node-title">{d.name}</span>
        {isDirty && <span title="Unsaved changes — republish to apply" style={{ fontSize: 10, opacity: 0.6 }}>💾</span>}
        {(actionPending || d.status === "redeploying") ? (
          <span title={d.redeployStep as string || ""} className="vm-btn-spinner" style={{ width: 8, height: 8 }} />
        ) : (
        <span
          className="vm-node-status-dot"
          style={{
            background: isRunning
              ? "var(--troshka-green)"
              : d.status === "not_found"
                ? "#6b7280"
                : isDeployed
                  ? "var(--troshka-red)"
                  : "#6b7280",
            boxShadow: isRunning ? "0 0 6px var(--troshka-green)" : "none",
          }}
        />
        )}
      </div>

      {/* Redeploy progress */}
      {d.status === "redeploying" && d.redeployStep && (
        <div style={{ fontSize: 9, color: "#fbbf24", textAlign: "center", padding: "2px 0" }}>
          {d.redeployStep as string}{d.redeployDetail ? `: ${d.redeployDetail}` : ""}
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
          <span className="vm-node-spec-label">Boot{(d as unknown as Record<string, any>).liveBootDevs ? " (live)" : ""}</span>
          <span className="vm-node-spec-val" style={{ fontSize: 10 }}>{(() => {
            const liveDevs = (d as unknown as Record<string, any>).liveBootDevs as string[] | null;
            if (liveDevs && liveDevs.length > 0) {
              const labels: Record<string, string> = { hd: "disk", network: "PXE", cdrom: "CD", fd: "floppy" };
              return liveDevs.map((dev) => labels[dev] || dev).join(" → ");
            }
            const bootDevs = (d as unknown as Record<string, any>).bootDevices as string[] | undefined;
            if (!bootDevs || bootDevs.length === 0) return "None";
            for (const bd of bootDevs) {
              if (bd === "network") return <span style={{ display: "inline-flex", alignItems: "center", gap: 3 }}><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><rect x="4" y="2" width="16" height="16" rx="2" /><line x1="8" y1="18" x2="8" y2="22" /><line x1="12" y1="18" x2="12" y2="22" /><line x1="16" y1="18" x2="16" y2="22" /><rect x="6" y="5" width="12" height="6" rx="1" /><line x1="9" y1="5" x2="9" y2="11" /><line x1="12" y1="5" x2="12" y2="11" /><line x1="15" y1="5" x2="15" y2="11" /></svg>Network (PXE)</span>;
              if (bd === "hd") {
                const disk = connectedStorageIds
                  .map((sid) => nodes.find((n) => n.id === sid))
                  .find((n) => n && (n.data as Record<string, any>).format !== "iso");
                if (disk) return `🛢 ${(disk.data as Record<string, any>).name}`;
                return "hd";
              }
              const sn = nodes.find((n) => n.id === bd);
              if (sn) {
                const fmt = (sn.data as Record<string, any>).format as string;
                const name = (sn.data as Record<string, any>).name as string;
                return fmt === "iso" ? `💿 ${name}` : `🛢 ${name}`;
              }
            }
            // All boot device IDs were stale — find first connected disk
            const fallback = connectedStorageIds
              .map((sid) => nodes.find((n) => n.id === sid))
              .find((n) => n && (n.data as Record<string, any>).format !== "iso");
            return fallback ? `🛢 ${(fallback.data as Record<string, any>).name}` : "hd";
          })()}</span>
        </div>

        {/* NIC IPs */}
        {(() => {
          const nics = d.nics || [];
          if (nics.length === 0) return null;
          return (
            <div style={{ fontSize: 9, color: "var(--troshka-text-dim)", fontFamily: "monospace", lineHeight: 1.4, padding: "2px 0" }}>
              <div
                style={{ cursor: "pointer", userSelect: "none" }}
                onClick={(e) => { e.stopPropagation(); setNicsExpanded(!nicsExpanded); }}
              >
                {nicsExpanded ? "▾" : "▸"} {nics.length} NIC{nics.length !== 1 ? "s" : ""}
              </div>
              {nicsExpanded && nics.map((nic) => (
                <div key={nic.id}>{nic.name}: {nic.ip || "DHCP"}</div>
              ))}
            </div>
          );
        })()}
        {/* Warnings */}
        {(() => {
          const bmcMissingIp = d.bmcEnabled && !d.bmcIp;
          return (!hasStorage || !hasWritableDisk || !hasNetwork || hasSharedDisk || bmcMissingIp) ? (
            <div className="vm-node-warnings">
              {!hasStorage && (
                <span className="vm-node-warning" title="No storage attached">⚠ No disk</span>
              )}
              {hasStorage && !hasWritableDisk && (
                <span className="vm-node-warning" title="Only ISO attached — no writable disk to install onto">⚠ No installable disk device</span>
              )}
              {!hasNetwork && (
                <span className="vm-node-warning" title="No network connected">⚠ No network</span>
              )}
              {hasSharedDisk && (
                <span className="vm-node-warning" title="Disk shared with another VM — requires cluster-aware filesystem">⚠ Shared disk</span>
              )}
              {bmcMissingIp && (
                <span className="vm-node-warning" title="BMC enabled but no BMC IP assigned">⚠ No BMC IP</span>
              )}
            </div>
          ) : null;
        })()}
        {/* Auto-start */}
        <label className="nopan nodrag" style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 9, color: "var(--troshka-text-dim)", cursor: "pointer", padding: "2px 0" }} onClick={(e) => e.stopPropagation()}>
        <input
          type="checkbox"
          checked={autoStart}
          onChange={(e) => {
            e.stopPropagation();
            const store = useCanvasStore.getState();
            const order = [...store.startOrder];
            const idx = order.findIndex((o) => o.vmId === id);
            if (idx >= 0) {
              order[idx] = { ...order[idx], autoStart: e.target.checked };
            } else {
              order.push({ vmId: id, autoStart: e.target.checked, waitForVm: null, waitForService: "", waitForPort: "", delaySeconds: 0 });
            }
            store.setStartOrder(order);
          }}
          style={{ width: 12, height: 12 }}
        />
          Auto-start
        </label>
      </div>

      <div className="vm-node-footer nopan nodrag">
        {isDeployed && !isRunning && !isRedeploying && !isNotFound && (
          <button
            className="vm-node-action power-stopped"
            title="Start"
            onClick={(e) => { e.stopPropagation(); vmAction("start"); }}
            disabled={!!actionPending || isRedeploying}
          >
            {actionPending === "start" ? <span className="vm-btn-spinner" /> : "▶"}
          </button>
        )}
        {isDeployed && isRunning && !isRedeploying && !isNotFound && (
          <>
            <button
              className="vm-node-action power-running"
              title="Graceful Shutdown"
              onClick={(e) => { e.stopPropagation(); if (window.confirm(`Shut down "${d.name}"?`)) vmAction("stop"); }}
              disabled={!!actionPending || isRedeploying}
            >
              {actionPending === "stop" ? <span className="vm-btn-spinner" /> : "■"}
            </button>
            <button
              className="vm-node-action power-running"
              title="Force Power Off"
              onClick={(e) => { e.stopPropagation(); if (window.confirm(`Force power off "${d.name}"? This may cause data loss.`)) vmAction("forcestop"); }}
              disabled={!!actionPending || isRedeploying}
              style={{ color: "#ef4444" }}
            >
              {actionPending === "forcestop" ? <span className="vm-btn-spinner" /> : "⏻"}
            </button>
            <button className="vm-node-action restart" title="Restart" onClick={(e) => { e.stopPropagation(); if (window.confirm(`Restart "${d.name}"?`)) vmAction("restart"); }} disabled={!!actionPending || isRedeploying}>
              {actionPending === "restart" ? <span className="vm-btn-spinner" /> : "↻"}
            </button>
          </>
        )}
        <button className="vm-node-action duplicate" title="Duplicate" onClick={(e) => { e.stopPropagation(); duplicateNode(id); }}>
          ⧉
        </button>
        {isDeployed && !isNotFound && <button className="vm-node-action console" title="Console" onClick={(e) => { e.stopPropagation(); openConsole(); }}>
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
        </button>}
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
