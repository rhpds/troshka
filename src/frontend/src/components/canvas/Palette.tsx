"use client";

import React, { useEffect, useState } from "react";
import { useCanvasStore } from "@/stores/canvasStore";

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

export default function Palette({ onOpenStartOrder, onOpenExternalIps, projectDescription, onDescriptionChange, ocpHealth }: { onOpenStartOrder?: () => void; onOpenExternalIps?: () => void; projectDescription?: string; onDescriptionChange?: (desc: string) => void; ocpHealth?: { phase: string; detail: string; items?: string[] } | null }) {
  const [showDesc, setShowDesc] = useState(false);
  const [editingDesc, setEditingDesc] = useState(false);
  const [showPasswords, setShowPasswords] = useState(false);
  const [showOcpStatus, setShowOcpStatus] = useState(true);
  const [collapsedSections, setCollapsedSections] = useState<Set<string>>(new Set(["Compute", "Networking", "Storage", "Project"]));
  const [revealedPasswords, setRevealedPasswords] = useState<Set<string>>(new Set());
  const nodes = useCanvasStore((s) => s.nodes);
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
      {projectDescription && (
        <div style={{ borderBottom: "1px solid var(--pf-t--global--border--color--default)" }}>
          <div
            style={{ fontSize: 11, padding: "6px 12px", cursor: "pointer", display: "flex", justifyContent: "space-between", alignItems: "center", color: "var(--pf-t--global--text--color--subtle)" }}
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
                style={{ fontSize: 11, color: "var(--pf-t--global--text--color--subtle)", padding: "0 12px 8px", lineHeight: 1.6, cursor: "pointer" }}
                onClick={() => setEditingDesc(true)}
                title="Click to edit"
              >
                {projectDescription.split(" | ").map((part, i) => <div key={i}>{part}</div>)}
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
              {ocpHealth.phase !== "ready" && <span className="project-btn-spinner" style={{ width: 10, height: 10 }} />}
              OCP STATUS
            </span>
            <span style={{ fontSize: 9 }}>{showOcpStatus ? "▾" : "▸"}</span>
          </div>
          {showOcpStatus && (
            <div style={{ padding: "0 12px 8px", fontSize: 11 }}>
              <div style={{ marginBottom: 4, color: ocpHealth.phase === "ready" ? "#4ade80" : "var(--pf-t--global--text--color--regular)", display: "flex", alignItems: "center", gap: 6 }}>
                {ocpHealth.phase === "ready" && "✓"} {ocpHealth.detail}
              </div>
              {ocpHealth.items && (
                <div style={{ fontSize: 10, lineHeight: 1.6, color: "var(--pf-t--global--text--color--subtle)" }}>
                  {ocpHealth.items.map((item, i) => (
                    <div key={i} style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
                      <span>{item.split(":")[0]}</span>
                      <span style={{ color: item.includes("Ready") || item.includes("available") || item.includes("reachable") || item.includes("ready") ? "#4ade80" : item.includes("degraded") || item.includes("failed") ? "#f87171" : "#fbbf24" }}>
                        {item.split(":").slice(1).join(":").trim()}
                      </span>
                    </div>
                  ))}
                </div>
              )}
              {ocpHealth.phase === "ready" && (() => {
                const bastionPw = passwords.find(p => p.label.includes("bastion"));
                const kubeadminPw = bastionPw?.value || "";
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
            </div>
          )}
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
          <div className="palette-item" onClick={onOpenStartOrder} style={{ cursor: "pointer" }}>
            <div className="palette-icon" style={{ background: "rgba(108,99,255,0.15)" }}>🔢</div>
            <div>
              <div className="palette-item-label">Start Order</div>
              <div className="palette-item-desc">VM boot sequence</div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
