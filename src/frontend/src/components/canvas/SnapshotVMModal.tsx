"use client";

import ModalBackdrop from "@/components/ModalBackdrop";
import { CaptureError, checkCaptureName, stopForCapture, waitForDiskCapture } from "./captureLifecycle";
import LabeledField from "@/components/LabeledField";
import React, { useState } from "react";

interface SnapshotVMModalProps {
  projectId: string;
  vmId: string;
  vmName: string;
  isRunning: boolean;
  onSaved: () => void;
  onClose: () => void;
}

export default function SnapshotVMModal({ projectId, vmId, vmName, isRunning, onSaved, onClose }: Readonly<SnapshotVMModalProps>) {
  const [name, setName] = useState(`${vmName} snapshot`);
  const [description, setDescription] = useState("");
  const [stopVM, setStopVM] = useState(isRunning);
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
    let stopRequested = false;
    try {
      await checkCaptureName(`/api/v1/library/?type=snapshot&q=${encodeURIComponent(name.trim())}`, name.trim(), "snapshot");
      if (isRunning && stopVM) {
        stopRequested = true;
        await stopForCapture({
          baseUrl: `/api/v1/projects/${projectId}/vms/${vmId}`,
          statusUrl: `/api/v1/projects/${projectId}/vms/${vmId}/status`,
          forceAction: "forcestop", stoppedStates: ["shut off", "shut_off"],
          subject: "VM", onStatus: setSavingStatus,
        });
      }

      setSavingStatus("Creating snapshot...");
      const resp = await fetch(`/api/v1/projects/${projectId}/vms/${vmId}/snapshot`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, description }),
      });
      if (resp.ok) {
        const snapData = await resp.json();
        // Wait for disk capture to complete (flatten + S3 upload)
        setSavingStatus("Capturing disk...");
        await waitForDiskCapture(snapData.id);
        if (stopRequested) {
          setSavingStatus("Restarting VM...");
          await fetch(`/api/v1/projects/${projectId}/vms/${vmId}/start`, { method: "POST" });
        }
        onSaved();
      } else {
        const err = await resp.json().catch(() => ({ detail: "Failed to create snapshot" }));
        setError(err.detail || "Failed to create snapshot");
        if (stopRequested) {
          await fetch(`/api/v1/projects/${projectId}/vms/${vmId}/start`, { method: "POST" });
        }
      }
    } catch (error) {
      setError(error instanceof CaptureError ? error.message : "Failed to connect to server");
      if (stopRequested) {
        await fetch(`/api/v1/projects/${projectId}/vms/${vmId}/start`, { method: "POST" }).catch(() => {});
      }
    }
    setSaving(false);
  };

  return (
    <ModalBackdrop label="Save VM snapshot" style={{
      position: "fixed", inset: 0, zIndex: 10000,
      display: "flex", alignItems: "center", justifyContent: "center",
      background: "rgba(0,0,0,0.6)",
    }} onClose={() => { if (!saving) onClose(); }}>
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
            <div>This VM is currently running. The snapshot may capture inconsistent disk state.</div>
            <label style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 8, cursor: saving ? "not-allowed" : "pointer", opacity: saving ? 0.5 : 1 }}>
              <input type="checkbox" checked={stopVM} onChange={(e) => setStopVM(e.target.checked)} disabled={saving} />
              {"Shut down VM before snapshot, restart after"}
            </label>
          </div>
        )}

        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <div>
            <LabeledField style={{ fontSize: 12, display: "block", marginBottom: 4 }} label={<>Name</>}>
            <input style={inputStyle} value={name} onChange={(e) => setName(e.target.value)} placeholder="Snapshot name" disabled={saving} /></LabeledField>
          </div>
          <div>
            <LabeledField style={{ fontSize: 12, display: "block", marginBottom: 4 }} label={<>Description</>}>
            <textarea
              style={{ ...inputStyle, minHeight: 60, resize: "vertical" }}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Optional description"
              disabled={saving}
            /></LabeledField>
          </div>

          {error && <div style={{ color: "#f87171", fontSize: 13 }}>{error}</div>}

          <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginTop: 4 }}>
            <button type="button"
              onClick={onClose}
              disabled={saving}
              style={{ ...inputStyle, width: "auto", cursor: saving ? "not-allowed" : "pointer", padding: "6px 16px", opacity: saving ? 0.4 : 1 }}
            >
              Cancel
            </button>
            <button type="button"
              onClick={handleSave}
              disabled={saving}
              style={{
                ...inputStyle, width: "auto", cursor: saving ? "wait" : "pointer",
                padding: "6px 16px", background: "rgba(74,222,128,0.15)",
                borderColor: "#4ade80", color: "#4ade80", opacity: saving ? 0.6 : 1,
              }}
            >
              {saving ? (savingStatus || "Saving...") : "Save Snapshot"}
            </button>
          </div>
        </div>
      </div>
    </ModalBackdrop>
  );
}
