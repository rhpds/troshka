"use client";

import { useState, useEffect, useRef, useCallback } from "react";

interface VmProgress {
  step: string;
  detail: string;
}

interface DeployProgress {
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
  ocpHealth: OcpHealth | null;
  topologyUpdate: any | null;
  deleted: boolean;
  timerWarning: { timer: string; expires_at: string; minutes_remaining: number } | null;
  timerFired: string | null;
  autoStopExpiresAt: string | null;
  lifetimeExpiresAt: string | null;
  autoStopped: boolean;
}

const BACKOFF_BASE = 1000;
const BACKOFF_MAX = 30000;

export function useVmStateSocket(projectId: string | null): VmStateSocket {
  const [connected, setConnected] = useState(false);
  const [vmStates, setVmStates] = useState<Record<string, string>>({});
  const [vmProgress, setVmProgress] = useState<Record<string, VmProgress>>({});
  const [vmBootDevs, setVmBootDevs] = useState<Record<string, string[]>>({});
  const [projectState, setProjectState] = useState<string | null>(null);
  const [deployError, setDeployError] = useState<string | null>(null);
  const [deployProgress, setDeployProgress] = useState<DeployProgress | null>(null);
  const [ocpHealth, setOcpHealth] = useState<OcpHealth | null>(null);
  const [topologyUpdate, setTopologyUpdate] = useState<any | null>(null);
  const [deleted, setDeleted] = useState(false);
  const [timerWarning, setTimerWarning] = useState<VmStateSocket["timerWarning"]>(null);
  const [timerFired, setTimerFired] = useState<string | null>(null);
  const [autoStopExpiresAt, setAutoStopExpiresAt] = useState<string | null>(null);
  const [lifetimeExpiresAt, setLifetimeExpiresAt] = useState<string | null>(null);
  const [autoStopped, setAutoStopped] = useState(false);

  const wsRef = useRef<WebSocket | null>(null);
  const retriesRef = useRef(0);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const mountedRef = useRef(true);

  const connect = useCallback(() => {
    if (!projectId || !mountedRef.current) return;

    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const host = window.location.host;
    const url = `${proto}//${host}/api/v1/projects/${projectId}/ws`;

    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => {
      if (!mountedRef.current) { ws.close(); return; }
      setConnected(true);
      retriesRef.current = 0;
    };

    ws.onmessage = (e) => {
      if (!mountedRef.current) return;
      try {
        const msg = JSON.parse(e.data);
        switch (msg.type) {
          case "snapshot":
            if (msg.vm_states && Object.keys(msg.vm_states).length > 0) {
              setVmStates(msg.vm_states);
            }
            setVmProgress(msg.vm_progress || {});
            setProjectState(msg.project_state || null);
            setDeployError(msg.deploy_error || null);
            setDeployProgress(msg.deploy_progress || null);
            break;
          case "vm-state":
            setVmStates((prev) => ({ ...prev, ...msg.states }));
            setVmProgress((prev) => ({ ...prev, ...msg.progress }));
            if (msg.boot_devs) setVmBootDevs((prev) => ({ ...prev, ...msg.boot_devs }));
            break;
          case "project-state":
            setProjectState(msg.state || null);
            setDeployError(msg.deploy_error ?? null);
            if ("auto_stop_expires_at" in msg) setAutoStopExpiresAt(msg.auto_stop_expires_at ?? null);
            if ("lifetime_expires_at" in msg) setLifetimeExpiresAt(msg.lifetime_expires_at ?? null);
            if ("auto_stopped" in msg) setAutoStopped(!!msg.auto_stopped);
            break;
          case "deploy-progress":
            setDeployProgress(msg.progress || null);
            break;
          case "ocp-health":
            setOcpHealth({ phase: msg.phase, detail: msg.detail, items: msg.items });
            break;
          case "topology-update":
            setTopologyUpdate(msg.topology || null);
            break;
          case "project-deleted":
            setDeleted(true);
            break;
          case "timer_warning":
            setTimerWarning({ timer: msg.timer, expires_at: msg.expires_at, minutes_remaining: msg.minutes_remaining });
            break;
          case "timer_fired":
            setTimerFired(msg.timer);
            break;
          case "ping":
            break;
        }
      } catch { /* ignore malformed messages */ }
    };

    ws.onclose = () => {
      if (!mountedRef.current) return;
      setConnected(false);
      wsRef.current = null;
      const delay = Math.min(BACKOFF_BASE * Math.pow(2, retriesRef.current), BACKOFF_MAX);
      retriesRef.current += 1;
      timerRef.current = setTimeout(connect, delay);
    };

    ws.onerror = () => {
      // onclose will fire after onerror — reconnect handled there
    };
  }, [projectId]);

  useEffect(() => {
    mountedRef.current = true;
    connect();

    return () => {
      mountedRef.current = false;
      if (timerRef.current) clearTimeout(timerRef.current);
      if (wsRef.current) {
        wsRef.current.onclose = null;
        wsRef.current.close();
        wsRef.current = null;
      }
    };
  }, [connect]);

  return { connected, vmStates, vmProgress, vmBootDevs, projectState, deployError, deployProgress, ocpHealth, topologyUpdate, deleted, timerWarning, timerFired, autoStopExpiresAt, lifetimeExpiresAt, autoStopped };
}
