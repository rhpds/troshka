"use client";

import React from "react";
import { useCanvasStore } from "@/stores/canvasStore";
import type { ExternalIp } from "@/stores/canvasStore";

interface Props {
  onClose: () => void;
}

export default function ExternalIpsPanel({ onClose }: Props) {
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

  const removeIp = (i: number) => {
    setExternalIps(externalIps.filter((_, idx) => idx !== i));
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
            Allocate external IPs for this project. These can be assigned to gateway port forwarding rules. Leave the IP blank to auto-assign when deployed.
          </p>
          {externalIps.length === 0 && (
            <p style={{ fontSize: 12, color: "var(--troshka-text-dim)", textAlign: "center", padding: 20 }}>
              No external IPs allocated. Click below to add one.
            </p>
          )}
          {externalIps.map((eip, i) => (
            <div key={eip.id} className="start-order-item">
              <div style={{ padding: 10, display: "flex", gap: 8, alignItems: "end" }}>
                <div style={{ flex: "0 0 100px" }}>
                  <label style={{ fontSize: 11, color: "var(--troshka-text-dim)", display: "block", marginBottom: 3 }}>Name</label>
                  <input
                    className="props-input"
                    value={eip.name}
                    onChange={(e) => updateIp(i, { name: e.target.value })}
                    placeholder="e.g. Primary"
                    style={{ fontSize: 12 }}
                  />
                </div>
                <div style={{ flex: 1 }}>
                  <label style={{ fontSize: 11, color: "var(--troshka-text-dim)", display: "block", marginBottom: 3 }}>IP Address</label>
                  <input
                    className="props-input"
                    value={eip.ip}
                    onChange={(e) => updateIp(i, { ip: e.target.value })}
                    placeholder="auto-assigned on deploy"
                    style={{ fontFamily: "monospace", fontSize: 12 }}
                  />
                </div>
                <button
                  style={{ background: "none", border: "none", color: "var(--troshka-red)", cursor: "pointer", fontSize: 14, padding: 4 }}
                  onClick={() => removeIp(i)}
                >✕</button>
              </div>
            </div>
          ))}
        </div>
        <div className="start-order-footer">
          <button className="start-order-btn cancel" onClick={addIp}>+ Add IP</button>
          <button className="start-order-btn save" onClick={onClose}>Done</button>
        </div>
      </div>
    </div>
  );
}
