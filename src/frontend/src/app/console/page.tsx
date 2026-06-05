"use client";

import React, { useEffect, useRef, useState, useCallback } from "react";
import { useSearchParams } from "next/navigation";

export default function ConsolePage() {
  const searchParams = useSearchParams();
  const vmName = searchParams.get("vm") || "VM";
  const projectId = searchParams.get("project");
  const canvasRef = useRef<HTMLDivElement>(null);
  const [status, setStatus] = useState("Connecting...");
  const [wsPort, setWsPort] = useState<number | null>(null);
  const [scaled, setScaled] = useState(true);
  const rfbRef = useRef<unknown>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const RFBClass = useRef<unknown>(null);
  const mountedRef = useRef(true);

  // Suppress noVNC async errors that Next.js dev mode catches
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

  // Fetch WebSocket port from API, retry if VM not running
  const fetchConsolePort = useCallback(async (): Promise<number | null> => {
    if (!projectId || !vmName) return null;
    try {
      const resp = await fetch(`/api/v1/projects/${projectId}/vms/${vmName}/console`);
      const data = await resp.json();
      if (data.ws_port) return data.ws_port;
    } catch { /* ignore */ }
    return null;
  }, [projectId, vmName]);

  const pollForPort = useCallback(() => {
    if (!mountedRef.current) return;
    setStatus("Waiting for VM...");
    fetchConsolePort().then((port) => {
      if (!mountedRef.current) return;
      if (port) {
        setWsPort(port);
      } else {
        reconnectTimer.current = setTimeout(pollForPort, 3000);
      }
    });
  }, [fetchConsolePort]);

  const probe = useCallback(() => {
    if (!wsPort || !mountedRef.current) return;
    const testWs = new WebSocket(`ws://localhost:${wsPort}`);
    testWs.onopen = () => {
      testWs.close();
      if (mountedRef.current) createRfb();
    };
    testWs.onerror = () => {
      testWs.close();
      if (mountedRef.current) {
        // Port might be stale, re-fetch from API
        setWsPort(null);
        pollForPort();
      }
    };
  }, [wsPort]);

  const createRfb = useCallback(() => {
    if (!wsPort || !canvasRef.current || !RFBClass.current || !mountedRef.current) return;

    try {
      const old = rfbRef.current as { disconnect?: () => void; _rfbConnectionState?: string } | null;
      if (old?.disconnect && old._rfbConnectionState !== "disconnected") old.disconnect();
    } catch { /* ignore */ }
    rfbRef.current = null;
    if (canvasRef.current) canvasRef.current.replaceChildren();

    try {
      const RFB = RFBClass.current as new (target: HTMLElement, url: string, opts: Record<string, unknown>) => Record<string, unknown>;
      const rfb = new RFB(canvasRef.current!, `ws://localhost:${wsPort}`, {});
      rfbRef.current = rfb;
      rfb.scaleViewport = true;
      rfb.resizeSession = true;
      rfb.focusOnClick = true;

      const r = rfb as unknown as { addEventListener: (e: string, cb: (ev: Record<string, unknown>) => void) => void };
      r.addEventListener("connect", () => {
        if (mountedRef.current) setStatus("Connected");
      });
      r.addEventListener("disconnect", () => {
        if (mountedRef.current) {
          setStatus("Reconnecting...");
          reconnectTimer.current = setTimeout(probe, 3000);
        }
      });
    } catch {
      if (mountedRef.current) {
        setStatus("Reconnecting...");
        reconnectTimer.current = setTimeout(probe, 3000);
      }
    }
  }, [wsPort, probe]);

  // Load noVNC module once
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
      try {
        const rfb = rfbRef.current as { disconnect?: () => void; _rfbConnectionState?: string } | null;
        if (rfb?.disconnect && rfb._rfbConnectionState !== "disconnected") rfb.disconnect();
      } catch { /* ignore */ }
    };
  }, []);

  // When we have a port, connect. When we don't, poll for one.
  useEffect(() => {
    if (wsPort && RFBClass.current) {
      createRfb();
    } else if (!wsPort) {
      pollForPort();
    }
  }, [wsPort, createRfb, pollForPort]);

  useEffect(() => {
    document.title = `Console: ${vmName}`;
  }, [vmName]);

  const btnStyle = { background: "none", border: "1px solid #555", color: "#fff", padding: "2px 8px", borderRadius: 4, fontSize: 11, cursor: "pointer" } as const;

  if (!projectId) {
    return (
      <div style={{ padding: 20, color: "#fff", background: "#000", height: "100vh" }}>
        <p>Missing project parameter.</p>
      </div>
    );
  }

  const statusColor = status === "Connected" ? "#4ade80" : status.startsWith("Waiting") ? "#94a3b8" : "#fbbf24";

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
        <span>{vmName}</span>
        <span style={{ color: statusColor }}>{status}</span>
        <div style={{ display: "flex", gap: 8 }}>
          <button
            onClick={() => {
              const next = !scaled;
              setScaled(next);
              const r = rfbRef.current as Record<string, unknown> | null;
              if (r) r.scaleViewport = next;
            }}
            style={{ ...btnStyle, background: scaled ? "rgba(74,222,128,0.15)" : "none", borderColor: scaled ? "#4ade80" : "#555" }}
          >
            {scaled ? "Scaled" : "1:1"}
          </button>
          <button
            onClick={async () => {
              let text = "";
              try {
                text = await navigator.clipboard.readText();
              } catch {
                text = window.prompt("Paste text to send to VM:") || "";
              }
              if (!text) return;
              const r = rfbRef.current as Record<string, unknown> | null;
              if (!r) return;
              const sendKey = r.sendKey as ((k: number, c: string | null, d?: boolean) => void) | undefined;
              if (!sendKey) return;

              // Characters that require Shift on US keyboard
              const shiftChars: Record<string, number> = {
                "_": 0x005f, "~": 0x007e, "!": 0x0021, "@": 0x0040,
                "#": 0x0023, "$": 0x0024, "%": 0x0025, "^": 0x005e,
                "&": 0x0026, "*": 0x002a, "(": 0x0028, ")": 0x0029,
                "+": 0x002b, "{": 0x007b, "}": 0x007d, "|": 0x007c,
                ":": 0x003a, '"': 0x0022, "<": 0x003c, ">": 0x003e,
                "?": 0x003f,
              };
              const shiftKeysym = 0xffe1; // XK_Shift_L

              const controlKeys: Record<string, number> = {
                "\n": 0xff0d, "\r": 0xff0d, "\t": 0xff09,
              };

              for (const ch of text) {
                if (ch in controlKeys) {
                  sendKey.call(r, controlKeys[ch], "", true);
                  sendKey.call(r, controlKeys[ch], "", false);
                  continue;
                }
                let keysym = ch.charCodeAt(0);
                if (keysym > 0x00ff) keysym = 0x01000000 | keysym;

                const needsShift = ch in shiftChars || (ch >= "A" && ch <= "Z");
                if (needsShift) sendKey.call(r, shiftKeysym, "", true);
                sendKey.call(r, keysym, "", true);
                sendKey.call(r, keysym, "", false);
                if (needsShift) sendKey.call(r, shiftKeysym, "", false);
              }
            }}
            style={btnStyle}
          >
            Paste
          </button>
          <button
            onClick={() => {
              const r = rfbRef.current as { sendCtrlAltDel: () => void } | null;
              if (r) r.sendCtrlAltDel();
            }}
            style={btnStyle}
          >
            Ctrl+Alt+Del
          </button>
        </div>
      </div>
      <div style={{ flex: 1, position: "relative", background: "#000" }}>
        <div ref={canvasRef} style={{ width: "100%", height: "100%" }} />
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
              <line x1="2" y1="3" x2="22" y2="17" stroke="#ef4444" strokeWidth="2" />
            </svg>
            <span style={{ fontSize: 13 }}>{status}</span>
          </div>
        )}
      </div>
    </div>
  );
}
