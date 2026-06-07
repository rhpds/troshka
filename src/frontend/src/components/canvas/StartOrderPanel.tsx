"use client";

import React, { useEffect, useState } from "react";
import { useCanvasStore } from "@/stores/canvasStore";
import type { StartOrderEntry, VMNodeData } from "@/stores/canvasStore";

interface Props {
  onClose: () => void;
}

export default function StartOrderPanel({ onClose }: Props) {
  const nodes = useCanvasStore((s) => s.nodes);
  const startOrder = useCanvasStore((s) => s.startOrder);
  const setStartOrder = useCanvasStore((s) => s.setStartOrder);

  const vmNodes = nodes.filter((n) => n.type === "vmNode");

  const [order, setOrder] = useState<StartOrderEntry[]>([]);

  useEffect(() => {
    if (startOrder.length > 0) {
      const validIds = new Set(vmNodes.map((v) => v.id));
      const existing = startOrder.filter((e) => validIds.has(e.vmId)).map((e) => ({ ...e, autoStart: e.autoStart ?? true }));
      const missing = vmNodes.filter((v) => !existing.some((e) => e.vmId === v.id));
      setOrder([
        ...existing,
        ...missing.map((v) => ({ vmId: v.id, autoStart: true, waitForVm: null, waitForService: "", waitForPort: "", delaySeconds: 0 })),
      ]);
    } else {
      setOrder(vmNodes.map((v) => ({ vmId: v.id, autoStart: true, waitForVm: null, waitForService: "", waitForPort: "", delaySeconds: 0 })));
    }
  }, []);

  const getVmName = (id: string) => {
    const vm = vmNodes.find((v) => v.id === id);
    return vm ? (vm.data as unknown as VMNodeData).name : id;
  };

  const moveUp = (i: number) => {
    if (i === 0) return;
    const updated = [...order];
    [updated[i - 1], updated[i]] = [updated[i], updated[i - 1]];
    setOrder(updated);
  };

  const moveDown = (i: number) => {
    if (i === order.length - 1) return;
    const updated = [...order];
    [updated[i], updated[i + 1]] = [updated[i + 1], updated[i]];
    setOrder(updated);
  };

  const updateEntry = (i: number, changes: Partial<StartOrderEntry>) => {
    const updated = [...order];
    updated[i] = { ...updated[i], ...changes };
    setOrder(updated);
  };

  const save = () => {
    setStartOrder(order);
    onClose();
  };

  if (vmNodes.length === 0) {
    return (
      <div className="start-order-overlay" onClick={onClose}>
        <div className="start-order-modal" onClick={(e) => e.stopPropagation()}>
          <div className="start-order-header">
            <span>VM Start Order</span>
            <button onClick={onClose}>✕</button>
          </div>
          <div className="start-order-body">
            <p style={{ color: "var(--troshka-text-dim)", textAlign: "center", padding: 20 }}>
              No VMs in this project yet.
            </p>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="start-order-overlay" onClick={onClose}>
      <div className="start-order-modal" onClick={(e) => e.stopPropagation()}>
        <div className="start-order-header">
          <span>VM Start Order</span>
          <button onClick={onClose}>✕</button>
        </div>
        <div className="start-order-body">
          <p style={{ fontSize: 12, color: "var(--troshka-text-dim)", marginBottom: 12 }}>
            Drag to reorder. VMs start top-to-bottom. Uncheck a VM to keep it powered off at deploy.
          </p>
          {order.map((entry, i) => (
            <div key={entry.vmId} className="start-order-item">
              <div className="start-order-item-header">
                <span className="start-order-num">{i + 1}</span>
                <label className="start-order-name" style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
                  <input
                    type="checkbox"
                    checked={entry.autoStart}
                    onChange={(e) => updateEntry(i, { autoStart: e.target.checked })}
                    title="Power on at project start"
                  />
                  🖥 {getVmName(entry.vmId)}
                </label>
                <div className="start-order-arrows">
                  <button onClick={() => moveUp(i)} title="Move up" disabled={i === 0}>↑</button>
                  <button onClick={() => moveDown(i)} title="Move down" disabled={i === order.length - 1}>↓</button>
                </div>
              </div>
              {i > 0 && (
                <div className="start-order-prereqs">
                  <div className="start-order-prereq-row">
                    <div className="start-order-prereq-field">
                      <label>Wait for VM</label>
                      <select
                        value={entry.waitForVm || ""}
                        onChange={(e) => updateEntry(i, { waitForVm: e.target.value || null })}
                      >
                        <option value="">None (start immediately after previous)</option>
                        {order.slice(0, i).map((prev) => (
                          <option key={prev.vmId} value={prev.vmId}>
                            {getVmName(prev.vmId)}
                          </option>
                        ))}
                      </select>
                    </div>
                  </div>
                  {entry.waitForVm && (
                    <div className="start-order-prereq-row">
                      <div className="start-order-prereq-field">
                        <label>Wait for service</label>
                        <select
                          value={entry.waitForService || "none"}
                          onChange={(e) => updateEntry(i, { waitForService: e.target.value })}
                        >
                          <option value="none">None (just wait for VM to be running)</option>
                          <option value="tcp">TCP port</option>
                          <option value="http">HTTP endpoint</option>
                          <option value="ping">Ping (ICMP)</option>
                        </select>
                      </div>
                      {(entry.waitForService === "tcp" || entry.waitForService === "http") && (
                        <div className="start-order-prereq-field">
                          <label>{entry.waitForService === "tcp" ? "Port" : "URL"}</label>
                          <input
                            value={entry.waitForPort}
                            onChange={(e) => updateEntry(i, { waitForPort: e.target.value })}
                            placeholder={entry.waitForService === "tcp" ? "e.g. 3306" : "e.g. http://10.0.1.10:8080/health"}
                            style={{ fontFamily: "monospace" }}
                          />
                        </div>
                      )}
                    </div>
                  )}
                  <div className="start-order-prereq-row">
                    <div className="start-order-prereq-field" style={{ maxWidth: 160 }}>
                      <label>Additional delay</label>
                      <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
                        <input
                          type="number"
                          min={0}
                          value={entry.delaySeconds}
                          onChange={(e) => updateEntry(i, { delaySeconds: parseInt(e.target.value) || 0 })}
                          style={{ width: 60 }}
                        />
                        <span style={{ fontSize: 11, color: "var(--troshka-text-dim)" }}>seconds</span>
                      </div>
                    </div>
                  </div>
                </div>
              )}
              {i === 0 && (
                <div className="start-order-prereqs">
                  <span style={{ fontSize: 11, color: "var(--troshka-green)" }}>Starts first</span>
                </div>
              )}
            </div>
          ))}
        </div>
        <div className="start-order-footer">
          <button className="start-order-btn cancel" onClick={onClose}>Cancel</button>
          <button className="start-order-btn save" onClick={save}>Save Order</button>
        </div>
      </div>
    </div>
  );
}
