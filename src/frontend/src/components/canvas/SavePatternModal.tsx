"use client";

import React, { useState } from "react";

interface SavePatternModalProps {
  projectId: string;
  projectName: string;
  hasRunningVMs: boolean;
  onSaved: (patternId: string) => void;
  onClose: () => void;
}

export default function SavePatternModal({ projectId, projectName, hasRunningVMs, onSaved, onClose }: SavePatternModalProps) {
  const [name, setName] = useState(projectName);
  const [description, setDescription] = useState("");
  const [stopVMs, setStopVMs] = useState(hasRunningVMs);
  const [saving, setSaving] = useState(false);
  const [savingStatus, setSavingStatus] = useState("");
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
      if (hasRunningVMs && stopVMs) {
        setSavingStatus("Stopping VMs...");
        const stopResp = await fetch(`/api/v1/projects/${projectId}/stop`, { method: "POST" });
        if (!stopResp.ok) {
          setError("Failed to stop VMs");
          setSaving(false);
          return;
        }
        // Poll until stopped
        for (let i = 0; i < 60; i++) {
          await new Promise((r) => setTimeout(r, 3000));
          const stateResp = await fetch(`/api/v1/projects/${projectId}`);
          if (stateResp.ok) {
            const proj = await stateResp.json();
            if (proj.state === "stopped") break;
          }
        }
      }

      setSavingStatus("Creating pattern...");
      const resp = await fetch("/api/v1/patterns/", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name,
          description,
          visibility: "private",
          source_project_id: projectId,
        }),
      });
      if (resp.ok) {
        const data = await resp.json();
        if (hasRunningVMs && stopVMs) {
          setSavingStatus("Restarting VMs...");
          await fetch(`/api/v1/projects/${projectId}/start`, { method: "POST" });
        }
        onSaved(data.id);
      } else {
        const err = await resp.json().catch(() => ({ detail: "Failed to save pattern" }));
        setError(err.detail || "Failed to save pattern");
        if (hasRunningVMs && stopVMs) {
          await fetch(`/api/v1/projects/${projectId}/start`, { method: "POST" });
        }
      }
    } catch {
      setError("Failed to connect to server");
      if (hasRunningVMs && stopVMs) {
        await fetch(`/api/v1/projects/${projectId}/start`, { method: "POST" }).catch(() => {});
      }
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
        borderRadius: 12, padding: 24, width: 480, maxWidth: "90vw",
        boxShadow: "0 8px 32px rgba(0,0,0,0.5)",
        border: "1px solid var(--pf-t--global--border--color--default)",
      }}>
        <h2 style={{ marginTop: 0, marginBottom: 16 }}>Save as Pattern</h2>

        {hasRunningVMs && (
          <div style={{
            padding: "8px 12px", marginBottom: 16, borderRadius: 6,
            background: "rgba(251,191,36,0.12)", border: "1px solid rgba(251,191,36,0.3)",
            color: "#fbbf24", fontSize: 13,
          }}>
            <div>For best results, stop all VMs before creating a pattern. Running VMs may have inconsistent disk state.</div>
            <label style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 8, cursor: "pointer" }}>
              <input type="checkbox" checked={stopVMs} onChange={(e) => setStopVMs(e.target.checked)} />
              Stop VMs before capture, restart after
            </label>
          </div>
        )}

        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <div>
            <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Name</label>
            <input style={inputStyle} value={name} onChange={(e) => setName(e.target.value)} placeholder="Pattern name" />
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
              {saving ? (savingStatus || "Saving...") : "Save Pattern"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
