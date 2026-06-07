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
import ReconfigureWarningModal from "@/components/canvas/ReconfigureWarningModal";

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
    if (projectId) {
      // Always reload if nodes are empty (e.g., returning from another page)
      const store = useCanvasStore.getState();
      if (projectId !== currentProjectId || store.nodes.length === 0) {
        loadProject(projectId);
      }
    }
  }, [projectId, currentProjectId, loadProject]);

  const [deployError, setDeployError] = useState<string | null>(null);

  const fetchProjectState = () => {
    fetch(`/api/v1/projects/${projectId}`)
      .then((r) => r.ok ? r.json() : null)
      .then((data) => {
        if (data) {
          setProjectName(data.name);
          setProjectState(data.state);
          setDeployError(data.deploy_error || null);
        }
      })
      .catch(() => {});
  };

  useEffect(() => {
    fetchProjectState();
  }, [projectId]);

  const [deployProgress, setDeployProgress] = useState<{ step: string; detail: string } | null>(null);

  // Poll during transitional states
  useEffect(() => {
    if (["deploying", "reconfiguring", "stopping", "starting"].includes(projectState)) {
      const interval = setInterval(fetchProjectState, 3000);
      return () => clearInterval(interval);
    }
  }, [projectState]);

  // Poll deploy progress during deploying state
  useEffect(() => {
    if (projectState !== "deploying" && projectState !== "reconfiguring") { setDeployProgress(null); return; }
    const poll = () => {
      fetch(`/api/v1/projects/${projectId}/deploy-progress`)
        .then((r) => r.ok ? r.json() : null)
        .then((d) => { if (d?.progress) setDeployProgress(d.progress); })
        .catch(() => {});
    };
    poll();
    const interval = setInterval(poll, 3000);
    return () => clearInterval(interval);
  }, [projectState, projectId]);

  const setAllVmStatus = useCanvasStore((s) => s.setAllVmStatus);
  const topologyDirty = useCanvasStore((s) => s.topologyDirty);

  // Sync project state into the store
  useEffect(() => {
    useCanvasStore.setState({ projectState });
  }, [projectState]);

  // Sync VM status from libvirt via API
  const [deployedVmIds, setDeployedVmIds] = useState<Set<string>>(new Set());

  const syncVmStates = () => {
    if (projectState !== "active" && projectState !== "stopped") return;
    fetch(`/api/v1/projects/${projectId}/vm-states`)
      .then((r) => r.ok ? r.json() : null)
      .then((data) => {
        if (!data?.states) return;
        const states: Record<string, string> = data.states;
        const ids = new Set<string>(Object.keys(states).filter((id) => states[id] !== "not_found"));
        const hasUndeployed = Object.values(states).some((s) => s === "not_found");
        setDeployedVmIds(ids);
        useCanvasStore.setState({ deployedVmIds: ids });
        if (hasUndeployed) {
          useCanvasStore.setState({ topologyDirty: true });
        }

        // Set per-VM status from libvirt + redeploy progress
        const progressMap: Record<string, Record<string, string>> = data.progress || {};
        const store = useCanvasStore.getState();
        useCanvasStore.setState({
          nodes: store.nodes.map((node) => {
            if (node.type !== "vmNode") return node;
            if (node.id in states) {
              const redeployInfo = progressMap[node.id];
              return { ...node, data: { ...node.data, status: states[node.id], redeployStep: redeployInfo?.step || null, redeployDetail: redeployInfo?.detail || null } };
            }
            return { ...node, data: { ...node.data, status: "stopped", redeployStep: null, redeployDetail: null } };
          }),
        });
      });

    // Check dirty flag
    fetch(`/api/v1/projects/${projectId}`)
      .then((r) => r.ok ? r.json() : null)
      .then((data) => {
        if (!data) return;
        const currentNodes = (data.topology?.nodes || []).map((n: Record<string, unknown>) => n.id).sort();
        const deployedNodes = (data.deployed_topology?.nodes || []).map((n: Record<string, unknown>) => n.id).sort();
        if (JSON.stringify(currentNodes) !== JSON.stringify(deployedNodes)) {
          useCanvasStore.setState({ topologyDirty: true });
        }
        });
  };

  useEffect(() => {
    syncVmStates();
  }, [projectState, projectId]);

  // Poll vm-states when any VM is redeploying
  useEffect(() => {
    const hasRedeploying = nodes.some((n) => n.type === "vmNode" && (n.data as Record<string, unknown>).status === "redeploying");
    if (hasRedeploying) {
      const interval = setInterval(syncVmStates, 3000);
      return () => clearInterval(interval);
    }
  }, [nodes]);

  useEffect(() => {
    if (projectState === "draft") {
      setAllVmStatus("stopped");
    }
  }, [projectState, setAllVmStatus]);

  const [reconfigWarnings, setReconfigWarnings] = useState<{ type: "iso" | "disk"; storageName: string; vmName: string; vmId: string }[] | null>(null);

  const saveTopology = async () => {
    const s = useCanvasStore.getState();
    await fetch(`/api/v1/projects/${projectId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ topology: { nodes: s.nodes, edges: s.edges, hiddenNodeIds: s.hiddenNodeIds, startOrder: s.startOrder, externalIps: s.externalIps } }),
    });
  };

  const doReconfigure = async (restartVmIds?: string[]) => {
    setReconfigWarnings(null);
    setApplyingChanges(true);
    try {
      await saveTopology();
      const body: Record<string, unknown> = {};
      if (restartVmIds) body.restart_vm_ids = restartVmIds;
      const resp = await fetch(`/api/v1/projects/${projectId}/reconfigure`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await resp.json();
      if (data.status === "reconfiguring") {
        setProjectState("reconfiguring");
        useCanvasStore.setState({ topologyDirty: false });
      } else {
        alert(`Reconfigure failed:\n${data.output?.slice(-300) || data.errors?.join("\n") || data.detail || "unknown error"}`);
      }
    } catch { alert("Failed to connect to server"); }
    setApplyingChanges(false);
  };

  const handleApplyChanges = async () => {
    if (applyingChanges) return;
    // Save topology first so we diff against current canvas state
    await saveTopology();
    const projResp = await fetch(`/api/v1/projects/${projectId}`);
    const projData = await projResp.json();
    const deployed = projData?.deployed_topology || {};
    const cur = useCanvasStore.getState();
    const depStorageMap: Record<string, Record<string, unknown>> = {};
    for (const n of (deployed.nodes || [])) {
      if (n.type === "storageNode") depStorageMap[n.id] = n.data;
    }
    const changes: { type: "iso" | "disk"; storageName: string; vmName: string; vmId: string }[] = [];
    const runningVmIds = new Set<string>();
    for (const n of cur.nodes) {
      if (n.type === "vmNode" && (n.data as Record<string, unknown>).status === "running") runningVmIds.add(n.id);
    }
    for (const n of cur.nodes) {
      if (n.type !== "storageNode") continue;
      const curData = n.data as Record<string, unknown>;
      const depData = depStorageMap[n.id];
      if (!depData) continue;
      if ((curData.libraryItemId as string) === (depData.libraryItemId as string)) continue;
      const connectedVm = cur.edges.find((e) => e.source === n.id || e.target === n.id);
      const vmId = connectedVm ? (connectedVm.source === n.id ? connectedVm.target : connectedVm.source) : null;
      const vmNode = vmId ? cur.nodes.find((v) => v.id === vmId && v.type === "vmNode") : null;
      const vmName = vmNode ? (vmNode.data as Record<string, unknown>).name as string : "a VM";
      if (curData.format === "iso") {
        if (vmId && runningVmIds.has(vmId)) {
          changes.push({ type: "iso", storageName: curData.name as string, vmName, vmId });
        }
      } else {
        if (vmId) changes.push({ type: "disk", storageName: curData.name as string, vmName, vmId: vmId });
      }
    }
    if (changes.length > 0) {
      setReconfigWarnings(changes);
    } else {
      doReconfigure();
    }
  };

  const [toast, setToast] = useState<string | null>(null);
  const [applyingChanges, setApplyingChanges] = useState(false);

  const showToast = (msg: string, duration = 4000) => {
    setToast(msg);
    setTimeout(() => setToast(null), duration);
  };

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
        useCanvasStore.setState({ topologyDirty: false });
        const userStr = localStorage.getItem("troshka-user");
        const isAdmin = userStr ? JSON.parse(userStr).role === "admin" : false;
        showToast(`Deploying ${data.requirements.vm_count} VM(s)${isAdmin ? ` to ${data.host_ip}` : ""}`);
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
    reconfiguring: "#fbbf24",
    starting: "#fbbf24",
    stopping: "#fbbf24",
    active: "#4ade80",
    stopped: "#f87171",
    error: "#ef4444",
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
          {projectState === "deploying" && (
            <button className="project-stop-btn" disabled style={{ opacity: 0.8 }}>
              <span className="project-btn-spinner" /> {deployProgress ? `${deployProgress.step}: ${deployProgress.detail}` : "Deploying..."}
            </button>
          )}
          {projectState === "reconfiguring" && (
            <button className="project-stop-btn" disabled style={{ opacity: 0.8 }}>
              <span className="project-btn-spinner" /> {deployProgress ? `${deployProgress.step}: ${deployProgress.detail}` : "Applying changes..."}
            </button>
          )}
          {projectState === "active" && (
            <>
              <button className="project-stop-btn" onClick={() => {
                if (window.confirm("Stop all VMs in this environment?")) {
                  fetch(`/api/v1/projects/${projectId}/stop`, { method: "POST" })
                    .then(() => setProjectState("stopping"));
                }
              }}>
                ■ Stop
              </button>
              <button className="project-publish-btn" disabled={!topologyDirty || applyingChanges} style={(!topologyDirty || applyingChanges) ? { opacity: 0.4 } : {}} onClick={handleApplyChanges}>
                {applyingChanges ? <><span className="project-btn-spinner" /> Applying...</> : "Apply Changes"}
              </button>
              <button className="project-publish-btn" onClick={() => {
                if (window.confirm("Republish? This will DESTROY all VMs and disks, and redeploy from scratch.")) {
                  fetch(`/api/v1/projects/${projectId}/redeploy`, { method: "POST" })
                    .then(() => { setProjectState("deploying"); useCanvasStore.setState({ deployedVmIds: new Set() }); });
                }
              }}>
                ↻ Republish
              </button>
            </>
          )}
          {projectState === "stopping" && (
            <button className="project-stop-btn" disabled style={{ opacity: 0.8 }}>
              <span className="project-btn-spinner" /> Stopping...
            </button>
          )}
          {projectState === "stopped" && (
            <>
              <button className="project-publish-btn" onClick={() => {
                fetch(`/api/v1/projects/${projectId}/start`, { method: "POST" })
                  .then(() => setProjectState("starting"));
              }}>
                ▶ Start
              </button>
              <button className="project-publish-btn" onClick={async () => {
                const s = useCanvasStore.getState();
                await fetch(`/api/v1/projects/${projectId}`, {
                  method: "PATCH",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ topology: { nodes: s.nodes, edges: s.edges, hiddenNodeIds: s.hiddenNodeIds, startOrder: s.startOrder, externalIps: s.externalIps } }),
                });
                const resp = await fetch(`/api/v1/projects/${projectId}/reconfigure`, { method: "POST" });
                const data = await resp.json();
                if (data.status === "reconfiguring") {
                  setProjectState("reconfiguring");
                } else {
                  alert(`Reconfigure failed:\n${data.output?.slice(-300) || data.detail || "unknown error"}`);
                }
              }}>
                Apply Changes
              </button>
              <button className="project-publish-btn" onClick={() => {
                if (window.confirm("Republish? This will DESTROY all VMs and disks, and redeploy from scratch.")) {
                  fetch(`/api/v1/projects/${projectId}/redeploy`, { method: "POST" })
                    .then(() => { setProjectState("deploying"); useCanvasStore.setState({ deployedVmIds: new Set() }); });
                }
              }}>
                ↻ Republish
              </button>
              <button className="project-stop-btn" onClick={() => {
                if (window.confirm("Undeploy? This will destroy all VMs and return to design mode.")) {
                  fetch(`/api/v1/projects/${projectId}/undeploy`, { method: "POST" })
                    .then(() => { setProjectState("draft"); setDeployError(null); });
                }
              }}>
                Undeploy
              </button>
            </>
          )}
          {projectState === "starting" && (
            <button className="project-publish-btn" disabled style={{ opacity: 0.8 }}>
              <span className="project-btn-spinner" /> Starting...
            </button>
          )}
          {projectState === "error" && (
            <>
              <button className="project-stop-btn" onClick={() => {
                fetch(`/api/v1/projects/${projectId}/undeploy`, { method: "POST" })
                  .then(() => { setProjectState("draft"); setDeployError(null); });
              }}>
                Reset to Draft
              </button>
              <button className="project-publish-btn" onClick={() => {
                if (window.confirm("Republish? This will destroy all VMs and redeploy with the current topology.")) {
                  fetch(`/api/v1/projects/${projectId}/redeploy`, { method: "POST" })
                    .then(() => { setProjectState("deploying"); setDeployError(null); });
                }
              }}>
                ↻ Republish
              </button>
            </>
          )}
        </div>
      </div>
      {deployError && (
        <div style={{ padding: "8px 16px", background: "rgba(239,68,68,0.15)", color: "#ef4444", fontSize: 12, fontFamily: "monospace", whiteSpace: "pre-wrap", maxHeight: 120, overflowY: "auto", borderBottom: "1px solid rgba(239,68,68,0.3)" }}>
          {deployError}
        </div>
      )}
      <div className={`canvas-editor ${projectState === "draft" ? "design-mode" : ""}`} style={{ position: "relative" }}>
        <Palette onOpenStartOrder={() => setShowStartOrder(true)} onOpenExternalIps={() => setShowExternalIps(true)} />
        <Canvas />
        <PropertiesPanel />
        {toast && (
          <div style={{
            position: "absolute", bottom: 24, left: "50%", transform: "translateX(-50%)",
            padding: "8px 20px", borderRadius: 8,
            background: "rgba(30,30,50,0.95)", color: "#4ade80",
            fontSize: 13, boxShadow: "0 4px 20px rgba(0,0,0,0.4)",
            border: "1px solid rgba(74,222,128,0.3)",
            animation: "toast-in 0.3s ease-out",
            zIndex: 1000,
          }}>
            {toast}
          </div>
        )}
      </div>
      {showStartOrder && <StartOrderPanel onClose={() => setShowStartOrder(false)} />}
      {showExternalIps && <ExternalIpsPanel onClose={() => setShowExternalIps(false)} />}
      {reconfigWarnings && (
        <ReconfigureWarningModal
          changes={reconfigWarnings}
          onConfirm={(restartVmIds) => doReconfigure(restartVmIds)}
          onCancel={() => setReconfigWarnings(null)}
        />
      )}
    </ReactFlowProvider>
  );
}
