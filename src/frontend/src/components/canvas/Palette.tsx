"use client";

import React, { useEffect, useState } from "react";

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

function PaletteIcon({ icon, iconClass }: { icon: string; iconClass: string }) {
  if (icon === "rj45") return <div className={`palette-icon ${iconClass}`}><RJ45Icon /></div>;
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

export default function Palette({ onOpenStartOrder, onOpenExternalIps }: { onOpenStartOrder?: () => void; onOpenExternalIps?: () => void }) {
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
      {sections.map((section, sIdx) => (
        <React.Fragment key={section.title}>
          {sIdx > 0 && <div className="palette-divider" />}
          <div className="palette-section">
            <div className="palette-section-title">{section.title}</div>
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
          </div>
        </React.Fragment>
      ))}
      <div className="palette-divider" />
      <div className="palette-section">
        <div className="palette-section-title">Library</div>
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
            <div className="palette-item-label">VM Snapshots</div>
            <div className="palette-item-desc">Drag to canvas</div>
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
      </div>
      <div className="palette-divider" />
      <div className="palette-section">
        <div className="palette-section-title">Project</div>
        <div className="palette-item" onClick={onOpenStartOrder} style={{ cursor: "pointer" }}>
          <div className="palette-icon" style={{ background: "rgba(108,99,255,0.15)" }}>🔢</div>
          <div>
            <div className="palette-item-label">Start Order</div>
            <div className="palette-item-desc">VM boot sequence</div>
          </div>
        </div>
        <div className="palette-item" onClick={onOpenExternalIps} style={{ cursor: "pointer" }}>
          <div className="palette-icon palette-icon-gateway">🌍</div>
          <div>
            <div className="palette-item-label">External IPs</div>
            <div className="palette-item-desc">Public IP pool</div>
          </div>
        </div>
      </div>
    </div>
  );
}
