"use client";

import React, { useEffect, useState } from "react";
import { useCanvasStore } from "@/stores/canvasStore";

interface ReqRow {
  name: string;
  type: string;
  version: string;
}

interface Props {
  projectId: string;
  onClose: () => void;
  onLaunched: (runIds: string[]) => void;
  initialMode?: "cluster" | "vms";
  initialClusterIds?: string[];
  initialVmNames?: string[];
}

export default function RunWorkloadModal({
  projectId,
  onClose,
  onLaunched,
  initialMode,
  initialClusterIds,
  initialVmNames,
}: Props) {
  const [roleFqcn, setRoleFqcn] = useState("");
  const [eeImage, setEeImage] = useState("");
  const [rows, setRows] = useState<ReqRow[]>([]);
  const [mode, setMode] = useState<"cluster" | "vms">(initialMode || "cluster");
  const [selectedClusterIds, setSelectedClusterIds] = useState<string[]>(() => {
    // Initialize with props if provided
    if (initialClusterIds && initialClusterIds.length > 0) {
      return initialClusterIds;
    }
    return [];
  });
  const [selectedVmNames, setSelectedVmNames] = useState<string[]>(() => {
    if (initialVmNames && initialVmNames.length > 0) {
      return initialVmNames;
    }
    return [];
  });
  const [extraVarsText, setExtraVarsText] = useState("");
  const [showExtraVars, setShowExtraVars] = useState(false);
  const [launching, setLaunching] = useState(false);
  const [error, setError] = useState("");

  const clusters = useCanvasStore((s) => s.clusters);
  const nodes = useCanvasStore((s) => s.nodes);
  const vmNodes = nodes.filter((n) => n.type === "vmNode");

  // Auto-select single cluster if no initial selection and only one exists
  useEffect(() => {
    if (initialClusterIds === undefined && clusters.length === 1) {
      setSelectedClusterIds((prev) => (prev.length === 0 ? [clusters[0].id] : prev));
    }
  }, [clusters, initialClusterIds]);

  const launchDisabled =
    launching ||
    !roleFqcn.trim() ||
    (mode === "cluster" && selectedClusterIds.length === 0) ||
    (mode === "vms" && selectedVmNames.length === 0);

  const toggleCluster = (id: string) => {
    setSelectedClusterIds((prev) =>
      prev.includes(id) ? prev.filter((cid) => cid !== id) : [...prev, id],
    );
  };

  const toggleVm = (name: string) => {
    setSelectedVmNames((prev) =>
      prev.includes(name) ? prev.filter((n) => n !== name) : [...prev, name],
    );
  };

  const toggleAllClusters = () => {
    if (selectedClusterIds.length === clusters.length) {
      setSelectedClusterIds([]);
    } else {
      setSelectedClusterIds(clusters.map((c) => c.id));
    }
  };

  const toggleAllVms = () => {
    const allNames = vmNodes.map((n) => (n.data as any).name);
    if (selectedVmNames.length === allNames.length) {
      setSelectedVmNames([]);
    } else {
      setSelectedVmNames(allNames);
    }
  };

  const launch = async () => {
    setLaunching(true);
    setError("");

    const collections = rows
      .filter((r) => r.name.trim())
      .map((r) => ({ name: r.name.trim(), type: r.type || "git", version: r.version || "main" }));

    const baseBody: Record<string, unknown> = {
      kind: "ad_hoc",
      role_fqcn: roleFqcn.trim(),
    };

    if (collections.length) baseBody.requirements_content = { collections };
    if (eeImage.trim()) baseBody.ee_image = eeImage.trim();
    if (extraVarsText.trim()) baseBody.extra_vars_text = extraVarsText;

    try {
      if (mode === "cluster") {
        // Fan-out: one POST per cluster
        const runIds: string[] = [];
        for (const clusterId of selectedClusterIds) {
          const body = {
            ...baseBody,
            target_map: { mode: "cluster", cluster_id: clusterId },
          };
          const r = await fetch(`/api/v1/projects/${projectId}/workloads`, {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify(body),
          });
          if (r.status !== 202) {
            const data = await r.json().catch(() => ({}));
            setError(data.detail || `Launch failed (${r.status})`);
            setLaunching(false);
            return;
          }
          const data = await r.json();
          runIds.push(data.id);
        }
        onLaunched(runIds);
      } else {
        // VMs mode: one POST with vm_names
        const body = {
          ...baseBody,
          target_map: { mode: "vms", vm_names: selectedVmNames },
        };
        const r = await fetch(`/api/v1/projects/${projectId}/workloads`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(body),
        });
        if (r.status !== 202) {
          const data = await r.json().catch(() => ({}));
          setError(data.detail || `Launch failed (${r.status})`);
          setLaunching(false);
          return;
        }
        const data = await r.json();
        onLaunched([data.id]);
      }
    } catch {
      setError("Launch failed");
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

        {mode === "cluster" && clusters.length > 0 && (
          <div style={{ marginBottom: 12 }}>
            <div style={{ fontSize: 12, opacity: 0.8, marginBottom: 4 }}>Select clusters:</div>
            {clusters.length > 1 && (
              <label style={{ display: "block", marginBottom: 4 }}>
                <input
                  type="checkbox"
                  checked={selectedClusterIds.length === clusters.length}
                  onChange={toggleAllClusters}
                />{" "}
                All
              </label>
            )}
            {clusters.map((c) => (
              <label key={c.id} style={{ display: "block", marginBottom: 4 }}>
                <input
                  type="checkbox"
                  checked={selectedClusterIds.includes(c.id)}
                  onChange={() => toggleCluster(c.id)}
                  aria-label={c.name}
                />{" "}
                {c.name}
              </label>
            ))}
          </div>
        )}

        {mode === "vms" && vmNodes.length > 0 && (
          <div style={{ marginBottom: 12 }}>
            <div style={{ fontSize: 12, opacity: 0.8, marginBottom: 4 }}>Select VMs:</div>
            {vmNodes.length > 1 && (
              <label style={{ display: "block", marginBottom: 4 }}>
                <input
                  type="checkbox"
                  checked={selectedVmNames.length === vmNodes.length}
                  onChange={toggleAllVms}
                />{" "}
                All
              </label>
            )}
            {vmNodes.map((n) => {
              const vmName = (n.data as any).name;
              return (
                <label key={n.id} style={{ display: "block", marginBottom: 4 }}>
                  <input
                    type="checkbox"
                    checked={selectedVmNames.includes(vmName)}
                    onChange={() => toggleVm(vmName)}
                    aria-label={vmName}
                  />{" "}
                  {vmName}
                </label>
              );
            })}
          </div>
        )}

        <button
          className="props-library-btn"
          onClick={() => setShowExtraVars((v) => !v)}
          style={{ padding: "4px 8px", fontSize: 11, marginBottom: 12 }}
        >
          {showExtraVars ? "− Hide extra variables" : "+ Extra variables"}
        </button>

        {showExtraVars && (
          <textarea
            className="props-input"
            placeholder="key: value  (YAML or JSON)"
            value={extraVarsText}
            onChange={(e) => setExtraVarsText(e.target.value)}
            rows={6}
            style={{ width: "100%", marginBottom: 12, fontFamily: "monospace", fontSize: 12 }}
          />
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
