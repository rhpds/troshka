"use client";

import React, { useEffect, useMemo, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { ReactFlowProvider } from "@xyflow/react";
import Canvas from "@/components/canvas/Canvas";
import Palette from "@/components/canvas/Palette";
import PropertiesPanel from "@/components/canvas/PropertiesPanel";
import StartOrderPanel from "@/components/canvas/StartOrderPanel";
import ExternalIpsPanel from "@/components/canvas/ExternalIpsPanel";
import { useCanvasStore, computeTopologyDirty, computeTopologyDiff, setLatestVmStates, setLatestContainerStates, _saveTopologyToApi, applyDeployedTopologyFromServer, resetClustersForOcpRedeploy, type ExternalIp, type TopologyDiffEntry } from "@/stores/canvasStore";
import { healClusterTopology } from "@/components/canvas/clusterTopologyHeal";
import { reconcileDeployedClusters } from "@/components/canvas/clusterNetworkBackfill";
import ReconfigureWarningModal from "@/components/canvas/ReconfigureWarningModal";
import SavePatternModal from "@/components/canvas/SavePatternModal";
import RunWorkloadModal from "@/components/canvas/RunWorkloadModal";
import WorkloadRunsModal from "@/components/canvas/WorkloadRunsModal";
import WorkloadRunDetailModal from "@/components/canvas/WorkloadRunDetailModal";
import WorkloadStatusChip from "@/components/canvas/WorkloadStatusChip";
import {
  findInflightRun,
  findFailedChainRun,
  formatWorkloadChipLabel,
  normalizeWorkloadRoles,
  succeededRoleSet,
  type WorkloadRunSummary,
} from "@/components/canvas/workloadStatus";
import SnapshotVMModal from "@/components/canvas/SnapshotVMModal";
import { useVmStateSocket } from "@/hooks/useVmStateSocket";
import AlertModal from "@/components/AlertModal";
import ConfirmModal from "@/components/ConfirmModal";
import RedeployOcpModal from "@/components/RedeployOcpModal";
import { appConfirm } from "@/lib/confirm";
import { POWER_WARN_MESSAGE, HIBERNATE_WARN_SUFFIX } from "@/lib/powerWarn";
import {
  allOcpClustersReady,
  collectOcpClusters,
  type OcpClusterStatus,
  type OcpRedeployMode,
} from "@/lib/redeployOcp";
import { formatApiDetail } from "@/lib/apiError";
import { resolveShowroomUrl } from "@/lib/routeUrl";
import UserIcon from "@patternfly/react-icons/dist/esm/icons/user-icon";

function formatRunningElapsed(totalSeconds: number): string {
  const total = Math.max(0, Math.floor(totalSeconds));
  const d = Math.floor(total / 86400);
  const h = Math.floor((total % 86400) / 3600);
  const m = Math.floor((total % 3600) / 60);
  const parts: string[] = [];
  if (d > 0) parts.push(`${d}d`);
  if (h > 0) parts.push(`${h}h`);
  parts.push(`${m}m`);
  return parts.join(" ");
}

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
  const [showWorkloadModal, setShowWorkloadModal] = useState(false);
  const [showWorkloadRuns, setShowWorkloadRuns] = useState(false);
  const [openRunId, setOpenRunId] = useState<string | null>(null);
  const [workloadRuns, setWorkloadRuns] = useState<WorkloadRunSummary[]>([]);
  const topologyWorkloads = useCanvasStore((s) => s.topologyWorkloads);
  const [runWorkloadTarget, setRunWorkloadTarget] = useState<{
    mode: "cluster" | "vms";
    clusterIds?: string[];
    vmNames?: string[];
  } | null>(null);
  const [snapshotTarget, setSnapshotTarget] = useState<{ vmId: string; vmName: string; isRunning: boolean } | null>(null);
  const [showImportModal, setShowImportModal] = useState(false);
  const [showExportModal, setShowExportModal] = useState(false);
  const [exportPasswordMode, setExportPasswordMode] = useState<"current" | "custom" | "none">("current");
  const [exportCustomPassword, setExportCustomPassword] = useState("");
  const [exportIncludeIds, setExportIncludeIds] = useState(false);
  const [importYaml, setImportYaml] = useState("");
  const [importError, setImportError] = useState("");
  const [importing, setImporting] = useState(false);
  const [importPassword, setImportPassword] = useState(() => {
    const chars = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789";
    return Array.from({ length: 12 }, () => chars[Math.floor(Math.random() * chars.length)]).join("");
  });
  const [importSshKeyId, setImportSshKeyId] = useState("");
  const [importSshKeys, setImportSshKeys] = useState<Array<{ id: string; name: string }>>([]);
  const [importAutoDeploy, setImportAutoDeploy] = useState(true);
  const [projectName, setProjectName] = useState("");
  const [projectDesc, setProjectDesc] = useState("");
  const [projectGuid, setProjectGuid] = useState("");
  const [projectState, setProjectState] = useState("");
  const [projectHostId, setProjectHostId] = useState("");
  const [offAction, setOffAction] = useState<"stop" | "pause" | "hibernate">("stop");
  const [powerWarnDismissed, setPowerWarnDismissed] = useState(false);
  const [supportsHibernate, setSupportsHibernate] = useState(true);
  const [showOffMenu, setShowOffMenu] = useState(false);
  const offMenuRef = React.useRef<HTMLDivElement>(null);
  const [ownerEmail, setOwnerEmail] = useState<string | null>(null);
  const [hostPlacement, setHostPlacement] = useState<{
    provider: string | null;
    host: string | null;
  }>({ provider: null, host: null });
  const [autoStopMinutes, setAutoStopMinutes] = useState<number | null>(null);
  const [autoDeleteMinutes, setAutoDeleteMinutes] = useState<number | null>(null);
  const [autoStopExpiresAt, setAutoStopExpiresAt] = useState<string | null>(null);
  const [lifetimeExpiresAt, setLifetimeExpiresAt] = useState<string | null>(null);
  const [autoStopped, setAutoStopped] = useState(false);
  const [budgetUsd, setBudgetUsd] = useState<number | null>(null);
  const [showBudgetModal, setShowBudgetModal] = useState(false);
  const [budgetModalDraft, setBudgetModalDraft] = useState("");
  const [liveSpend, setLiveSpend] = useState<{
    total_usd: number;
    budget_usd: number | null;
    budget_warned: boolean;
    budget_stopped: boolean;
    running_since?: string | null;
    running_until?: string | null;
    running_seconds?: number;
  } | null>(null);
  const [spendNowMs, setSpendNowMs] = useState(() => Date.now());
  const [spendFetchedAt, setSpendFetchedAt] = useState(() => Date.now());
  const [clockTarget, setClockTarget] = useState<string | null>(null);
  const [guestExecEnabled, setGuestExecEnabled] = useState(true);
  const [alertMsg, setAlertMsg] = useState<string | null>(null);
  const [showDeleteModal, setShowDeleteModal] = useState(false);
  const [redeployOcpModal, setRedeployOcpModal] = useState<{
    clusters: OcpClusterStatus[];
    allReady: boolean;
  } | null>(null);
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
        setOffAction((data.off_action as "stop" | "pause" | "hibernate") || "stop");
        setPowerWarnDismissed(!!data.power_warn_dismissed);
        setSupportsHibernate(data.supports_hibernate !== false);
        setOwnerEmail(data.owner_email || null);
        {
          const providerType =
            data.host_provider_type ||
            data.provider_type ||
            null;
          const host =
            data.host_ip ||
            (typeof data.host_instance_id === "string" &&
            !data.host_instance_id.startsWith("http")
              ? data.host_instance_id
              : null) ||
            (data.host_id ? String(data.host_id).slice(0, 8) : null);
          setHostPlacement({ provider: providerType, host });
        }
        useCanvasStore.setState({
          providerType: data.provider_type || null,
          clusterCapabilities: data.cluster_capabilities || null,
        });
        setDeployError(data.deploy_error || null);
        if (data.deploy_progress) setDeployProgress(data.deploy_progress);
        setAutoStopMinutes(data.auto_stop_minutes ?? null);
        setAutoDeleteMinutes(data.auto_delete_minutes ?? null);
        setAutoStopExpiresAt(data.auto_stop_expires_at ?? null);
        setLifetimeExpiresAt(data.lifetime_expires_at ?? null);
        setAutoStopped(!!data.auto_stopped);
        setBudgetUsd(data.budget_usd ?? null);
        setClockTarget(data.clock_target ?? null);
        setGuestExecEnabled(data.guest_exec_enabled !== false);
        const clusterStatuses = (
          (data.deployed_topology || data.topology)?.clusters || []
        )
          .map((c: { ocpInstallStatus?: string }) => c.ocpInstallStatus)
          .filter(Boolean) as string[];
        const allClusterError =
          clusterStatuses.length > 0 && clusterStatuses.every((s) => s === "error");
        // Project ocp_status can lag at monitoring after a real cluster failure.
        if (allClusterError) setOcpStatus("error");
        else if (data.ocp_status) setOcpStatus(data.ocp_status);
        if (data.ocp_status_detail) setOcpStatusDetail(data.ocp_status_detail);
        if (data.ocp_install_elapsed != null) setOcpInstallElapsed(data.ocp_install_elapsed);
        setHasDeployedTopology(!!(data.deployed_topology?.nodes?.length));
      })
      .catch(() => {});
  }, [projectId]);

  useEffect(() => {
    if (!projectId) return;
    const loadSpend = () => {
      fetch(`/api/v1/projects/${projectId}/metering`)
        .then((r) => (r.ok ? r.json() : null))
        .then((data) => {
          if (data) {
            setLiveSpend(data);
            setSpendFetchedAt(Date.now());
          }
        })
        .catch(() => {});
    };
    loadSpend();
    const interval = setInterval(loadSpend, 30000);
    return () => clearInterval(interval);
  }, [projectId]);

  useEffect(() => {
    if (!liveSpend?.running_since || liveSpend.running_until) return;
    if (
      projectState === "stopped" ||
      projectState === "stopping" ||
      projectState === "error"
    ) {
      return;
    }
    const tick = () => setSpendNowMs(Date.now());
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [liveSpend?.running_since, liveSpend?.running_until, projectState]);

  useEffect(() => {
    fetch("/api/v1/auth/me").then(r => r.ok ? r.json() : {}).then((d: { role?: string; email?: string }) => {
      setIsAdmin(d.role === "admin");
      if (d.role === "admin") {
        Promise.all([
          fetch("/api/v1/hosts/").then(r => r.ok ? r.json() : []),
          fetch("/api/v1/providers/").then(r => r.ok ? r.json() : []),
        ]).then(([hosts, providers]) => {
          const provMap = new Map<string, string>();
          for (const p of providers) provMap.set(p.id, p.name);
          const active = hosts
            .filter((h: any) => h.state === "active" && h.agent_status === "connected" && h.host_type !== "pattern_buffer" && h.accepting_work !== false)
            .map((h: any) => ({ ...h, provider_name: provMap.get(h.provider_id) || null }));
          setAvailableHosts(active);
        });
      }
    });
  }, []);

  const [deployProgress, setDeployProgress] = useState<{ step: string; detail: string; items?: string[] } | null>(null);
  const [ocpStatus, setOcpStatus] = useState<string | null>(null);
  const [ocpStatusDetail, setOcpStatusDetail] = useState<string | null>(null);
  const [ocpInstallElapsed, setOcpInstallElapsed] = useState<number | null>(null);

  // WebSocket → project state
  const prevStateRef = React.useRef(projectState);
  useEffect(() => {
    prevStateRef.current = projectState;
  }, [projectState]);

  useEffect(() => {
    if (!ws.projectState) return;
    const prev = prevStateRef.current;
    setProjectState(ws.projectState);
    setDeployError(ws.deployError || null);
    if (ws.projectState === "deploying") {
      setOcpStatus(null);
      setOcpInstallElapsed(null);
      // Prior install milestones (control-plane-usable, etc.) must not linger.
      setDeployProgress(null);
      // Drop stale Ready/complete from the last install so the status button
      // shows Deploying/Re-Cert for the new cycle.
      const store = useCanvasStore.getState();
      useCanvasStore.setState({
        clusterOcpPhases: {},
        clusters: resetClustersForOcpRedeploy(store.clusters),
      });
    }
    if (ws.projectState === "active" && prev !== "active") {
      void useCanvasStore.getState().loadProject(projectId);
      setDeployProgress(null);
    }
  }, [ws.projectState, ws.deployError, projectId]);

  // WebSocket → timer expiry updates (from project-state messages after stop/start/deploy)
  useEffect(() => {
    if (ws.autoStopExpiresAt !== undefined) setAutoStopExpiresAt(ws.autoStopExpiresAt);
    if (ws.lifetimeExpiresAt !== undefined) setLifetimeExpiresAt(ws.lifetimeExpiresAt);
    if (ws.autoStopped !== undefined) setAutoStopped(ws.autoStopped);
  }, [ws.autoStopExpiresAt, ws.lifetimeExpiresAt, ws.autoStopped]);

  // WebSocket → deploy progress (only update, never clear during deploy)
  useEffect(() => {
    if (ws.deployProgress) setDeployProgress(ws.deployProgress);
  }, [ws.deployProgress]);

  // Workload runs: seed on active + refresh when WS progress arrives
  const refreshWorkloadRuns = React.useCallback(() => {
    fetch(`/api/v1/projects/${projectId}/workloads`)
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (!Array.isArray(data)) return;
        setWorkloadRuns(
          data.map((row: WorkloadRunSummary) => ({
            id: row.id,
            role_fqcn: row.role_fqcn ?? null,
            status: row.status,
            created_at: row.created_at || "",
            error: row.error ?? null,
          })),
        );
      })
      .catch(() => {});
  }, [projectId]);

  useEffect(() => {
    if (projectState !== "active") {
      setWorkloadRuns([]);
      return;
    }
    refreshWorkloadRuns();
  }, [projectState, refreshWorkloadRuns]);

  useEffect(() => {
    if (!ws.workloadProgress) return;
    refreshWorkloadRuns();
  }, [ws.workloadProgress, refreshWorkloadRuns]);

  // Poll while a run is in flight so the chip clears on terminal status
  // (finalize does not always publish a final workload-progress event).
  useEffect(() => {
    if (projectState !== "active") return;
    const inflight = findInflightRun(workloadRuns);
    if (!inflight) return;
    const interval = setInterval(refreshWorkloadRuns, 10000);
    return () => clearInterval(interval);
  }, [projectState, workloadRuns, refreshWorkloadRuns]);

  const inflightWorkload = useMemo(
    () => findInflightRun(workloadRuns),
    [workloadRuns],
  );

  // When the template chain advances, keep an open log modal on the new run.
  useEffect(() => {
    if (!openRunId || !inflightWorkload) return;
    if (inflightWorkload.id === openRunId) return;
    const openRun = workloadRuns.find((r) => r.id === openRunId);
    if (!openRun) return;
    // Only auto-follow if the open run finished (chain handoff). Don't steal
    // focus if the user opened a different still-running run.
    if (["pending", "queued", "running"].includes(openRun.status)) return;
    setOpenRunId(inflightWorkload.id);
  }, [openRunId, inflightWorkload, workloadRuns]);

  const chainRoles = useMemo(
    () => normalizeWorkloadRoles(topologyWorkloads),
    [topologyWorkloads],
  );

  const failedChainWorkload = useMemo(() => {
    if (inflightWorkload) return null;
    return findFailedChainRun(workloadRuns, chainRoles);
  }, [inflightWorkload, workloadRuns, chainRoles]);

  const chipWorkload = inflightWorkload || failedChainWorkload;

  const workloadChipLabel = useMemo(() => {
    if (!chipWorkload) return null;
    return formatWorkloadChipLabel({
      roleFqcn: chipWorkload.role_fqcn,
      chainRoles,
      succeededRoles: succeededRoleSet(workloadRuns),
    });
  }, [chipWorkload, chainRoles, workloadRuns]);

  const [chainRetrying, setChainRetrying] = useState(false);
  const [chainCancelling, setChainCancelling] = useState(false);

  const storeClusters = useCanvasStore((s) => s.clusters);

  // Block disruptive actions while OCP install or topology workloads are in flight.
  // Prefer per-cluster stamps when present: project ocp_status can lag at
  // "monitoring" after a finalized cluster error (held ops-pod resume flap).
  const clusterInstallStatuses = storeClusters
    .map((c) => c.ocpInstallStatus)
    .filter((s): s is string => !!s);
  const clustersTerminal =
    clusterInstallStatuses.length > 0 &&
    clusterInstallStatuses.every((s) => s === "ready" || s === "error");
  const ocpBusy =
    !clustersTerminal &&
    !!ocpStatus &&
    !["ready", "error", "warning", "none", "complete"].includes(ocpStatus);
  const disruptiveActionsDisabled = !!inflightWorkload || ocpBusy;
  const disruptiveDisabledTitle = inflightWorkload
    ? "Wait for workloads to finish or fail"
    : ocpBusy
      ? "Wait for OpenShift install to finish or fail"
      : undefined;
  const disruptiveBtnStyle = disruptiveActionsDisabled
    ? { opacity: 0.4, cursor: "not-allowed" as const }
    : { opacity: 0.85 };

  // Project stays "active" while individual VMs are paused/hibernated (off_action
  // keeps the project up) — surface a toolbar Start so the user isn't limited to
  // resuming one VM card at a time.
  const resumableVmNodes = nodes.filter((n) => {
    if (n.type !== "vmNode") return false;
    const status = (n.data as Record<string, unknown>).status;
    return status === "paused" || status === "hibernated";
  });
  const [resumingAll, setResumingAll] = useState(false);
  const handleResumeAll = async () => {
    if (disruptiveActionsDisabled || resumingAll) return;
    setResumingAll(true);
    try {
      await Promise.all(
        resumableVmNodes.map((n) => {
          const status = (n.data as Record<string, unknown>).status;
          const action = status === "paused" ? "unpause" : "start";
          return fetch(`/api/v1/projects/${projectId}/vms/${n.id}/${action}`, {
            method: "POST",
          }).catch(() => {});
        }),
      );
    } finally {
      setResumingAll(false);
    }
  };

  const OFF_ACTION_LABEL: Record<"stop" | "pause" | "hibernate", string> = {
    stop: "■ Stop",
    pause: "⏸ Pause",
    hibernate: "💤 Hibernate",
  };
  useEffect(() => {
    if (!showOffMenu) return;
    const handler = (e: MouseEvent) => {
      if (offMenuRef.current && !offMenuRef.current.contains(e.target as HTMLElement)) {
        setShowOffMenu(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [showOffMenu]);

  const handleSetOffAction = async (action: "stop" | "pause" | "hibernate") => {
    setShowOffMenu(false);
    if (action === "hibernate" && !supportsHibernate) return;
    const previous = offAction;
    if (action === previous) return;
    setOffAction(action);
    try {
      const r = await fetch(`/api/v1/projects/${projectId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ off_action: action }),
      });
      if (!r.ok) throw new Error("off_action patch failed");
    } catch {
      setOffAction(previous);
    }
  };

  const handleOffClick = async () => {
    if (disruptiveActionsDisabled) return;
    if ((offAction === "pause" || offAction === "hibernate") && !powerWarnDismissed) {
      const checkboxRef = { current: false };
      const message =
        offAction === "hibernate" ? POWER_WARN_MESSAGE + HIBERNATE_WARN_SUFFIX : POWER_WARN_MESSAGE;
      const confirmed = await appConfirm({
        title: offAction === "hibernate" ? "Hibernate environment?" : "Pause environment?",
        message,
        confirmLabel: offAction === "hibernate" ? "Hibernate" : "Pause",
        checkboxLabel: "Don't show this again for this project",
        checkboxRef,
      });
      if (!confirmed) return;
      if (checkboxRef.current) {
        setPowerWarnDismissed(true);
        useCanvasStore.setState({ powerWarnDismissed: true });
        fetch(`/api/v1/projects/${projectId}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ power_warn_dismissed: true }),
        }).catch(() => {});
      }
    }
    fetch(`/api/v1/projects/${projectId}/stop`, { method: "POST" })
      .then(() => {
        // Pause finalizes back to "active" via WS (project never reaches
        // "stopped"); setting "stopping" here optimistically can lose a
        // race with that WS update and get stuck. stop/hibernate both
        // finalize to "stopped", so the optimistic transition is safe and
        // matches the backend's own synchronous state write.
        if (offAction !== "pause") setProjectState("stopping");
      });
  };

  // REST fallback: poll deploy progress when WS isn't delivering updates
  useEffect(() => {
    if (!["deploying", "reconfiguring", "starting", "stopping"].includes(projectState)) return;
    const interval = setInterval(() => {
      fetch(`/api/v1/projects/${projectId}`)
        .then((r) => r.ok ? r.json() : null)
        .then((data) => {
          if (data?.deploy_progress) setDeployProgress(data.deploy_progress);
          if (data?.deploy_error) setDeployError(data.deploy_error);
          if (data?.state && data.state !== projectState) {
            const prev = projectState;
            setProjectState(data.state);
            if (data.state === "active" && prev !== "active") {
              void useCanvasStore.getState().loadProject(projectId);
              setDeployProgress(null);
            }
          }
        })
        .catch(() => {});
    }, 5000);
    return () => clearInterval(interval);
  }, [projectState, projectId]);

  // WebSocket → topology update from another session / server stamp
  useEffect(() => {
    if (!ws.topologyUpdate) return;
    const update = ws.topologyUpdate;
    const topo = update.topology || update;
    const store = useCanvasStore.getState();
    if (topo.nodes && topo.edges) {
      const preservedEndpoints = new Map<string, unknown[]>();
      for (const n of store.nodes) {
        const data = (n.data || {}) as Record<string, unknown>;
        const eps = data.externalEndpoints as unknown[] | undefined;
        if (n.type === "networkNode" && data.subtype === "gateway" && eps?.length) {
          preservedEndpoints.set(n.id, eps);
        }
      }
      const mergedNodes = topo.nodes.map((n: { id: string; data?: Record<string, unknown> }) => {
        const data = n.data || {};
        if (data.subtype !== "gateway") return n;
        const incoming = data.externalEndpoints as unknown[] | undefined;
        const preserved = preservedEndpoints.get(n.id);
        if (incoming?.length) return n;
        if (preserved?.length) {
          return { ...n, data: { ...data, externalEndpoints: preserved } };
        }
        return n;
      });
      const healed = healClusterTopology({
        nodes: mergedNodes as typeof store.nodes,
        edges: topo.edges,
        clusters: Array.isArray(topo.clusters) ? topo.clusters : store.clusters,
        deployedClusters: store.deployedClusterRows,
      });
      const currentKey =
        store.nodes.map((n) => `${n.id}:${JSON.stringify(n.data)}`).join("|")
        + "||cl:" + JSON.stringify(store.clusters);
      const incomingKey =
        healed.nodes.map((n) => `${n.id}:${JSON.stringify(n.data)}`).join("|")
        + "||cl:" + JSON.stringify(healed.clusters);
      if (currentKey !== incomingKey) {
        useCanvasStore.setState({
          nodes: healed.nodes,
          edges: healed.edges,
          clusters: healed.clusters,
        });
      }
    }
    // Server-stamped baseline (e.g. deferred-worker powerOnAtDeploy flip) so
    // Apply Changes does not read canvas vs deployed drift as a user edit.
    if (update.deployed_topology?.nodes?.length) {
      applyDeployedTopologyFromServer(update.deployed_topology, projectState);
      const depClusters = update.deployed_topology.clusters;
      if (Array.isArray(depClusters) && depClusters.length > 0) {
        const storeNow = useCanvasStore.getState();
        useCanvasStore.setState({
          deployedClusterRows: depClusters as typeof storeNow.deployedClusterRows,
          clusters: reconcileDeployedClusters(storeNow.clusters, depClusters),
        });
      }
    }
  }, [ws.topologyUpdate, projectState]);

  // WebSocket → EIP allocation during/after deploy
  useEffect(() => {
    if (!ws.externalIpsUpdate?.length) return;
    const incoming = ws.externalIpsUpdate;
    const byId = new Map(incoming.map((e) => [e.id, e]));
    const current = useCanvasStore.getState().externalIps;
    const merged: ExternalIp[] = current.map((e) => {
      const upd = byId.get(e.id);
      if (!upd) return e;
      return {
        ...e,
        ...upd,
        ip: upd.ip ?? e.ip,
        state:
          upd.state === "pending" || upd.state === "allocated" || upd.state === "associated"
            ? upd.state
            : e.state,
      };
    });
    for (const e of incoming) {
      if (merged.some((m) => m.id === e.id)) continue;
      merged.push({
        id: e.id,
        name: e.name,
        ip: e.ip ?? "",
        ...(e._private_ip ? { _private_ip: e._private_ip } : {}),
        ...(e.state === "pending" || e.state === "allocated" || e.state === "associated"
          ? { state: e.state }
          : {}),
      });
    }
    useCanvasStore.getState().setExternalIps(merged);
  }, [ws.externalIpsUpdate]);

  useEffect(() => {
    if (ws.ocpHealth?.phase === "ready") setOcpStatus("ready");
    else if (ws.ocpHealth?.phase === "warning") setOcpStatus("warning");
    else if (ws.ocpHealth?.phase === "error") setOcpStatus("error");
    else if (ws.ocpHealth?.phase === "timeout") setOcpStatus("monitoring");
    // Never demote a finalized install error to monitoring — a held ops pod
    // can still publish in-progress health frames and would flap Republish off.
    else if (
      ws.ocpHealth &&
      ocpStatus !== "ready" &&
      ocpStatus !== "warning" &&
      ocpStatus !== "error"
    ) {
      setOcpStatus("monitoring");
    }
  }, [ws.ocpHealth, ocpStatus]);

  // Project-level ready means no cluster is still monitoring — heal canvas stamps
  // that were left on "monitoring" when finalize wrote ready without a live WS push.
  // Re-run when clusters load (ocpStatus can be ready before loadProject finishes).
  useEffect(() => {
    if (ocpStatus !== "ready") return;
    const store = useCanvasStore.getState();
    let changed = false;
    const phases = { ...store.clusterOcpPhases };
    const clusters = store.clusters.map((c) => {
      if (c.ocpInstallStatus === "ready" || c.ocpInstallStatus === "error") return c;
      changed = true;
      const key = c.id || c.name;
      if (key) phases[key] = "complete";
      return { ...c, ocpInstallStatus: "ready" };
    });
    if (changed) useCanvasStore.setState({ clusters, clusterOcpPhases: phases });
  }, [ocpStatus, storeClusters]);

  const resolvedOcpHealth = useMemo(() => {
    const fallback =
      ocpStatus === "ready"
        ? {
            phase: "ready",
            detail:
              ocpInstallElapsed != null
                ? `cluster ready (${Math.floor(ocpInstallElapsed / 60)}m ${(ocpInstallElapsed % 60).toString().padStart(2, "0")}s)`
                : "cluster ready",
          }
        : ocpStatus === "error"
          ? { phase: "error", detail: "install failed" }
          : ocpStatus === "warning"
            ? {
                phase: "warning",
                detail:
                  ocpInstallElapsed != null
                    ? `cluster issues (${Math.floor(ocpInstallElapsed / 60)}m ${(ocpInstallElapsed % 60).toString().padStart(2, "0")}s)`
                    : "cluster issues",
              }
            : ocpStatus === "monitoring"
              ? { phase: "ssh", detail: "monitoring..." }
              : null;

    if (!ws.ocpHealth) return fallback;
    return ws.ocpHealth;
  }, [ws.ocpHealth, ocpStatus, ocpInstallElapsed]);

  // Mirror OCP health + per-cluster phases into the canvas store.
  useEffect(() => {
    useCanvasStore.setState({ ocpHealth: resolvedOcpHealth });
  }, [resolvedOcpHealth]);

  useEffect(() => {
    if (!Object.keys(ws.clusterOcpPhases).length) return;
    const phases = ws.clusterOcpPhases;
    const store = useCanvasStore.getState();
    const clusters = store.clusters.map((c) => {
      const key = c.id || c.name;
      const phase = phases[key] || (c.id && phases[c.id]) || (c.name && phases[c.name]);
      if (!phase) return c;
      const status =
        phase === "complete"
          ? "ready"
          : phase === "failed" || phase === "cancelled" || phase === "timeout"
            ? "error"
            : "monitoring";
      // While a new deploy/redeploy is running, in-progress frames must replace
      // a leftover Ready. Only protect ready→monitoring once the project is idle
      // (finalize stamped ready while a late waiting frame can still arrive).
      if (
        c.ocpInstallStatus === "ready" &&
        status === "monitoring" &&
        projectState !== "deploying"
      ) {
        return c;
      }
      // Stored error is terminal while idle — do not let a held ops pod's
      // waiting frames flap Republish off. During deploy/redeploy, in-progress
      // frames must replace leftover error so Status & Log is not stuck ERROR.
      if (
        c.ocpInstallStatus === "error" &&
        status === "monitoring" &&
        projectState !== "deploying"
      ) {
        return c;
      }
      if (c.ocpInstallStatus === status) return c;
      return { ...c, ocpInstallStatus: status };
    });
    useCanvasStore.setState({ clusterOcpPhases: phases, clusters });
  }, [ws.clusterOcpPhases, projectState]);

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
        if (data.container_states && Object.keys(data.container_states).length > 0) {
          setLatestContainerStates(data.container_states);
        }
        const allIds = new Set<string>([
          ...Object.keys(data.states || {}),
          ...Object.keys(data.container_states || {}),
        ]);
        if (allIds.size > 0) {
          setDeployedVmIds(allIds);
          useCanvasStore.setState({ deployedVmIds: allIds });
        }
      } catch { /* ignore */ }
    };
    const timer = setInterval(poll, 5000);
    poll();
    return () => clearInterval(timer);
  }, [projectId, projectState]);

  const [reconfigWarnings, setReconfigWarnings] = useState<{ type: "iso" | "disk"; storageName: string; vmName: string; vmId: string }[] | null>(null);
  const [reconfigDiff, setReconfigDiff] = useState<TopologyDiffEntry[]>([]);

  const saveTopology = async () => {
    await _saveTopologyToApi(projectId, useCanvasStore.getState());
  };

  const doReconfigure = async (restartVmIds?: string[]) => {
    setReconfigWarnings(null);
    setReconfigDiff([]);
    setDeployError(null);
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
    // Semantic diff of everything that will be applied (same source of truth as
    // the Apply Changes dirty state), shown in the review modal alongside any
    // ISO/disk data-loss warnings.
    const diff = computeTopologyDiff(cur);
    if (diff.length > 0 || changes.length > 0) {
      setReconfigDiff(diff);
      setReconfigWarnings(changes);
    } else {
      // Nothing to preview (shouldn't happen while the button is enabled) — apply.
      doReconfigure();
    }
  };

  const [toast, setToast] = useState<string | null>(null);
  const [applyingChanges, setApplyingChanges] = useState(false);
  const [showMigrate, setShowMigrate] = useState(false);
  const [availableHosts, setAvailableHosts] = useState<{id: string; instance_id: string | null; ip_address: string; used_vcpus: number; total_vcpus: number; used_ram_mb: number; total_ram_mb: number; storage_pool_id: string | null; provider_id: string | null; provider_name: string | null; provider_type: string | null; host_type?: string}[]>([]);
  const topologyPlacement = useCanvasStore((s) => s.topologyPlacement);
  const requiresKubevirt = !!(
    topologyPlacement?.requires_kubevirt || topologyPlacement?.requiresKubevirt
  );
  const deployHosts = useMemo(
    () =>
      requiresKubevirt
        ? availableHosts.filter((h) => h.host_type === "kubevirt-cluster")
        : availableHosts,
    [availableHosts, requiresKubevirt]
  );
  const [migrateTarget, setMigrateTarget] = useState("");
  const [migrating, setMigrating] = useState(false);
  const [migrateSourceHost, setMigrateSourceHost] = useState<{instance_id: string | null; ip_address: string} | null>(null);
  const [isAdmin, setIsAdmin] = useState(false);

  useEffect(() => {
    if (!deployHostId) return;
    if (deployHostId.startsWith("provider:")) {
      const pid = deployHostId.slice(9);
      if (!deployHosts.some((h) => h.provider_id === pid)) setDeployHostId("");
      return;
    }
    if (!deployHosts.some((h) => h.id === deployHostId)) setDeployHostId("");
  }, [deployHostId, deployHosts]);

  const showToast = (msg: string, duration = 4000) => {
    setToast(msg);
    setTimeout(() => setToast(null), duration);
  };

  const resumeWorkloadChain = React.useCallback(async () => {
    setChainRetrying(true);
    try {
      const r = await fetch(`/api/v1/projects/${projectId}/workloads/resume`, {
        method: "POST",
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) {
        showToast(
          typeof data.detail === "string"
            ? data.detail
            : "Could not resume workload chain",
        );
        return;
      }
      if (data.id) setOpenRunId(data.id);
      refreshWorkloadRuns();
    } catch {
      showToast("Could not resume workload chain");
    } finally {
      setChainRetrying(false);
    }
  }, [projectId, refreshWorkloadRuns]);

  const cancelInflightWorkload = React.useCallback(async () => {
    if (!inflightWorkload) return;
    setChainCancelling(true);
    try {
      const r = await fetch(`/api/v1/workloads/${inflightWorkload.id}/cancel`, {
        method: "POST",
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) {
        showToast(
          typeof data.detail === "string" ? data.detail : "Could not cancel workload",
        );
        return;
      }
      showToast("Workload cancelled");
      refreshWorkloadRuns();
    } catch {
      showToast("Could not cancel workload");
    } finally {
      setChainCancelling(false);
    }
  }, [inflightWorkload, refreshWorkloadRuns]);

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
      h.agent_status === "connected" &&
      h.accepting_work !== false
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
      setAlertMsg(data.detail || "Migration failed");
    }
  };

  const vmCount = nodes.filter((n) => n.type === "vmNode").length;
  const spendPaused =
    projectState === "stopped" ||
    projectState === "stopping" ||
    projectState === "error" ||
    Boolean(liveSpend?.running_until);
  const spendElapsedSec =
    (liveSpend?.running_seconds || 0) +
    (spendPaused || !liveSpend?.running_since
      ? 0
      : Math.max(0, (spendNowMs - spendFetchedAt) / 1000));
  const spendLabel =
    projectState === "error" ? "Paused" : spendPaused ? "Stopped" : "Running";
  const showroomUrl = useMemo(() => {
    const deployed =
      typeof window !== "undefined"
        ? (
            window as unknown as {
              __deployedTopology?: {
                _showroom_url?: string;
                _showroom_access_token?: string;
                nodes?: Array<{ data?: Record<string, unknown> }>;
              };
            }
          ).__deployedTopology
        : undefined;
    return resolveShowroomUrl(
      nodes as Array<{ data?: Record<string, unknown> }>,
      deployed || null,
    );
    // Recompute when gateway endpoints / project state change; also when the
    // deployed snapshot is applied to window (nodes refresh after loadProject).
  }, [nodes, projectState, hasDeployedTopology]);
  const containerCount = nodes.filter((n) => n.type === "containerNode").length;
  const netCount = nodes.filter((n) => n.type === "networkNode" && (n.data as Record<string, any>).subtype === "network").length;
  const diskCount = nodes.filter((n) => n.type === "storageNode").length;

  const handlePublish = async () => {
    // Host list is only fetched for admins; non-admins rely on backend placement.
    if (isAdmin && requiresKubevirt && deployHosts.length === 0) {
      setAlertMsg(
        "No KubeVirt cluster host is available — this project requires kubevirt-cluster placement.",
      );
      return;
    }
    if (vmCount === 0 && containerCount === 0) {
      setAlertMsg("Add at least one VM or container before publishing.");
      return;
    }
    const parts = [];
    if (vmCount > 0) parts.push(`${vmCount} VM${vmCount !== 1 ? "s" : ""}`);
    if (containerCount > 0) parts.push(`${containerCount} container${containerCount !== 1 ? "s" : ""}`);
    parts.push(`${netCount} network${netCount !== 1 ? "s" : ""}`);
    parts.push(`${diskCount} disk${diskCount !== 1 ? "s" : ""}`);
    if (!(await appConfirm({
      title: "Deploy Environment",
      message: `Deploy this environment?\n\n${parts.join(", ")}\n\nThis will provision real infrastructure.`,
      confirmLabel: "Deploy",
    }))) return;

    try {
      await saveTopology();
      setProjectState("deploying");
      setDeployProgress(null);
      const deployParams = new URLSearchParams();
      if (deployHostId?.startsWith("provider:")) {
        deployParams.set("provider_id", deployHostId.slice(9));
      } else if (deployHostId) {
        deployParams.set("host_id", deployHostId);
      }
      const deployQs = deployParams.toString() ? `?${deployParams.toString()}` : "";
      const resp = await fetch(`/api/v1/projects/${projectId}/deploy${deployQs}`, {
        method: "POST",
      });
      const data = await resp.json();
      if (resp.ok) {
        useCanvasStore.setState({ topologyDirty: false });
        const userStr = localStorage.getItem("troshka-user");
        const isAdmin = userStr ? JSON.parse(userStr).role === "admin" : false;
        if (data.multi_host) {
          showToast(`Deploying across ${data.host_count} hosts`);
        } else {
          showToast(`Deploying ${data.requirements?.vm_count || ""} VM(s)${isAdmin ? ` to ${data.host_ip}` : ""}`);
        }
      } else {
        setProjectState("draft");
        setAlertMsg(data.detail || "Deployment failed");
      }
    } catch {
      setProjectState("draft");
      setAlertMsg("Failed to connect to server");
    }
  };

  const doRedeploy = async (ocpMode?: OcpRedeployMode) => {
    setProjectState("deploying");
    setDeployProgress(null);
    const store = useCanvasStore.getState();
    useCanvasStore.setState({
      clusterOcpPhases: {},
      clusters: resetClustersForOcpRedeploy(store.clusters),
    });
    const r = await fetch(`/api/v1/projects/${projectId}/redeploy`, {
      method: "POST",
      headers: ocpMode ? { "Content-Type": "application/json" } : undefined,
      body: ocpMode ? JSON.stringify({ ocp_mode: ocpMode }) : undefined,
    });
    if (r.ok) {
      useCanvasStore.setState({ deployedVmIds: new Set() });
      setDeployError(null);
    } else {
      setProjectState("active");
      const err = await r.json().catch(() => ({ detail: "Redeploy failed" }));
      setAlertMsg(err.detail || "Redeploy failed");
    }
  };

  const handleRepublish = async (message: string) => {
    const store = useCanvasStore.getState();
    // Authoritative install status lives on the server (finalize stamps ready on
    // topology + deployed_topology). Refresh before gating rebuild vs recert so a
    // stale canvas "monitoring" stamp cannot block Re-cert.
    let clustersForModal = store.clusters;
    try {
      const resp = await fetch(`/api/v1/projects/${projectId}`);
      if (resp.ok) {
        const data = await resp.json();
        const serverClusters =
          (data.deployed_topology?.clusters as typeof store.clusters | undefined) ||
          (data.topology?.clusters as typeof store.clusters | undefined) ||
          [];
        if (serverClusters.length > 0) {
          const byId = new Map(
            serverClusters.filter((c) => c?.id).map((c) => [String(c.id), c]),
          );
          const base =
            store.clusters.length > 0
              ? store.clusters
              : (serverClusters as typeof store.clusters);
          clustersForModal = base.map((c) => {
            const srv = byId.get(String(c.id));
            if (!srv?.ocpInstallStatus) return c;
            return { ...c, ocpInstallStatus: srv.ocpInstallStatus };
          });
          // Project-level ready is authoritative when every cluster should clear.
          if (data.ocp_status === "ready") {
            clustersForModal = clustersForModal.map((c) =>
              c.ocpInstallStatus === "error"
                ? c
                : { ...c, ocpInstallStatus: "ready" },
            );
          }
          const phases = { ...store.clusterOcpPhases };
          for (const c of clustersForModal) {
            const key = c.id || c.name;
            if (!key) continue;
            if (c.ocpInstallStatus === "ready") phases[key] = "complete";
            else if (c.ocpInstallStatus === "error") phases[key] = "failed";
          }
          useCanvasStore.setState({
            clusters: clustersForModal,
            clusterOcpPhases: phases,
            deployedClusterRows: serverClusters.map((c) => {
              const row = { ...c } as Record<string, unknown>;
              delete row._generatedInstallConfig;
              delete row._generatedAgentConfig;
              delete row.controlPlaneDisks;
              delete row.workerDisks;
              return row as unknown as (typeof store.clusters)[number];
            }),
          });
          if (data.ocp_status) setOcpStatus(data.ocp_status);
          if (data.ocp_status_detail) setOcpStatusDetail(data.ocp_status_detail);
        }
      }
    } catch {
      /* keep canvas clusters */
    }
    const clusters = collectOcpClusters({
      clusters: clustersForModal,
      nodes: store.nodes,
    });
    if (clusters.length > 0) {
      setRedeployOcpModal({
        clusters,
        allReady: allOcpClustersReady(clusters),
      });
      return;
    }
    if (!(await appConfirm({
      title: "Republish",
      message,
      confirmLabel: "Republish",
      variant: "danger",
    }))) return;
    await doRedeploy();
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
        <div className="project-action-bar-top">
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
          <span
            title={ownerEmail ? `Owned by ${ownerEmail}` : "System"}
            style={{
              fontSize: 11,
              padding: "1px 6px",
              borderRadius: 4,
              background: "rgba(96,165,250,0.18)",
              color: "#60a5fa",
              display: "inline-flex",
              alignItems: "center",
              gap: 4,
              fontWeight: 600,
              flexShrink: 0,
            }}
          >
            <UserIcon style={{ width: 11, height: 11 }} />
            {ownerEmail ? ownerEmail.split("@")[0] : "system"}
          </span>
        </div>
        <div className="project-action-bar-center">
          <span className="project-action-stats">
            {vmCount} VM{vmCount !== 1 ? "s" : ""}{containerCount > 0 ? ` · ${containerCount} container${containerCount !== 1 ? "s" : ""}` : ""} · {netCount} net{netCount !== 1 ? "s" : ""} · {diskCount} disk{diskCount !== 1 ? "s" : ""}
            {hostPlacement.provider && <> · ({hostPlacement.provider})</>}
          </span>
        </div>
        <div className="project-action-bar-right">
          <span className="project-action-state" style={{ background: `${stateColors[projectState] || "#94a3b8"}22`, color: stateColors[projectState] || "#94a3b8" }}>
            {projectState === "stopped" && autoStopped ? "stopped (auto)" : projectState}
          </span>
          {projectState !== "deleting" && ocpStatus && ocpStatus !== "none" && (
            <span
              style={{
                fontSize: 11,
                padding: "2px 8px",
                borderRadius: 4,
                flexShrink: 0,
                whiteSpace: "nowrap",
                background:
                  ocpStatus === "ready"
                    ? "rgba(74,222,128,0.15)"
                    : ocpStatus === "error"
                      ? "rgba(248,113,113,0.15)"
                      : "rgba(251,191,36,0.15)",
                color:
                  ocpStatus === "ready"
                    ? "#4ade80"
                    : ocpStatus === "error"
                      ? "#f87171"
                      : "#fbbf24",
              }}
            >
              {resolvedOcpHealth?.detail || ocpStatusDetail || `OCP ${ocpStatus}`}
            </span>
          )}
          {liveSpend && (
            <span
              className="project-timer-badge"
              style={{
                fontSize: 11, padding: "2px 8px", borderRadius: 10,
                color: liveSpend.budget_stopped ? "#ef4444" : liveSpend.budget_warned ? "#fbbf24" : "#86efac",
                background: liveSpend.budget_stopped ? "rgba(239,68,68,0.12)" : liveSpend.budget_warned ? "rgba(251,191,36,0.12)" : "rgba(34,197,94,0.12)",
                cursor: "pointer",
                flexShrink: 0,
                whiteSpace: "nowrap",
              }}
              title={`$${liveSpend.total_usd.toFixed(8).replace(/\.?0+$/, "") || "0"} — click to set budget`}
              onClick={() => {
                setBudgetModalDraft(budgetUsd == null ? "" : String(budgetUsd));
                setShowBudgetModal(true);
              }}
            >
              {liveSpend.running_since
                ? `${spendLabel}: ${formatRunningElapsed(spendElapsedSec)} / $${liveSpend.total_usd.toFixed(2)}`
                : `$${liveSpend.total_usd.toFixed(2)}`}
              {liveSpend.budget_usd != null ? ` / $${liveSpend.budget_usd.toFixed(2)}` : ""}
            </span>
          )}
          {timerCountdown && (
            <span
              className={`project-timer-badge ${timerUrgency}`}
              style={{
                fontSize: 11, padding: "2px 8px", borderRadius: 10,
                color: timerUrgency === "critical" ? "#ef4444" : timerUrgency === "warning" ? "#fbbf24" : "#94a3b8",
                background: timerUrgency === "critical" ? "rgba(239,68,68,0.12)" : timerUrgency === "warning" ? "rgba(251,191,36,0.12)" : "rgba(148,163,184,0.08)",
                animation: timerUrgency === "critical" ? "pulse 1s infinite" : "none",
                flexShrink: 0,
                whiteSpace: "nowrap",
                cursor: "pointer",
              }}
              title="Time remaining (click to open Project settings)"
              onClick={() => setShowPalette(true)}
            >
              ⏱ {timerCountdown === "Auto-Shutdown" || timerCountdown === "Auto-Deleted" ? `Project was ${timerCountdown}` : `${timerLabel} in ${timerCountdown}`}
            </span>
          )}
        </div>
        </div>
        <div className="project-action-bar-actions">
          {(projectState === "active" || projectState === "stopped" || projectState === "starting") && (
            <button
              className="project-publish-btn"
              onClick={() => window.open(`/console/monitor?project=${projectId}`, "_blank")}
              style={{ opacity: 0.85 }}
            >
              MegaConsole
            </button>
          )}
          {showroomUrl && (projectState === "active" || projectState === "stopped") && (
            <button
              className="project-publish-btn"
              onClick={() => window.open(showroomUrl, "_blank", "noopener,noreferrer")}
              style={{ opacity: 0.85 }}
              title={showroomUrl}
            >
              Open Showroom
            </button>
          )}
          {(projectState === "active" || projectState === "stopped") && (
            <button
              className="project-publish-btn"
              disabled={disruptiveActionsDisabled}
              title={disruptiveDisabledTitle}
              onClick={() => {
                if (disruptiveActionsDisabled) return;
                setShowPatternModal(true);
              }}
              style={disruptiveBtnStyle}
            >
              Save as Pattern
            </button>
          )}
          {projectState === "active" && workloadChipLabel && chipWorkload && (
            <WorkloadStatusChip
              headline={workloadChipLabel.headline}
              detail={workloadChipLabel.detail}
              variant={failedChainWorkload ? "failed" : "inflight"}
              onClick={() => setOpenRunId(chipWorkload.id)}
              onRetry={failedChainWorkload ? resumeWorkloadChain : undefined}
              onCancel={inflightWorkload ? cancelInflightWorkload : undefined}
              retrying={chainRetrying}
              cancelling={chainCancelling}
            />
          )}
          {projectState === "active" && (
            <button
              className="project-publish-btn"
              disabled={disruptiveActionsDisabled}
              title={disruptiveDisabledTitle}
              onClick={() => {
                if (disruptiveActionsDisabled) return;
                setShowWorkloadModal(true);
              }}
              style={disruptiveBtnStyle}
            >
              Run Workload
            </button>
          )}
          {projectState === "active" && (
            <button
              className="project-publish-btn"
              onClick={() => setShowWorkloadRuns(true)}
              style={{ opacity: 0.85 }}
            >
              Workloads
              {inflightWorkload && (
                <span
                  title="Workload running"
                  style={{
                    display: "inline-block",
                    width: 8,
                    height: 8,
                    marginLeft: 6,
                    borderRadius: "50%",
                    background: "var(--pf-t--global--color--status--info--default, #39a5dc)",
                    verticalAlign: "middle",
                  }}
                />
              )}
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
          {projectState !== "deleting" && (
            <button
              className="project-stop-btn"
              style={{ borderColor: "var(--pf-t--global--color--status--danger--default)", color: "var(--pf-t--global--color--status--danger--default)" }}
              onClick={() => setShowDeleteModal(true)}
            >
              Delete
            </button>
          )}
          {projectState === "draft" && (
            <>
              {isAdmin && deployHosts.length > 0 && (
                <select style={{
                  padding: "6px 10px", borderRadius: 6, fontSize: 12,
                  border: "1px solid var(--pf-t--global--border--color--default)",
                  background: "var(--pf-t--global--background--color--primary--default)",
                  color: "var(--pf-t--global--text--color--regular)",
                }} value={deployHostId} onChange={(e) => setDeployHostId(e.target.value)}
                  title={requiresKubevirt ? "This project requires a KubeVirt cluster host" : undefined}
                >
                  <option value="">Auto (best host)</option>
                  {(() => {
                    const providers = new Map<string, {id: string; name: string; type: string}>();
                    for (const h of deployHosts) {
                      if (h.provider_id && h.provider_name && !providers.has(h.provider_id)) {
                        providers.set(h.provider_id, {id: h.provider_id, name: h.provider_name, type: h.provider_type || ""});
                      }
                    }
                    return Array.from(providers.values()).map((p) => (
                      <option key={`provider:${p.id}`} value={`provider:${p.id}`}>Auto ({p.name})</option>
                    ));
                  })()}
                  {deployHosts.map((h) => <option key={h.id} value={h.id}>{h.id.slice(0, 8)} — {h.ip_address}{h.provider_type ? ` (${h.provider_type})` : ""}, {h.total_vcpus - h.used_vcpus} vCPUs / {Math.round((h.total_ram_mb - h.used_ram_mb) / 1024)}G free</option>)}
                </select>
              )}
              {isAdmin && requiresKubevirt && deployHosts.length === 0 && (
                <span style={{ fontSize: 11, color: "#f59e0b", maxWidth: 220 }}>
                  Needs a KubeVirt cluster host
                </span>
              )}
              <button
                className="project-publish-btn"
                onClick={handlePublish}
                disabled={isAdmin && requiresKubevirt && deployHosts.length === 0}
                title={
                  isAdmin && requiresKubevirt && deployHosts.length === 0
                    ? "This project requires a KubeVirt cluster host"
                    : undefined
                }
                style={
                  isAdmin && requiresKubevirt && deployHosts.length === 0
                    ? { opacity: 0.4, cursor: "not-allowed" }
                    : undefined
                }
              >
                ⚡ Deploy
              </button>
            </>
          )}
          {projectState === "active" && (
            <>
              {resumableVmNodes.length > 0 && (
                <button
                  className="project-publish-btn"
                  disabled={disruptiveActionsDisabled || resumingAll}
                  title={
                    resumableVmNodes.length === 1
                      ? "Resume the paused/hibernated VM"
                      : `Resume ${resumableVmNodes.length} paused/hibernated VMs`
                  }
                  style={
                    disruptiveActionsDisabled || resumingAll
                      ? { opacity: 0.4, cursor: "not-allowed" }
                      : { opacity: 0.85 }
                  }
                  onClick={handleResumeAll}
                >
                  {resumingAll ? (
                    <><span className="project-btn-spinner" /> Resuming...</>
                  ) : (
                    "▶ Start"
                  )}
                </button>
              )}
              <div ref={offMenuRef} style={{ position: "relative", display: "flex", flexShrink: 0 }}>
                <button
                  className="project-stop-btn"
                  disabled={disruptiveActionsDisabled}
                  title={disruptiveDisabledTitle}
                  style={{
                    ...(disruptiveActionsDisabled ? { opacity: 0.4, cursor: "not-allowed" } : {}),
                    borderRadius: "6px 0 0 6px",
                    borderRight: "none",
                  }}
                  onClick={handleOffClick}
                >
                  {OFF_ACTION_LABEL[offAction]}
                </button>
                <button
                  className="project-stop-btn"
                  aria-label="Choose off mode"
                  disabled={disruptiveActionsDisabled}
                  title={disruptiveDisabledTitle}
                  style={{
                    ...(disruptiveActionsDisabled ? { opacity: 0.4, cursor: "not-allowed" } : {}),
                    padding: "5px 8px",
                    borderRadius: "0 6px 6px 0",
                  }}
                  onClick={() => {
                    if (disruptiveActionsDisabled) return;
                    setShowOffMenu((v) => !v);
                  }}
                >
                  ▾
                </button>
                {showOffMenu && (
                  <div
                    className="node-context-menu"
                    style={{ position: "absolute", top: "calc(100% + 4px)", right: 0, zIndex: 20 }}
                  >
                    {(["stop", "pause", "hibernate"] as const).map((action) => (
                      <button
                        key={action}
                        disabled={action === "hibernate" && !supportsHibernate}
                        title={action === "hibernate" && !supportsHibernate ? "Not supported on this host type" : undefined}
                        style={
                          action === "hibernate" && !supportsHibernate
                            ? { opacity: 0.4, cursor: "not-allowed" }
                            : undefined
                        }
                        onClick={() => handleSetOffAction(action)}
                      >
                        {offAction === action ? "✓ " : ""}
                        {OFF_ACTION_LABEL[action]}
                      </button>
                    ))}
                  </div>
                )}
              </div>
              {isAdmin && (
                <button
                  className="project-publish-btn"
                  disabled={disruptiveActionsDisabled}
                  title={disruptiveDisabledTitle}
                  onClick={() => {
                    if (disruptiveActionsDisabled) return;
                    openMigrate();
                  }}
                  style={disruptiveBtnStyle}
                >
                  Migrate
                </button>
              )}
              <button className="project-publish-btn" disabled={!topologyDirty || applyingChanges} style={(!topologyDirty || applyingChanges) ? { opacity: 0.4 } : {}} onClick={handleApplyChanges}>
                {applyingChanges ? <><span className="project-btn-spinner" /> Applying...</> : "Apply Changes"}
              </button>
              <button
                className="project-publish-btn"
                disabled={disruptiveActionsDisabled}
                title={disruptiveDisabledTitle}
                onClick={() => {
                  if (disruptiveActionsDisabled) return;
                  handleRepublish("Republish? This will DESTROY all VMs and disks, and redeploy from scratch.");
                }}
                style={disruptiveBtnStyle}
              >
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
                setDeployError(null);
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
              <button className="project-publish-btn" onClick={() => handleRepublish("Republish? This will DESTROY all VMs and disks, and redeploy from scratch.")}>
                ↻ Republish
              </button>
              <button className="project-stop-btn" onClick={async () => {
                if (!(await appConfirm({
                  title: "Undeploy",
                  message: "Undeploy? This will destroy all VMs and return to design mode.",
                  confirmLabel: "Undeploy",
                  variant: "danger",
                }))) return;
                fetch(`/api/v1/projects/${projectId}/undeploy`, { method: "POST" })
                  .then(() => { setProjectState("draft"); setDeployError(null); });
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
                      else r.json().then((d) => setAlertMsg(d.detail || "Start failed"));
                    });
                }}>
                  ▶ Retry Start
                </button>
              )}
              <button className="project-publish-btn" onClick={() => handleRepublish("Republish? This will destroy all VMs and redeploy with the current topology.")}>
                ↻ Republish
              </button>
            </>
          )}
        </div>
      </div>
      {projectState === "deleting" && (
        <div style={{ position: "absolute", bottom: 24, right: 24, background: "var(--pf-t--global--background--color--primary--default)", border: "1px solid var(--pf-t--global--border--color--default)", borderRadius: 8, padding: 16, minWidth: 200, zIndex: 10 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <span className="project-btn-spinner" style={{ width: 16, height: 16 }} />
            <strong>Deleting...</strong>
          </div>
        </div>
      )}
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
            {deployProgress && projectState !== "error" && deployProgress.step === "queued" && (
              <div style={{ fontSize: 13, display: "flex", alignItems: "center", gap: 8 }}>
                <span style={{ color: "#818cf8", fontWeight: 500 }}>Queued</span>
                <span style={{ opacity: 0.6 }}>position {deployProgress.detail}</span>
              </div>
            )}
            {deployProgress && projectState !== "error" && deployProgress.step !== "queued" && (
              <div style={{ fontSize: 13, marginBottom: deployProgress.items ? 8 : 0, whiteSpace: "pre-line", maxHeight: 300, overflowY: "auto" }}>
                <span style={{ opacity: 0.7 }}>{deployProgress.step}</span>
                {deployProgress.detail ? `:\n${deployProgress.detail}` : ""}
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
            <div style={{ textAlign: "center", pointerEvents: "none" }}>
              <div style={{ fontSize: 14, opacity: 0.5, marginBottom: 16 }}>
                Drag components from the palette or import a template
              </div>
              <button
                onClick={() => {
                  setImportYaml("");
                  setImportError("");
                  setImportSshKeyId("");
                  const chars = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789";
                  setImportPassword(Array.from({ length: 12 }, () => chars[Math.floor(Math.random() * chars.length)]).join(""));
                  fetch("/api/v1/auth/ssh-keys")
                    .then((r) => (r.ok ? r.json() : []))
                    .then((data) => setImportSshKeys(Array.isArray(data) ? data : []))
                    .catch(() => setImportSshKeys([]));
                  setShowImportModal(true);
                }}
                style={{
                  padding: "10px 24px", borderRadius: 8,
                  border: "1px solid var(--pf-t--global--border--color--default)",
                  background: "var(--pf-t--global--background--color--primary--default)",
                  color: "#fff", cursor: "pointer", fontSize: 14, fontWeight: 500,
                  pointerEvents: "auto",
                }}
              >
                Import Template YAML
              </button>
            </div>
          </div>
        )}
        {showPalette && <Palette onOpenStartOrder={() => setShowStartOrder(true)} onOpenExternalIps={() => setShowExternalIps(true)} projectDescription={projectDesc} projectGuid={projectGuid} projectId={projectId} hostId={isAdmin ? projectHostId : undefined} ocpHealth={resolvedOcpHealth} onDescriptionChange={(desc) => {
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
        }} clockTarget={clockTarget} onClockTargetChange={(v: string | null) => {
          setClockTarget(v);
          fetch(`/api/v1/projects/${projectId}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ clock_target: v }),
          }).then(r => {
            if (r.ok && v !== null && projectState === "active") {
              setToast("Clock updated — VMs syncing");
              setTimeout(() => setToast(null), 3000);
            }
          });
        }} guestExecEnabled={guestExecEnabled} onGuestExecChange={(v: boolean) => {
          setGuestExecEnabled(v);
          fetch(`/api/v1/projects/${projectId}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ guest_exec_enabled: v }),
          });
        }} budgetUsd={budgetUsd} onBudgetChange={(v: number | null) => {
          setBudgetUsd(v);
          fetch(`/api/v1/projects/${projectId}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ budget_usd: v }),
          }).then((r) => r.ok ? r.json() : null).then((data) => {
            if (data) setBudgetUsd(data.budget_usd ?? null);
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
          onRunWorkload={(target) => {
            setRunWorkloadTarget(target);
            setShowWorkloadModal(true);
          }}
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
          diff={reconfigDiff}
          onConfirm={(restartVmIds) => doReconfigure(restartVmIds)}
          onCancel={() => { setReconfigWarnings(null); setReconfigDiff([]); }}
        />
      )}
      {showPatternModal && (
        <SavePatternModal
          projectId={projectId}
          projectName={projectName}
          hasRunningVMs={nodes.some((n) => n.type === "vmNode" && (n.data as Record<string, any>).status === "running")}
          isSno={nodes.filter((n) => n.type === "vmNode" && (n.data as Record<string, any>).os === "rhcos").length === 1}
          onSaved={(_id, capturing) => {
            setShowPatternModal(false);
            showToast(capturing ? "Pattern capture started — track progress on the Patterns page" : "Pattern saved successfully");
          }}
          onClose={() => setShowPatternModal(false)}
        />
      )}
      {showWorkloadModal && (
        <RunWorkloadModal
          projectId={projectId}
          initialMode={runWorkloadTarget?.mode}
          initialClusterIds={runWorkloadTarget?.clusterIds}
          initialVmNames={runWorkloadTarget?.vmNames}
          onClose={() => {
            setShowWorkloadModal(false);
            setRunWorkloadTarget(null);
          }}
          onLaunched={(runIds) => {
            setShowWorkloadModal(false);
            setRunWorkloadTarget(null);
            if (runIds.length === 1) {
              setOpenRunId(runIds[0]);
            } else if (runIds.length > 1) {
              setShowWorkloadRuns(true);
            }
          }}
        />
      )}
      {showWorkloadRuns && (
        <WorkloadRunsModal
          projectId={projectId}
          onClose={() => setShowWorkloadRuns(false)}
          onOpenRun={(runId) => {
            setShowWorkloadRuns(false);
            setOpenRunId(runId);
          }}
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
      {openRunId && (
        <WorkloadRunDetailModal
          runId={openRunId}
          wsNudge={ws.workloadProgress}
          onClose={() => setOpenRunId(null)}
          onRunIdChange={(id) => {
            setOpenRunId(id);
            refreshWorkloadRuns();
          }}
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
            <div style={{ borderTop: "1px solid var(--pf-t--global--border--color--default)", paddingTop: 12, marginTop: 12 }}>
              <div style={{ fontSize: 11, color: "var(--pf-t--global--text--color--subtle)", marginBottom: 8 }}>
                Cloud-init credentials (applied when the template enables cloud-init and omits them)
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                <div>
                  <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>SSH Key</label>
                  <select
                    style={{
                      width: "100%", padding: "6px 10px", borderRadius: 6, fontSize: 13,
                      border: "1px solid var(--pf-t--global--border--color--default)",
                      background: "var(--pf-t--global--background--color--primary--default)",
                      color: "var(--pf-t--global--text--color--regular)",
                    }}
                    value={importSshKeyId}
                    onChange={(e) => setImportSshKeyId(e.target.value)}
                  >
                    <option value="">None</option>
                    {importSshKeys.map((k) => (
                      <option key={k.id} value={k.id}>{k.name}</option>
                    ))}
                  </select>
                </div>
                <div>
                  <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>
                    Password <span style={{ color: "var(--pf-t--global--text--color--subtle)" }}>(cloud-user console)</span>
                  </label>
                  <div style={{ display: "flex", gap: 4 }}>
                    <input
                      style={{
                        flex: "1 1 0", minWidth: 0, padding: "6px 10px", borderRadius: 6, fontSize: 13,
                        border: "1px solid var(--pf-t--global--border--color--default)",
                        background: "var(--pf-t--global--background--color--primary--default)",
                        color: "var(--pf-t--global--text--color--regular)",
                      }}
                      value={importPassword}
                      onChange={(e) => setImportPassword(e.target.value)}
                      placeholder="Used for console login"
                    />
                    <button
                      type="button"
                      style={{
                        padding: "4px 10px", borderRadius: 6, fontSize: 12, cursor: "pointer",
                        border: "1px solid var(--pf-t--global--border--color--default)",
                        background: "var(--pf-t--global--background--color--primary--default)",
                        color: "var(--pf-t--global--text--color--regular)",
                      }}
                      onClick={() => { navigator.clipboard.writeText(importPassword); }}
                      title="Copy password"
                    >Copy</button>
                  </div>
                  {!importPassword.trim() && (
                    <div style={{ fontSize: 11, color: "#f59e0b", marginTop: 4 }}>
                      No password set — cloud images may have no console login until you set one on the VM.
                    </div>
                  )}
                </div>
                <label style={{ fontSize: 12, display: "flex", alignItems: "center", gap: 8, cursor: "pointer", marginTop: 4 }}>
                  <input type="checkbox" checked={importAutoDeploy} onChange={(e) => setImportAutoDeploy(e.target.checked)} />
                  Deploy immediately after import
                </label>
              </div>
            </div>
            {importError && (
              <div style={{ color: "var(--pf-t--global--color--status--danger--default)", fontSize: 12, marginTop: 8, whiteSpace: "pre-wrap", lineHeight: 1.4 }}>
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
                    const importBody: Record<string, unknown> = { template_yaml: parsed };
                    if (importPassword) importBody.common_password = importPassword;
                    if (importSshKeyId) importBody.bastion_ssh_key_id = importSshKeyId;
                    const resp = await fetch(`/api/v1/projects/${projectId}/import-template`, {
                      method: "POST",
                      headers: { "Content-Type": "application/json" },
                      body: JSON.stringify(importBody),
                    });
                    if (!resp.ok) {
                      const err = await resp.json().catch(() => ({ detail: "Import failed" }));
                      setImportError(formatApiDetail(err.detail, "Import failed"));
                      setImporting(false);
                      return;
                    }
                    const okData = await resp.json().catch(() => ({}));
                    const warnings: string[] = okData?.warnings || [];
                    if (warnings.length > 0) {
                      // Non-blocking: the import succeeded, but some template IPs
                      // overlap Troshka's reserved infra IPs (gateway .1 /
                      // dnsmasq .2) and will conflict at deploy time.
                      window.alert(
                        "Imported with warnings:\n\n" + warnings.join("\n"),
                      );
                    }
                    setShowImportModal(false);
                    await loadProject(projectId);
                    if (importAutoDeploy) {
                      const deployResp = await fetch(`/api/v1/projects/${projectId}/deploy`, { method: "POST" });
                      if (!deployResp.ok) {
                        const err = await deployResp.json().catch(() => ({ detail: "Deploy failed" }));
                        setAlertMsg(typeof err.detail === "string" ? err.detail : "Deploy failed");
                      } else {
                        // Refresh state so the toolbar reflects deploying.
                        loadProject(projectId);
                      }
                    }
                  } catch (err: unknown) {
                    setImportError(err instanceof Error ? err.message : "Import failed");
                  } finally {
                    setImporting(false);
                  }
                }}
                style={{
                  padding: "8px 20px", borderRadius: 6, border: "1px solid var(--pf-t--global--border--color--default)",
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
              This exports the infrastructure topology (VMs, networks, gateway config, disk sizes) as a YAML template. Disks reference their <strong>source library items</strong> (not snapshots) — on import, those library items must exist. To capture a fully built environment including disk contents, use <strong>Save as Pattern</strong> instead.
            </div>
            <div style={{ marginBottom: 16 }}>
              <div style={{ fontSize: 13, fontWeight: 500, marginBottom: 8 }}>Passwords</div>
              <div style={{ display: "flex", flexDirection: "column", gap: 6, fontSize: 13 }}>
                <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
                  <input type="radio" name="exportPw" checked={exportPasswordMode === "current"} onChange={() => setExportPasswordMode("current")} />
                  Include current passwords (plain text)
                </label>
                <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
                  <input type="radio" name="exportPw" checked={exportPasswordMode === "custom"} onChange={() => setExportPasswordMode("custom")} />
                  Set a single password for all
                </label>
                {exportPasswordMode === "custom" && (
                  <input
                    className="props-input"
                    value={exportCustomPassword}
                    onChange={(e) => setExportCustomPassword(e.target.value)}
                    placeholder="Enter password"
                    style={{ marginLeft: 24, width: 200, fontSize: 13 }}
                  />
                )}
                <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
                  <input type="radio" name="exportPw" checked={exportPasswordMode === "none"} onChange={() => setExportPasswordMode("none")} />
                  Omit all passwords
                </label>
              </div>
              {exportPasswordMode === "current" && (
                <div style={{
                  fontSize: 11, padding: "8px 12px", borderRadius: 6, marginTop: 8,
                  background: "rgba(239,68,68,0.08)", border: "1px solid rgba(239,68,68,0.25)",
                  color: "var(--pf-t--global--text--color--subtle)",
                }}>
                  Passwords will be stored in plain text. Do not share the file if it contains sensitive credentials.
                </div>
              )}
            </div>
            <div style={{ marginBottom: 16 }}>
              <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer", fontSize: 13 }}>
                <input type="checkbox" checked={exportIncludeIds} onChange={(e) => setExportIncludeIds(e.target.checked)} />
                Include library item IDs
              </label>
              {exportIncludeIds && (
                <div style={{
                  fontSize: 11, padding: "8px 12px", borderRadius: 6, marginTop: 8,
                  background: "rgba(251,191,36,0.08)", border: "1px solid rgba(251,191,36,0.25)",
                  color: "var(--pf-t--global--text--color--subtle)",
                }}>
                  IDs are instance-specific. Templates with IDs may fail to import on other Troshka instances. Leave off for portable templates — items will be resolved by name.
                </div>
              )}
            </div>
            <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
              <button onClick={() => setShowExportModal(false)}
                style={{ padding: "8px 16px", borderRadius: 6, border: "1px solid var(--pf-t--global--border--color--default)",
                  background: "transparent", color: "var(--pf-t--global--text--color--regular)", cursor: "pointer" }}>
                Cancel
              </button>
              <button
                disabled={exportPasswordMode === "custom" && !exportCustomPassword}
                onClick={async () => {
                  const exportBody: Record<string, string | boolean> = { password_mode: exportPasswordMode, include_ids: exportIncludeIds };
                  if (exportPasswordMode === "custom" && exportCustomPassword) exportBody.custom_password = exportCustomPassword;
                  const resp = await fetch(`/api/v1/projects/${projectId}/export-template`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(exportBody),
                  });
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
      {redeployOcpModal && (
        <RedeployOcpModal
          clusters={redeployOcpModal.clusters}
          allReady={redeployOcpModal.allReady}
          onCancel={() => setRedeployOcpModal(null)}
          onChoose={(mode) => {
            setRedeployOcpModal(null);
            void doRedeploy(mode);
          }}
        />
      )}
      <AlertModal message={alertMsg} onClose={() => setAlertMsg(null)} />
      {showBudgetModal && (
        <div className="start-order-overlay" onClick={() => setShowBudgetModal(false)}>
          <div className="start-order-modal" style={{ maxWidth: 400 }} onClick={(e) => e.stopPropagation()}>
            <div className="start-order-header">
              <span>Budget</span>
              <button onClick={() => setShowBudgetModal(false)}>&#x2715;</button>
            </div>
            <div className="start-order-body" style={{ padding: 16 }}>
              <label style={{ fontSize: 13, display: "block", marginBottom: 6 }}>
                Stop the project at this spend (USD)
              </label>
              <input
                type="number"
                min={0}
                step="0.01"
                autoFocus
                placeholder="None"
                value={budgetModalDraft}
                onChange={(e) => setBudgetModalDraft(e.target.value)}
                style={{
                  width: "100%", padding: "8px 10px", borderRadius: 6, fontSize: 14,
                  border: "1px solid var(--pf-t--global--border--color--default)",
                  background: "var(--pf-t--global--background--color--primary--default)",
                  color: "var(--pf-t--global--text--color--regular)",
                }}
              />
              {liveSpend && (
                <div style={{ marginTop: 8, fontSize: 12, opacity: 0.75 }}>
                  Spend so far ${liveSpend.total_usd < 0.01 && liveSpend.total_usd > 0
                    ? liveSpend.total_usd.toFixed(4)
                    : liveSpend.total_usd.toFixed(2)}
                </div>
              )}
            </div>
            <div className="start-order-footer" style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
              <button className="start-order-btn" onClick={() => setShowBudgetModal(false)}>Cancel</button>
              <button
                className="start-order-btn save"
                onClick={() => {
                  const raw = budgetModalDraft.trim();
                  const next = raw === "" ? null : Number(raw);
                  if (next != null && (Number.isNaN(next) || next < 0)) return;
                  setBudgetUsd(next);
                  setShowBudgetModal(false);
                  fetch(`/api/v1/projects/${projectId}`, {
                    method: "PATCH",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ budget_usd: next }),
                  }).then((r) => (r.ok ? r.json() : null)).then((data) => {
                    if (data) setBudgetUsd(data.budget_usd ?? null);
                  });
                }}
              >
                Save
              </button>
            </div>
          </div>
        </div>
      )}
      {showDeleteModal && (
        <ConfirmModal
          title="Delete Project"
          message={`Delete project "${projectName}"? This cannot be undone.`}
          confirmLabel="Delete"
          variant="danger"
          onCancel={() => setShowDeleteModal(false)}
          onConfirm={() => {
            setShowDeleteModal(false);
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
        />
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
