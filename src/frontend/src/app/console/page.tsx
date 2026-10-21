"use client";

import React, { Suspense, useEffect, useRef, useState, useCallback } from "react";
import { useSearchParams } from "next/navigation";
import AlertModal from "@/components/AlertModal";
import { appConfirm } from "@/lib/confirm";
import { useVmStateSocket } from "@/hooks/useVmStateSocket";

export default function ConsolePageWrapper() {
  return (
    <Suspense fallback={<div style={{ color: "#fff", padding: 20 }}>Loading console...</div>}>
      <ConsolePage />
    </Suspense>
  );
}

function ConsolePage() {
  const searchParams = useSearchParams();
  const vmId = searchParams.get("vm") || "";
  const projectId = searchParams.get("project");
  const vmName = searchParams.get("name") || vmId.slice(0, 8) || "VM";
  return <ConsoleSession key={`${projectId}/${vmId}`} projectId={projectId} vmId={vmId} vmName={vmName} />;
}

type ConsoleKeyboard = {
  sendKey?: (keysym: number, code: string | null, down?: boolean) => void;
  focus?: () => void;
};

function sendConsoleText(rfb: ConsoleKeyboard, text: string, controlCharacters: boolean) {
  const sendKey = rfb.sendKey;
  if (!sendKey) return;
  const controlKeys: Record<string, number> = { "\n": 0xff0d, "\r": 0xff0d, "\t": 0xff09 };
  const shifted = new Set('_~!@#$%^&*()+{}|:"<>?');
  for (const ch of text) {
    if (controlCharacters && ch in controlKeys) {
      sendKey.call(rfb, controlKeys[ch], "", true);
      sendKey.call(rfb, controlKeys[ch], "", false);
      continue;
    }
    let keysym = ch.codePointAt(0)!;
    if (keysym > 0x00ff) keysym = 0x01000000 | keysym;
    const needsShift = shifted.has(ch) || (ch >= "A" && ch <= "Z");
    if (needsShift) sendKey.call(rfb, 0xffe1, "", true);
    sendKey.call(rfb, keysym, "", true);
    sendKey.call(rfb, keysym, "", false);
    if (needsShift) sendKey.call(rfb, 0xffe1, "", false);
  }
}

async function captureConsoleScreenshot(canvas: HTMLCanvasElement, vmName: string, setToast: (message: string | null) => void) {
  const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve));
  if (!blob) return;
  try {
    await navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]);
    setToast("Screenshot copied");
  } catch {
    const link = document.createElement("a");
    link.href = canvas.toDataURL("image/png");
    link.download = `${vmName}-${new Date().toISOString().slice(0, 19)}.png`;
    link.click();
    setToast("Screenshot saved");
  }
  setTimeout(() => setToast(null), 2000);
}

function consoleStatus(headless: boolean, starting: boolean, poweredOff: boolean, status: string): string {
  if (headless) return "Headless Mode";
  if (starting) return "Starting...";
  return poweredOff && status !== "Connected" ? "Powered Off" : status;
}

function consoleStatusColor(headless: boolean, status: string): string {
  if (headless || status.startsWith("Waiting")) return "#94a3b8";
  if (status === "Connected" || status === "Starting...") return "#4ade80";
  return status === "Powered Off" ? "#ef4444" : "#fbbf24";
}

type ConsoleMenu = "linux" | "windows" | "power" | null;

function ConsoleToolbar({ scaled, setScaled, rfbRef, btnStyle, vmName, setToast, lastPasteRef, vmPasswords, openKeyboard, linuxMacros, windowsMacros, openMenu, setOpenMenu, sendCombo, vmState, vmPowerAction }: Readonly<{
  scaled: boolean;
  setScaled: React.Dispatch<React.SetStateAction<boolean>>;
  rfbRef: React.RefObject<unknown>;
  btnStyle: React.CSSProperties;
  vmName: string;
  setToast: React.Dispatch<React.SetStateAction<string | null>>;
  lastPasteRef: React.RefObject<number>;
  vmPasswords: { label: string; value: string; }[];
  openKeyboard: () => void;
  linuxMacros: { label: string; keys: number[]; }[];
  windowsMacros: { label: string; keys: number[]; }[];
  openMenu: ConsoleMenu;
  setOpenMenu: React.Dispatch<React.SetStateAction<ConsoleMenu>>;
  sendCombo: (keysyms: number[]) => void;
  vmState: string | null;
  vmPowerAction: (action: string, label: string, confirm?: string) => Promise<void>;
}>) {
  return (<div style={{ display: "flex", gap: 8 }}>
    <button type="button"
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
    <button type="button"
      onClick={() => {
        const r = rfbRef.current as Record<string, any> | null;
        if (!r?._canvas) return;
        const canvas = r._canvas as HTMLCanvasElement;
        void captureConsoleScreenshot(canvas, vmName, setToast);
      }}
      style={btnStyle}
      title="Copy screenshot to clipboard"
    >
      Screenshot
    </button>
    <button type="button"
      onClick={async (e) => {
        (e.target as HTMLElement).blur();
        if (Date.now() - lastPasteRef.current < 2000) return;
        lastPasteRef.current = Date.now();
        let text = "";
        try {
          text = await navigator.clipboard.readText();
        } catch {
          text = window.prompt("Paste text to send to VM:") || "";
        }
        if (!text) return;
        const r = rfbRef.current as Record<string, any> | null;
        if (!r) return;
        const sendKey = r.sendKey as ((k: number, c: string | null, d?: boolean) => void) | undefined;
        if (!sendKey) return;

        sendConsoleText(r, text, true);
        r.focus?.();
      }}
      style={btnStyle}
    >
      Paste
    </button>
    {vmPasswords.map((pw) => (
      <button type="button"
        key={pw.label}
        onClick={async (e) => {
          (e.target as HTMLElement).blur();
          if (Date.now() - lastPasteRef.current < 2000) return;
          lastPasteRef.current = Date.now();
          const r = rfbRef.current as Record<string, any> | null;
          if (!r) return;
          const sendKey = r.sendKey as ((k: number, c: string | null, d?: boolean) => void) | undefined;
          if (!sendKey) return;
          sendConsoleText(r, pw.value, false);
          sendKey.call(r, 0xff0d, "", true);
          sendKey.call(r, 0xff0d, "", false);
          setToast(`${pw.label} password sent`);
          setTimeout(() => setToast(null), 2000);
          r.focus?.();
        }}
        style={{ ...btnStyle, fontSize: 11 }}
        title={`Type ${pw.label} password + Enter`}
      >
        🔑 {pw.label}
      </button>
    ))}
    <button type="button"
      onClick={openKeyboard}
      style={{ ...btnStyle, display: "flex", alignItems: "center", gap: 4 }}
      title="Virtual Keyboard"
    >
      <svg width="18" height="12" viewBox="0 0 18 12" fill="none" stroke="currentColor" strokeWidth="0.8">
        <rect x="0.5" y="0.5" width="17" height="11" rx="1.5" />
        <rect x="2" y="2" width="2" height="1.5" rx="0.3" fill="currentColor" stroke="none" />
        <rect x="5" y="2" width="2" height="1.5" rx="0.3" fill="currentColor" stroke="none" />
        <rect x="8" y="2" width="2" height="1.5" rx="0.3" fill="currentColor" stroke="none" />
        <rect x="11" y="2" width="2" height="1.5" rx="0.3" fill="currentColor" stroke="none" />
        <rect x="14" y="2" width="2" height="1.5" rx="0.3" fill="currentColor" stroke="none" />
        <rect x="2" y="5" width="2" height="1.5" rx="0.3" fill="currentColor" stroke="none" />
        <rect x="5" y="5" width="2" height="1.5" rx="0.3" fill="currentColor" stroke="none" />
        <rect x="8" y="5" width="2" height="1.5" rx="0.3" fill="currentColor" stroke="none" />
        <rect x="11" y="5" width="2" height="1.5" rx="0.3" fill="currentColor" stroke="none" />
        <rect x="14" y="5" width="2" height="1.5" rx="0.3" fill="currentColor" stroke="none" />
        <rect x="5" y="8.5" width="8" height="1.5" rx="0.3" fill="currentColor" stroke="none" />
      </svg>
    </button>
    {(["linux", "windows"] as const).map((os) => {
      const macros = os === "linux" ? linuxMacros : windowsMacros;
      const isOpen = openMenu === os;
      return (
        <div key={os} style={{ position: "relative" }}>
          <button type="button"
            onClick={(e) => { e.stopPropagation(); setOpenMenu(isOpen ? null : os); }}
            style={{ ...btnStyle, background: isOpen ? "rgba(74,222,128,0.15)" : "none", borderColor: isOpen ? "#4ade80" : "#555" }}
          >
            {os === "linux" ? "Linux" : "Windows"} ▾
          </button>
          {isOpen && (
            <div
              style={{
                position: "absolute", top: "100%", right: 0, marginTop: 4,
                background: "#1a1a2e", border: "1px solid #444", borderRadius: 6,
                padding: "4px 0", minWidth: 220, zIndex: 100,
                boxShadow: "0 8px 24px rgba(0,0,0,0.6)",
              }}
            >
              {macros.map((m) => (
                <button type="button"
                  key={m.label}
                  onClick={() => { sendCombo(m.keys); setOpenMenu(null); }}
                  style={{
                    display: "block", width: "100%", textAlign: "left",
                    background: "none", border: "none", color: "#fff",
                    padding: "5px 12px", fontSize: 11, cursor: "pointer",
                    whiteSpace: "nowrap",
                  }}
                  onMouseEnter={(e) => { (e.target as HTMLElement).style.background = "rgba(255,255,255,0.08)"; }}
                  onMouseLeave={(e) => { (e.target as HTMLElement).style.background = "none"; }}
                >
                  {m.label}
                </button>
              ))}
            </div>
          )}
        </div>
      );
    })}
    <div style={{ position: "relative" }}>
      <button type="button"
        onClick={(e) => { e.stopPropagation(); setOpenMenu(openMenu === "power" ? null : "power"); }}
        style={{ ...btnStyle, background: openMenu === "power" ? "rgba(251,191,36,0.15)" : "none", borderColor: openMenu === "power" ? "#fbbf24" : "#555", color: openMenu === "power" ? "#fbbf24" : "#fff" }}
      >
        Power ▾
      </button>
      {openMenu === "power" && (
        <div
          style={{
            position: "absolute", top: "100%", right: 0, marginTop: 4,
            background: "#1a1a2e", border: "1px solid #444", borderRadius: 6,
            padding: "4px 0", minWidth: 180, zIndex: 100,
            boxShadow: "0 8px 24px rgba(0,0,0,0.6)",
          }}
        >
          {([
            ...(vmState !== "running" ? [{ action: "start", label: "Start" }] : []),
            ...(vmState === "running" ? [{ action: "restart", label: "Restart", confirm: `Restart "${vmName}"?` }] : []),
            ...(vmState === "running" ? [{ action: "stop", label: "Shutdown", confirm: `Shut down "${vmName}"?` }] : []),
            ...(vmState === "running" ? [{ action: "forcestop", label: "Force Off", confirm: `Force off "${vmName}"? This may cause data loss.` }] : []),
          ] as { action: string; label: string; confirm?: string }[]).map((item) => (
            <button type="button"
              key={item.action}
              onClick={() => { setOpenMenu(null); vmPowerAction(item.action, item.label, "confirm" in item ? item.confirm : undefined); }}
              style={{
                display: "block", width: "100%", textAlign: "left",
                background: "none", border: "none",
                color: item.action === "forcestop" ? "#f87171" : "#fff",
                padding: "5px 12px", fontSize: 11, cursor: "pointer",
                whiteSpace: "nowrap",
              }}
              onMouseEnter={(e) => { (e.target as HTMLElement).style.background = "rgba(255,255,255,0.08)"; }}
              onMouseLeave={(e) => { (e.target as HTMLElement).style.background = "none"; }}
            >
              {item.label}
            </button>
          ))}
        </div>
      )}
    </div>
  </div>);
}

function ConsoleOverlay({ displayStatus, headless, fetchConsoleUrl, setWsUrl, startingRef }: Readonly<{
  displayStatus: string;
  headless: boolean;
  fetchConsoleUrl: (force?: boolean) => Promise<{ url: string; hostType: string; headless?: boolean; } | null>;
  setWsUrl: React.Dispatch<React.SetStateAction<string | null>>;
  startingRef: React.RefObject<boolean>;
}>) {
  return (<div style={{
    position: "absolute", inset: 0,
    display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center",
    background: "#000", color: "#555", gap: 12,
    pointerEvents: displayStatus === "Console in use by another session" ? "auto" : "none",
  }}>
    {headless ? (
      <>
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="#94a3b8" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
          <rect x="2" y="3" width="20" height="14" rx="2" />
          <line x1="8" y1="21" x2="16" y2="21" />
          <line x1="12" y1="17" x2="12" y2="21" />
          <path d="M6 7h12M6 11h8" />
        </svg>
        <span style={{ fontSize: 15, color: "#94a3b8", fontWeight: 500 }}>Headless Mode</span>
        <span style={{ fontSize: 12, color: "#666", maxWidth: 360, textAlign: "center", lineHeight: 1.5 }}>
          VNC is disabled so the guest uses the serial port (ttyS0).
          Use VM exec with <code style={{ color: "#888" }}>method: serial</code> or the bootstrap playbook.
        </span>
      </>
    ) : null}
    {!headless && displayStatus === "Console in use by another session" && (
      <>
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="#fbbf24" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
          <rect x="2" y="3" width="20" height="14" rx="2" />
          <line x1="8" y1="21" x2="16" y2="21" />
          <line x1="12" y1="17" x2="12" y2="21" />
        </svg>
        <span style={{ fontSize: 13, color: "#fbbf24" }}>Console in use by another session</span>
        <button type="button"
          onClick={() => {
            fetchConsoleUrl().then((result) => {
              if (!result) return;
              const forceUrl = result.url.replace(/\/([^/]+)$/, "/force/$1");
              setWsUrl(forceUrl);
            });
          }}
          style={{ padding: "6px 16px", borderRadius: 6, border: "1px solid #ef4444", background: "rgba(239,68,68,0.15)", color: "#ef4444", cursor: "pointer", fontSize: 13 }}
        >
          Force Connect
        </button>
      </>
    )}
    {!headless && displayStatus !== "Console in use by another session" && startingRef.current && (
      <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="#4ade80" strokeWidth="2" strokeLinecap="round" style={{ animation: "vkb-spin 1s linear infinite" }}>
        <path d="M12 2a10 10 0 0 1 10 10" />
      </svg>
    )}
    {!headless && displayStatus !== "Console in use by another session" && !startingRef.current && (
      <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <rect x="2" y="3" width="20" height="14" rx="2" />
        <line x1="8" y1="21" x2="16" y2="21" />
        <line x1="12" y1="17" x2="12" y2="21" />
        <line x1="2" y1="3" x2="22" y2="17" stroke="#ef4444" strokeWidth="2" />
      </svg>
    )}
    <span style={{ fontSize: 13, color: startingRef.current ? "#4ade80" : undefined }}>{displayStatus}</span>
  </div>);
}

function ConsoleSession({ projectId, vmId, vmName }: Readonly<{ projectId: string | null; vmId: string; vmName: string }>) {
  const canvasRef = useRef<HTMLDivElement>(null);
  const [status, setStatus] = useState("Connecting...");
  const [headless, setHeadless] = useState(false);
  const [wsUrl, setWsUrl] = useState<string | null>(null);
  const [scaled, setScaled] = useState(true);
  const [focused, setFocused] = useState(false);
  const [openMenu, setOpenMenu] = useState<ConsoleMenu>(null);
  const [vmState, setVmState] = useState<string | null>(null);
  const ws = useVmStateSocket(projectId);
  const projectDeleted = useRef(false);
  const [toast, setToast] = useState<string | null>(null);
  const [vmPasswords, setVmPasswords] = useState<{label: string; value: string}[]>([]);
  const [alertMsg, setAlertMsg] = useState<string | null>(null);
  const [projectName, setProjectName] = useState("");
  const startingRef = useRef(false);
  const kbWindowRef = useRef<Window | null>(null);
  const rfbRef = useRef<unknown>(null);
  const lastPasteRef = useRef(0);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const RFBClass = useRef<unknown>(null);
  const [rfbReady, setRfbReady] = useState(false);
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

  // Fetch VM password from topology
  useEffect(() => {
    if (!projectId || !vmId) return;
    fetch(`/api/v1/projects/${projectId}`)
      .then((r) => r.ok ? r.json() : null)
      .then((data) => {
        if (!data) return;
        if (data.name) setProjectName(data.name);
        if (!data.topology?.nodes) return;
        const vm = data.topology.nodes.find((n: any) => n.id === vmId && n.type === "vmNode");
        if (!vm?.data) return;
        const pw: {label: string; value: string}[] = [];
        if (vm.data.ciCloudUserPassword) pw.push({ label: "cloud-user", value: vm.data.ciCloudUserPassword });
        if (vm.data.ciRootPassword) pw.push({ label: "root", value: vm.data.ciRootPassword });
        setVmPasswords(pw);
      })
      .catch(() => {});
  }, [projectId, vmId]);

  // Fetch console WebSocket URL from API
  const fetchConsoleUrl = useCallback(async (force?: boolean): Promise<{url: string; hostType: string; headless?: boolean} | null> => {
    if (!projectId || !vmId || projectDeleted.current) return null;
    try {
      const q = force ? "?force=true" : "";
      const resp = await fetch(`/api/v1/projects/${projectId}/vms/${vmId}/console${q}`);
      if (!mountedRef.current) return null;
      if (resp.status === 404) { projectDeleted.current = true; setStatus("Project deleted"); return null; }
      const data = await resp.json();
      if (data.headless) {
        setHeadless(true);
        setStatus("Headless Mode");
        return { url: "", hostType: data.host_type || "", headless: true };
      }
      setHeadless(false);
      if (data.ws_url) return { url: data.ws_url, hostType: data.host_type || "" };
    } catch { /* ignore */ }
    return null;
  }, [projectId, vmId]);

  // Pre-flight: open a plain WebSocket to check if session is available
  // If proxy rejects with 4010, the session is in use — no noVNC needed
  const preflightCheck = useCallback((url: string): Promise<"ok" | "in_use" | "error"> => {
    return new Promise((resolve) => {
      const ws = new WebSocket(url);
      const timer = setTimeout(() => {
        ws.close();
        resolve("ok");
      }, 800);
      ws.onclose = (e) => {
        clearTimeout(timer);
        if (e.code === 4010) {
          resolve("in_use");
        } else if (e.code === 4000) {
          resolve("ok");
        } else {
          resolve("error");
        }
      };
      ws.onerror = () => {
        clearTimeout(timer);
        resolve("error");
      };
    });
  }, []);

  const pollForPort = useCallback(() => {
    if (!projectId || !vmId || !mountedRef.current || projectDeleted.current) return;
    setStatus("Waiting for VM...");
    fetchConsoleUrl().then((result) => {
      if (!mountedRef.current || projectDeleted.current) return;
      if (!result) {
        reconnectTimer.current = setTimeout(pollForPort, 3000);
        return;
      }
      if (result.headless) return;
      const { url, hostType } = result;
      if (hostType === "kubevirt-cluster") {
        // KubeVirt: pre-flight check for session-in-use detection
        preflightCheck(url.replace("/ws/", "/check/")).then((check) => {
          if (!mountedRef.current || projectDeleted.current) return;
          if (check === "in_use") {
            setStatus("Console in use by another session");
            return;
          }
          if (check === "ok") {
            setWsUrl(url);
          } else {
            reconnectTimer.current = setTimeout(pollForPort, 3000);
          }
        });
      } else {
        // troshkad: connect directly, no pre-flight
        setWsUrl(url);
      }
    });
  }, [fetchConsoleUrl, preflightCheck, projectId, vmId]);

  const createRfb = useCallback(() => {
    if (!wsUrl || !canvasRef.current || !RFBClass.current || !mountedRef.current) return;

    try {
      const old = rfbRef.current as { disconnect?: () => void; _rfbConnectionState?: string } | null;
      rfbRef.current = null;
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

      const r = rfb as unknown as { addEventListener: (e: string, cb: (ev: Record<string, unknown>) => void) => void };
      r.addEventListener("connect", () => {
        if (rfbRef.current !== rfb) return;
        if (mountedRef.current) { startingRef.current = false; setStatus("Connected"); }
      });
      r.addEventListener("disconnect", () => {
        if (!mountedRef.current || rfbRef.current !== rfb) return;
        if (reconnectTimer.current) { clearTimeout(reconnectTimer.current); reconnectTimer.current = null; }
        setStatus("Reconnecting...");
        setWsUrl(null);
        reconnectTimer.current = setTimeout(pollForPort, 3000);
      });
    } catch {
      if (mountedRef.current) {
        setStatus("Reconnecting...");
        setWsUrl(null);
        reconnectTimer.current = setTimeout(pollForPort, 3000);
      }
    }
  }, [wsUrl, pollForPort]);

  // Load noVNC module once
  useEffect(() => {
    mountedRef.current = true;

    const init = async () => {
      try {
        RFBClass.current = (await import("@novnc/novnc")).default;
        if (mountedRef.current) setRfbReady(true);
      } catch (err) {
        setStatus(`Failed to load noVNC: ${err}`);
      }
    };
    init();

    return () => {
      mountedRef.current = false;
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
      const rfb = rfbRef.current as { disconnect?: () => void } | null;
      rfbRef.current = null;
      rfb?.disconnect?.();
    };
  }, []);

  // Module loading and URL discovery are independent; connect once both finish.
  useEffect(() => {
    if (wsUrl && rfbReady) createRfb();
  }, [wsUrl, rfbReady, createRfb]);

  useEffect(() => { pollForPort(); }, [pollForPort]);

  useEffect(() => {
    if (ws.deleted) {
      projectDeleted.current = true;
      if (reconnectTimer.current) { clearTimeout(reconnectTimer.current); reconnectTimer.current = null; }
      const rfb = rfbRef.current as { disconnect?: () => void } | null;
      rfbRef.current = null;
      rfb?.disconnect?.();
      window.close();
    }
  }, [ws.deleted]);

  useEffect(() => {
    document.title = projectName ? `${projectName} — ${vmName}` : vmName;
  }, [vmName, projectName]);

  // WebSocket → VM state
  useEffect(() => {
    if (!vmId || !ws.vmStates[vmId]) return;
    const state = ws.vmStates[vmId];
    setVmState(state);
    if (state === "starting") {
      startingRef.current = true;
      setStatus("Starting...");
    } else if (state === "running" && startingRef.current) {
      startingRef.current = false;
    }
  }, [ws.vmStates, vmId]);

  useEffect(() => {
    const el = canvasRef.current;
    if (!el) return;
    const onIn = () => setFocused(true);
    const onOut = () => setFocused(false);
    el.addEventListener("focusin", onIn);
    el.addEventListener("focusout", onOut);
    return () => { el.removeEventListener("focusin", onIn); el.removeEventListener("focusout", onOut); };
  }, []);

  const btnStyle = { background: "none", border: "1px solid #555", color: "#fff", padding: "2px 8px", borderRadius: 4, fontSize: 11, cursor: "pointer" } as const;

  // Close dropdown on outside click
  useEffect(() => {
    if (!openMenu) return;
    const close = () => setOpenMenu(null);
    window.addEventListener("click", close);
    return () => window.removeEventListener("click", close);
  }, [openMenu]);

  const sendCombo = useCallback((keysyms: number[]) => {
    const r = rfbRef.current as Record<string, any> | null;
    if (!r?.sendKey) return;
    const send = r.sendKey as (k: number, c: string | null, d?: boolean) => void;
    for (const k of keysyms) send.call(r, k, null, true);
    for (const k of [...keysyms].reverse()) send.call(r, k, null, false);
  }, []);

  // Listen for key combos from the virtual keyboard popup
  useEffect(() => {
    const onMessage = (e: MessageEvent) => {
      if (e.origin !== window.location.origin) return;
      if (e.data?.type === "vkb-combo" && Array.isArray(e.data.keysyms)) {
        sendCombo(e.data.keysyms);
      }
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [sendCombo]);

  // Close keyboard popup when console page unloads
  useEffect(() => {
    return () => { kbWindowRef.current?.close(); };
  }, []);

  const openKeyboard = useCallback(() => {
    if (kbWindowRef.current && !kbWindowRef.current.closed) {
      kbWindowRef.current.focus();
      return;
    }
    kbWindowRef.current = window.open(
      `/console/keyboard?name=${encodeURIComponent(vmName)}`,
      "troshka-vkb",
      "width=900,height=290,menubar=no,toolbar=no,location=no,status=no",
    );
  }, [vmName]);

  const vmPowerAction = useCallback(async (action: string, label: string, confirm?: string) => {
    if (confirm && !(await appConfirm({ message: confirm, confirmLabel: label }))) return;
    if (action === "start") { startingRef.current = true; setStatus("Starting..."); }
    try {
      const resp = await fetch(`/api/v1/projects/${projectId}/vms/${vmId}/${action}`, { method: "POST" });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({ detail: `${label} failed` }));
        setAlertMsg(err.detail || `${label} failed`);
      }
    } catch {
      setAlertMsg("Failed to connect to server");
    }
  }, [projectId, vmId]);

  const XK = {
    Ctrl: 0xffe3, Alt: 0xffe9, Shift: 0xffe1, Super: 0xffeb,
    Tab: 0xff09, Esc: 0xff1b, Del: 0xffff, Return: 0xff0d,
    F1: 0xffbe, F2: 0xffbf, F3: 0xffc0, F4: 0xffc1, F5: 0xffc2, F6: 0xffc3,
    F7: 0xffc4, F8: 0xffc5, F9: 0xffc6, F10: 0xffc7, F11: 0xffc8, F12: 0xffc9,
  } as const;
  const k = (ch: string) => ch.codePointAt(0)!; // ASCII letter keysym

  type Macro = { label: string; keys: number[] };
  const linuxMacros: Macro[] = [
    { label: "Ctrl+C  Interrupt", keys: [XK.Ctrl, k("c")] },
    { label: "Ctrl+D  EOF / Logout", keys: [XK.Ctrl, k("d")] },
    { label: "Ctrl+Z  Suspend", keys: [XK.Ctrl, k("z")] },
    { label: "Ctrl+L  Clear", keys: [XK.Ctrl, k("l")] },
    { label: "Ctrl+Alt+T  Terminal", keys: [XK.Ctrl, XK.Alt, k("t")] },
    { label: "Alt+F2  Run Dialog", keys: [XK.Alt, XK.F2] },
    { label: "Alt+Tab  Switch Window", keys: [XK.Alt, XK.Tab] },
    { label: "Ctrl+Alt+F1  TTY 1", keys: [XK.Ctrl, XK.Alt, XK.F1] },
    { label: "Ctrl+Alt+F2  TTY 2", keys: [XK.Ctrl, XK.Alt, XK.F2] },
    { label: "Ctrl+Alt+F3  TTY 3", keys: [XK.Ctrl, XK.Alt, XK.F3] },
    { label: "Ctrl+Alt+Del", keys: [XK.Ctrl, XK.Alt, XK.Del] },
  ];
  const windowsMacros: Macro[] = [
    { label: "Ctrl+Alt+Del", keys: [XK.Ctrl, XK.Alt, XK.Del] },
    { label: "Alt+Tab  Switch Window", keys: [XK.Alt, XK.Tab] },
    { label: "Alt+F4  Close Window", keys: [XK.Alt, XK.F4] },
    { label: "Win  Start Menu", keys: [XK.Super] },
    { label: "Win+R  Run", keys: [XK.Super, k("r")] },
    { label: "Win+E  Explorer", keys: [XK.Super, k("e")] },
    { label: "Win+D  Show Desktop", keys: [XK.Super, k("d")] },
    { label: "Win+L  Lock", keys: [XK.Super, k("l")] },
    { label: "Ctrl+Shift+Esc  Task Mgr", keys: [XK.Ctrl, XK.Shift, XK.Esc] },
  ];

  if (!projectId) {
    return (
      <div style={{ padding: 20, color: "#fff", background: "#000", height: "100vh" }}>
        <p>Missing project parameter.</p>
      </div>
    );
  }

  const poweredOff = vmState !== null && vmState !== "running";
  const displayStatus = consoleStatus(headless, startingRef.current, poweredOff, status);
  const statusColor = consoleStatusColor(headless, displayStatus);

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
          <img src="/images/troshka-logo-dark-200.png" alt="" style={{ height: 20 }} />
          <div style={{ display: "flex", flexDirection: "column", lineHeight: 1.2 }}>
            {projectName && <span style={{ fontSize: 10, opacity: 0.5 }}>{projectName}</span>}
            <span>{vmName}</span>
          </div>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ color: statusColor }}>{displayStatus}</span>
          {displayStatus === "Connected" && (
            <span
              title={focused ? "Keyboard active — typing goes to VM" : "Click console to activate keyboard"}
              style={{ display: "flex", alignItems: "center", gap: 5, transition: "opacity 0.2s", opacity: focused ? 1 : 0.5 }}
            >
              {focused ? (
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#4ade80" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
                  <circle cx="12" cy="12" r="3" />
                </svg>
              ) : (
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#888" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94" />
                  <path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19" />
                  <line x1="1" y1="1" x2="23" y2="23" />
                </svg>
              )}
              <span style={{ fontSize: 10, color: focused ? "#4ade80" : "#888" }}>
                {focused ? "Focused" : "Unfocused"}
              </span>
            </span>
          )}
        </div>
        <ConsoleToolbar scaled={scaled} setScaled={setScaled} rfbRef={rfbRef} btnStyle={btnStyle} vmName={vmName} setToast={setToast} lastPasteRef={lastPasteRef} vmPasswords={vmPasswords} openKeyboard={openKeyboard} linuxMacros={linuxMacros} windowsMacros={windowsMacros} openMenu={openMenu} setOpenMenu={setOpenMenu} sendCombo={sendCombo} vmState={vmState} vmPowerAction={vmPowerAction} />
      </div>
      <div style={{
        height: 2,
        background: focused ? "#4ade80" : "transparent",
        transition: "background 0.2s",
      }} />
      <div style={{ flex: 1, position: "relative", background: "#000" }}>
        <div ref={canvasRef} style={{ position: "absolute", top: 0, left: 0, right: 0, bottom: 0 }} />
        {displayStatus !== "Connected" && (
          <ConsoleOverlay displayStatus={displayStatus} headless={headless} fetchConsoleUrl={fetchConsoleUrl} setWsUrl={setWsUrl} startingRef={startingRef} />
        )}
        <style>{`@keyframes vkb-spin { to { transform: rotate(360deg); } }`}</style>
      </div>
      {toast && (
        <div style={{
          position: "fixed", bottom: 32, left: "50%", transform: "translateX(-50%)",
          padding: "6px 16px", borderRadius: 8,
          background: "rgba(30,30,50,0.9)", color: "#4ade80",
          fontSize: 13, whiteSpace: "nowrap", pointerEvents: "none",
          boxShadow: "0 4px 16px rgba(0,0,0,0.4)",
          border: "1px solid rgba(74,222,128,0.3)",
          animation: "fadeIn 0.2s",
          zIndex: 9999,
        }}>{toast}</div>
      )}
      <AlertModal message={alertMsg} onClose={() => setAlertMsg(null)} />
    </div>
  );
}
