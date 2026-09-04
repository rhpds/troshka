"use client";

import React, { useState } from "react";
import type { TopologyDiffEntry } from "@/stores/canvasStore";

interface Change {
  type: "iso" | "disk";
  storageName: string;
  vmName: string;
  vmId: string;
}

interface Props {
  changes: Change[];
  diff: TopologyDiffEntry[];
  onConfirm: (restartVmIds: string[]) => void;
  onCancel: () => void;
}

const RESOURCE_ICONS: Record<string, string> = {
  VM: "🖥",
  Network: "🌐",
  Gateway: "☁",
  Storage: "💽",
  Container: "📦",
  Showroom: "📖",
  Cluster: "☸",
  "External IP": "🌍",
  Connection: "🔗",
};

const KIND_COLORS: Record<string, string> = {
  added: "#4ade80",
  removed: "#f87171",
  modified: "#fbbf24",
};

function DiffEntryRow({ entry }: { entry: TopologyDiffEntry }) {
  return (
    <div style={{ padding: "8px 0", fontSize: 13, borderBottom: "1px solid rgba(255,255,255,0.06)" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <span aria-hidden style={{ width: 16, textAlign: "center" }}>{RESOURCE_ICONS[entry.resourceType] || "▪"}</span>
        <span style={{ color: "var(--troshka-text-dim)" }}>{entry.resourceType}</span>
        <strong>{entry.name}</strong>
        <span
          style={{
            marginLeft: "auto",
            fontSize: 11,
            fontWeight: 600,
            textTransform: "uppercase",
            letterSpacing: 0.4,
            color: KIND_COLORS[entry.kind] || "var(--troshka-text-dim)",
          }}
        >
          {entry.kind}
        </span>
      </div>
      {entry.fields.length > 0 && (
        <div style={{ marginTop: 4, marginLeft: 24 }}>
          {entry.fields.map((f) => (
            <div
              key={f.key}
              style={{ display: "flex", gap: 8, alignItems: "baseline", fontSize: 12, padding: "1px 0" }}
            >
              <span style={{ minWidth: 130, color: "var(--troshka-text-dim)" }}>{f.label}</span>
              <span style={{ textDecoration: "line-through", opacity: 0.6 }}>{f.from}</span>
              <span style={{ opacity: 0.6 }}>→</span>
              <span style={{ color: "var(--troshka-text)", fontWeight: 500 }}>{f.to}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default function ReconfigureWarningModal({ changes, diff, onConfirm, onCancel }: Props) {
  const isoChanges = changes.filter((c) => c.type === "iso");
  const diskChanges = changes.filter((c) => c.type === "disk");
  const [restartVmIds, setRestartVmIds] = useState<Set<string>>(new Set());

  const toggleRestart = (vmId: string) => {
    setRestartVmIds((prev) => {
      const next = new Set(prev);
      if (next.has(vmId)) next.delete(vmId);
      else next.add(vmId);
      return next;
    });
  };

  const hasWarnings = isoChanges.length > 0 || diskChanges.length > 0;

  return (
    <div className="start-order-overlay" onClick={onCancel}>
      <div className="start-order-modal" style={{ maxWidth: 560 }} onClick={(e) => e.stopPropagation()}>
        <div className="start-order-header">
          <span>Apply Changes</span>
          <button onClick={onCancel}>&#x2715;</button>
        </div>
        <div className="start-order-body" style={{ padding: 16, maxHeight: "60vh", overflowY: "auto" }}>
          {diff.length > 0 && (
            <div style={{ marginBottom: hasWarnings ? 16 : 0 }}>
              <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>
                {diff.length} change{diff.length !== 1 ? "s" : ""} will be applied to the deployed environment
              </div>
              {diff.map((entry) => (
                <DiffEntryRow key={`${entry.resourceType}:${entry.id}:${entry.kind}`} entry={entry} />
              ))}
            </div>
          )}
          {diff.length === 0 && !hasWarnings && (
            <p style={{ fontSize: 13, color: "var(--troshka-text-dim)" }}>No topology changes detected.</p>
          )}
          {isoChanges.length > 0 && (
            <div style={{ marginBottom: diskChanges.length > 0 ? 16 : 0 }}>
              <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>ISO Changes</div>
              <p style={{ fontSize: 12, color: "var(--troshka-text-dim)", marginBottom: 8 }}>
                The following ISOs have changed. Check the VMs you want to restart now.
                Unchecked VMs will use the new ISO on their next boot.
              </p>
              {isoChanges.map((c) => (
                <label key={c.vmId + c.storageName} style={{ display: "flex", alignItems: "center", gap: 8, padding: "6px 0", fontSize: 13, cursor: "pointer" }}>
                  <input
                    type="checkbox"
                    checked={restartVmIds.has(c.vmId)}
                    onChange={() => toggleRestart(c.vmId)}
                  />
                  <span>Restart <strong>{c.vmName}</strong> <span style={{ opacity: 0.5 }}>(ISO: {c.storageName})</span></span>
                </label>
              ))}
            </div>
          )}
          {diskChanges.length > 0 && (
            <div>
              <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 8, color: "#f87171" }}>Disk Changes (data loss)</div>
              <p style={{ fontSize: 12, color: "var(--troshka-text-dim)", marginBottom: 8 }}>
                The following disks will be recreated from a new image. All existing data on these disks will be lost.
                The affected VMs will be restarted.
              </p>
              {diskChanges.map((c) => (
                <div key={c.vmId + c.storageName} style={{ padding: "6px 0", fontSize: 13 }}>
                  <span style={{ color: "#f87171" }}>&#x26A0;</span> <strong>{c.vmName}</strong> &mdash; disk <strong>{c.storageName}</strong> will be destroyed and recreated
                </div>
              ))}
            </div>
          )}
        </div>
        <div className="start-order-footer">
          <button className="start-order-btn cancel" onClick={onCancel}>Cancel</button>
          <button className="start-order-btn save" onClick={() => {
            const allRestartIds = new Set(restartVmIds);
            for (const c of diskChanges) allRestartIds.add(c.vmId);
            onConfirm(Array.from(allRestartIds));
          }}>
            Apply Changes
          </button>
        </div>
      </div>
    </div>
  );
}
