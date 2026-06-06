"use client";

import React, { useEffect, useRef } from "react";
import { useCanvasStore } from "@/stores/canvasStore";

interface NodeContextMenuProps {
  nodeId: string;
  x: number;
  y: number;
  onClose: () => void;
}

export default function NodeContextMenu({ nodeId, x, y, onClose }: NodeContextMenuProps) {
  const duplicateNode = useCanvasStore((s) => s.duplicateNode);
  const deleteNode = useCanvasStore((s) => s.deleteNode);
  const hideNode = useCanvasStore((s) => s.hideNode);
  const nodes = useCanvasStore((s) => s.nodes);
  const deployedVmIds = useCanvasStore((s) => s.deployedVmIds);
  const projectId = useCanvasStore((s) => s.currentProjectId);
  const ref = useRef<HTMLDivElement>(null);

  const node = nodes.find((n) => n.id === nodeId);
  const isVm = node?.type === "vmNode";
  const isDeployed = isVm && deployedVmIds.has(nodeId);
  const vmName = isVm ? (node?.data as Record<string, unknown>).name as string : "";
  const isRunning = isVm && (node?.data as Record<string, unknown>).status === "running";
  const isRedeploying = isVm && (node?.data as Record<string, unknown>).status === "redeploying";

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as HTMLElement)) onClose();
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [onClose]);

  return (
    <div
      ref={ref}
      className="node-context-menu"
      style={{ position: "fixed", left: x, top: y, zIndex: 9999 }}
    >
      {isDeployed && isRunning && !isRedeploying && (
        <>
          <button onClick={async () => { await fetch(`/api/v1/projects/${projectId}/vms/${vmName}/stop`, { method: "POST" }); onClose(); }}>
            ■ Graceful Shutdown
          </button>
          <button onClick={async () => { await fetch(`/api/v1/projects/${projectId}/vms/${vmName}/forcestop`, { method: "POST" }); onClose(); }}>
            ⏻ Force Power Off
          </button>
          <button onClick={async () => { await fetch(`/api/v1/projects/${projectId}/vms/${vmName}/restart`, { method: "POST" }); onClose(); }}>
            ↻ Restart
          </button>
        </>
      )}
      {isDeployed && !isRunning && !isRedeploying && (
        <button onClick={async () => { await fetch(`/api/v1/projects/${projectId}/vms/${vmName}/start`, { method: "POST" }); onClose(); }}>
          ▶ Start
        </button>
      )}
      {isDeployed && (
        <button onClick={() => {
          window.open(`/console?vm=${encodeURIComponent(vmName)}&project=${projectId}`, `console-${vmName}`, "width=1024,height=768,menubar=no,toolbar=no,location=no");
          onClose();
        }}>
          🖥 Console
        </button>
      )}
      <button onClick={() => { duplicateNode(nodeId); onClose(); }}>
        ⧉ Duplicate
      </button>
      <button onClick={() => { hideNode(nodeId); onClose(); }}>
        👁 Hide
      </button>
      {isDeployed && !isRedeploying && (
        <button className="danger" onClick={() => {
          const updateNodeData = useCanvasStore.getState().updateNodeData;
          onClose();
          setTimeout(async () => {
            if (!window.confirm(`Redeploy ${vmName}? This will destroy and recreate this VM (disk data will be lost).`)) return;
            updateNodeData(nodeId, { status: "redeploying" });
            const resp = await fetch(`/api/v1/projects/${projectId}/vms/${vmName}/redeploy`, { method: "POST" });
            const result = await resp.json();
            updateNodeData(nodeId, { status: result.status === "redeployed" ? "running" : "stopped" });
            if (result.status !== "redeployed") alert(`Redeploy failed: ${result.output || result.error || "unknown"}`);
          }, 50);
        }}>
          🔄 Redeploy VM
        </button>
      )}
      <button onClick={() => { deleteNode(nodeId); onClose(); }} className="danger">
        ✕ Delete
      </button>
    </div>
  );
}
