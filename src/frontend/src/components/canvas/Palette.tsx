"use client";

import React, { useEffect, useState } from "react";
import { useCanvasStore } from "@/stores/canvasStore";

const TIMER_PRESETS = [
  { label: "None", value: null },
  { label: "30m", value: 30 },
  { label: "1h", value: 60 },
  { label: "2h", value: 120 },
  { label: "4h", value: 240 },
  { label: "8h", value: 480 },
  { label: "24h", value: 1440 },
  { label: "Custom...", value: -1 },
] as const;

function formatMinutes(m: number): string {
  const h = Math.floor(m / 60);
  const min = m % 60;
  if (h && min) return `${h}h ${min}m`;
  if (h) return `${h}h`;
  return `${min}m`;
}

function currentPresetLabel(minutes: number | null | undefined): string {
  if (minutes == null) return "None";
  const preset = TIMER_PRESETS.find(p => p.value === minutes);
  return preset ? preset.label : formatMinutes(minutes);
}

interface PaletteItemDef {
  type: string;
  label: string;
  desc: string;
  icon: string;
  iconClass: string;
  defaults?: Record<string, unknown>;
}

interface PaletteSection {
  title: string;
  items: PaletteItemDef[];
}

const sections: PaletteSection[] = [
  {
    title: "Compute",
    items: [
      {
        type: "vm-linux",
        label: "VM",
        desc: "Virtual machine",
        icon: "🖥",
        iconClass: "palette-icon-vm",
      },
    ],
  },
  {
    title: "Containers",
    items: [
      {
        type: "container",
        label: "Container",
        desc: "Podman container",
        icon: "📦",
        iconClass: "palette-icon-container",
      },
      {
        type: "pod",
        label: "Pod",
        desc: "Container group (shared network)",
        icon: "🫛",
        iconClass: "palette-icon-container",
      },
    ],
  },
  {
    title: "Networking",
    items: [
      {
        type: "network",
        label: "Network",
        desc: "Virtual bridge",
        icon: "rj45",
        iconClass: "palette-icon-network",
      },
      {
        type: "router",
        label: "Router",
        desc: "L3 routing",
        icon: "🔀",
        iconClass: "palette-icon-router",
      },
      {
        type: "gateway",
        label: "Gateway",
        desc: "Internet access",
        icon: "🌐",
        iconClass: "palette-icon-gateway",
      },
      {
        type: "loadbalancer",
        label: "Load Balancer",
        desc: "HAProxy L4",
        icon: "lb",
        iconClass: "palette-icon-lb",
      },
    ],
  },
  {
    title: "Storage",
    items: [
      {
        type: "disk",
        label: "Disk",
        desc: "Virtual disk",
        icon: "🛢",
        iconClass: "palette-icon-storage",
      },
      {
        type: "iso",
        label: "ISO",
        desc: "CD/DVD image",
        icon: "💿",
        iconClass: "palette-icon-storage",
      },
    ],
  },
  /* Templates removed — OS/config set via disk library images and VM properties */
  /*{
    title: "Templates",
    items: [
      {
        type: "template-rhel9",
        label: "RHEL 9",
        desc: "2 vCPU / 4GB",
        icon: "📦",
        iconClass: "palette-icon-template",
        defaults: { vcpus: 2, ram: 4, os: "RHEL 9" },
      },
      {
        type: "template-ubuntu",
        label: "Ubuntu 24.04",
        desc: "2 vCPU / 4GB",
        icon: "📦",
        iconClass: "palette-icon-template",
        defaults: { vcpus: 2, ram: 4, os: "Ubuntu 24.04" },
      },
      {
        type: "template-windows",
        label: "Win Server 2025",
        desc: "4 vCPU / 8GB",
        icon: "📦",
        iconClass: "palette-icon-template",
        defaults: { vcpus: 4, ram: 8, os: "Win Server 2025" },
      },
    ],
  },*/
];

function RJ45Icon({ size = 20 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <rect x="4" y="2" width="16" height="16" rx="2" />
      <line x1="8" y1="18" x2="8" y2="22" />
      <line x1="12" y1="18" x2="12" y2="22" />
      <line x1="16" y1="18" x2="16" y2="22" />
      <rect x="6" y="5" width="12" height="6" rx="1" />
      <line x1="9" y1="5" x2="9" y2="11" />
      <line x1="12" y1="5" x2="12" y2="11" />
      <line x1="15" y1="5" x2="15" y2="11" />
    </svg>
  );
}

function LBIcon({ size = 20 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="4" cy="12" r="2" />
      <circle cx="20" cy="6" r="2" />
      <circle cx="20" cy="12" r="2" />
      <circle cx="20" cy="18" r="2" />
      <line x1="6" y1="12" x2="11" y2="12" />
      <line x1="11" y1="12" x2="18" y2="6" />
      <line x1="11" y1="12" x2="18" y2="12" />
      <line x1="11" y1="12" x2="18" y2="18" />
    </svg>
  );
}

function PaletteIcon({ icon, iconClass }: { icon: string; iconClass: string }) {
  if (icon === "rj45") return <div className={`palette-icon ${iconClass}`}><RJ45Icon /></div>;
  if (icon === "lb") return <div className={`palette-icon ${iconClass}`}><LBIcon /></div>;
  return <div className={`palette-icon ${iconClass}`}>{icon}</div>;
}

function onDragStart(
  event: React.DragEvent<HTMLDivElement>,
  item: PaletteItemDef,
) {
  event.dataTransfer.setData(
    "application/troshka-node",
    JSON.stringify(item),
  );
  event.dataTransfer.effectAllowed = "move";
}

interface SnapshotItem {
  id: string;
  name: string;
  description: string;
  size_bytes: number;
  state: string;
  vm_config: Record<string, unknown> | null;
}

export default function Palette({ onOpenStartOrder, onOpenExternalIps, projectDescription, projectGuid, onDescriptionChange, ocpHealth, projectId, hostId, autoStopMinutes, autoDeleteMinutes, onAutoStopChange, onAutoDeleteChange, clockTarget, onClockTargetChange, guestExecEnabled, onGuestExecChange }: { onOpenStartOrder?: () => void; onOpenExternalIps?: () => void; projectDescription?: string; projectGuid?: string; onDescriptionChange?: (desc: string) => void; ocpHealth?: { phase: string; detail: string; items?: string[] } | null; projectId?: string; hostId?: string; autoStopMinutes?: number | null; autoDeleteMinutes?: number | null; onAutoStopChange?: (minutes: number | null) => void; onAutoDeleteChange?: (minutes: number | null) => void; clockTarget?: string | null; onClockTargetChange?: (value: string | null) => void; guestExecEnabled?: boolean; onGuestExecChange?: (enabled: boolean) => void }) {
  const [showDesc, setShowDesc] = useState(false);
  const [editingDesc, setEditingDesc] = useState(false);
  const [showPasswords, setShowPasswords] = useState(false);
  const [showOcpStatus, setShowOcpStatus] = useState(true);
  const [ocpLogModal, setOcpLogModal] = useState(false);
  const [ocpLog, setOcpLog] = useState("");
  const [showPortal, setShowPortal] = useState(false);
  const [portalToken, setPortalToken] = useState<{ token: string; access_level: string; portal_url: string } | null>(null);
  const [portalAccessLevel, setPortalAccessLevel] = useState("console");
  const [portalCopied, setPortalCopied] = useState(false);
  const ocpLogRef = React.useRef<HTMLPreElement>(null);

  const [collapsedSections, setCollapsedSections] = useState<Set<string>>(new Set(["Compute", "Containers", "Networking", "Storage", "Project"]));
  const [revealedPasswords, setRevealedPasswords] = useState<Set<string>>(new Set());
  const [hostInfo, setHostInfo] = useState<{ instance_id: string; ip_address: string; provider_type: string; provider_name: string } | null>(null);
  const [customStopOpen, setCustomStopOpen] = useState(false);
  const [customDeleteOpen, setCustomDeleteOpen] = useState(false);
  const [clockOpen, setClockOpen] = useState(!!clockTarget);
  const [clockDraft, setClockDraft] = useState(clockTarget ? clockTarget.slice(0, 16) : "");
  const [customStopH, setCustomStopH] = useState(0);
  const [customStopM, setCustomStopM] = useState(0);
  const [customDeleteH, setCustomDeleteH] = useState(0);
  const [customDeleteM, setCustomDeleteM] = useState(0);
  const nodes = useCanvasStore((s) => s.nodes);

  useEffect(() => {
    if (!hostId) return;
    Promise.all([
      fetch(`/api/v1/hosts/${hostId}`).then((r) => r.ok ? r.json() : null),
      fetch("/api/v1/providers/").then((r) => r.ok ? r.json() : []),
    ]).then(([h, providers]) => {
      if (!h) return;
      const prov = Array.isArray(providers) ? providers.find((p: any) => p.id === h.provider_id) : null;
      setHostInfo({ instance_id: h.instance_id, ip_address: h.ip_address, provider_type: prov?.type || h.provider_type || "", provider_name: prov?.name || "" });
    }).catch(() => {});
  }, [hostId]);

  const createOrUpdatePortalToken = async (level: string) => {
    if (!projectId) return;
    try {
      const resp = await fetch(`/api/v1/projects/${projectId}/portal-token`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ access_level: level }),
      });
      if (resp.ok) {
        const data = await resp.json();
        const portalUrl = `${window.location.origin}/portal/${data.token}`;
        setPortalToken({ ...data, portal_url: portalUrl });
        setPortalAccessLevel(level);
      }
    } catch { /* ignore */ }
  };

  React.useEffect(() => {
    if (!ocpLogModal || !projectId) return;
    const bastionNode = nodes.find((n: any) => n.type === "vmNode" && n.data?.label === "bastion");
    if (!bastionNode) return;
    let active = true;
    const poll = async () => {
      while (active) {
        try {
          const r = await fetch(`/api/v1/projects/${projectId}/vms/${bastionNode.id}/exec`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ command: "cat /home/cloud-user/install.log 2>/dev/null", timeout: 10 }),
          });
          if (r.ok) {
            const d = await r.json();
            if (active) {
              setOcpLog(d.output || "Install log not available yet — waiting for bastion to start the OCP installer...");
              setTimeout(() => { if (ocpLogRef.current) ocpLogRef.current.scrollTop = ocpLogRef.current.scrollHeight; }, 50);
            }
          }
        } catch {}
        await new Promise(r => setTimeout(r, 5000));
      }
    };
    poll();
    return () => { active = false; };
  }, [ocpLogModal, projectId, nodes]);
  const passwords = React.useMemo(() => {
    const result: { label: string; value: string }[] = [];
    for (const n of nodes) {
      const d = n.data as Record<string, any>;
      if (n.type === "vmNode" && d.ciCloudUserPassword) {
        result.push({ label: `${d.name || d.label || n.id} (cloud-user)`, value: d.ciCloudUserPassword });
      }
      if (n.type === "networkNode" && d.networkType === "bmc" && d.bmcPassword) {
        result.push({ label: "BMC", value: d.bmcPassword });
      }
    }
    return result;
  }, [nodes]);
  const [showSnapshots, setShowSnapshots] = useState(false);
  const [snapshots, setSnapshots] = useState<SnapshotItem[]>([]);
  const [snapshotsLoaded, setSnapshotsLoaded] = useState(false);

  const loadSnapshots = () => {
    fetch("/api/v1/library/?type=snapshot")
      .then((r) => r.ok ? r.json() : [])
      .then((data) => {
        setSnapshots(Array.isArray(data) ? data.filter((s: SnapshotItem) => s.state === "ready") : []);
        setSnapshotsLoaded(true);
      })
      .catch(() => setSnapshotsLoaded(true));
  };

  const onSnapshotDragStart = (event: React.DragEvent<HTMLDivElement>, snapshot: SnapshotItem) => {
    event.dataTransfer.setData(
      "application/troshka-node",
      JSON.stringify({
        type: "snapshot",
        label: snapshot.name,
        icon: "📸",
        defaults: { snapshotId: snapshot.id },
      }),
    );
    event.dataTransfer.effectAllowed = "move";
  };

  return (
    <div className="canvas-palette">
      {(
        <div style={{ borderBottom: "1px solid var(--pf-t--global--border--color--default)" }}>
          <div
            className="palette-section-title"
            style={{ padding: "6px 12px", cursor: "pointer", display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 0 }}
            onClick={() => setShowDesc(!showDesc)}
          >
            <span>INFO</span>
            <span style={{ fontSize: 9 }}>{showDesc ? "▾" : "▸"}</span>
          </div>
          {showDesc && (
            editingDesc ? (
              <div style={{ padding: "0 12px 8px" }}>
                <textarea
                  autoFocus
                  defaultValue={projectDescription}
                  style={{ width: "100%", fontSize: 11, lineHeight: 1.6, background: "var(--pf-t--global--background--color--primary--default)", color: "var(--pf-t--global--text--color--regular)", border: "1px solid var(--pf-t--global--border--color--default)", borderRadius: 4, padding: "4px 6px", resize: "vertical", minHeight: 60 }}
                  onBlur={(e) => { setEditingDesc(false); if (onDescriptionChange && e.target.value !== projectDescription) onDescriptionChange(e.target.value); }}
                  onKeyDown={(e) => { if (e.key === "Escape") setEditingDesc(false); }}
                />
              </div>
            ) : (
              <div
                style={{ fontSize: 11, color: "var(--pf-t--global--text--color--subtle)", padding: "0 12px 8px", lineHeight: 1.6, cursor: "pointer", maxHeight: 200, overflowY: "auto", overflowX: "auto", whiteSpace: "nowrap" }}
                onClick={() => setEditingDesc(true)}
                title="Click to edit"
              >
                {projectDescription ? projectDescription.split(" | ").map((part, i) => <div key={i}>{part}</div>) : <span style={{ opacity: 0.4, fontStyle: "italic" }}>Click to add description</span>}
                {projectGuid && (
                  <div style={{ marginTop: 4, display: "flex", alignItems: "center", gap: 4 }}>
                    <span style={{ opacity: 0.4 }}>GUID:</span>
                    <code style={{ fontSize: 11 }}>{projectGuid}</code>
                  </div>
                )}
                {hostInfo && (
                  <>
                    <div style={{ marginTop: 4 }}>
                      <span style={{ opacity: 0.4 }}>Host:</span> {hostInfo.instance_id} · {hostInfo.ip_address}
                    </div>
                    {hostInfo.provider_name && (
                      <div>
                        <span style={{ opacity: 0.4 }}>Provider:</span> {hostInfo.provider_name} ({hostInfo.provider_type})
                      </div>
                    )}
                  </>
                )}
                {(() => {
                  const bmcData = (window as any).__deployedTopology?.bmc;
                  if (!bmcData?.vms) return null;
                  const bmcVms = Object.values(bmcData.vms) as any[];
                  if (!bmcVms.length) return null;
                  return bmcVms.map((vm: any, i: number) => (
                    <div key={i} style={{ marginTop: i === 0 ? 4 : 0 }}>
                      <span style={{ opacity: 0.4 }}>BMC:</span>{" "}
                      <code style={{ fontSize: 10 }}>http://{vm.ip}:8000</code>{" · "}
                      <code style={{ fontSize: 10 }}>https://{vm.ip}:8443</code>
                    </div>
                  ));
                })()}
              </div>
            )
          )}
        </div>
      )}
      {passwords.length > 0 && (
        <div style={{ borderBottom: "1px solid var(--pf-t--global--border--color--default)" }}>
          <div
            className="palette-section-title"
            style={{ padding: "6px 12px", cursor: "pointer", display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 0 }}
            onClick={() => setShowPasswords(!showPasswords)}
          >
            <span>PASSWORDS</span>
            <span style={{ fontSize: 9 }}>{showPasswords ? "▾" : "▸"}</span>
          </div>
          {showPasswords && (
            <div style={{ padding: "0 12px 8px", fontSize: 11 }}>
              {passwords.map((pw, i) => (
                <div key={i} style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 4 }}>
                  <span style={{ color: "var(--pf-t--global--text--color--subtle)", minWidth: 0, flex: 1 }}>{pw.label}</span>
                  <code style={{ fontSize: 11, cursor: "pointer", userSelect: "all" }}
                    onClick={() => setRevealedPasswords((prev) => { const s = new Set(prev); if (s.has(pw.label)) s.delete(pw.label); else s.add(pw.label); return s; })}
                  >{revealedPasswords.has(pw.label) ? pw.value : "••••••"}</code>
                  <span style={{ cursor: "pointer", fontSize: 10, opacity: 0.6 }} onClick={() => navigator.clipboard.writeText(pw.value)} title="Copy">Copy</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
      {ocpHealth && (
        <div style={{ borderBottom: "1px solid var(--pf-t--global--border--color--default)" }}>
          <div
            className="palette-section-title"
            style={{ padding: "6px 12px", cursor: "pointer", display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 0 }}
            onClick={() => setShowOcpStatus(!showOcpStatus)}
          >
            <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
              {ocpHealth.phase !== "ready" && ocpHealth.phase !== "error" && ocpHealth.phase !== "timeout" && ocpHealth.phase !== "waiting" && ocpHealth.phase !== "warning" && <span className="project-btn-spinner" style={{ width: 10, height: 10 }} />}
              {(ocpHealth.phase === "waiting" || ocpHealth.phase === "warning") && <span style={{ color: "#fbbf24" }}>⚠</span>}
              {(ocpHealth.phase === "error" || ocpHealth.phase === "timeout") && <span style={{ color: "#f87171" }}>✗</span>}
              OCP STATUS
            </span>
            <span style={{ fontSize: 9 }}>{showOcpStatus ? "▾" : "▸"}</span>
          </div>
          {showOcpStatus && (
            <div style={{ padding: "0 12px 8px", fontSize: 11 }}>
              <div style={{ marginBottom: 4, color: ocpHealth.phase === "ready" ? "#4ade80" : ocpHealth.phase === "error" || ocpHealth.phase === "timeout" ? "#f87171" : ocpHealth.phase === "warning" ? "#fbbf24" : "var(--pf-t--global--text--color--regular)", display: "flex", alignItems: "center", gap: 6 }}>
                {ocpHealth.phase === "ready" && "✓"} {(ocpHealth.phase === "error" || ocpHealth.phase === "timeout") && "✗"} {ocpHealth.phase === "warning" && "⚠"} {ocpHealth.detail}
              </div>
              {ocpHealth.items && (
                <div style={{ fontSize: 10, lineHeight: 1.6, color: "var(--pf-t--global--text--color--subtle)" }}>
                  {ocpHealth.items.map((item, i) => {
                    const [name, status] = [item.split(":")[0].trim(), item.split(":").slice(1).join(":").trim()];
                    const isGood = status.includes("✓") || status.includes("available") || status.includes("Ready") || status.includes("reachable") || status.includes("ready");
                    const isBad = status.includes("✗") || status.includes("degraded") || status.includes("failed") || status.includes("not available");
                    return (
                      <div key={i} style={{ display: "flex", justifyContent: "space-between", gap: 4 }}>
                        <span style={{ whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{name}</span>
                        <span style={{ color: isGood ? "#4ade80" : isBad ? "#f87171" : "#fbbf24", whiteSpace: "nowrap", flexShrink: 0 }}>
                          {isGood ? "✓" : isBad ? "✗" : status}
                        </span>
                      </div>
                    );
                  })}
                </div>
              )}
              {ocpHealth.phase === "ready" && (() => {
                const ocpVm = nodes.find((n: any) => n.type === "vmNode" && (n.data as any)?.ocpKubeadminPassword);
                const bastionPw = passwords.find(p => p.label.includes("bastion"));
                const kubeadminPw = (ocpVm?.data as any)?.ocpKubeadminPassword || bastionPw?.value || "";
                return (
                  <div style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 4 }}>
                    <span style={{ color: "var(--pf-t--global--text--color--subtle)", minWidth: 0, flex: 1 }}>kubeadmin</span>
                    <code style={{ fontSize: 11, cursor: "pointer", userSelect: "all" }}
                      onClick={() => setRevealedPasswords((prev) => { const s = new Set(prev); if (s.has("kubeadmin")) s.delete("kubeadmin"); else s.add("kubeadmin"); return s; })}
                    >{revealedPasswords.has("kubeadmin") ? kubeadminPw : "••••••"}</code>
                    <span style={{ cursor: "pointer", fontSize: 10, opacity: 0.6 }} onClick={() => navigator.clipboard.writeText(kubeadminPw)} title="Copy">Copy</span>
                  </div>
                );
              })()}
              {ocpHealth.phase !== "ssh" && ocpHealth.phase !== "waiting" && projectId && (
                <div style={{ marginTop: 4 }}>
                  <span style={{ cursor: "pointer", fontSize: 10, opacity: 0.6, textDecoration: "underline" }} onClick={() => { setOcpLog(""); setOcpLogModal(true); }}>View Install Log</span>
                </div>
              )}
            </div>
          )}
        </div>
      )}
      {ocpLogModal && (
        <div style={{
          position: "fixed", inset: 0, zIndex: 10000,
          display: "flex", alignItems: "center", justifyContent: "center",
          background: "rgba(0,0,0,0.6)",
        }} onClick={() => setOcpLogModal(false)}>
          <div style={{
            background: "var(--pf-t--global--background--color--primary--default)",
            borderRadius: 12, padding: 24, width: "80vw", maxWidth: 800, maxHeight: "80vh",
            boxShadow: "0 8px 32px rgba(0,0,0,0.5)",
            border: "1px solid var(--pf-t--global--border--color--default)",
            display: "flex", flexDirection: "column",
          }} onClick={(e) => e.stopPropagation()}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
              <h3 style={{ margin: 0 }}>OpenShift Install Log</h3>
              <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                {ocpLog && <button onClick={(e) => { navigator.clipboard.writeText(ocpLog); const btn = e.currentTarget; btn.textContent = "Copied!"; btn.style.background = "rgba(34,197,94,0.2)"; btn.style.borderColor = "rgba(34,197,94,0.5)"; setTimeout(() => { btn.textContent = "Copy All"; btn.style.background = "rgba(255,255,255,0.08)"; btn.style.borderColor = "rgba(255,255,255,0.15)"; }, 1500); }} style={{ background: "rgba(255,255,255,0.08)", border: "1px solid rgba(255,255,255,0.15)", color: "var(--pf-t--global--text--color--regular)", cursor: "pointer", fontSize: 11, padding: "4px 10px", borderRadius: 4, transition: "all 0.2s" }}>Copy All</button>}
                <button onClick={() => setOcpLogModal(false)} style={{ background: "transparent", border: "none", color: "var(--pf-t--global--text--color--regular)", cursor: "pointer", fontSize: 18 }}>✕</button>
              </div>
            </div>
            <pre ref={ocpLogRef} style={{ fontSize: 11, fontFamily: "monospace", whiteSpace: "pre-wrap", overflowY: "auto", flex: 1, margin: 0, padding: 8, background: "rgba(0,0,0,0.2)", borderRadius: 6, lineHeight: 1.5 }}>
              {ocpLog || <span style={{ opacity: 0.5 }}><span className="project-btn-spinner" style={{ width: 12, height: 12, display: "inline-block", verticalAlign: "middle", marginRight: 6 }} />Loading install log...</span>}
            </pre>
          </div>
        </div>
      )}
      {sections.map((section, sIdx) => (
        <React.Fragment key={section.title}>
          {sIdx > 0 && <div className="palette-divider" />}
          <div className="palette-section">
            <div
              className="palette-section-title"
              style={{ cursor: "pointer", display: "flex", justifyContent: "space-between", alignItems: "center" }}
              onClick={() => setCollapsedSections((prev) => { const s = new Set(prev); if (s.has(section.title)) s.delete(section.title); else s.add(section.title); return s; })}
            >
              <span>{section.title}</span>
              <span style={{ fontSize: 9 }}>{collapsedSections.has(section.title) ? "▸" : "▾"}</span>
            </div>
            {!collapsedSections.has(section.title) && (<>
              {section.items.map((item) => (
                <div
                  key={item.type}
                  className="palette-item"
                  draggable
                  onDragStart={(e) => onDragStart(e, item)}
                >
                  <PaletteIcon icon={item.icon} iconClass={item.iconClass} />
                  <div>
                    <div className="palette-item-label">{item.label}</div>
                    <div className="palette-item-desc">{item.desc}</div>
                  </div>
                </div>
              ))}
            {section.title === "Networking" && (
              <div className="palette-item" onClick={onOpenExternalIps} style={{ cursor: "pointer" }}>
                <div className="palette-icon palette-icon-gateway">🌍</div>
                <div>
                  <div className="palette-item-label">External IPs</div>
                  <div className="palette-item-desc">Public IP pool</div>
                </div>
              </div>
            )}
            {section.title === "Compute" && (
              <>
                <div
                  className="palette-item"
                  style={{ cursor: "pointer" }}
                  onClick={() => {
                    if (!snapshotsLoaded) loadSnapshots();
                    setShowSnapshots(!showSnapshots);
                  }}
                >
                  <div className="palette-icon" style={{ background: "rgba(74,222,128,0.15)" }}>📸</div>
                  <div>
                    <div className="palette-item-label">Snapshots</div>
                    <div className="palette-item-desc">Virtual machine snapshot</div>
                  </div>
                </div>
                {showSnapshots && (
                  <div style={{ paddingLeft: 8, display: "flex", flexDirection: "column", gap: 2 }}>
                    {!snapshotsLoaded ? (
                      <div style={{ fontSize: 11, opacity: 0.5, padding: "4px 8px" }}>Loading...</div>
                    ) : snapshots.length === 0 ? (
                      <div style={{ fontSize: 11, opacity: 0.5, padding: "4px 8px" }}>No snapshots available</div>
                    ) : (
                      snapshots.map((snap) => (
                        <div
                          key={snap.id}
                          className="palette-item"
                          draggable
                          onDragStart={(e) => onSnapshotDragStart(e, snap)}
                          style={{ padding: "4px 8px", fontSize: 12 }}
                        >
                          <div style={{ fontSize: 14 }}>🖥</div>
                          <div>
                            <div className="palette-item-label" style={{ fontSize: 12 }}>{snap.name}</div>
                            <div className="palette-item-desc" style={{ fontSize: 10 }}>
                              {snap.vm_config ? `${snap.vm_config.vcpus} vCPU · ${snap.vm_config.ram} GB` : "VM snapshot"}
                            </div>
                          </div>
                        </div>
                      ))
                    )}
                  </div>
                )}
              </>
            )}
            </>)}
          </div>
        </React.Fragment>
      ))}
      <div className="palette-divider" />
      <div className="palette-section">
        <div
          className="palette-section-title"
          style={{ cursor: "pointer", display: "flex", justifyContent: "space-between", alignItems: "center" }}
          onClick={() => setCollapsedSections((prev) => { const s = new Set(prev); if (s.has("Project")) s.delete("Project"); else s.add("Project"); return s; })}
        >
          <span>Project</span>
          <span style={{ fontSize: 9 }}>{collapsedSections.has("Project") ? "▸" : "▾"}</span>
        </div>
        {!collapsedSections.has("Project") && (
          <>
            <div className="palette-item" onClick={onOpenStartOrder} style={{ cursor: "pointer" }}>
              <div className="palette-icon" style={{ background: "rgba(108,99,255,0.15)" }}>🔢</div>
              <div>
                <div className="palette-item-label">Start Order</div>
                <div className="palette-item-desc">VM boot sequence</div>
              </div>
            </div>

            {/* Auto-Stop Timer */}
            <div className="palette-item" style={{ cursor: "default" }}>
              <div className="palette-icon" style={{ background: "rgba(251,191,36,0.15)" }}>⏱</div>
              <div style={{ flex: 1 }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <div className="palette-item-label">Auto-Stop</div>
                  <select
                    value={autoStopMinutes == null ? "null" : String(autoStopMinutes)}
                    onChange={(e) => {
                      const v = e.target.value;
                      if (v === "-1") { setCustomStopOpen(true); return; }
                      setCustomStopOpen(false);
                      onAutoStopChange?.(v === "null" ? null : Number(v));
                    }}
                    style={{
                      fontSize: 10, padding: "1px 4px", borderRadius: 3,
                      border: "1px solid var(--pf-t--global--border--color--default)",
                      background: "var(--pf-t--global--background--color--secondary--default)",
                      color: "var(--pf-t--global--text--color--regular)",
                      maxWidth: 80,
                    }}
                  >
                    {TIMER_PRESETS.map((p) => (
                      <option key={String(p.value)} value={String(p.value)}>{p.label}</option>
                    ))}
                    {autoStopMinutes != null && !TIMER_PRESETS.some(p => p.value === autoStopMinutes) && (
                      <option value={String(autoStopMinutes)}>{formatMinutes(autoStopMinutes)}</option>
                    )}
                  </select>
                </div>
                <div className="palette-item-desc">Stop VMs after duration</div>
                {customStopOpen && (
                  <div style={{ display: "flex", alignItems: "center", gap: 4, marginTop: 4 }}>
                    <input type="number" min={0} max={999} value={customStopH} onChange={e => setCustomStopH(Number(e.target.value))}
                      style={{ width: 36, fontSize: 10, padding: "2px 4px", borderRadius: 3, border: "1px solid var(--pf-t--global--border--color--default)", background: "var(--pf-t--global--background--color--secondary--default)", color: "var(--pf-t--global--text--color--regular)" }}
                    />
                    <span style={{ fontSize: 10, opacity: 0.6 }}>h</span>
                    <input type="number" min={0} max={59} value={customStopM} onChange={e => setCustomStopM(Number(e.target.value))}
                      style={{ width: 36, fontSize: 10, padding: "2px 4px", borderRadius: 3, border: "1px solid var(--pf-t--global--border--color--default)", background: "var(--pf-t--global--background--color--secondary--default)", color: "var(--pf-t--global--text--color--regular)" }}
                    />
                    <span style={{ fontSize: 10, opacity: 0.6 }}>m</span>
                    <button onClick={() => {
                      const total = customStopH * 60 + customStopM;
                      if (total > 0) { onAutoStopChange?.(total); setCustomStopOpen(false); }
                    }}
                      style={{ fontSize: 10, padding: "2px 6px", borderRadius: 3, border: "1px solid var(--pf-t--global--border--color--default)", background: "rgba(108,99,255,0.2)", color: "var(--pf-t--global--text--color--regular)", cursor: "pointer" }}
                    >Set</button>
                  </div>
                )}
              </div>
            </div>

            {/* Auto-Delete Timer */}
            <div className="palette-item" style={{ cursor: "default" }}>
              <div className="palette-icon" style={{ background: "rgba(239,68,68,0.15)" }}>🗑</div>
              <div style={{ flex: 1 }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <div className="palette-item-label">Auto-Delete</div>
                  <select
                    value={autoDeleteMinutes == null ? "null" : String(autoDeleteMinutes)}
                    onChange={(e) => {
                      const v = e.target.value;
                      if (v === "-1") { setCustomDeleteOpen(true); return; }
                      setCustomDeleteOpen(false);
                      onAutoDeleteChange?.(v === "null" ? null : Number(v));
                    }}
                    style={{
                      fontSize: 10, padding: "1px 4px", borderRadius: 3,
                      border: "1px solid var(--pf-t--global--border--color--default)",
                      background: "var(--pf-t--global--background--color--secondary--default)",
                      color: "var(--pf-t--global--text--color--regular)",
                      maxWidth: 80,
                    }}
                  >
                    {TIMER_PRESETS.map((p) => (
                      <option key={String(p.value)} value={String(p.value)}>{p.label}</option>
                    ))}
                    {autoDeleteMinutes != null && !TIMER_PRESETS.some(p => p.value === autoDeleteMinutes) && (
                      <option value={String(autoDeleteMinutes)}>{formatMinutes(autoDeleteMinutes)}</option>
                    )}
                  </select>
                </div>
                <div className="palette-item-desc">Delete project after duration</div>
                {customDeleteOpen && (
                  <div style={{ display: "flex", alignItems: "center", gap: 4, marginTop: 4 }}>
                    <input type="number" min={0} max={999} value={customDeleteH} onChange={e => setCustomDeleteH(Number(e.target.value))}
                      style={{ width: 36, fontSize: 10, padding: "2px 4px", borderRadius: 3, border: "1px solid var(--pf-t--global--border--color--default)", background: "var(--pf-t--global--background--color--secondary--default)", color: "var(--pf-t--global--text--color--regular)" }}
                    />
                    <span style={{ fontSize: 10, opacity: 0.6 }}>h</span>
                    <input type="number" min={0} max={59} value={customDeleteM} onChange={e => setCustomDeleteM(Number(e.target.value))}
                      style={{ width: 36, fontSize: 10, padding: "2px 4px", borderRadius: 3, border: "1px solid var(--pf-t--global--border--color--default)", background: "var(--pf-t--global--background--color--secondary--default)", color: "var(--pf-t--global--text--color--regular)" }}
                    />
                    <span style={{ fontSize: 10, opacity: 0.6 }}>m</span>
                    <button onClick={() => {
                      const total = customDeleteH * 60 + customDeleteM;
                      if (total > 0) { onAutoDeleteChange?.(total); setCustomDeleteOpen(false); }
                    }}
                      style={{ fontSize: 10, padding: "2px 6px", borderRadius: 3, border: "1px solid var(--pf-t--global--border--color--default)", background: "rgba(239,68,68,0.2)", color: "var(--pf-t--global--text--color--regular)", cursor: "pointer" }}
                    >Set</button>
                  </div>
                )}
              </div>
            </div>

            {/* Clock Target */}
            <div className="palette-item" style={{ cursor: "default" }}>
              <div className="palette-icon" style={{ background: "rgba(147,51,234,0.15)" }}>🕐</div>
              <div style={{ flex: 1, minWidth: 0, overflow: "hidden" }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <div className="palette-item-label">Clock</div>
                  <div
                    onClick={() => {
                      if (clockOpen) {
                        setClockOpen(false);
                        setClockDraft("");
                        onClockTargetChange?.(null);
                      } else {
                        setClockOpen(true);
                        const d = new Date(); d.setFullYear(d.getFullYear() - 1);
                        setClockDraft(d.toISOString().slice(0, 16));
                      }
                    }}
                    style={{
                      width: 28, height: 14, borderRadius: 7, cursor: "pointer", flexShrink: 0,
                      background: clockOpen ? "rgba(108,99,255,0.7)" : "rgba(255,255,255,0.15)",
                      position: "relative", transition: "background 0.2s",
                    }}
                  >
                    <div style={{
                      width: 10, height: 10, borderRadius: 5,
                      background: "#fff", position: "absolute", top: 2,
                      left: clockOpen ? 16 : 2, transition: "left 0.2s",
                    }} />
                  </div>
                </div>
                <div className="palette-item-desc">{clockTarget ? (() => {
                  const target = new Date(clockTarget);
                  const now = new Date();
                  const diffMs = now.getTime() - target.getTime();
                  const days = Math.floor(Math.abs(diffMs) / 86400000);
                  const months = Math.floor(days / 30);
                  const remDays = days % 30;
                  const label = months > 0 ? `${months}mo ${remDays}d` : `${days}d`;
                  return diffMs > 0 ? `${label} behind real time` : `${label} ahead`;
                })() : "Backdate VM clocks"}</div>
                {clockOpen && (
                  <div style={{ marginTop: 4, display: "flex", alignItems: "center", gap: 4 }}>
                    <input
                      type="datetime-local"
                      value={clockDraft}
                      onChange={(e) => setClockDraft(e.target.value)}
                      style={{
                        fontSize: 10, padding: "2px 4px", borderRadius: 3,
                        border: "1px solid var(--pf-t--global--border--color--default)",
                        background: "var(--pf-t--global--background--color--secondary--default)",
                        color: "var(--pf-t--global--text--color--regular)",
                        flex: 1, minWidth: 0, boxSizing: "border-box",
                      }}
                    />
                    <button
                      onClick={() => { if (clockDraft) onClockTargetChange?.(clockDraft + ":00Z"); }}
                      style={{
                        fontSize: 10, padding: "2px 6px", borderRadius: 3, flexShrink: 0,
                        border: "1px solid var(--pf-t--global--border--color--default)",
                        background: "rgba(108,99,255,0.2)",
                        color: "var(--pf-t--global--text--color--regular)",
                        cursor: "pointer",
                      }}
                    >Set</button>
                  </div>
                )}
              </div>
            </div>

            {/* Guest Exec */}
            <div className="palette-item" style={{ cursor: "default" }}>
              <div className="palette-icon" style={{ background: "rgba(34,197,94,0.15)" }}>⚡</div>
              <div style={{ flex: 1, minWidth: 0, overflow: "hidden" }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <div className="palette-item-label">Guest Exec</div>
                  <div
                    onClick={() => onGuestExecChange?.(!guestExecEnabled)}
                    style={{
                      width: 28, height: 14, borderRadius: 7, cursor: "pointer", flexShrink: 0,
                      background: (guestExecEnabled !== false) ? "rgba(108,99,255,0.7)" : "rgba(255,255,255,0.15)",
                      position: "relative", transition: "background 0.2s",
                    }}
                  >
                    <div style={{
                      width: 10, height: 10, borderRadius: 5,
                      background: "#fff", position: "absolute", top: 2,
                      left: (guestExecEnabled !== false) ? 16 : 2, transition: "left 0.2s",
                    }} />
                  </div>
                </div>
                <div className="palette-item-desc">Enable VM command exec</div>
              </div>
            </div>
          </>
        )}
      </div>
      <div className="palette-divider" />
      <div className="palette-section">
        <div
          className="palette-section-title"
          style={{ cursor: "pointer", display: "flex", justifyContent: "space-between", alignItems: "center" }}
          onClick={() => { if (!showPortal && !portalToken) createOrUpdatePortalToken(portalAccessLevel); setShowPortal(!showPortal); }}
        >
          <span>Lab Portal</span>
          <span style={{ fontSize: 9 }}>{showPortal ? "▾" : "▸"}</span>
        </div>
        {showPortal && (
          <div style={{ padding: "0 12px 8px", fontSize: 11 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 8 }}>
              <span style={{ color: "var(--pf-t--global--text--color--subtle)" }}>Access:</span>
              <select
                value={portalAccessLevel}
                onChange={(e) => {
                  setPortalAccessLevel(e.target.value);
                  if (portalToken) createOrUpdatePortalToken(e.target.value);
                }}
                style={{
                  fontSize: 11, padding: "2px 4px", borderRadius: 3,
                  border: "1px solid var(--pf-t--global--border--color--default)",
                  background: "var(--pf-t--global--background--color--secondary--default)",
                  color: "var(--pf-t--global--text--color--regular)",
                }}
              >
                <option value="readonly">Read Only</option>
                <option value="power">Power</option>
                <option value="console">Console</option>
              </select>
            </div>
            {portalToken ? (
              <>
                <div style={{ display: "flex", alignItems: "center", gap: 4, marginBottom: 6 }}>
                  <input
                    readOnly
                    value={portalToken.portal_url}
                    style={{
                      flex: 1, fontSize: 10, padding: "3px 6px", borderRadius: 3,
                      border: "1px solid var(--pf-t--global--border--color--default)",
                      background: "var(--pf-t--global--background--color--secondary--default)",
                      color: "var(--pf-t--global--text--color--regular)",
                    }}
                    onClick={(e) => (e.target as HTMLInputElement).select()}
                  />
                  <span
                    style={{ cursor: "pointer", fontSize: 10, opacity: portalCopied ? 1 : 0.6, whiteSpace: "nowrap" }}
                    onClick={() => { navigator.clipboard.writeText(portalToken.portal_url); setPortalCopied(true); setTimeout(() => setPortalCopied(false), 2000); }}
                    title="Copy link"
                  >{portalCopied ? "✓ Copied" : "Copy"}</span>
                </div>
                <a
                  href={portalToken.portal_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  style={{ fontSize: 10, color: "#73bcf7", textDecoration: "underline" }}
                >Open Portal ↗</a>
              </>
            ) : (
              <div style={{ fontSize: 10, opacity: 0.5 }}>Generating link...</div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
