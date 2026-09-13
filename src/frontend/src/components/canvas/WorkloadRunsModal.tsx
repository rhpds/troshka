"use client";

import React, { useEffect, useState } from "react";

interface RunItem {
  id: string;
  kind: string;
  catalog_item: string | null;
  role_fqcn: string | null;
  status: string;
  error: string | null;
  created_at: string;
}
interface Props {
  projectId: string;
  onClose: () => void;
  onOpenRun: (runId: string) => void;
}

export default function WorkloadRunsModal({ projectId, onClose, onOpenRun }: Props) {
  const [runs, setRuns] = useState<RunItem[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await fetch(`/api/v1/projects/${projectId}/workloads`);
        const data = await r.json();
        if (!cancelled) setRuns(Array.isArray(data) ? data : []);
      } catch {
        /* leave empty */
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [projectId]);

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
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        style={{
          background: "var(--pf-t--global--background--color--primary--default)",
          borderRadius: 12,
          padding: 24,
          width: 720,
          maxWidth: "92vw",
          maxHeight: "80vh",
          overflowY: "auto",
          boxShadow: "0 8px 32px rgba(0,0,0,0.5)",
          border: "1px solid var(--pf-t--global--border--color--default)",
        }}
      >
        <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 16 }}>
          <h2 style={{ margin: 0 }}>Workload Runs</h2>
          <button onClick={onClose}>✕</button>
        </div>
        {loading ? (
          <div style={{ opacity: 0.6 }}>Loading…</div>
        ) : runs.length === 0 ? (
          <div style={{ opacity: 0.6 }}>No workload runs yet.</div>
        ) : (
          <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
            <thead>
              <tr style={{ borderBottom: "1px solid var(--pf-t--global--border--color--default)" }}>
                <th style={{ textAlign: "left", padding: "4px 8px", fontSize: 11, opacity: 0.6 }}>TARGET</th>
                <th style={{ textAlign: "left", padding: "4px 8px", fontSize: 11, opacity: 0.6 }}>KIND</th>
                <th style={{ textAlign: "left", padding: "4px 8px", fontSize: 11, opacity: 0.6 }}>STATUS</th>
                <th style={{ textAlign: "left", padding: "4px 8px", fontSize: 11, opacity: 0.6 }}>CREATED</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => (
                <tr key={run.id} style={{ borderBottom: "1px solid var(--pf-t--global--border--color--default)" }}>
                  <td style={{ padding: "6px 8px" }}>{run.role_fqcn || run.catalog_item || "—"}</td>
                  <td style={{ padding: "6px 8px" }}>{run.kind}</td>
                  <td style={{ padding: "6px 8px" }}>{run.status}</td>
                  <td style={{ padding: "6px 8px" }}>{run.created_at?.slice(0, 19).replace("T", " ")}</td>
                  <td style={{ padding: "6px 8px", textAlign: "right" }}>
                    <button className="props-library-btn" onClick={() => onOpenRun(run.id)}>
                      View
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
