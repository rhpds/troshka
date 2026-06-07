"use client";

import React, { useState } from "react";

interface SnapshotVMModalProps {
  projectId: string;
  vmId: string;
  vmName: string;
  isRunning: boolean;
  onSaved: () => void;
  onClose: () => void;
}

export default function SnapshotVMModal({ projectId, vmId, vmName, isRunning, onSaved, onClose }: SnapshotVMModalProps) {
  const [name, setName] = useState(`${vmName} snapshot`);
  const [description, setDescription] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const inputStyle = {
    width: "100%",
    padding: "6px 10px",
    borderRadius: 6,
    border: "1px solid var(--pf-t--global--border--color--default)",
    background: "var(--pf-t--global--background--color--primary--default)",
    color: "var(--pf-t--global--text--color--regular)",
    fontSize: 13,
  };

  const handleSave = async () => {
    if (!name.trim()) { setError("Name is required"); return; }
    setSaving(true);
    setError("");
    try {
      const resp = await fetch(`/api/v1/projects/${projectId}/vms/${vmId}/snapshot`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, description }),
      });
      if (resp.ok) {
        onSaved();
      } else {
        const err = await resp.json().catch(() => ({ detail: "Failed to create snapshot" }));
        setError(err.detail || "Failed to create snapshot");
      }
    } catch {
      setError("Failed to connect to server");
    }
    setSaving(false);
  };

  return (
    <div style={{
      position: "fixed", inset: 0, zIndex: 10000,
      display: "flex", alignItems: "center", justifyContent: "center",
      background: "rgba(0,0,0,0.6)",
    }} onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div style={{
        background: "var(--pf-t--global--background--color--primary--default)",
        borderRadius: 12, padding: 24, width: 440, maxWidth: "90vw",
        boxShadow: "0 8px 32px rgba(0,0,0,0.5)",
        border: "1px solid var(--pf-t--global--border--color--default)",
      }}>
        <h2 style={{ marginTop: 0, marginBottom: 16 }}>Save VM Snapshot</h2>

        {isRunning && (
          <div style={{
            padding: "8px 12px", marginBottom: 16, borderRadius: 6,
            background: "rgba(251,191,36,0.12)", border: "1px solid rgba(251,191,36,0.3)",
            color: "#fbbf24", fontSize: 13,
          }}>
            This VM is currently running. The snapshot may capture inconsistent disk state. Consider shutting down first.
          </div>
        )}

        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <div>
            <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Name</label>
            <input style={inputStyle} value={name} onChange={(e) => setName(e.target.value)} placeholder="Snapshot name" />
          </div>
          <div>
            <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Description</label>
            <textarea
              style={{ ...inputStyle, minHeight: 60, resize: "vertical" }}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Optional description"
            />
          </div>

          {error && <div style={{ color: "#f87171", fontSize: 13 }}>{error}</div>}

          <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginTop: 4 }}>
            <button
              onClick={onClose}
              style={{ ...inputStyle, width: "auto", cursor: "pointer", padding: "6px 16px" }}
            >
              Cancel
            </button>
            <button
              onClick={handleSave}
              disabled={saving}
              style={{
                ...inputStyle, width: "auto", cursor: saving ? "wait" : "pointer",
                padding: "6px 16px", background: "rgba(74,222,128,0.15)",
                borderColor: "#4ade80", color: "#4ade80", opacity: saving ? 0.6 : 1,
              }}
            >
              {saving ? "Saving..." : "Save Snapshot"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
