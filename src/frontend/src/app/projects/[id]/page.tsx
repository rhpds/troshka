"use client";

import React, { useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { ReactFlowProvider } from "@xyflow/react";
import Canvas from "@/components/canvas/Canvas";
import Palette from "@/components/canvas/Palette";
import PropertiesPanel from "@/components/canvas/PropertiesPanel";
import StartOrderPanel from "@/components/canvas/StartOrderPanel";
import ExternalIpsPanel from "@/components/canvas/ExternalIpsPanel";
import { useCanvasStore } from "@/stores/canvasStore";

export default function ProjectCanvasPage() {
  const params = useParams();
  const router = useRouter();
  const projectId = params.id as string;
  const loadProject = useCanvasStore((s) => s.loadProject);
  const currentProjectId = useCanvasStore((s) => s.currentProjectId);
  const nodes = useCanvasStore((s) => s.nodes);
  const [showStartOrder, setShowStartOrder] = useState(false);
  const [showExternalIps, setShowExternalIps] = useState(false);
  const [projectName, setProjectName] = useState("");
  const [projectState, setProjectState] = useState("draft");

  useEffect(() => {
    if (projectId && projectId !== currentProjectId) {
      loadProject(projectId);
    }
  }, [projectId, currentProjectId, loadProject]);

  useEffect(() => {
    fetch(`/api/v1/projects/${projectId}`)
      .then((r) => r.ok ? r.json() : null)
      .then((data) => {
        if (data) {
          setProjectName(data.name);
          setProjectState(data.state);
        }
      })
      .catch(() => {});
  }, [projectId]);

  const vmCount = nodes.filter((n) => n.type === "vmNode").length;
  const netCount = nodes.filter((n) => n.type === "networkNode" && (n.data as Record<string, unknown>).subtype === "network").length;
  const diskCount = nodes.filter((n) => n.type === "storageNode").length;

  const handlePublish = async () => {
    if (vmCount === 0) {
      alert("Add at least one VM before publishing.");
      return;
    }
    if (!window.confirm(
      `Deploy this environment?\n\n` +
      `${vmCount} VM${vmCount !== 1 ? "s" : ""}, ${netCount} network${netCount !== 1 ? "s" : ""}, ${diskCount} disk${diskCount !== 1 ? "s" : ""}\n\n` +
      `This will provision real infrastructure.`
    )) return;

    try {
      const resp = await fetch(`/api/v1/projects/${projectId}/deploy`, {
        method: "POST",
      });
      const data = await resp.json();
      if (resp.ok) {
        setProjectState("deploying");
        alert(`Deployment started!\n\nHost: ${data.host_ip}\nVMs: ${data.requirements.vm_count}\nvCPUs: ${data.requirements.total_vcpus}\nRAM: ${data.requirements.total_ram_mb} MB`);
      } else {
        alert(data.detail || "Deployment failed");
      }
    } catch {
      alert("Failed to connect to server");
    }
  };

  const stateColors: Record<string, string> = {
    draft: "#94a3b8",
    deploying: "#fbbf24",
    active: "#4ade80",
    stopping: "#fbbf24",
    stopped: "#f87171",
  };

  return (
    <ReactFlowProvider>
      <div className="project-action-bar">
        <div className="project-action-bar-left">
          <button className="project-back-btn" onClick={() => router.push("/projects")} title="Back to projects">←</button>
          <span className="project-action-name">{projectName || "Untitled"}</span>
          <span className="project-action-state" style={{ background: `${stateColors[projectState] || "#94a3b8"}22`, color: stateColors[projectState] || "#94a3b8" }}>
            {projectState}
          </span>
        </div>
        <div className="project-action-bar-center">
          <span className="project-action-stats">
            {vmCount} VM{vmCount !== 1 ? "s" : ""} · {netCount} net{netCount !== 1 ? "s" : ""} · {diskCount} disk{diskCount !== 1 ? "s" : ""}
          </span>
        </div>
        <div className="project-action-bar-right">
          {projectState === "draft" && (
            <button className="project-publish-btn" onClick={handlePublish}>
              ⚡ Deploy
            </button>
          )}
          {projectState === "active" && (
            <button className="project-stop-btn" onClick={() => {
              if (window.confirm("Stop all VMs in this environment?")) {
                fetch(`/api/v1/projects/${projectId}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ state: "stopped" }) })
                  .then(() => setProjectState("stopped"));
              }
            }}>
              ■ Stop
            </button>
          )}
          {projectState === "stopped" && (
            <button className="project-publish-btn" onClick={() => {
              fetch(`/api/v1/projects/${projectId}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ state: "active" }) })
                .then(() => setProjectState("active"));
            }}>
              ▶ Start
            </button>
          )}
        </div>
      </div>
      <div className={`canvas-editor ${projectState === "draft" ? "design-mode" : ""}`}>
        <Palette onOpenStartOrder={() => setShowStartOrder(true)} onOpenExternalIps={() => setShowExternalIps(true)} />
        <Canvas />
        <PropertiesPanel />
      </div>
      {showStartOrder && <StartOrderPanel onClose={() => setShowStartOrder(false)} />}
      {showExternalIps && <ExternalIpsPanel onClose={() => setShowExternalIps(false)} />}
    </ReactFlowProvider>
  );
}
