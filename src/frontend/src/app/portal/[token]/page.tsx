"use client";

import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import SunIcon from "@patternfly/react-icons/dist/esm/icons/sun-icon";
import MoonIcon from "@patternfly/react-icons/dist/esm/icons/moon-icon";
import PlayIcon from "@patternfly/react-icons/dist/esm/icons/play-icon";
import StopIcon from "@patternfly/react-icons/dist/esm/icons/stop-icon";
import RedoIcon from "@patternfly/react-icons/dist/esm/icons/redo-icon";
import PowerOffIcon from "@patternfly/react-icons/dist/esm/icons/power-off-icon";
import DesktopIcon from "@patternfly/react-icons/dist/esm/icons/desktop-icon";
import "@patternfly/react-core/dist/styles/base.css";
import { Alert, Spinner } from "@patternfly/react-core";

interface VMInfo {
  id: string;
  name: string;
  vcpus: number;
  ram: number;
  os: string;
  status?: string;
  icon?: string;
}

interface PortalData {
  project_id: string;
  project_name: string;
  project_state: string;
  access_level: string;
  topology: {
    nodes: any[];
    edges: any[];
  };
}

export default function PortalPage() {
  const { token } = useParams<{ token: string }>();
  const [data, setData] = useState<PortalData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [isDark, setIsDark] = useState(true);
  const [vmStates, setVmStates] = useState<Record<string, string>>({});
  const [vmPending, setVmPending] = useState<Record<string, string>>({});

  useEffect(() => {
    const saved = localStorage.getItem("troshka-theme");
    if (saved === "light") {
      setIsDark(false);
      document.documentElement.classList.remove("pf-v6-theme-dark");
    } else {
      document.documentElement.classList.add("pf-v6-theme-dark");
    }
  }, []);

  const toggleTheme = () => {
    setIsDark((prev) => {
      const next = !prev;
      if (next) {
        document.documentElement.classList.add("pf-v6-theme-dark");
        localStorage.setItem("troshka-theme", "dark");
      } else {
        document.documentElement.classList.remove("pf-v6-theme-dark");
        localStorage.setItem("troshka-theme", "light");
      }
      return next;
    });
  };

  const fetchPortal = useCallback(async () => {
    try {
      const resp = await fetch(`/api/v1/portal/${token}`);
      if (!resp.ok) {
        if (resp.status === 404) {
          setError("This portal link is invalid or has expired.");
        } else {
          setError("Failed to load portal.");
        }
        return;
      }
      setData(await resp.json());
    } catch {
      setError("Failed to connect to server.");
    } finally {
      setLoading(false);
    }
  }, [token]);

  const fetchVmStates = useCallback(async () => {
    try {
      const resp = await fetch(`/api/v1/portal/${token}/vm-states`);
      if (resp.ok) {
        const d = await resp.json();
        setVmStates(d.states || {});
      }
    } catch { /* ignore */ }
  }, [token]);

  useEffect(() => {
    fetchPortal();
    const interval = setInterval(fetchPortal, 10000);
    return () => clearInterval(interval);
  }, [fetchPortal]);

  useEffect(() => {
    if (!data) return;
    document.title = `Lab Portal: ${data.project_name}`;
    fetchVmStates();
    const interval = setInterval(fetchVmStates, 5000);
    return () => clearInterval(interval);
  }, [data, fetchVmStates]);

  const openConsole = (vmId: string, vmName: string) => {
    if (!data) return;
    window.open(
      `/console?vm=${vmId}&project=${data.project_id}&name=${encodeURIComponent(vmName)}`,
      `console-${vmId}`,
      "width=1024,height=768",
    );
  };

  const handleVmAction = async (vmId: string, action: string) => {
    if (action === "stop" || action === "forcestop" || action === "restart") {
      const vm = (data?.topology.nodes || []).find((n: any) => n.id === vmId);
      const name = vm?.data?.name || "this VM";
      const msg = action === "forcestop"
        ? `Force power off "${name}"? This is equivalent to pulling the power cord and may cause data loss.`
        : action === "restart"
          ? `Restart "${name}"?`
          : `Stop "${name}"? The VM will be gracefully shut down.`;
      if (!window.confirm(msg)) return;
    }
    setVmPending((prev) => ({ ...prev, [vmId]: action }));
    if (action === "stop") {
      setVmStates((prev) => ({ ...prev, [vmId]: "stopping" }));
    }
    await fetch(`/api/v1/portal/${token}/vms/${vmId}/${action}`, { method: "POST" });
    setVmPending((prev) => { const next = { ...prev }; delete next[vmId]; return next; });
    setTimeout(fetchVmStates, 1500);
  };

  if (loading) {
    return (
      <div style={{ display: "flex", justifyContent: "center", alignItems: "center", height: "100vh" }}>
        <Spinner size="xl" />
      </div>
    );
  }

  if (error || !data) {
    return (
      <div style={{ display: "flex", justifyContent: "center", alignItems: "center", height: "100vh" }}>
        <Alert variant="danger" title={error || "Portal not available"} />
      </div>
    );
  }

  const canPower = ["power", "console", "manage"].includes(data.access_level);
  const canConsole = ["console", "manage"].includes(data.access_level);

  const vms: VMInfo[] = (data.topology.nodes || [])
    .filter((n: any) => n.type === "vmNode")
    .map((n: any) => ({
      id: n.id,
      name: n.data.name || n.data.label || "VM",
      vcpus: n.data.vcpus,
      ram: n.data.ram,
      os: n.data.os || "",
      status: vmStates[n.id] || n.data.status,
      icon: n.data.icon,
    }));

  const statusColor = (s?: string) => {
    if (s === "running") return "#3e8635";
    if (s === "stopping") return "#f0ab00";
    if (s === "stopped") return "#c9190b";
    return "#6a6e73";
  };

  const bg = isDark ? "#1b1d21" : "#f0f0f0";
  const cardBg = isDark ? "#292e34" : "#fff";
  const textColor = isDark ? "#e0e0e0" : "#151515";
  const subtextColor = isDark ? "#8a8d90" : "#6a6e73";

  return (
    <div style={{ height: "100vh", display: "flex", flexDirection: "column", background: bg }}>
      <div style={{
        display: "flex", alignItems: "center", justifyContent: "space-between",
        padding: "8px 16px", background: "#1b1d21", borderBottom: "1px solid #444",
        minHeight: 48, flexShrink: 0,
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <img
            src={isDark ? "/images/troshka-logo-dark-200.png" : "/images/troshka-logo-light-200.png"}
            alt="Troshka"
            style={{ height: 32 }}
          />
          <span style={{ color: "white", fontSize: 16, fontWeight: 600 }}>
            {data.project_name}
          </span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <span style={{ color: "#73bcf7", fontSize: 13 }}>
            {data.project_state}
          </span>
          <button
            onClick={toggleTheme}
            aria-label="Toggle theme"
            style={{ background: "none", border: "none", color: "white", cursor: "pointer", fontSize: 16, padding: 4 }}
          >
            {isDark ? <SunIcon /> : <MoonIcon />}
          </button>
        </div>
      </div>

      <div style={{ flex: 1, overflow: "auto", padding: 24 }}>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))", gap: 16, maxWidth: 1200, margin: "0 auto" }}>
          {vms.map((vm) => (
            <div key={vm.id} style={{
              background: cardBg, borderRadius: 8, padding: 16,
              border: `1px solid ${isDark ? "#444" : "#d2d2d2"}`,
            }}>
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 12 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <span style={{ fontSize: 20 }}>{vm.icon || "🖥"}</span>
                  <span style={{ fontWeight: 600, fontSize: 15, color: textColor }}>{vm.name}</span>
                </div>
                <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                  <span style={{
                    width: 8, height: 8, borderRadius: "50%",
                    background: statusColor(vm.status), display: "inline-block",
                  }} />
                  <span style={{ fontSize: 12, color: subtextColor }}>{vm.status || "unknown"}</span>
                </div>
              </div>

              <div style={{ fontSize: 13, color: subtextColor, marginBottom: 12 }}>
                {vm.vcpus} vCPU · {vm.ram} GB · {vm.os}
              </div>

              <div style={{ display: "flex", gap: 6 }}>
                {canConsole && (
                  <button
                    onClick={() => openConsole(vm.id, vm.name)}
                    title="Console"
                    style={{
                      flex: 1, padding: "6px 12px", borderRadius: 4, border: "none",
                      background: "#0066cc", color: "white", cursor: "pointer",
                      fontSize: 14, display: "flex", alignItems: "center", justifyContent: "center", gap: 6,
                    }}
                  >
                    <DesktopIcon />
                  </button>
                )}
                {canPower && (
                  <>
                    <button
                      onClick={() => handleVmAction(vm.id, vm.status === "running" ? "stop" : "start")}
                      title={vm.status === "running" ? "Stop" : "Start"}
                      disabled={!!vmPending[vm.id]}
                      style={{
                        padding: "6px 10px", borderRadius: 4,
                        border: `1px solid ${isDark ? "#555" : "#ccc"}`,
                        background: "transparent", color: vm.status === "running" ? "#f0ab00" : "#3e8635",
                        cursor: vmPending[vm.id] ? "wait" : "pointer", fontSize: 14,
                        opacity: vmPending[vm.id] ? 0.5 : 1,
                      }}
                    >
                      {vmPending[vm.id] === "start" || vmPending[vm.id] === "stop"
                        ? <Spinner size="sm" />
                        : vm.status === "running" ? <StopIcon /> : <PlayIcon />}
                    </button>
                    {vm.status === "running" && (
                      <>
                        <button
                          onClick={() => handleVmAction(vm.id, "restart")}
                          title="Restart"
                          disabled={!!vmPending[vm.id]}
                          style={{
                            padding: "6px 10px", borderRadius: 4,
                            border: `1px solid ${isDark ? "#555" : "#ccc"}`,
                            background: "transparent", color: textColor,
                            cursor: vmPending[vm.id] ? "wait" : "pointer", fontSize: 14,
                            opacity: vmPending[vm.id] ? 0.5 : 1,
                          }}
                        >
                          {vmPending[vm.id] === "restart" ? <Spinner size="sm" /> : <RedoIcon />}
                        </button>
                        <button
                          onClick={() => handleVmAction(vm.id, "forcestop")}
                          title="Force Power Off"
                          disabled={!!vmPending[vm.id]}
                          style={{
                            padding: "6px 10px", borderRadius: 4,
                            border: "1px solid #c9190b",
                            background: "transparent", color: "#c9190b",
                            cursor: vmPending[vm.id] ? "wait" : "pointer", fontSize: 14,
                            opacity: vmPending[vm.id] ? 0.5 : 1,
                          }}
                        >
                          {vmPending[vm.id] === "forcestop" ? <Spinner size="sm" /> : <PowerOffIcon />}
                        </button>
                      </>
                    )}
                  </>
                )}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
