"use client";

import { useEffect, useReducer } from "react";
import type { useCanvasStore, applyDeployedTopologyFromServer } from "@/stores/canvasStore";

type SocketTopology = Partial<Pick<ReturnType<typeof useCanvasStore.getState>, "nodes" | "edges" | "clusters">>;
type TopologyUpdate = SocketTopology & {
  topology?: SocketTopology;
  deployed_topology?: Exclude<Parameters<typeof applyDeployedTopologyFromServer>[0], undefined>;
};

interface VmProgress {
  step: string;
  detail: string;
}

interface DeployProgress {
  step: string;
  detail: string;
  items?: string[];
}

interface WorkloadProgress {
  run_id?: string;
  step: string;
  detail: string;
}

export interface OcpHealth {
  phase: string;
  detail: string;
  items?: string[];
}

interface VmStateSocket {
  connected: boolean;
  vmStates: Record<string, string>;
  vmProgress: Record<string, VmProgress>;
  vmBootDevs: Record<string, string[]>;
  projectState: string | null;
  deployError: string | null;
  deployProgress: DeployProgress | null;
  workloadProgress: WorkloadProgress | null;
  ocpHealth: OcpHealth | null;
  /** Ops-pod per-cluster install phases (cluster key → phase). */
  clusterOcpPhases: Record<string, string>;
  topologyUpdate: TopologyUpdate | null;
  externalIpsUpdate: Array<{ id: string; name: string; ip?: string; _private_ip?: string; state?: string }> | null;
  deleted: boolean;
  timerWarning: { timer: string; expires_at: string; minutes_remaining: number } | null;
  timerFired: string | null;
  autoStopExpiresAt: string | null;
  lifetimeExpiresAt: string | null;
  autoStopped: boolean;
}

type ProgressMessage = Partial<DeployProgress & WorkloadProgress>;
type SocketMessage =
  | { type: "snapshot"; vm_states?: Record<string, string>; vm_progress?: Record<string, VmProgress>; project_state?: string; deploy_error?: string; deploy_progress?: DeployProgress }
  | { type: "vm-state"; states?: Record<string, string>; progress?: Record<string, VmProgress>; boot_devs?: Record<string, string[]> }
  | { type: "project-state"; state?: string; deploy_error?: string | null; auto_stop_expires_at?: string | null; lifetime_expires_at?: string | null; auto_stopped?: boolean }
  | ({ type: "deploy-progress" | "workload-progress"; progress?: ProgressMessage } & ProgressMessage)
  | ({ type: "ocp-health" } & OcpHealth)
  | { type: "ocp-install-progress"; clusters?: Record<string, string>; overall?: string; detail?: string; items?: string[] }
  | { type: "topology-update"; topology?: unknown; deployed_topology?: unknown }
  | { type: "external-ips-updated"; externalIps?: VmStateSocket["externalIpsUpdate"] }
  | { type: "project-deleted" | "ping" }
  | { type: "timer_warning"; timer: string; expires_at: string; minutes_remaining: number }
  | { type: "timer_fired"; timer: string }
  | { type: "connection"; connected: boolean };

const initialState: VmStateSocket = {
  connected: false, vmStates: {}, vmProgress: {}, vmBootDevs: {},
  projectState: null, deployError: null, deployProgress: null, workloadProgress: null,
  ocpHealth: null, clusterOcpPhases: {}, topologyUpdate: null, externalIpsUpdate: null,
  deleted: false, timerWarning: null, timerFired: null, autoStopExpiresAt: null,
  lifetimeExpiresAt: null, autoStopped: false,
};

function snapshotState(state: VmStateSocket, msg: Extract<SocketMessage, { type: "snapshot" }>): VmStateSocket {
  return {
    ...state,
    // Empty snapshots must not erase VM states learned from incremental updates.
    vmStates: msg.vm_states && Object.keys(msg.vm_states).length > 0 ? msg.vm_states : state.vmStates,
    vmProgress: msg.vm_progress || {},
    projectState: msg.project_state || null,
    deployError: msg.deploy_error || null,
    deployProgress: msg.deploy_progress || state.deployProgress,
  };
}

function projectStateUpdate(state: VmStateSocket, msg: Extract<SocketMessage, { type: "project-state" }>): VmStateSocket {
  const next = { ...state, projectState: msg.state || null, deployError: msg.deploy_error ?? null };
  // A new deployment must not display milestones from the previous cycle.
  if (msg.state === "deploying") next.deployProgress = null;
  if ("auto_stop_expires_at" in msg) next.autoStopExpiresAt = msg.auto_stop_expires_at ?? null;
  if ("lifetime_expires_at" in msg) next.lifetimeExpiresAt = msg.lifetime_expires_at ?? null;
  if ("auto_stopped" in msg) next.autoStopped = !!msg.auto_stopped;
  return next;
}

function installHealthPhase(overall?: string): string {
  if (overall === "complete") return "ready";
  if (overall === "failed" || overall === "cancelled") return "error";
  return overall === "timeout" ? "timeout" : "ssh";
}

function deploymentProgressUpdate(state: VmStateSocket, msg: ProgressMessage & { progress?: ProgressMessage }): VmStateSocket {
  const progress = msg.progress || msg;
  if (!progress.step && !progress.detail) return state;
  return { ...state, deployProgress: { step: progress.step || "", detail: progress.detail || "", items: progress.items } };
}

function socketStateReducer(state: VmStateSocket, msg: SocketMessage): VmStateSocket {
  switch (msg.type) {
    case "connection": return { ...state, connected: msg.connected };
    case "snapshot": return snapshotState(state, msg);
    case "vm-state": return {
      ...state, vmStates: { ...state.vmStates, ...msg.states },
      vmProgress: { ...state.vmProgress, ...msg.progress },
      vmBootDevs: { ...state.vmBootDevs, ...msg.boot_devs },
    };
    case "project-state": return projectStateUpdate(state, msg);
    case "deploy-progress": return deploymentProgressUpdate(state, msg);
    case "workload-progress": {
      const progress = msg.progress || msg;
      return { ...state, workloadProgress: { run_id: progress.run_id, step: progress.step || "", detail: progress.detail || "" } };
    }
    case "ocp-health": return { ...state, ocpHealth: { phase: msg.phase, detail: msg.detail, items: msg.items } };
    case "ocp-install-progress": return {
      ...state, clusterOcpPhases: msg.clusters || {},
      ocpHealth: { phase: installHealthPhase(msg.overall), detail: String(msg.detail || ""), items: msg.items },
    };
    case "topology-update": return {
      ...state, topologyUpdate: msg.topology ? { topology: msg.topology, deployed_topology: msg.deployed_topology || null } : null,
    };
    case "external-ips-updated": return { ...state, externalIpsUpdate: msg.externalIps || [] };
    case "project-deleted": return { ...state, deleted: true };
    case "timer_warning": return { ...state, timerWarning: { timer: msg.timer, expires_at: msg.expires_at, minutes_remaining: msg.minutes_remaining } };
    case "timer_fired": return { ...state, timerFired: msg.timer };
    default: return state;
  }
}

async function socketTokenParam(): Promise<string> {
  try {
    const response = await fetch("/api/v1/auth/ws-token");
    if (!response.ok) return "";
    const data = await response.json();
    return data.token ? `?token=${encodeURIComponent(data.token)}` : "";
  } catch {
    // Development mode can connect without a token.
    return "";
  }
}

const BACKOFF_BASE = 1000;
const BACKOFF_MAX = 30000;

export function useVmStateSocket(projectId: string | null): VmStateSocket {
  const [state, dispatch] = useReducer(socketStateReducer, initialState);

  useEffect(() => {
    if (!projectId) return;
    // Each project owns its connection lifetime, including pending authentication.
    let active = true;
    let socket: WebSocket | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    let retries = 0;

    async function connect() {
      const tokenParam = await socketTokenParam();
      if (!active) return;
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const ws = new WebSocket(`${protocol}//${window.location.host}/api/v1/projects/${projectId}/ws${tokenParam}`);
      socket = ws;
      ws.onopen = () => {
        if (!active || socket !== ws) { ws.close(); return; }
        dispatch({ type: "connection", connected: true });
        retries = 0;
      };
      ws.onmessage = (event) => {
        if (!active || socket !== ws) return;
        try {
          const message = JSON.parse(event.data);
          if (message && typeof message.type === "string") dispatch(message as SocketMessage);
        } catch { /* Ignore malformed messages. */ }
      };
      ws.onclose = () => {
        if (!active || socket !== ws) return;
        dispatch({ type: "connection", connected: false });
        socket = null;
        const delay = Math.min(BACKOFF_BASE * 2 ** retries, BACKOFF_MAX);
        retries += 1;
        retryTimer = setTimeout(connect, delay);
      };
      // The close event follows errors and owns reconnection.
      ws.onerror = () => {};
    }

    connect();
    return () => {
      active = false;
      clearTimeout(retryTimer);
      if (socket) {
        socket.onclose = null;
        socket.close();
        socket = null;
      }
    };
  }, [projectId]);

  return state;
}
