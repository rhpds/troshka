"use client";

import React, { useState, useMemo } from "react";

interface BulkDeployModalProps {
  patternId: string;
  onClose: () => void;
  onDeployed: (count: number) => void;
}

export default function BulkDeployModal({ patternId, onClose, onDeployed }: BulkDeployModalProps) {
  const [count, setCount] = useState(5);
  const [nameTemplate, setNameTemplate] = useState("lab-{n}");
  const [autoDeploy, setAutoDeploy] = useState(false);
  const [deploying, setDeploying] = useState(false);
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

  const previewNames = useMemo(() => {
    const names: string[] = [];
    const limit = Math.min(count, 3);
    for (let i = 1; i <= limit; i++) {
      names.push(nameTemplate.replace(/\{n\}/g, String(i)));
    }
    if (count > 3) names.push("...");
    return names;
  }, [count, nameTemplate]);

  const handleDeploy = async () => {
    if (count < 1 || count > 500) { setError("Count must be between 1 and 500"); return; }
    setDeploying(true);
    setError("");
    try {
      const resp = await fetch(`/api/v1/patterns/${patternId}/bulk-deploy`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          count,
          name_template: nameTemplate,
          auto_deploy: autoDeploy,
        }),
      });
      if (resp.ok) {
        const data = await resp.json();
        onDeployed(data.created || count);
      } else {
        const err = await resp.json().catch(() => ({ detail: "Bulk deploy failed" }));
        setError(err.detail || "Bulk deploy failed");
      }
    } catch {
      setError("Failed to connect to server");
    }
    setDeploying(false);
  };

  return (
    <div style={{
      position: "fixed", inset: 0, zIndex: 10000,
      display: "flex", alignItems: "center", justifyContent: "center",
      background: "rgba(0,0,0,0.6)",
    }} onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div style={{
        background: "var(--pf-t--global--background--color--primary--default)",
        borderRadius: 12, padding: 24, width: 460, maxWidth: "90vw",
        boxShadow: "0 8px 32px rgba(0,0,0,0.5)",
        border: "1px solid var(--pf-t--global--border--color--default)",
      }}>
        <h2 style={{ marginTop: 0, marginBottom: 16 }}>Bulk Deploy Pattern</h2>

        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <div>
            <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Number of Projects</label>
            <input
              style={inputStyle}
              type="number"
              min={1}
              max={500}
              value={count}
              onChange={(e) => setCount(Math.max(1, Math.min(500, parseInt(e.target.value) || 1)))}
            />
          </div>
          <div>
            <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>
              Naming Template
              <span style={{ opacity: 0.5, marginLeft: 8 }}>{"{n}"} = sequence number</span>
            </label>
            <input
              style={inputStyle}
              value={nameTemplate}
              onChange={(e) => setNameTemplate(e.target.value)}
              placeholder="lab-{n}"
            />
          </div>
          <div>
            <label style={{ fontSize: 12, display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
              <input
                type="checkbox"
                checked={autoDeploy}
                onChange={(e) => setAutoDeploy(e.target.checked)}
              />
              Auto-deploy after creation
            </label>
          </div>

          <div style={{
            padding: "8px 12px", borderRadius: 6,
            background: "rgba(148,163,184,0.08)", border: "1px solid rgba(148,163,184,0.15)",
            fontSize: 12, opacity: 0.8,
          }}>
            <div style={{ marginBottom: 4, fontWeight: 500 }}>Preview:</div>
            {previewNames.map((name, i) => (
              <div key={i} style={{ paddingLeft: 8 }}>{name}</div>
            ))}
            {count > 0 && <div style={{ marginTop: 4, opacity: 0.6 }}>{count} project{count !== 1 ? "s" : ""} total</div>}
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
              onClick={handleDeploy}
              disabled={deploying}
              style={{
                ...inputStyle, width: "auto", cursor: deploying ? "wait" : "pointer",
                padding: "6px 16px", background: "rgba(74,222,128,0.15)",
                borderColor: "#4ade80", color: "#4ade80", opacity: deploying ? 0.6 : 1,
              }}
            >
              {deploying ? "Creating..." : `Create ${count} Project${count !== 1 ? "s" : ""}`}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
