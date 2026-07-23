"use client";

import React, { useEffect, useRef, useState } from "react";
import AlertModal from "@/components/AlertModal";
import { useCanvasStore } from "@/stores/canvasStore";

interface NodeContextMenuProps {
  nodeId: string;
  x: number;
  y: number;
  onClose: () => void;
  onSnapshotVM?: (nodeId: string, nodeName: string, isRunning: boolean) => void;
}

export default function NodeContextMenu({ nodeId, x, y, onClose, onSnapshotVM }: NodeContextMenuProps) {
  const duplicateNode = useCanvasStore((s) => s.duplicateNode);
  const deleteNode = useCanvasStore((s) => s.deleteNode);
  const hideNode = useCanvasStore((s) => s.hideNode);
  const nodes = useCanvasStore((s) => s.nodes);
  const deployedVmIds = useCanvasStore((s) => s.deployedVmIds);
  const projectId = useCanvasStore((s) => s.currentProjectId);
  const ref = useRef<HTMLDivElement>(null);
  const [alertMsg, setAlertMsg] = useState<string | null>(null);

  const node = nodes.find((n) => n.id === nodeId);
  const isVm = node?.type === "vmNode";
  const isDeployed = isVm && deployedVmIds.has(nodeId);
  const vmName = isVm ? (node?.data as Record<string, any>).name as string : "";
  const vmStatus = isVm ? (node?.data as Record<string, any>).status as string : "";
  const isRunning = vmStatus === "running";
  const isRedeploying = vmStatus === "redeploying";
  const isNotFound = vmStatus === "not_found";

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
      {isDeployed && isRunning && !isRedeploying && !isNotFound && (
        <>
          <button onClick={async () => { await fetch(`/api/v1/projects/${projectId}/vms/${nodeId}/stop`, { method: "POST" }); onClose(); }}>
            ■ Graceful Shutdown
          </button>
          <button onClick={async () => { await fetch(`/api/v1/projects/${projectId}/vms/${nodeId}/forcestop`, { method: "POST" }); onClose(); }}>
            ⏻ Force Power Off
          </button>
          <button onClick={async () => { await fetch(`/api/v1/projects/${projectId}/vms/${nodeId}/restart`, { method: "POST" }); onClose(); }}>
            ↻ Restart
          </button>
        </>
      )}
      {isDeployed && !isRunning && !isRedeploying && !isNotFound && (
        <button onClick={async () => { await fetch(`/api/v1/projects/${projectId}/vms/${nodeId}/start`, { method: "POST" }); onClose(); }}>
          ▶ Start
        </button>
      )}
      {isDeployed && !isNotFound && (
        <button onClick={() => {
          window.open(`/console?vm=${encodeURIComponent(nodeId)}&project=${projectId}&name=${encodeURIComponent(vmName)}`, `console_${(projectId ?? "").replace(/-/g, "")}_${nodeId.replace(/-/g, "")}`, "width=1024,height=768,menubar=no,toolbar=no,location=no");
          onClose();
        }}>
          🖥 Console
        </button>
      )}
      {isVm && isDeployed && !isNotFound && onSnapshotVM && (
        <button onClick={() => { onSnapshotVM(nodeId, vmName, isRunning); onClose(); }}>
          📸 Save VM Snapshot
        </button>
      )}
      <button onClick={() => { duplicateNode(nodeId); onClose(); }}>
        ⧉ Duplicate
      </button>
      <button onClick={() => { hideNode(nodeId); onClose(); }}>
        👁 Hide
      </button>
      {isDeployed && isRedeploying && (
        <button className="danger" onClick={async () => {
          await fetch(`/api/v1/projects/${projectId}/vms/${nodeId}/cancel-redeploy`, { method: "POST" });
          const updateNodeData = useCanvasStore.getState().updateNodeData;
          updateNodeData(nodeId, { status: "stopped", redeployStep: null, redeployDetail: null });
          onClose();
        }}>
          ✕ Cancel Redeploy
        </button>
      )}
      {isDeployed && !isRedeploying && (
        <button className="danger" onClick={() => {
          const updateNodeData = useCanvasStore.getState().updateNodeData;
          onClose();
          setTimeout(async () => {
            if (!window.confirm(`Redeploy ${vmName}? This will destroy and recreate this VM (disk data will be lost).`)) return;
            updateNodeData(nodeId, { status: "redeploying" });
            const resp = await fetch(`/api/v1/projects/${projectId}/vms/${nodeId}/redeploy`, { method: "POST" });
            const result = await resp.json();
            if (result.status === "redeploying") {
              updateNodeData(nodeId, { status: "redeploying" });
            } else {
              updateNodeData(nodeId, { status: "stopped" });
              setAlertMsg(`Redeploy failed: ${result.output || result.error || "unknown"}`);
            }
          }, 50);
        }}>
          🔄 Redeploy VM
        </button>
      )}
      <button onClick={() => { deleteNode(nodeId); onClose(); }} className="danger">
        ✕ Delete
      </button>
      <AlertModal message={alertMsg} onClose={() => setAlertMsg(null)} />
    </div>
  );
}
