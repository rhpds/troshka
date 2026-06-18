"use client";

import React, { useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { ReactFlowProvider } from "@xyflow/react";
import Canvas from "@/components/canvas/Canvas";
import Palette from "@/components/canvas/Palette";
import PropertiesPanel from "@/components/canvas/PropertiesPanel";
import StartOrderPanel from "@/components/canvas/StartOrderPanel";
import ExternalIpsPanel from "@/components/canvas/ExternalIpsPanel";
import { useCanvasStore, computeTopologyDirty, setLatestVmStates } from "@/stores/canvasStore";
import ReconfigureWarningModal from "@/components/canvas/ReconfigureWarningModal";
import SavePatternModal from "@/components/canvas/SavePatternModal";
import SnapshotVMModal from "@/components/canvas/SnapshotVMModal";
import { useVmStateSocket } from "@/hooks/useVmStateSocket";

export default function ProjectCanvasPage() {
  const params = useParams();
  const router = useRouter();
  const projectId = params.id as string;
  const loadProject = useCanvasStore((s) => s.loadProject);
  const currentProjectId = useCanvasStore((s) => s.currentProjectId);
  const nodes = useCanvasStore((s) => s.nodes);
  const [showStartOrder, setShowStartOrder] = useState(false);
  const [showExternalIps, setShowExternalIps] = useState(false);
  const [showPalette, setShowPalette] = useState(true);
  const [showProperties, setShowProperties] = useState(true);
  const [showPatternModal, setShowPatternModal] = useState(false);
  const [snapshotTarget, setSnapshotTarget] = useState<{ vmId: string; vmName: string; isRunning: boolean } | null>(null);
  const [showImportModal, setShowImportModal] = useState(false);
  const [showExportModal, setShowExportModal] = useState(false);
  const [importYaml, setImportYaml] = useState("");
  const [importError, setImportError] = useState("");
  const [importing, setImporting] = useState(false);
  const [projectName, setProjectName] = useState("");
  const [projectDesc, setProjectDesc] = useState("");
  const [projectGuid, setProjectGuid] = useState("");
  const [projectState, setProjectState] = useState("draft");
  const [projectHostId, setProjectHostId] = useState("");
  const [autoStopMinutes, setAutoStopMinutes] = useState<number | null>(null);
  const [autoDeleteMinutes, setAutoDeleteMinutes] = useState<number | null>(null);
  const [autoStopExpiresAt, setAutoStopExpiresAt] = useState<string | null>(null);
  const [lifetimeExpiresAt, setLifetimeExpiresAt] = useState<string | null>(null);
  const [autoStopped, setAutoStopped] = useState(false);
  const ws = useVmStateSocket(projectId);

  useEffect(() => {
    if (ws.deleted) router.push("/projects");
  }, [ws.deleted]);

  useEffect(() => {
    document.title = projectName ? `${projectName} — Troshka` : "Troshka";
    return () => { document.title = "Troshka"; };
  }, [projectName]);

  useEffect(() => {
    if (projectId) {
      const store = useCanvasStore.getState();
      if (projectId !== currentProjectId || store.nodes.length === 0) {
        loadProject(projectId);
      }
    }
  }, [projectId, currentProjectId, loadProject]);

  const [deployError, setDeployError] = useState<string | null>(null);
  const [hasDeployedTopology, setHasDeployedTopology] = useState(false);
  const [deployHostId, setDeployHostId] = useState("");
  const [timerCountdown, setTimerCountdown] = useState<string | null>(null);
  const [timerUrgency, setTimerUrgency] = useState<"normal" | "warning" | "critical">("normal");
  const [timerLabel, setTimerLabel] = useState<string>("Shutdown");
  const [timerToast, setTimerToast] = useState<{ timer: string; minutes: number } | null>(null);

  // One-time REST fetch for project name + dirty flag + deployed disk sizes (WS doesn't carry these)
  useEffect(() => {
    fetch(`/api/v1/projects/${projectId}`)
      .then((r) => {
        if (r.status === 404) { router.push("/projects"); return null; }
        return r.ok ? r.json() : null;
      })
      .then((data) => {
        if (!data) return;
        setProjectName(data.name);
        setProjectDesc(data.description || "");
        setProjectGuid(data.guid || "");
        setProjectState(data.state);
        setProjectHostId(data.host_id || "");
        setDeployError(data.deploy_error || null);
        setAutoStopMinutes(data.auto_stop_minutes ?? null);
        setAutoDeleteMinutes(data.auto_delete_minutes ?? null);
        setAutoStopExpiresAt(data.auto_stop_expires_at ?? null);
        setLifetimeExpiresAt(data.lifetime_expires_at ?? null);
        setAutoStopped(!!data.auto_stopped);
        if (data.ocp_status) setOcpStatus(data.ocp_status);
        if (data.ocp_install_elapsed != null) setOcpInstallElapsed(data.ocp_install_elapsed);
        prevStateRef.current = data.state;
        setHasDeployedTopology(!!(data.deployed_topology?.nodes?.length));
        // topologyDirty is computed from deployedNodeData/deployedEdgeKey after they're set below
        const depSizes: Record<string, number> = {};
        for (const n of (data.deployed_topology?.nodes || [])) {
          if (n.type === "storageNode" && n.data?.size) {
            depSizes[n.id] = n.data.size;
          }
        }
        useCanvasStore.setState({ deployedDiskSizes: depSizes });
        const depNodeData: Record<string, string> = {};
        for (const n of (data.deployed_topology?.nodes || [])) {
          const { status, redeployStep, redeployDetail, liveBootDevs, ...stable } = (n.data || {}) as Record<string, unknown>;
          depNodeData[n.id] = JSON.stringify(stable);
        }
        const depEdgeKey = (data.deployed_topology?.edges || [])
          .map((e: any) => `${e.source}-${e.sourceHandle || ""}-${e.target}-${e.targetHandle || ""}`)
          .sort().join("|");
        useCanvasStore.setState({ deployedNodeData: depNodeData, deployedEdgeKey: depEdgeKey });
        setTimeout(() => {
          const s = useCanvasStore.getState();
          useCanvasStore.setState({ topologyDirty: computeTopologyDirty(s) });
        }, 100);

        // Expose BMC data to properties panel
        if (data.bmc) {
          (window as any).__deployedTopology = { bmc: data.bmc };
        } else if (data.deployed_topology?.bmc) {
          (window as any).__deployedTopology = data.deployed_topology;
        }

        // Clean up BMC data when project is in draft
        if (data.state === "draft") {
          delete (window as any).__deployedTopology;
        }
      })
      .catch(() => {});
  }, [projectId]);

  useEffect(() => {
    fetch("/api/v1/auth/me").then(r => r.ok ? r.json() : {}).then((d: { role?: string }) => {
      setIsAdmin(d.role === "admin");
      if (d.role === "admin") {
        fetch("/api/v1/hosts/").then(r => r.ok ? r.json() : []).then(hosts => {
          const active = hosts.filter((h: any) => h.state === "active" && h.agent_status === "connected" && h.host_type !== "pattern_buffer");
          setAvailableHosts(active);
        });
      }
    });
  }, []);

  const [deployProgress, setDeployProgress] = useState<{ step: string; detail: string; items?: string[] } | null>(null);
  const [ocpStatus, setOcpStatus] = useState<string | null>(null);
  const [ocpInstallElapsed, setOcpInstallElapsed] = useState<number | null>(null);

  // WebSocket → project state
  const prevStateRef = React.useRef(projectState);
  useEffect(() => {
    if (!ws.projectState) return;
    const wasTransitional = ["reconfiguring", "deploying", "starting"].includes(prevStateRef.current);
    setProjectState(ws.projectState);
    setDeployError(ws.deployError || null);
    prevStateRef.current = ws.projectState;
    if (wasTransitional && ws.projectState === "active") {
      fetch(`/api/v1/projects/${projectId}`).then((r) => r.ok ? r.json() : null).then((proj) => {
        if (!proj) return;
        const depData: Record<string, string> = {};
        for (const n of (proj.deployed_topology?.nodes || [])) {
          const { status, redeployStep, redeployDetail, liveBootDevs, ...stable } = (n.data || {}) as Record<string, unknown>;
          depData[n.id] = JSON.stringify(stable);
        }
        const depEdge = (proj.deployed_topology?.edges || [])
          .map((e: any) => `${e.source}-${e.sourceHandle || ""}-${e.target}-${e.targetHandle || ""}`)
          .sort().join("|");
        useCanvasStore.setState({ deployedNodeData: depData, deployedEdgeKey: depEdge });
        setTimeout(() => {
          const s = useCanvasStore.getState();
          useCanvasStore.setState({ topologyDirty: computeTopologyDirty(s) });
        }, 100);
      });
    }
  }, [ws.projectState, ws.deployError]);

  // WebSocket → timer expiry updates (from project-state messages after stop/start/deploy)
  useEffect(() => {
    if (ws.autoStopExpiresAt !== undefined) setAutoStopExpiresAt(ws.autoStopExpiresAt);
    if (ws.lifetimeExpiresAt !== undefined) setLifetimeExpiresAt(ws.lifetimeExpiresAt);
    if (ws.autoStopped !== undefined) setAutoStopped(ws.autoStopped);
  }, [ws.autoStopExpiresAt, ws.lifetimeExpiresAt, ws.autoStopped]);

  // WebSocket → deploy progress
  useEffect(() => {
    setDeployProgress(ws.deployProgress);
  }, [ws.deployProgress]);

  // WebSocket → topology update from another session
  useEffect(() => {
    if (!ws.topologyUpdate) return;
    const topo = ws.topologyUpdate;
    const store = useCanvasStore.getState();
    if (topo.nodes && topo.edges) {
      const currentKey = store.nodes.map((n: any) => `${n.id}:${JSON.stringify(n.data)}`).join("|");
      const incomingKey = topo.nodes.map((n: any) => `${n.id}:${JSON.stringify(n.data)}`).join("|");
      if (currentKey !== incomingKey) {
        useCanvasStore.setState({ nodes: topo.nodes, edges: topo.edges });
      }
    }
  }, [ws.topologyUpdate]);

  useEffect(() => {
    if (ws.ocpHealth?.phase === "ready") setOcpStatus("ready");
    else if (ws.ocpHealth) setOcpStatus("monitoring");
  }, [ws.ocpHealth]);

  // Timer countdown ticker
  useEffect(() => {
    const candidates = [
      autoStopExpiresAt ? { time: new Date(autoStopExpiresAt).getTime(), label: "Shutdown" } : null,
      lifetimeExpiresAt ? { time: new Date(lifetimeExpiresAt).getTime(), label: "Deleting" } : null,
    ].filter(Boolean).sort((a, b) => a!.time - b!.time);
    const nearest = candidates[0];

    if (!nearest) { setTimerCountdown(null); return; }
    const earliest = nearest.time;
    setTimerLabel(nearest.label);

    let id: ReturnType<typeof setInterval>;
    const tick = () => {
      const remaining = earliest - Date.now();
      if (remaining <= 0) { setTimerCountdown(nearest!.label === "Shutdown" ? "Auto-Shutdown" : "Auto-Deleted"); setTimerUrgency("critical"); clearInterval(id); return; }
      const totalSecs = Math.floor(remaining / 1000);
      const h = Math.floor(totalSecs / 3600);
      const m = Math.floor((totalSecs % 3600) / 60);
      const s = totalSecs % 60;
      const pad = (n: number) => String(n).padStart(2, "0");
      setTimerCountdown(h > 0 ? `${h}h ${pad(m)}m ${pad(s)}s` : `${m}m ${pad(s)}s`);
      setTimerUrgency(totalSecs <= 300 ? "critical" : totalSecs <= 900 ? "warning" : "normal");
    };
    tick();
    id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [autoStopExpiresAt, lifetimeExpiresAt]);

  // Timer warning toast
  useEffect(() => {
    if (ws.timerWarning) {
      setTimerToast({ timer: ws.timerWarning.timer, minutes: ws.timerWarning.minutes_remaining });
    }
  }, [ws.timerWarning]);

  // Timer fired handler
  useEffect(() => {
    if (ws.timerFired === "auto_stop") {
      setProjectState("stopping");
    } else if (ws.timerFired === "auto_delete") {
      setProjectState("deleting");
      router.push("/projects");
    }
  }, [ws.timerFired]);

  const setAllVmStatus = useCanvasStore((s) => s.setAllVmStatus);
  const topologyDirty = useCanvasStore((s) => s.topologyDirty);

  // Sync project state into the store
  useEffect(() => {
    useCanvasStore.setState({ projectState });
  }, [projectState]);

  // WebSocket → VM states into canvas store
  const [deployedVmIds, setDeployedVmIds] = useState<Set<string>>(new Set());

  // VM states driven by REST polling (below), not WS — WS is unreliable in dev mode
  useEffect(() => {
    if (!Object.keys(ws.vmStates).length) return;
    setLatestVmStates(ws.vmStates);
    const ids = new Set<string>(Object.keys(ws.vmStates));
    setDeployedVmIds(ids);
    useCanvasStore.setState({ deployedVmIds: ids });
  }, [ws.vmStates]);

  useEffect(() => {
    if (projectState === "draft") {
      setAllVmStatus("stopped");
    }
  }, [projectState, setAllVmStatus]);

  // REST poll for VM states — stored in latestVmStates, not on node data
  // (writing status to node data triggers auto-save which fights with WS)
  useEffect(() => {
    if (projectState !== "active" && projectState !== "stopped") return;
    if (!projectId) return;
    const poll = async () => {
      try {
        const resp = await fetch(`/api/v1/projects/${projectId}/vm-states`);
        if (!resp.ok) return;
        const data = await resp.json();
        if (data.states && Object.keys(data.states).length > 0) {
          setLatestVmStates(data.states);
        }
      } catch { /* ignore */ }
    };
    const timer = setInterval(poll, 5000);
    poll();
    return () => clearInterval(timer);
  }, [projectId, projectState]);

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
        setDeployError(data.output?.slice(-300) || data.errors?.join("\n") || data.detail || "Reconfigure failed");
      }
    } catch { setDeployError("Failed to connect to server"); }
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
      if (n.type === "vmNode" && (n.data as Record<string, any>).status === "running") runningVmIds.add(n.id);
    }
    for (const n of cur.nodes) {
      if (n.type !== "storageNode") continue;
      const curData = n.data as Record<string, any>;
      const depData = depStorageMap[n.id];
      if (!depData) continue;
      if ((curData.libraryItemId as string || null) === (depData.libraryItemId as string || null)) continue;
      const connectedVm = cur.edges.find((e) => e.source === n.id || e.target === n.id);
      const vmId = connectedVm ? (connectedVm.source === n.id ? connectedVm.target : connectedVm.source) : null;
      const vmNode = vmId ? cur.nodes.find((v) => v.id === vmId && v.type === "vmNode") : null;
      const vmName = vmNode ? (vmNode.data as Record<string, any>).name as string : "a VM";
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
  const [showMigrate, setShowMigrate] = useState(false);
  const [availableHosts, setAvailableHosts] = useState<{id: string; instance_id: string | null; ip_address: string; used_vcpus: number; total_vcpus: number; used_ram_mb: number; total_ram_mb: number; storage_pool_id: string | null; provider_type: string | null}[]>([]);
  const [migrateTarget, setMigrateTarget] = useState("");
  const [migrating, setMigrating] = useState(false);
  const [migrateSourceHost, setMigrateSourceHost] = useState<{instance_id: string | null; ip_address: string} | null>(null);
  const [isAdmin, setIsAdmin] = useState(false);

  const showToast = (msg: string, duration = 4000) => {
    setToast(msg);
    setTimeout(() => setToast(null), duration);
  };

  const openMigrate = async () => {
    const [hostsResp, projectResp] = await Promise.all([
      fetch("/api/v1/hosts/"),
      fetch(`/api/v1/projects/${projectId}`),
    ]);
    if (!hostsResp.ok || !projectResp.ok) return;
    const hosts = await hostsResp.json();
    const proj = await projectResp.json();
    const currentHost = hosts.find((h: any) => h.id === proj.host_id);
    if (!currentHost?.storage_pool_id) return;
    setMigrateSourceHost({ instance_id: currentHost.instance_id, ip_address: currentHost.ip_address });
    const samePool = hosts.filter((h: any) =>
      h.storage_pool_id === currentHost.storage_pool_id &&
      h.id !== proj.host_id &&
      h.state === "active" &&
      h.agent_status === "connected"
    );
    setAvailableHosts(samePool);
    setShowMigrate(true);
  };

  const handleMigrate = async () => {
    if (!migrateTarget) return;
    setMigrating(true);
    const resp = await fetch(`/api/v1/projects/${projectId}/migrate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target_host_id: migrateTarget }),
    });
    setMigrating(false);
    if (resp.ok) {
      setShowMigrate(false);
      setMigrateTarget("");
      setProjectState("migrating");
    } else {
      const data = await resp.json();
      alert(data.detail || "Migration failed");
    }
  };

  const vmCount = nodes.filter((n) => n.type === "vmNode").length;
  const netCount = nodes.filter((n) => n.type === "networkNode" && (n.data as Record<string, any>).subtype === "network").length;
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
      await saveTopology();
      setProjectState("deploying");
      const deployParams = new URLSearchParams();
      if (deployHostId) deployParams.set("host_id", deployHostId);
      const deployQs = deployParams.toString() ? `?${deployParams.toString()}` : "";
      const resp = await fetch(`/api/v1/projects/${projectId}/deploy${deployQs}`, {
        method: "POST",
      });
      const data = await resp.json();
      if (resp.ok) {
        useCanvasStore.setState({ topologyDirty: false });
        const userStr = localStorage.getItem("troshka-user");
        const isAdmin = userStr ? JSON.parse(userStr).role === "admin" : false;
        showToast(`Deploying ${data.requirements.vm_count} VM(s)${isAdmin ? ` to ${data.host_ip}` : ""}`);
      } else {
        setProjectState("draft");
        alert(data.detail || "Deployment failed");
      }
    } catch {
      setProjectState("draft");
      alert("Failed to connect to server");
    }
  };

  const stateColors: Record<string, string> = {
    draft: "#94a3b8",
    deploying: "#fbbf24",
    reconfiguring: "#fbbf24",
    starting: "#fbbf24",
    stopping: "#fbbf24",
    migrating: "var(--troshka-yellow, #f0ab00)",
    active: "#4ade80",
    stopped: "#f87171",
    error: "#ef4444",
    deleting: "#f87171",
  };

  return (
    <ReactFlowProvider>
      {timerToast && (
        <div style={{
          position: "fixed", top: 16, left: "50%", transform: "translateX(-50%)", zIndex: 9999,
          background: timerToast.timer === "auto_delete" ? "rgba(239,68,68,0.95)" : "rgba(251,191,36,0.95)",
          color: "#fff", padding: "10px 20px", borderRadius: 8,
          display: "flex", alignItems: "center", gap: 12, fontSize: 13, fontWeight: 500,
          boxShadow: "0 4px 20px rgba(0,0,0,0.3)",
        }}>
          <span>
            {timerToast.timer === "auto_stop" ? "⏱ Auto-stop" : "🗑 Auto-delete"} in {timerToast.minutes} minute{timerToast.minutes !== 1 ? "s" : ""}
          </span>
          <button
            onClick={() => {
              fetch(`/api/v1/projects/${projectId}/extend-timer`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ timer: timerToast.timer, add_minutes: 60 }),
              }).then(r => r.json()).then(data => {
                setAutoStopExpiresAt(data.auto_stop_expires_at ?? null);
                setLifetimeExpiresAt(data.lifetime_expires_at ?? null);
              });
              setTimerToast(null);
            }}
            style={{
              padding: "4px 12px", borderRadius: 4, border: "1px solid rgba(255,255,255,0.4)",
              background: "rgba(255,255,255,0.15)", color: "#fff", cursor: "pointer", fontSize: 12,
            }}
          >Extend 1h</button>
          <button
            onClick={() => setTimerToast(null)}
            style={{
              padding: "4px 8px", borderRadius: 4, border: "none",
              background: "transparent", color: "rgba(255,255,255,0.7)", cursor: "pointer", fontSize: 14,
            }}
          >✕</button>
        </div>
      )}
      <div className="project-action-bar">
        <div className="project-action-bar-left">
          <button className="project-back-btn" onClick={() => router.push("/projects")} title="Back to projects">←</button>
          <span
            className="project-action-name"
            style={{ cursor: "pointer", borderBottom: "1px dashed rgba(255,255,255,0.2)" }}
            onClick={() => {
              const newName = window.prompt("Rename project:", projectName);
              if (newName && newName.trim() && newName !== projectName) {
                fetch(`/api/v1/projects/${projectId}`, {
                  method: "PATCH",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ name: newName.trim() }),
                }).then((r) => {
                  if (r.ok) setProjectName(newName.trim());
                });
              }
            }}
            title="Click to rename"
          >{projectName || "Untitled"}</span>
          <span className="project-action-state" style={{ background: `${stateColors[projectState] || "#94a3b8"}22`, color: stateColors[projectState] || "#94a3b8" }}>
            {projectState === "stopped" && autoStopped ? "stopped (auto)" : projectState}
          </span>
          {timerCountdown && (
            <span
              className={`project-timer-badge ${timerUrgency}`}
              style={{
                fontSize: 11, marginLeft: 8, padding: "2px 8px", borderRadius: 10,
                color: timerUrgency === "critical" ? "#ef4444" : timerUrgency === "warning" ? "#fbbf24" : "#94a3b8",
                background: timerUrgency === "critical" ? "rgba(239,68,68,0.12)" : timerUrgency === "warning" ? "rgba(251,191,36,0.12)" : "rgba(148,163,184,0.08)",
                animation: timerUrgency === "critical" ? "pulse 1s infinite" : "none",
              }}
              title="Time remaining (click to open Project settings)"
              onClick={() => setShowPalette(true)}
            >
              ⏱ {timerCountdown === "Auto-Shutdown" || timerCountdown === "Auto-Deleted" ? `Project was ${timerCountdown}` : `${timerLabel} in ${timerCountdown}`}
            </span>
          )}
        </div>
        <div className="project-action-bar-center">
          <span className="project-action-stats">
            {vmCount} VM{vmCount !== 1 ? "s" : ""} · {netCount} net{netCount !== 1 ? "s" : ""} · {diskCount} disk{diskCount !== 1 ? "s" : ""}
          </span>
        </div>
        <div className="project-action-bar-right">
          {(projectState === "active" || projectState === "stopped" || projectState === "starting") && (
            <button
              className="project-publish-btn"
              onClick={() => window.open(`/console/monitor?project=${projectId}`, "_blank")}
              style={{ opacity: 0.85 }}
            >
              MegaConsole
            </button>
          )}
          {(projectState === "active" || projectState === "stopped") && (
            <button className="project-publish-btn" onClick={() => setShowPatternModal(true)} style={{ opacity: 0.85 }}>
              Save as Pattern
            </button>
          )}
          {nodes.length > 0 && (
            <button
              className="project-publish-btn"
              style={{ opacity: 0.85 }}
              onClick={() => setShowExportModal(true)}
            >
              Export Template
            </button>
          )}
          {projectState !== "deploying" && projectState !== "reconfiguring" && (
            <button
              className="project-stop-btn"
              style={{ borderColor: "var(--pf-t--global--color--status--danger--default)", color: "var(--pf-t--global--color--status--danger--default)" }}
              onClick={() => {
                if (!window.confirm(`Delete project "${projectName}"? This cannot be undone.`)) return;
                setProjectState("deleting");
                localStorage.removeItem(`troshka-canvas-${projectId}`);
                const deleting = JSON.parse(localStorage.getItem("troshka-deleting-projects") || "[]");
                deleting.push(projectId);
                localStorage.setItem("troshka-deleting-projects", JSON.stringify(deleting));
                router.push("/projects");
                fetch(`/api/v1/projects/${projectId}`, { method: "DELETE" }).then(() => {
                  const remaining = JSON.parse(localStorage.getItem("troshka-deleting-projects") || "[]").filter((id: string) => id !== projectId);
                  localStorage.setItem("troshka-deleting-projects", JSON.stringify(remaining));
                });
              }}
            >
              Delete
            </button>
          )}
          {projectState === "draft" && (
            <>
              {isAdmin && availableHosts.length > 0 && (
                <select style={{
                  padding: "6px 10px", borderRadius: 6, fontSize: 12,
                  border: "1px solid var(--pf-t--global--border--color--default)",
                  background: "var(--pf-t--global--background--color--primary--default)",
                  color: "var(--pf-t--global--text--color--regular)",
                }} value={deployHostId} onChange={(e) => setDeployHostId(e.target.value)}>
                  <option value="">Auto (best host)</option>
                  {availableHosts.map((h) => <option key={h.id} value={h.id}>{h.id.slice(0, 8)} — {h.ip_address}{h.provider_type ? ` (${h.provider_type})` : ""}, {h.total_vcpus - h.used_vcpus} vCPUs / {Math.round((h.total_ram_mb - h.used_ram_mb) / 1024)}G free</option>)}
                </select>
              )}
              <button className="project-publish-btn" onClick={handlePublish}>
                ⚡ Deploy
              </button>
            </>
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
              {isAdmin && <button className="project-publish-btn" onClick={openMigrate} style={{ opacity: 0.85 }}>
                Migrate
              </button>}
              <button className="project-publish-btn" disabled={!topologyDirty || applyingChanges} style={(!topologyDirty || applyingChanges) ? { opacity: 0.4 } : {}} onClick={handleApplyChanges}>
                {applyingChanges ? <><span className="project-btn-spinner" /> Applying...</> : "Apply Changes"}
              </button>
              <button className="project-publish-btn" onClick={() => {
                if (window.confirm("Republish? This will DESTROY all VMs and disks, and redeploy from scratch.")) {
                  setProjectState("deploying");
                  fetch(`/api/v1/projects/${projectId}/redeploy`, { method: "POST" })
                    .then(async (r) => {
                      if (r.ok) { useCanvasStore.setState({ deployedVmIds: new Set() }); }
                      else { setProjectState("active"); const err = await r.json().catch(() => ({ detail: "Redeploy failed" })); alert(err.detail || "Redeploy failed"); }
                    });
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
                  setDeployError(data.output?.slice(-300) || data.detail || "Reconfigure failed");
                }
              }}>
                Apply Changes
              </button>
              <button className="project-publish-btn" onClick={() => {
                if (window.confirm("Republish? This will DESTROY all VMs and disks, and redeploy from scratch.")) {
                  setProjectState("deploying");
                  fetch(`/api/v1/projects/${projectId}/redeploy`, { method: "POST" })
                    .then(async (r) => {
                      if (r.ok) { useCanvasStore.setState({ deployedVmIds: new Set() }); }
                      else { setProjectState("active"); const err = await r.json().catch(() => ({ detail: "Redeploy failed" })); alert(err.detail || "Redeploy failed"); }
                    });
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
              {hasDeployedTopology && (
                <button className="project-publish-btn" onClick={() => {
                  fetch(`/api/v1/projects/${projectId}/start`, { method: "POST" })
                    .then((r) => {
                      if (r.ok) { setProjectState("starting"); setDeployError(null); }
                      else r.json().then((d) => alert(d.detail || "Start failed"));
                    });
                }}>
                  ▶ Retry Start
                </button>
              )}
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
      {(projectState === "deploying" || projectState === "reconfiguring" || (projectState === "error" && deployError)) && (
        <div style={{
          position: "absolute", inset: 0, zIndex: 100,
          display: "flex", alignItems: "center", justifyContent: "center",
          pointerEvents: "none",
        }}>
          <div style={{
            background: "var(--pf-t--global--background--color--primary--default)",
            borderRadius: 12, padding: 24, width: 420, maxWidth: "90vw",
            boxShadow: "0 8px 32px rgba(0,0,0,0.5)",
            pointerEvents: "auto",
            border: `1px solid ${projectState === "error" ? "rgba(239,68,68,0.4)" : "var(--pf-t--global--border--color--default)"}`,
          }}>
            <h3 style={{ margin: "0 0 16px", display: "flex", alignItems: "center", gap: 8 }}>
              {projectState === "error" ? (
                <span style={{ color: "#ef4444" }}>Deploy Failed</span>
              ) : (
                <><span className="project-btn-spinner" /> {projectState === "deploying" ? "Deploying..." : "Applying Changes..."}</>
              )}
            </h3>
            {deployProgress && projectState !== "error" && (
              <div style={{ fontSize: 13, marginBottom: deployProgress.items ? 8 : 0 }}>
                <span style={{ opacity: 0.7 }}>{deployProgress.step}</span>
                {deployProgress.detail ? `: ${deployProgress.detail}` : ""}
              </div>
            )}
            {deployProgress?.items && projectState !== "error" && (
              <div style={{ fontSize: 12, opacity: 0.6, whiteSpace: "pre-line", lineHeight: 1.6, maxHeight: 200, overflowY: "auto" }}>
                {deployProgress.items.join("\n")}
              </div>
            )}
            {deployError && (
              <div style={{ fontSize: 12, color: "#ef4444", fontFamily: "monospace", whiteSpace: "pre-wrap", maxHeight: 200, overflowY: "auto", marginTop: 8, padding: 8, background: "rgba(239,68,68,0.08)", borderRadius: 6 }}>
                {deployError}
              </div>
            )}
            {projectState === "error" && (
              <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginTop: 16 }}>
                {deployError && (
                  <button onClick={() => navigator.clipboard.writeText(deployError)} style={{ padding: "6px 16px", borderRadius: 6, border: "1px solid var(--pf-t--global--border--color--default)", background: "transparent", color: "var(--pf-t--global--text--color--subtle)", cursor: "pointer", fontSize: 12 }}>
                    Copy Error
                  </button>
                )}
                <button onClick={() => setDeployError(null)} style={{ padding: "6px 16px", borderRadius: 6, border: "1px solid var(--pf-t--global--border--color--default)", background: "transparent", color: "var(--pf-t--global--text--color--regular)", cursor: "pointer" }}>
                  Dismiss
                </button>
              </div>
            )}
          </div>
        </div>
      )}
      <div className={`canvas-editor ${projectState === "draft" ? "design-mode" : ""}`} style={{ position: "relative" }}>
        {nodes.length === 0 && !projectName && (
          <div style={{
            position: "absolute", inset: 0, zIndex: 20,
            display: "flex", alignItems: "center", justifyContent: "center",
            background: "var(--troshka-bg)",
          }}>
            <div style={{ textAlign: "center", opacity: 0.6 }}>
              <span className="project-btn-spinner" style={{ width: 24, height: 24, marginBottom: 8 }} />
              <div style={{ fontSize: 13 }}>Loading topology...</div>
            </div>
          </div>
        )}
        {nodes.length === 0 && projectName && projectState === "draft" && (
          <div style={{
            position: "absolute", inset: 0, zIndex: 20,
            display: "flex", alignItems: "center", justifyContent: "center",
            pointerEvents: "none",
          }}>
            <div style={{ textAlign: "center", pointerEvents: "auto" }}>
              <div style={{ fontSize: 14, opacity: 0.5, marginBottom: 16 }}>
                Drag components from the palette or import a template
              </div>
              <button
                onClick={() => { setImportYaml(""); setImportError(""); setShowImportModal(true); }}
                style={{
                  padding: "10px 24px", borderRadius: 8,
                  border: "1px solid var(--pf-t--global--border--color--default)",
                  background: "var(--pf-t--global--background--color--primary--default)",
                  color: "#fff", cursor: "pointer", fontSize: 14, fontWeight: 500,
                }}
              >
                Import Template YAML
              </button>
            </div>
          </div>
        )}
        {showPalette && <Palette onOpenStartOrder={() => setShowStartOrder(true)} onOpenExternalIps={() => setShowExternalIps(true)} projectDescription={projectDesc} projectGuid={projectGuid} projectId={projectId} hostId={isAdmin ? projectHostId : undefined} ocpHealth={ws.ocpHealth || (ocpStatus === "ready" ? { phase: "ready", detail: ocpInstallElapsed != null ? `cluster ready (${Math.floor(ocpInstallElapsed / 60)}m ${(ocpInstallElapsed % 60).toString().padStart(2, "0")}s)` : "cluster ready" } : ocpStatus === "error" ? { phase: "error", detail: "install failed" } : ocpStatus === "monitoring" ? { phase: "ssh", detail: "monitoring..." } : null)} onDescriptionChange={(desc) => {
          fetch(`/api/v1/projects/${projectId}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ description: desc }) })
            .then((r) => { if (r.ok) setProjectDesc(desc); });
        }} autoStopMinutes={autoStopMinutes} autoDeleteMinutes={autoDeleteMinutes} onAutoStopChange={(v) => {
          setAutoStopMinutes(v);
          fetch(`/api/v1/projects/${projectId}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ auto_stop_minutes: v }),
          }).then(r => r.json()).then(data => {
            setAutoStopExpiresAt(data.auto_stop_expires_at ?? null);
          });
        }} onAutoDeleteChange={(v) => {
          setAutoDeleteMinutes(v);
          fetch(`/api/v1/projects/${projectId}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ auto_delete_minutes: v }),
          }).then(r => r.json()).then(data => {
            setLifetimeExpiresAt(data.lifetime_expires_at ?? null);
          });
        }} />}
        <button
          onClick={() => setShowPalette(!showPalette)}
          title={showPalette ? "Hide palette" : "Show palette"}
          style={{
            position: "absolute", left: showPalette ? 220 : 0, top: "50%", transform: "translateY(-50%)",
            zIndex: 10, width: 20, height: 48, borderRadius: showPalette ? "0 6px 6px 0" : "0 6px 6px 0",
            background: "var(--troshka-surface)", border: "1px solid var(--troshka-border)", borderLeft: "none",
            cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center",
            color: "var(--troshka-text-dim)", fontSize: 11, transition: "left 0.2s",
          }}
        >{showPalette ? "◂" : "▸"}</button>
        <Canvas
          onSnapshotVM={(vmId, vmName, isRunning) => setSnapshotTarget({ vmId, vmName, isRunning })}
        />
        <button
          onClick={() => setShowProperties(!showProperties)}
          title={showProperties ? "Hide properties" : "Show properties"}
          style={{
            position: "absolute", right: showProperties ? 280 : 0, top: "50%", transform: "translateY(-50%)",
            zIndex: 10, width: 20, height: 48, borderRadius: showProperties ? "6px 0 0 6px" : "6px 0 0 6px",
            background: "var(--troshka-surface)", border: "1px solid var(--troshka-border)", borderRight: "none",
            cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center",
            color: "var(--troshka-text-dim)", fontSize: 11, transition: "right 0.2s",
          }}
        >{showProperties ? "▸" : "◂"}</button>
        {showProperties && <PropertiesPanel />}
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
      {showExternalIps && <ExternalIpsPanel projectId={projectId} onClose={() => setShowExternalIps(false)} />}
      {reconfigWarnings && (
        <ReconfigureWarningModal
          changes={reconfigWarnings}
          onConfirm={(restartVmIds) => doReconfigure(restartVmIds)}
          onCancel={() => setReconfigWarnings(null)}
        />
      )}
      {showPatternModal && (
        <SavePatternModal
          projectId={projectId}
          projectName={projectName}
          hasRunningVMs={nodes.some((n) => n.type === "vmNode" && (n.data as Record<string, any>).status === "running")}
          onSaved={() => {
            setShowPatternModal(false);
            showToast("Pattern saved successfully");
          }}
          onClose={() => setShowPatternModal(false)}
        />
      )}
      {snapshotTarget && (
        <SnapshotVMModal
          projectId={projectId}
          vmId={snapshotTarget.vmId}
          vmName={snapshotTarget.vmName}
          isRunning={snapshotTarget.isRunning}
          onSaved={() => {
            setSnapshotTarget(null);
            showToast("VM snapshot saved to library");
          }}
          onClose={() => setSnapshotTarget(null)}
        />
      )}
      {showMigrate && (
        <div style={{ position: "fixed", inset: 0, zIndex: 10000, display: "flex",
          alignItems: "center", justifyContent: "center", background: "rgba(0,0,0,0.6)" }}
          onClick={(e) => { if (e.target === e.currentTarget) setShowMigrate(false); }}>
          <div style={{ background: "var(--pf-t--global--background--color--primary--default)",
            borderRadius: 12, padding: 24, width: 500, maxWidth: "90vw",
            boxShadow: "0 8px 32px rgba(0,0,0,0.5)",
            border: "1px solid var(--pf-t--global--border--color--default)" }}>
            <div style={{ fontWeight: 600, fontSize: 16, marginBottom: 16 }}>Migrate Project</div>
            {migrateSourceHost && (
              <div style={{ fontSize: 12, color: "var(--pf-t--global--text--color--subtle)", marginBottom: 12,
                padding: "8px 12px", borderRadius: 6, background: "rgba(255,255,255,0.05)",
                border: "1px solid var(--pf-t--global--border--color--default)" }}>
                <strong>Source:</strong> {migrateSourceHost.ip_address} ({migrateSourceHost.instance_id})
              </div>
            )}
            {availableHosts.length === 0 ? (
              <p style={{ color: "var(--pf-t--global--text--color--subtle)" }}>No available hosts in the same storage pool.</p>
            ) : (
              <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
                <label style={{ fontSize: 12 }}>Destination:</label>
                <select style={{
                  width: "100%", padding: "6px 10px", borderRadius: 6,
                  border: "1px solid var(--pf-t--global--border--color--default)",
                  background: "var(--pf-t--global--background--color--primary--default)",
                  color: "var(--pf-t--global--text--color--regular)", fontSize: 13,
                }} value={migrateTarget} onChange={(e) => setMigrateTarget(e.target.value)}>
                  <option value="">Select host...</option>
                  {availableHosts.map((h) => (
                    <option key={h.id} value={h.id}>
                      {h.instance_id} — {h.ip_address} (CPU: {h.used_vcpus}/{h.total_vcpus}, RAM: {Math.round(h.used_ram_mb/1024)}/{Math.round(h.total_ram_mb/1024)} GB)
                    </option>
                  ))}
                </select>
                <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
                  <button className="project-stop-btn" onClick={() => setShowMigrate(false)}>Cancel</button>
                  <button className="project-publish-btn" onClick={handleMigrate} disabled={!migrateTarget || migrating}
                          style={(!migrateTarget || migrating) ? { opacity: 0.4 } : {}}>
                    {migrating ? <><span className="project-btn-spinner" /> Migrating...</> : "Migrate"}
                  </button>
                </div>
              </div>
            )}
          </div>
        </div>
      )}
      {showImportModal && (
        <div style={{ position: "fixed", inset: 0, zIndex: 10000, display: "flex",
          alignItems: "center", justifyContent: "center", background: "rgba(0,0,0,0.6)" }}
          onClick={(e) => { if (e.target === e.currentTarget) setShowImportModal(false); }}>
          <div style={{ background: "var(--pf-t--global--background--color--primary--default)",
            borderRadius: 12, padding: 24, width: 600, maxWidth: "90vw",
            boxShadow: "0 8px 32px rgba(0,0,0,0.5)",
            border: "1px solid var(--pf-t--global--border--color--default)" }}>
            <div style={{ fontWeight: 600, fontSize: 16, marginBottom: 12 }}>Import Template YAML</div>
            <div style={{ fontSize: 12, color: "var(--pf-t--global--text--color--subtle)", marginBottom: 12 }}>
              Paste an infra_template.yaml to generate the canvas topology.
            </div>
            <textarea
              value={importYaml}
              onChange={(e) => { setImportYaml(e.target.value); setImportError(""); }}
              placeholder={"networks:\n  cluster:\n    cidr: 10.0.0.0/24\n    dhcp: true\n\nvms:\n  bastion:\n    role: bastion\n    vcpus: 4\n    ram_gb: 8\n    ..."}
              style={{
                width: "100%", height: 300, fontFamily: "monospace", fontSize: 12,
                padding: 12, borderRadius: 8, resize: "vertical",
                background: "var(--pf-t--global--background--color--secondary--default)",
                color: "var(--pf-t--global--text--color--regular)",
                border: "1px solid var(--pf-t--global--border--color--default)",
              }}
            />
            {importError && (
              <div style={{ color: "var(--pf-t--global--color--status--danger--default)", fontSize: 12, marginTop: 8 }}>
                {importError}
              </div>
            )}
            <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginTop: 16 }}>
              <button onClick={() => setShowImportModal(false)}
                style={{ padding: "8px 16px", borderRadius: 6, border: "1px solid var(--pf-t--global--border--color--default)",
                  background: "transparent", color: "var(--pf-t--global--text--color--regular)", cursor: "pointer" }}>
                Cancel
              </button>
              <label style={{ padding: "8px 16px", borderRadius: 6, border: "1px solid var(--pf-t--global--border--color--default)",
                background: "transparent", color: "var(--pf-t--global--text--color--regular)", cursor: "pointer", display: "inline-block" }}>
                Upload File
                <input type="file" accept=".yaml,.yml" style={{ display: "none" }} onChange={(e) => {
                  const file = e.target.files?.[0];
                  if (file) {
                    file.text().then((text) => { setImportYaml(text); setImportError(""); });
                  }
                  e.target.value = "";
                }} />
              </label>
              <button
                disabled={!importYaml.trim() || importing}
                onClick={async () => {
                  setImporting(true);
                  setImportError("");
                  try {
                    let parsed: Record<string, unknown>;
                    try {
                      const jsYaml = await import("js-yaml");
                      parsed = jsYaml.load(importYaml) as Record<string, unknown>;
                    } catch {
                      setImportError("Invalid YAML syntax");
                      setImporting(false);
                      return;
                    }
                    if (!parsed || typeof parsed !== "object") {
                      setImportError("Template must be a YAML mapping");
                      setImporting(false);
                      return;
                    }
                    if (!parsed.vms || !parsed.networks) {
                      setImportError("Template must contain 'vms' and 'networks' sections");
                      setImporting(false);
                      return;
                    }
                    const resp = await fetch(`/api/v1/projects/${projectId}/import-template`, {
                      method: "POST",
                      headers: { "Content-Type": "application/json" },
                      body: JSON.stringify({ template_yaml: parsed }),
                    });
                    if (!resp.ok) {
                      const err = await resp.json().catch(() => ({ detail: "Import failed" }));
                      setImportError(err.detail || "Import failed");
                      setImporting(false);
                      return;
                    }
                    const data = await resp.json();
                    const t = data.topology || {};
                    useCanvasStore.setState({
                      nodes: t.nodes || [],
                      edges: t.edges || [],
                      hiddenNodeIds: t.hiddenNodeIds || [],
                      startOrder: t.startOrder || [],
                      externalIps: t.externalIps || [],
                    });
                    setShowImportModal(false);
                  } catch (err: unknown) {
                    setImportError(err instanceof Error ? err.message : "Import failed");
                  } finally {
                    setImporting(false);
                  }
                }}
                style={{
                  padding: "8px 20px", borderRadius: 6, border: "none",
                  background: importing ? "var(--pf-t--global--background--color--disabled--default)" : "var(--pf-t--global--background--color--primary--default)",
                  color: "#fff", cursor: importing ? "not-allowed" : "pointer", fontWeight: 500,
                }}
              >
                {importing ? "Importing..." : "Import"}
              </button>
            </div>
          </div>
        </div>
      )}
      {showExportModal && (
        <div style={{ position: "fixed", inset: 0, zIndex: 10000, display: "flex",
          alignItems: "center", justifyContent: "center", background: "rgba(0,0,0,0.6)" }}
          onClick={(e) => { if (e.target === e.currentTarget) setShowExportModal(false); }}>
          <div style={{ background: "var(--pf-t--global--background--color--primary--default)",
            borderRadius: 12, padding: 24, width: 480, maxWidth: "90vw",
            boxShadow: "0 8px 32px rgba(0,0,0,0.5)",
            border: "1px solid var(--pf-t--global--border--color--default)" }}>
            <div style={{ fontWeight: 600, fontSize: 16, marginBottom: 12 }}>Export Template</div>
            <div style={{
              fontSize: 13, padding: "12px 16px", borderRadius: 8, marginBottom: 16,
              background: "rgba(251,191,36,0.08)", border: "1px solid rgba(251,191,36,0.25)",
              color: "var(--pf-t--global--text--color--regular)", lineHeight: 1.5,
            }}>
              This exports the infrastructure topology (VMs, networks, disk sizes) with references to library items (disk images and ISOs). On import, referenced library items are validated to ensure they exist. To capture a fully built environment including disk images and installed software, use <strong>Save as Pattern</strong> instead.
            </div>
            <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
              <button onClick={() => setShowExportModal(false)}
                style={{ padding: "8px 16px", borderRadius: 6, border: "1px solid var(--pf-t--global--border--color--default)",
                  background: "transparent", color: "var(--pf-t--global--text--color--regular)", cursor: "pointer" }}>
                Cancel
              </button>
              <button
                onClick={async () => {
                  const resp = await fetch(`/api/v1/projects/${projectId}/export-template`);
                  if (!resp.ok) return;
                  const yaml = await resp.text();
                  const blob = new Blob([yaml], { type: "text/yaml" });
                  const url = URL.createObjectURL(blob);
                  const a = document.createElement("a");
                  a.href = url;
                  a.download = `${projectName || "project"}-template.yaml`;
                  a.click();
                  URL.revokeObjectURL(url);
                  setShowExportModal(false);
                }}
                style={{
                  padding: "8px 20px", borderRadius: 6,
                  border: "1px solid var(--pf-t--global--border--color--default)",
                  background: "var(--pf-t--global--background--color--primary--default)",
                  color: "#fff", cursor: "pointer", fontWeight: 500,
                }}
              >
                Download YAML
              </button>
            </div>
          </div>
        </div>
      )}
      <style>{`
        @keyframes pulse {
          0%, 100% { opacity: 1; }
          50% { opacity: 0.5; }
        }
      `}</style>
    </ReactFlowProvider>
  );
}
