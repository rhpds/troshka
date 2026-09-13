"use client";

import React, { useEffect, useState } from "react";

interface ReqRow {
  name: string;
  type: string;
  version: string;
}
interface Props {
  projectId: string;
  onClose: () => void;
  onLaunched: (runId: string) => void;
}

export default function RunWorkloadModal({ projectId, onClose, onLaunched }: Props) {
  const [roleFqcn, setRoleFqcn] = useState("");
  const [eeImage, setEeImage] = useState("");
  const [rows, setRows] = useState<ReqRow[]>([]);
  const [mode, setMode] = useState<"cluster" | "vms">("cluster");
  const [groups, setGroups] = useState<Record<string, string[]>>({});
  const [previewErrors, setPreviewErrors] = useState<string[]>([]);
  const [launching, setLaunching] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (mode !== "vms") return;
    let cancelled = false;
    (async () => {
      try {
        const r = await fetch(`/api/v1/projects/${projectId}/workloads/inventory-preview`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ target_map: { mode: "vms" } }),
        });
        const data = await r.json();
        if (!cancelled) {
          setGroups(data.groups || {});
          setPreviewErrors(data.errors || []);
        }
      } catch {
        if (!cancelled) setPreviewErrors(["Failed to load inventory preview"]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [mode, projectId]);

  const launchDisabled =
    launching || !roleFqcn.trim() || (mode === "vms" && previewErrors.length > 0);

  const launch = async () => {
    setLaunching(true);
    setError("");
    const collections = rows
      .filter((r) => r.name.trim())
      .map((r) => ({ name: r.name.trim(), type: r.type || "git", version: r.version || "main" }));
    const body: Record<string, unknown> = {
      kind: "ad_hoc",
      role_fqcn: roleFqcn.trim(),
      target_map: { mode, ...(mode === "vms" ? { vm_groups: Object.keys(groups) } : {}) },
    };
    if (collections.length) body.requirements_content = { collections };
    if (eeImage.trim()) body.ee_image = eeImage.trim();
    try {
      const r = await fetch(`/api/v1/projects/${projectId}/workloads`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
      if (r.status === 202) {
        const data = await r.json();
        onLaunched(data.id);
        return;
      }
      const data = await r.json().catch(() => ({}));
      setError(data.detail || `Launch failed (${r.status})`);
    } catch {
      setError("Launch failed");
    } finally {
      setLaunching(false);
    }
  };

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 10000,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        background: "rgba(0,0,0,0.6)",
      }}
      onClick={(e) => {
        if (e.target === e.currentTarget && !launching) onClose();
      }}
    >
      <div
        style={{
          background: "var(--pf-t--global--background--color--primary--default)",
          borderRadius: 12,
          padding: 24,
          width: 560,
          maxWidth: "92vw",
          maxHeight: "85vh",
          overflowY: "auto",
          boxShadow: "0 8px 32px rgba(0,0,0,0.5)",
          border: "1px solid var(--pf-t--global--border--color--default)",
        }}
      >
        <h2 style={{ marginTop: 0, marginBottom: 16 }}>Run Workload</h2>

        <label style={{ fontSize: 12, opacity: 0.8 }}>Role FQCN</label>
        <input
          className="props-input"
          value={roleFqcn}
          onChange={(e) => setRoleFqcn(e.target.value)}
          placeholder="role FQCN, e.g. agnosticd.core_workloads.ocp4_workload_example"
          style={{ width: "100%", marginBottom: 12 }}
        />

        <label style={{ fontSize: 12, opacity: 0.8 }}>EE image (optional)</label>
        <input
          className="props-input"
          value={eeImage}
          onChange={(e) => setEeImage(e.target.value)}
          placeholder="defaults to server config"
          style={{ width: "100%", marginBottom: 12 }}
        />

        <div style={{ fontSize: 12, opacity: 0.8, marginBottom: 4 }}>Requirements (git collections)</div>
        {rows.map((row, i) => (
          <div key={i} style={{ display: "flex", gap: 4, marginBottom: 4 }}>
            <input
              className="props-input"
              placeholder="git URL"
              value={row.name}
              onChange={(e) =>
                setRows((rs) => rs.map((r, j) => (j === i ? { ...r, name: e.target.value } : r)))
              }
              style={{ flex: 3 }}
            />
            <input
              className="props-input"
              placeholder="version"
              value={row.version}
              onChange={(e) =>
                setRows((rs) => rs.map((r, j) => (j === i ? { ...r, version: e.target.value } : r)))
              }
              style={{ flex: 1 }}
            />
            <button onClick={() => setRows((rs) => rs.filter((_, j) => j !== i))}>✕</button>
          </div>
        ))}
        <button
          className="props-library-btn"
          onClick={() => setRows((rs) => [...rs, { name: "", type: "git", version: "main" }])}
          style={{ padding: "4px 8px", fontSize: 11, marginBottom: 12 }}
        >
          + Add requirement
        </button>

        <div style={{ marginBottom: 12 }}>
          <label style={{ marginRight: 16 }}>
            <input
              type="radio"
              name="wl-target"
              checked={mode === "cluster"}
              onChange={() => setMode("cluster")}
            />{" "}
            Cluster
          </label>
          <label>
            <input
              type="radio"
              name="wl-target"
              aria-label="VMs"
              checked={mode === "vms"}
              onChange={() => setMode("vms")}
            />{" "}
            VMs
          </label>
        </div>

        {mode === "vms" && (
          <div style={{ marginBottom: 12, fontSize: 12 }}>
            {previewErrors.length > 0 ? (
              previewErrors.map((e, i) => (
                <div key={i} style={{ color: "var(--pf-t--global--color--status--danger--default)" }}>
                  {e}
                </div>
              ))
            ) : (
              <div style={{ opacity: 0.8 }}>
                {Object.entries(groups).map(([g, hosts]) => (
                  <div key={g}>
                    <strong>{g}</strong>: {hosts.join(", ")}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {error && (
          <div style={{ color: "var(--pf-t--global--color--status--danger--default)", marginBottom: 12 }}>
            {error}
          </div>
        )}

        <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
          <button onClick={onClose} disabled={launching}>
            Cancel
          </button>
          <button className="project-publish-btn" onClick={launch} disabled={launchDisabled}>
            {launching ? "Launching…" : "Launch"}
          </button>
        </div>
      </div>
    </div>
  );
}
