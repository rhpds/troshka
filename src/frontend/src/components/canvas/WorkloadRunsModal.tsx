"use client";

import React, { useEffect, useMemo, useState } from "react";
import { useCanvasStore } from "@/stores/canvasStore";
import { formatWorkloadStatusLabel } from "@/components/canvas/WorkloadRunDetailModal";
import {
  parseWorkloadChain,
  shortRoleLabel,
  workloadChainEntryStatus,
  type WorkloadChainEntryStatus,
} from "@/components/canvas/workloadStatus";

interface RunItem {
  id: string;
  kind: string;
  catalog_item: string | null;
  role_fqcn: string | null;
  status: string;
  error: string | null;
  created_at: string;
  started_at?: string | null;
  target_map: { mode?: string; cluster_id?: string; vm_names?: string[] } | null;
}
interface Props {
  projectId: string;
  onClose: () => void;
  onOpenRun: (runId: string) => void;
}

const CHAIN_STATUS_STYLE: Record<
  WorkloadChainEntryStatus,
  { label: string; color: string }
> = {
  pending: { label: "Pending", color: "rgba(148,163,184,0.95)" },
  running: { label: "Running", color: "rgba(56,189,248,0.95)" },
  succeeded: { label: "Succeeded", color: "rgba(74,222,128,0.95)" },
  failed: { label: "Failed", color: "rgba(248,113,113,0.95)" },
  done: { label: "Done (runOnce)", color: "rgba(167,139,250,0.95)" },
};

export default function WorkloadRunsModal({ projectId, onClose, onOpenRun }: Props) {
  const [runs, setRuns] = useState<RunItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState<string | null>(null);
  const clusters = useCanvasStore((s) => s.clusters);
  const topologyWorkloads = useCanvasStore((s) => s.topologyWorkloads);
  const topologyWorkloadsDone = useCanvasStore((s) => s.topologyWorkloadsDone);

  const chain = useMemo(
    () => parseWorkloadChain(topologyWorkloads),
    [topologyWorkloads],
  );
  const doneSet = useMemo(
    () => new Set((topologyWorkloadsDone || []).filter(Boolean)),
    [topologyWorkloadsDone],
  );

  const refresh = async () => {
    try {
      const r = await fetch(`/api/v1/projects/${projectId}/workloads`);
      const data = await r.json();
      setRuns(Array.isArray(data) ? data : []);
    } catch {
      /* leave */
    }
  };

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

  const cancelRun = async (runId: string) => {
    setBusyId(runId);
    try {
      const r = await fetch(`/api/v1/workloads/${runId}/cancel`, { method: "POST" });
      if (r.ok) await refresh();
    } finally {
      setBusyId(null);
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
          <h2 style={{ margin: 0 }}>Workloads</h2>
          <button onClick={onClose}>✕</button>
        </div>

        <section style={{ marginBottom: 20 }} data-testid="workload-chain-section">
          <h3
            style={{
              margin: "0 0 8px",
              fontSize: 12,
              fontWeight: 600,
              letterSpacing: "0.04em",
              textTransform: "uppercase",
              opacity: 0.65,
            }}
          >
            Template chain
          </h3>
          {chain.length === 0 ? (
            <div style={{ opacity: 0.6, fontSize: 13 }}>
              No template workload chain on this project.
            </div>
          ) : (
            <ol
              style={{
                margin: 0,
                padding: "8px 0 0 0",
                listStyle: "none",
                display: "flex",
                flexDirection: "column",
                gap: 6,
              }}
            >
              {chain.map((entry, i) => {
                const status = workloadChainEntryStatus(entry.role, runs, doneSet);
                const style = CHAIN_STATUS_STYLE[status];
                return (
                  <li
                    key={`${i}-${entry.role}`}
                    data-testid="workload-chain-entry"
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 10,
                      padding: "6px 10px",
                      borderRadius: 8,
                      border: "1px solid var(--pf-t--global--border--color--default)",
                      background: "var(--pf-t--global--background--color--secondary--default, transparent)",
                      fontSize: 13,
                    }}
                  >
                    <span style={{ opacity: 0.5, width: 22, flexShrink: 0 }}>{i + 1}.</span>
                    <span style={{ flex: 1, minWidth: 0 }}>
                      <span style={{ fontWeight: 500 }}>{shortRoleLabel(entry.role)}</span>
                      <span
                        style={{
                          display: "block",
                          fontSize: 11,
                          opacity: 0.55,
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                          whiteSpace: "nowrap",
                        }}
                        title={entry.role}
                      >
                        {entry.role}
                      </span>
                    </span>
                    {entry.runOnce && (
                      <span
                        style={{
                          fontSize: 10,
                          opacity: 0.7,
                          border: "1px solid rgba(148,163,184,0.45)",
                          borderRadius: 4,
                          padding: "1px 6px",
                        }}
                      >
                        runOnce
                      </span>
                    )}
                    <span style={{ fontSize: 12, fontWeight: 600, color: style.color }}>
                      {style.label}
                    </span>
                  </li>
                );
              })}
            </ol>
          )}
        </section>

        <h3
          style={{
            margin: "0 0 8px",
            fontSize: 12,
            fontWeight: 600,
            letterSpacing: "0.04em",
            textTransform: "uppercase",
            opacity: 0.65,
          }}
        >
          Run history
        </h3>
        {loading ? (
          <div style={{ opacity: 0.6 }}>Loading…</div>
        ) : runs.length === 0 ? (
          <div style={{ opacity: 0.6 }}>No workload runs yet.</div>
        ) : (
          <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
            <thead>
              <tr style={{ borderBottom: "1px solid var(--pf-t--global--border--color--default)" }}>
                <th style={{ textAlign: "left", padding: "4px 8px", fontSize: 11, opacity: 0.6 }}>WORKLOAD</th>
                <th style={{ textAlign: "left", padding: "4px 8px", fontSize: 11, opacity: 0.6 }}>TARGET</th>
                <th style={{ textAlign: "left", padding: "4px 8px", fontSize: 11, opacity: 0.6 }}>KIND</th>
                <th style={{ textAlign: "left", padding: "4px 8px", fontSize: 11, opacity: 0.6 }}>STATUS</th>
                <th style={{ textAlign: "left", padding: "4px 8px", fontSize: 11, opacity: 0.6 }}>CREATED</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => {
                // Derive target label from target_map
                let targetLabel = "—";
                if (run.target_map?.mode === "cluster" && run.target_map.cluster_id) {
                  const cluster = clusters.find((c) => c.id === run.target_map!.cluster_id);
                  targetLabel = cluster?.name ?? run.target_map.cluster_id;
                } else if (run.target_map?.mode === "vms" && run.target_map.vm_names) {
                  targetLabel = run.target_map.vm_names.join(", ");
                }

                return (
                  <tr key={run.id} style={{ borderBottom: "1px solid var(--pf-t--global--border--color--default)" }}>
                    <td style={{ padding: "6px 8px" }}>{run.role_fqcn || run.catalog_item || "—"}</td>
                    <td style={{ padding: "6px 8px" }}>{targetLabel}</td>
                    <td style={{ padding: "6px 8px" }}>{run.kind}</td>
                    <td style={{ padding: "6px 8px" }}>
                      <div>
                        {formatWorkloadStatusLabel(run.status, run.started_at, run.error)}
                      </div>
                      {run.error ? (
                        <div
                          style={{
                            fontSize: 11,
                            opacity: 0.7,
                            maxWidth: 220,
                            overflow: "hidden",
                            textOverflow: "ellipsis",
                            whiteSpace: "nowrap",
                          }}
                          title={run.error}
                        >
                          {run.error}
                        </div>
                      ) : null}
                    </td>
                    <td style={{ padding: "6px 8px" }}>{run.created_at?.slice(0, 19).replace("T", " ")}</td>
                    <td style={{ padding: "6px 8px", textAlign: "right", whiteSpace: "nowrap" }}>
                      {["pending", "queued", "running"].includes(run.status) && (
                        <button
                          className="props-library-btn"
                          style={{ marginRight: 6, color: "#f87171" }}
                          disabled={busyId === run.id}
                          onClick={() => cancelRun(run.id)}
                        >
                          {busyId === run.id ? "…" : "Cancel"}
                        </button>
                      )}
                      <button className="props-library-btn" onClick={() => onOpenRun(run.id)}>
                        View
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
