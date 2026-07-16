"use client";

import React, { Suspense, useEffect, useRef, useState, useCallback } from "react";
import { useSearchParams, useParams } from "next/navigation";

export default function PortalConsoleWrapper() {
  return (
    <Suspense fallback={<div style={{ color: "#fff", padding: 20 }}>Loading console...</div>}>
      <PortalConsolePage />
    </Suspense>
  );
}

let _activeRfb: Record<string, unknown> | null = null;
let _activeToken: string | null = null;

function PortalConsolePage() {
  const params = useParams();
  const portalToken = params.token as string;
  const searchParams = useSearchParams();
  const vmId = searchParams.get("vm") || "";
  const projectId = searchParams.get("project") || "";
  const vmName = searchParams.get("name") || vmId.slice(0, 8) || "VM";
  const canvasRef = useRef<HTMLDivElement>(null);
  const [status, setStatus] = useState("Connecting...");
  const [wsUrl, setWsUrl] = useState<string | null>(null);
  const [scaled, setScaled] = useState(true);
  const [focused, setFocused] = useState(false);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const RFBClass = useRef<unknown>(null);
  const rfbRef = useRef<unknown>(null);
  const mountedRef = useRef(true);

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

  const fetchConsoleUrl = useCallback(async (): Promise<string | null> => {
    if (!portalToken || !vmId) return null;
    try {
      const resp = await fetch(`/api/v1/portal/${portalToken}/vms/${vmId}/console`);
      const data = await resp.json();
      if (data.ws_url) return data.ws_url;
    } catch { /* ignore */ }
    return null;
  }, [portalToken, vmId]);

  const pollForPort = useCallback(() => {
    if (!mountedRef.current) return;
    setStatus("Waiting for VM...");
    fetchConsoleUrl().then((url) => {
      if (!mountedRef.current) return;
      if (url) {
        setWsUrl(url);
      } else {
        reconnectTimer.current = setTimeout(pollForPort, 3000);
      }
    });
  }, [fetchConsoleUrl]);

  const createRfb = useCallback(() => {
    if (!wsUrl || !canvasRef.current || !RFBClass.current || !mountedRef.current) return;
    try {
      const old = rfbRef.current as { disconnect?: () => void; _rfbConnectionState?: string } | null;
      if (old?.disconnect && old._rfbConnectionState !== "disconnected") old.disconnect();
    } catch { /* ignore */ }
    rfbRef.current = null;
    if (canvasRef.current) canvasRef.current.replaceChildren();
    try {
      const RFB = RFBClass.current as new (target: HTMLElement, url: string, opts: Record<string, unknown>) => Record<string, unknown>;
      const rfb = new RFB(canvasRef.current!, wsUrl, {});
      rfbRef.current = rfb;
      rfb.scaleViewport = true;
      rfb.resizeSession = false;
      rfb.focusOnClick = true;
      const r = rfb as unknown as { addEventListener: (e: string, cb: () => void) => void };
      r.addEventListener("connect", () => {
        _activeRfb = rfb;
        _activeToken = wsUrl;
        if (mountedRef.current) setStatus("Connected");
      });
      r.addEventListener("disconnect", () => {
        _activeRfb = null;
        _activeToken = null;
        if (mountedRef.current) {
          setStatus("Reconnecting...");
          setWsUrl(null);
          reconnectTimer.current = setTimeout(pollForPort, 3000);
        }
      });
    } catch {
      if (mountedRef.current) {
        setStatus("Reconnecting...");
        setWsUrl(null);
        reconnectTimer.current = setTimeout(pollForPort, 3000);
      }
    }
  }, [wsUrl, pollForPort]);

  useEffect(() => {
    mountedRef.current = true;
    const init = async () => {
      try {
        RFBClass.current = (await import("@novnc/novnc")).default;
      } catch (err) {
        setStatus(`Failed to load noVNC: ${err}`);
      }
    };
    init();
    return () => {
      mountedRef.current = false;
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
    };
  }, []);

  useEffect(() => {
    if (_activeRfb && _activeToken) {
      rfbRef.current = _activeRfb;
      setWsUrl(_activeToken);
      setStatus("Connected");
      return;
    }
    if (wsUrl && RFBClass.current) {
      createRfb();
    } else if (!wsUrl) {
      pollForPort();
    }
  }, [wsUrl, createRfb, pollForPort]);

  useEffect(() => {
    document.title = vmName;
  }, [vmName]);

  useEffect(() => {
    const el = canvasRef.current;
    if (!el) return;
    const onIn = () => setFocused(true);
    const onOut = () => setFocused(false);
    el.addEventListener("focusin", onIn);
    el.addEventListener("focusout", onOut);
    return () => { el.removeEventListener("focusin", onIn); el.removeEventListener("focusout", onOut); };
  }, []);

  const statusColor = status === "Connected" ? "#4ade80" : status.startsWith("Waiting") ? "#94a3b8" : "#fbbf24";
  const btnStyle = { background: "none", border: "1px solid #555", color: "#fff", padding: "2px 8px", borderRadius: 4, fontSize: 11, cursor: "pointer" } as const;

  return (
    <div style={{ background: "#000", height: "100vh", display: "flex", flexDirection: "column" }}>
      <div style={{
        padding: "4px 12px",
        background: "#1a1a2e",
        color: "#fff",
        fontSize: 12,
        display: "flex",
        justifyContent: "space-between",
        alignItems: "center",
        borderBottom: "1px solid #333",
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span>{vmName}</span>
        </div>
        <span style={{ color: statusColor }}>{status}</span>
        <button
          onClick={() => {
            const next = !scaled;
            setScaled(next);
            const r = rfbRef.current as Record<string, any> | null;
            if (r) r.scaleViewport = next;
          }}
          style={{ ...btnStyle, background: scaled ? "rgba(74,222,128,0.15)" : "none", borderColor: scaled ? "#4ade80" : "#555" }}
        >
          {scaled ? "Scaled" : "1:1"}
        </button>
      </div>
      <div style={{ height: 2, background: focused ? "#4ade80" : "transparent", transition: "background 0.2s" }} />
      <div style={{ flex: 1, position: "relative", background: "#000" }}>
        <div ref={canvasRef} style={{ position: "absolute", top: 0, left: 0, right: 0, bottom: 0 }} />
        {status !== "Connected" && (
          <div style={{
            position: "absolute", inset: 0,
            display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center",
            background: "#000", color: "#555", gap: 12,
            pointerEvents: "none",
          }}>
            <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
              <rect x="2" y="3" width="20" height="14" rx="2" />
              <line x1="8" y1="21" x2="16" y2="21" />
              <line x1="12" y1="17" x2="12" y2="21" />
            </svg>
            <span style={{ fontSize: 13 }}>{status}</span>
          </div>
        )}
      </div>
    </div>
  );
}
