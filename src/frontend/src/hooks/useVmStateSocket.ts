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

interface VmStateSocket {
  connected: boolean;
  vmStates: Record<string, string>;
  vmProgress: Record<string, VmProgress>;
  projectState: string | null;
  deployError: string | null;
  deployProgress: DeployProgress | null;
}

const BACKOFF_BASE = 1000;
const BACKOFF_MAX = 10000;

export function useVmStateSocket(projectId: string | null): VmStateSocket {
  const [connected, setConnected] = useState(false);
  const [vmStates, setVmStates] = useState<Record<string, string>>({});
  const [vmProgress, setVmProgress] = useState<Record<string, VmProgress>>({});
  const [projectState, setProjectState] = useState<string | null>(null);
  const [deployError, setDeployError] = useState<string | null>(null);
  const [deployProgress, setDeployProgress] = useState<DeployProgress | null>(null);

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
            setVmStates(msg.vm_states || {});
            setVmProgress(msg.vm_progress || {});
            setProjectState(msg.project_state || null);
            setDeployError(msg.deploy_error || null);
            setDeployProgress(msg.deploy_progress || null);
            break;
          case "vm-state":
            setVmStates((prev) => ({ ...prev, ...msg.states }));
            setVmProgress((prev) => ({ ...prev, ...msg.progress }));
            break;
          case "project-state":
            setProjectState(msg.state || null);
            setDeployError(msg.deploy_error ?? null);
            break;
          case "deploy-progress":
            setDeployProgress(msg.progress || null);
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

  return { connected, vmStates, vmProgress, projectState, deployError, deployProgress };
}
