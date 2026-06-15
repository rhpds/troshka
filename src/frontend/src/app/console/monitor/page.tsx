"use client";

import React, { Suspense, useEffect, useRef, useState, useCallback } from "react";
import { useSearchParams } from "next/navigation";
import { useVmStateSocket } from "@/hooks/useVmStateSocket";

export default function MegaConsoleWrapper() {
  return (
    <Suspense fallback={<div style={{ color: "#fff", padding: 20, background: "#0d1117", height: "100vh" }}>Loading MegaConsole...</div>}>
      <MegaConsolePage />
    </Suspense>
  );
}

interface VmInfo {
  id: string;
  name: string;
}

// Module-level to survive hot-reloads
let _rfbInstances: Record<string, any> = {};

function MegaConsolePage() {
  const searchParams = useSearchParams();
  const projectId = searchParams.get("project") || "";
  const [projectName, setProjectName] = useState("");
  const [vms, setVms] = useState<VmInfo[]>([]);
  const [columns, setColumnsState] = useState(() => {
    if (typeof window !== "undefined") {
      return parseInt(localStorage.getItem("megaconsole-cols") || "3") || 3;
    }
    return 3;
  });
  const [hidden, setHidden] = useState<Set<string>>(() => {
    if (typeof window !== "undefined") {
      try {
        return new Set(JSON.parse(localStorage.getItem(`megaconsole-hidden-${projectId}`) || "[]"));
      } catch { return new Set(); }
    }
    return new Set();
  });
  const [hiddenMenuOpen, setHiddenMenuOpen] = useState(false);
  const [focusedVm, setFocusedVm] = useState<string | null>(null);
  const [rfbStatuses, setRfbStatuses] = useState<Record<string, string>>({});
  const RFBClass = useRef<any>(null);
  const tileRefs = useRef<Record<string, HTMLDivElement | null>>({});
  const reconnectTimers = useRef<Record<string, ReturnType<typeof setTimeout>>>({});
  const mountedRef = useRef(true);
  const ws = useVmStateSocket(projectId || null);

  // Load noVNC once
  useEffect(() => {
    mountedRef.current = true;
    import("@novnc/novnc").then((mod) => { RFBClass.current = mod.default; });
    return () => {
      mountedRef.current = false;
      Object.values(reconnectTimers.current).forEach(clearTimeout);
    };
  }, []);

  // Suppress noVNC async errors
  useEffect(() => {
    const handler = (e: ErrorEvent) => {
      const msg = e.message || "";
      if (msg.includes("RFB") || msg.includes("Connection closed") || msg.includes("disconnected") || msg.includes("1006")) {
        e.preventDefault();
        e.stopImmediatePropagation();
        return false;
      }
    };
    const unhandled = (e: PromiseRejectionEvent) => {
      const msg = String(e.reason);
      if (msg.includes("RFB") || msg.includes("Connection closed") || msg.includes("disconnected") || msg.includes("1006")) {
        e.preventDefault();
      }
    };
    window.addEventListener("error", handler, true);
    window.addEventListener("unhandledrejection", unhandled, true);
    const observer = new MutationObserver(() => {
      const overlay = document.querySelector("nextjs-portal");
      if (overlay) overlay.remove();
    });
    observer.observe(document.body, { childList: true, subtree: true });
    return () => {
      window.removeEventListener("error", handler, true);
      window.removeEventListener("unhandledrejection", unhandled, true);
      observer.disconnect();
    };
  }, []);

  // Fetch project info and VM list
  useEffect(() => {
    if (!projectId) return;
    fetch(`/api/v1/projects/${projectId}`)
      .then((r) => r.ok ? r.json() : null)
      .then((data) => {
        if (!data) return;
        setProjectName(data.name || "");
        const vmNodes: VmInfo[] = (data.topology?.nodes || [])
          .filter((n: any) => n.type === "vmNode")
          .map((n: any) => ({ id: n.id, name: n.data?.name || n.id.slice(0, 8) }));

        // Sort by start order if available
        const startOrder: { vmId: string }[] = data.topology?.startOrder || [];
        const orderMap = new Map(startOrder.map((s, i) => [s.vmId, i]));
        vmNodes.sort((a, b) => {
          const ai = orderMap.get(a.id) ?? 999;
          const bi = orderMap.get(b.id) ?? 999;
          return ai - bi;
        });

        setVms(vmNodes);
      })
      .catch(() => {});
  }, [projectId]);

  useEffect(() => {
    document.title = projectName ? `MegaConsole — ${projectName}` : "MegaConsole";
  }, [projectName]);

  // Persist state
  const setColumns = useCallback((n: number) => {
    setColumnsState(n);
    localStorage.setItem("megaconsole-cols", String(n));
  }, []);

  const hideVm = useCallback((vmId: string) => {
    setHidden((prev) => {
      const next = new Set(prev);
      next.add(vmId);
      localStorage.setItem(`megaconsole-hidden-${projectId}`, JSON.stringify([...next]));
      return next;
    });
    // Disconnect RFB for hidden VM
    const rfb = _rfbInstances[vmId];
    if (rfb?.disconnect) {
      try { rfb.disconnect(); } catch { /* ignore */ }
    }
    delete _rfbInstances[vmId];
  }, [projectId]);

  const showVm = useCallback((vmId: string) => {
    setHidden((prev) => {
      const next = new Set(prev);
      next.delete(vmId);
      localStorage.setItem(`megaconsole-hidden-${projectId}`, JSON.stringify([...next]));
      return next;
    });
    setHiddenMenuOpen(false);
  }, [projectId]);

  // Connect/reconnect a single VM's console
  const connectVm = useCallback(async (vmId: string) => {
    if (!projectId || !mountedRef.current || !RFBClass.current) return;
    if (hidden.has(vmId)) return;

    const target = tileRefs.current[vmId];
    if (!target) return;

    setRfbStatuses((prev) => ({ ...prev, [vmId]: "connecting" }));

    try {
      const resp = await fetch(`/api/v1/projects/${projectId}/vms/${vmId}/console`);
      if (!resp.ok) {
        setRfbStatuses((prev) => ({ ...prev, [vmId]: "unavailable" }));
        return;
      }
      const data = await resp.json();
      if (!data.ws_url) {
        setRfbStatuses((prev) => ({ ...prev, [vmId]: "waiting" }));
        reconnectTimers.current[vmId] = setTimeout(() => connectVm(vmId), 3000);
        return;
      }

      // Clean up old instance
      const old = _rfbInstances[vmId];
      if (old?.disconnect) {
        try { old.disconnect(); } catch { /* ignore */ }
      }
      target.replaceChildren();

      const RFB = RFBClass.current;
      const rfb = new RFB(target, data.ws_url, {});
      _rfbInstances[vmId] = rfb;
      rfb.scaleViewport = true;
      rfb.resizeSession = false;
      rfb.focusOnClick = true;

      rfb.addEventListener("connect", () => {
        if (mountedRef.current) setRfbStatuses((prev) => ({ ...prev, [vmId]: "connected" }));
      });

      rfb.addEventListener("disconnect", () => {
        delete _rfbInstances[vmId];
        if (mountedRef.current && !hidden.has(vmId)) {
          setRfbStatuses((prev) => ({ ...prev, [vmId]: "reconnecting" }));
          reconnectTimers.current[vmId] = setTimeout(() => connectVm(vmId), 3000);
        }
      });
    } catch {
      if (mountedRef.current) {
        setRfbStatuses((prev) => ({ ...prev, [vmId]: "reconnecting" }));
        reconnectTimers.current[vmId] = setTimeout(() => connectVm(vmId), 3000);
      }
    }
  }, [projectId, hidden]);

  // Connect VMs when they become visible/running
  useEffect(() => {
    if (!RFBClass.current || vms.length === 0) return;

    const visibleVms = vms.filter((vm) => !hidden.has(vm.id));
    for (const vm of visibleVms) {
      const vmState = ws.vmStates[vm.id];
      const rfbStatus = rfbStatuses[vm.id];
      const hasRfb = !!_rfbInstances[vm.id];

      // Connect if running and not already connected/connecting
      if (vmState === "running" && !hasRfb && rfbStatus !== "connecting" && rfbStatus !== "connected") {
        connectVm(vm.id);
      }

      // Disconnect if VM stopped
      if (vmState && vmState !== "running" && hasRfb) {
        const rfb = _rfbInstances[vm.id];
        if (rfb?.disconnect) {
          try { rfb.disconnect(); } catch { /* ignore */ }
        }
        delete _rfbInstances[vm.id];
        setRfbStatuses((prev) => ({ ...prev, [vm.id]: "stopped" }));
      }
    }
  }, [vms, ws.vmStates, hidden, connectVm, rfbStatuses]);

  // Clean up on unmount
  useEffect(() => {
    return () => {
      Object.values(_rfbInstances).forEach((rfb: any) => {
        try { rfb?.disconnect?.(); } catch { /* ignore */ }
      });
      _rfbInstances = {};
    };
  }, []);

  // Focus management
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setFocusedVm(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // Track which canvas has focus
  useEffect(() => {
    const onFocusIn = (e: FocusEvent) => {
      const target = e.target as HTMLElement;
      for (const vm of vms) {
        const container = tileRefs.current[vm.id];
        if (container?.contains(target)) {
          setFocusedVm(vm.id);
          return;
        }
      }
    };
    document.addEventListener("focusin", onFocusIn);
    return () => document.removeEventListener("focusin", onFocusIn);
  }, [vms]);

  // Close when project is deleted
  useEffect(() => {
    if (ws.deleted) window.close();
  }, [ws.deleted]);

  const openConsole = useCallback((vmId: string, vmName: string) => {
    window.open(
      `/console?vm=${encodeURIComponent(vmId)}&project=${projectId}&name=${encodeURIComponent(vmName)}`,
      `console_${projectId.replace(/-/g, "")}_${vmId.replace(/-/g, "")}`,
      "width=1024,height=768,menubar=no,toolbar=no,location=no",
    );
  }, [projectId]);

  const visibleVms = vms.filter((vm) => !hidden.has(vm.id));
  const hiddenVms = vms.filter((vm) => hidden.has(vm.id));

  if (!projectId) {
    return <div style={{ padding: 20, color: "#fff", background: "#0d1117", height: "100vh" }}>Missing project parameter.</div>;
  }

  return (
    <div style={{ background: "#0d1117", minHeight: "100vh", color: "#e6edf3" }}>
      <style>{megaConsoleCSS}</style>

      {/* Overlay for closing hidden menu */}
      {hiddenMenuOpen && <div className="mc-overlay" onClick={() => setHiddenMenuOpen(false)} />}

      {/* Header */}
      <div className="mc-header">
        <div className="mc-header-left">
          <img src="/images/troshka-logo-dark-200.png" alt="" style={{ height: 22 }} />
          <span className="mc-project-name">{projectName || "Loading..."}</span>
          <span className="mc-vm-count">{visibleVms.length} of {vms.length} visible</span>
        </div>
        <div className="mc-header-right">
          <div className="mc-col-selector">
            {[2, 3, 4, 5].map((n) => (
              <button
                key={n}
                className={`mc-col-btn ${columns === n ? "active" : ""}`}
                onClick={() => setColumns(n)}
              >
                {n}
              </button>
            ))}
          </div>

          <div style={{ position: "relative" }}>
            <button className="mc-hidden-btn" onClick={() => setHiddenMenuOpen(!hiddenMenuOpen)}>
              <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor">
                <path d="M10.79 12.912l-1.614-1.615a3.5 3.5 0 01-4.474-4.474l-2.06-2.06C.938 6.278 0 8 0 8s3 5.5 8 5.5a7.029 7.029 0 002.79-.588zM5.21 3.088A7.028 7.028 0 018 2.5c5 0 8 5.5 8 5.5s-.939 1.721-2.641 3.238l-2.062-2.062a3.5 3.5 0 00-4.474-4.474L5.21 3.088z"/>
                <path d="M5.525 7.646a2.5 2.5 0 002.829 2.829l-2.83-2.829zm4.95.708l-2.829-2.83a2.5 2.5 0 012.829 2.829z"/>
                <path fillRule="evenodd" d="M13.646 14.354l-12-12 .708-.708 12 12-.708.708z"/>
              </svg>
              Hidden
              {hiddenVms.length > 0 && <span className="mc-hidden-badge">{hiddenVms.length}</span>}
            </button>
            {hiddenMenuOpen && (
              <div className="mc-hidden-menu">
                {hiddenVms.length === 0 ? (
                  <div className="mc-hidden-empty">No hidden VMs</div>
                ) : (
                  hiddenVms.map((vm) => {
                    const vmState = ws.vmStates[vm.id] || "unknown";
                    return (
                      <div key={vm.id} className="mc-hidden-item" onClick={() => showVm(vm.id)}>
                        <span>
                          <span className={`mc-status-dot ${vmState === "running" ? "running" : "stopped"}`} />
                          {vm.name}
                        </span>
                        <span className="mc-show-label">Show</span>
                      </div>
                    );
                  })
                )}
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Grid */}
      <div className={`mc-grid mc-cols-${columns}`}>
        {visibleVms.map((vm) => {
          const vmState = ws.vmStates[vm.id] || "unknown";
          const rfbStatus = rfbStatuses[vm.id] || "";
          const isRunning = vmState === "running";
          const isConnected = rfbStatus === "connected";
          const isFocused = focusedVm === vm.id;

          return (
            <div
              key={vm.id}
              className={`mc-tile ${isFocused ? "focused" : ""}`}
            >
              <div className="mc-tile-header">
                <span className="mc-tile-name">
                  <span className={`mc-status-dot ${isRunning ? "running" : "stopped"}`} />
                  {vm.name}
                  {isConnected && isFocused && (
                    <span className="mc-focus-badge">focused</span>
                  )}
                </span>
                <span className="mc-tile-actions">
                  <button
                    className="mc-tile-btn"
                    onClick={(e) => { e.stopPropagation(); openConsole(vm.id, vm.name); }}
                    title="Open full console"
                  >
                    <svg width="13" height="13" viewBox="0 0 16 16" fill="currentColor">
                      <path d="M5.828 10.172a.5.5 0 0 0-.707 0l-4.096 4.096V11.5a.5.5 0 0 0-1 0v3.975a.5.5 0 0 0 .5.5H4.5a.5.5 0 0 0 0-1H1.732l4.096-4.096a.5.5 0 0 0 0-.707z"/>
                      <path d="M10.172 5.828a.5.5 0 0 0 .707 0l4.096-4.096V4.5a.5.5 0 1 0 1 0V.525a.5.5 0 0 0-.5-.5H11.5a.5.5 0 0 0 0 1h2.768l-4.096 4.096a.5.5 0 0 0 0 .707z"/>
                    </svg>
                  </button>
                  <button
                    className="mc-tile-btn"
                    onClick={(e) => { e.stopPropagation(); hideVm(vm.id); }}
                    title="Hide this VM"
                  >
                    <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor">
                      <path d="M10.79 12.912l-1.614-1.615a3.5 3.5 0 01-4.474-4.474l-2.06-2.06C.938 6.278 0 8 0 8s3 5.5 8 5.5a7.029 7.029 0 002.79-.588zM5.21 3.088A7.028 7.028 0 018 2.5c5 0 8 5.5 8 5.5s-.939 1.721-2.641 3.238l-2.062-2.062a3.5 3.5 0 00-4.474-4.474L5.21 3.088z"/>
                      <path d="M5.525 7.646a2.5 2.5 0 002.829 2.829l-2.83-2.829zm4.95.708l-2.829-2.83a2.5 2.5 0 012.829 2.829z"/>
                      <path fillRule="evenodd" d="M13.646 14.354l-12-12 .708-.708 12 12-.708.708z"/>
                    </svg>
                  </button>
                </span>
              </div>

              <div className={`mc-console-area ${isRunning ? "running" : ""}`}>
                {isRunning ? (
                  <div
                    ref={(el) => { tileRefs.current[vm.id] = el; }}
                    className="mc-canvas-container"
                  />
                ) : (
                  <div className="mc-console-placeholder">
                    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" style={{ opacity: 0.4 }}>
                      <rect x="2" y="3" width="20" height="14" rx="2" />
                      <line x1="8" y1="21" x2="16" y2="21" />
                      <line x1="12" y1="17" x2="12" y2="21" />
                    </svg>
                    <span>{vmState === "stopped" || vmState === "shut off" ? "VM is stopped" : vmState === "unknown" ? "Loading..." : vmState}</span>
                  </div>
                )}
                {isRunning && !isConnected && (
                  <div className="mc-console-overlay">
                    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#58a6ff" strokeWidth="2" strokeLinecap="round" style={{ animation: "mc-spin 1s linear infinite" }}>
                      <path d="M12 2a10 10 0 0 1 10 10" />
                    </svg>
                    <span style={{ fontSize: 11, color: "#8b949e" }}>
                      {rfbStatus === "unavailable" ? "Console unavailable" : "Connecting..."}
                    </span>
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>

      {vms.length === 0 && (
        <div style={{ textAlign: "center", padding: 60, color: "#8b949e" }}>
          <div style={{ fontSize: 14 }}>Loading VMs...</div>
        </div>
      )}
    </div>
  );
}

const megaConsoleCSS = `
  @keyframes mc-spin { to { transform: rotate(360deg); } }
  @keyframes mc-tile-in {
    from { opacity: 0; transform: scale(0.95); }
    to { opacity: 1; transform: scale(1); }
  }

  .mc-header {
    position: sticky;
    top: 0;
    z-index: 100;
    display: flex;
    align-items: center;
    justify-content: space-between;
    height: 48px;
    padding: 0 16px;
    background: #161b22;
    border-bottom: 1px solid #30363d;
  }

  .mc-header-left {
    display: flex;
    align-items: center;
    gap: 12px;
  }

  .mc-project-name {
    font-size: 15px;
    font-weight: 600;
  }

  .mc-vm-count {
    font-size: 12px;
    color: #8b949e;
    background: #0d1117;
    padding: 2px 8px;
    border-radius: 10px;
  }

  .mc-header-right {
    display: flex;
    align-items: center;
    gap: 12px;
  }

  .mc-col-selector {
    display: flex;
    gap: 2px;
    background: #0d1117;
    border-radius: 6px;
    padding: 2px;
  }

  .mc-col-btn {
    background: none;
    border: none;
    color: #8b949e;
    font-size: 13px;
    font-weight: 500;
    padding: 4px 12px;
    border-radius: 4px;
    cursor: pointer;
    transition: all 0.15s;
  }
  .mc-col-btn:hover { color: #e6edf3; background: #1c2129; }
  .mc-col-btn.active { color: #e6edf3; background: #161b22; }

  .mc-hidden-btn {
    display: flex;
    align-items: center;
    gap: 6px;
    background: none;
    border: 1px solid #30363d;
    color: #8b949e;
    font-size: 13px;
    padding: 4px 10px;
    border-radius: 6px;
    cursor: pointer;
    transition: all 0.15s;
  }
  .mc-hidden-btn:hover { color: #e6edf3; border-color: #484f58; }

  .mc-hidden-badge {
    background: #58a6ff;
    color: #0d1117;
    font-size: 11px;
    font-weight: 600;
    padding: 0 6px;
    border-radius: 8px;
    min-width: 18px;
    text-align: center;
  }

  .mc-hidden-menu {
    position: absolute;
    top: calc(100% + 6px);
    right: 0;
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 8px;
    min-width: 200px;
    padding: 4px;
    box-shadow: 0 8px 24px rgba(0,0,0,0.4);
    z-index: 200;
  }

  .mc-hidden-empty {
    padding: 12px 16px;
    color: #8b949e;
    font-size: 13px;
    text-align: center;
  }

  .mc-hidden-item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 8px 12px;
    border-radius: 6px;
    cursor: pointer;
    transition: background 0.1s;
    font-size: 13px;
  }
  .mc-hidden-item:hover { background: #1c2129; }

  .mc-show-label {
    color: #58a6ff;
    font-size: 12px;
  }

  .mc-overlay {
    position: fixed;
    inset: 0;
    z-index: 99;
  }

  /* Grid */
  .mc-grid {
    display: grid;
    gap: 8px;
    padding: 12px;
    transition: all 0.25s ease;
  }
  .mc-cols-2 { grid-template-columns: repeat(2, 1fr); }
  .mc-cols-3 { grid-template-columns: repeat(3, 1fr); }
  .mc-cols-4 { grid-template-columns: repeat(4, 1fr); }
  .mc-cols-5 { grid-template-columns: repeat(5, 1fr); }

  /* Tile */
  .mc-tile {
    position: relative;
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 6px;
    overflow: hidden;
    transition: all 0.2s ease;
    animation: mc-tile-in 0.25s ease;
  }
  .mc-tile:hover { border-color: #484f58; }
  .mc-tile.focused { border-color: #3fb950; box-shadow: 0 0 0 1px #3fb950; }
  .mc-tile:hover .mc-tile-btn { opacity: 1; }

  .mc-tile-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 6px 8px;
    background: rgba(0,0,0,0.3);
  }

  .mc-tile-name {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;
    font-weight: 600;
  }

  .mc-focus-badge {
    font-size: 9px;
    font-weight: 500;
    color: #3fb950;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }

  .mc-status-dot {
    display: inline-block;
    width: 7px;
    height: 7px;
    border-radius: 50%;
    flex-shrink: 0;
  }
  .mc-status-dot.running { background: #3fb950; box-shadow: 0 0 6px #3fb950; }
  .mc-status-dot.stopped { background: #484f58; }

  .mc-tile-actions {
    display: flex;
    align-items: center;
    gap: 2px;
  }

  .mc-tile-btn {
    background: none;
    border: none;
    color: #8b949e;
    cursor: pointer;
    padding: 2px 4px;
    border-radius: 3px;
    line-height: 1;
    transition: all 0.15s;
    opacity: 0;
  }
  .mc-tile-btn:hover { color: #e6edf3; background: rgba(255,255,255,0.1); }

  /* Console area — uses aspect-ratio instead of padding-top so noVNC gets real dimensions */
  .mc-console-area {
    position: relative;
    width: 100%;
    aspect-ratio: 16 / 9;
    background: #0a0e14;
    overflow: hidden;
  }

  .mc-console-area.running::after {
    content: '';
    position: absolute;
    inset: 0;
    background: repeating-linear-gradient(0deg, transparent, transparent 2px, rgba(0,0,0,0.03) 2px, rgba(0,0,0,0.03) 4px);
    pointer-events: none;
    z-index: 1;
  }

  .mc-canvas-container {
    width: 100%;
    height: 100%;
  }

  .mc-console-placeholder {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-direction: column;
    gap: 6px;
    color: #8b949e;
    font-size: 13px;
  }

  .mc-console-overlay {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-direction: column;
    gap: 8px;
    background: rgba(10, 14, 20, 0.85);
    pointer-events: none;
    z-index: 2;
  }
`;
