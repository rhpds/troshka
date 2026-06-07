"use client";

import React from "react";
import { useCanvasStore } from "@/stores/canvasStore";
import type { ExternalIp } from "@/stores/canvasStore";

interface Props {
  projectId?: string;
  onClose: () => void;
}

export default function ExternalIpsPanel({ projectId, onClose }: Props) {
  const externalIps = useCanvasStore((s) => s.externalIps);
  const setExternalIps = useCanvasStore((s) => s.setExternalIps);

  const addIp = () => {
    setExternalIps([...externalIps, {
      id: `eip-${Date.now()}-${Math.random().toString(36).slice(2, 5)}`,
      name: `IP-${externalIps.length + 1}`,
      ip: "",
    }]);
  };

  const updateIp = (i: number, changes: Partial<ExternalIp>) => {
    const updated = [...externalIps];
    updated[i] = { ...updated[i], ...changes };
    setExternalIps(updated);
  };

  const removeIp = async (i: number) => {
    const eip = externalIps[i];
    if (eip.ip && projectId) {
      try {
        await fetch(`/api/v1/projects/${projectId}/eips/${eip.id}`, { method: "DELETE" });
      } catch {}
    }
    setExternalIps(externalIps.filter((_, idx) => idx !== i));
  };

  const statusDot = (eip: ExternalIp) => {
    if (eip.state === "associated") return { color: "#4caf50", title: "Associated (active)" };
    if (eip.state === "allocated") return { color: "#ff9800", title: "Allocated (not associated)" };
    if (eip.ip) return { color: "#4caf50", title: "Assigned" };
    return { color: "#666", title: "Not yet allocated" };
  };

  return (
    <div className="start-order-overlay" onClick={onClose}>
      <div className="start-order-modal" style={{ width: 450 }} onClick={(e) => e.stopPropagation()}>
        <div className="start-order-header">
          <span>External IPs</span>
          <button onClick={onClose}>✕</button>
        </div>
        <div className="start-order-body">
          <p style={{ fontSize: 12, color: "var(--troshka-text-dim)", marginBottom: 12 }}>
            Allocate external IPs for this project. EIPs are assigned on first deploy and remain stable across redeploys.
          </p>
          {externalIps.length === 0 && (
            <p style={{ fontSize: 12, color: "var(--troshka-text-dim)", textAlign: "center", padding: 20 }}>
              No external IPs allocated. Click below to add one.
            </p>
          )}
          {externalIps.map((eip, i) => {
            const dot = statusDot(eip);
            return (
              <div key={eip.id} className="start-order-item">
                <div style={{ padding: 10, display: "flex", gap: 8, alignItems: "center" }}>
                  <span style={{
                    width: 8, height: 8, borderRadius: "50%", flexShrink: 0,
                    backgroundColor: dot.color, display: "inline-block",
                  }} title={dot.title} />
                  <div style={{ flex: 1 }}>
                    <input
                      className="props-input"
                      value={eip.name}
                      onChange={(e) => updateIp(i, { name: e.target.value })}
                      placeholder="e.g. Primary"
                      style={{ fontSize: 12 }}
                    />
                  </div>
                  {eip.ip && (
                    <span style={{ fontFamily: "monospace", fontSize: 12, color: "var(--troshka-green)", flexShrink: 0 }}>
                      {eip.ip}
                    </span>
                  )}
                  <button
                    style={{ background: "none", border: "none", color: "var(--troshka-red)", cursor: "pointer", fontSize: 14, padding: 4, flexShrink: 0 }}
                    onClick={() => removeIp(i)}
                    title={eip.ip ? "Release and remove" : "Remove"}
                  >✕</button>
                </div>
              </div>
            );
          })}
        </div>
        <div className="start-order-footer">
          <button className="start-order-btn cancel" onClick={addIp}>+ Add IP</button>
          <button className="start-order-btn save" onClick={onClose}>Done</button>
        </div>
      </div>
    </div>
  );
}
