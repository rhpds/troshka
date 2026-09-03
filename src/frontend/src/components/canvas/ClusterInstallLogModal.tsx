"use client";

import React, { useEffect, useRef, useState } from "react";
import { useCanvasStore } from "@/stores/canvasStore";

/**
 * Per-cluster OCP install log + status modal, opened from a cluster box's
 * "Log" button (store `clusterLogTarget`). Polls
 * `GET /projects/{id}/ocp/install-log?cluster=<key>` while open so the log and
 * derived status update live during the (long) agent-based install.
 */
/** Split "name: status" health item into its parts and classify good/bad. */
function classifyItem(item: string): { name: string; status: string; good: boolean; bad: boolean } {
  const name = item.split(":")[0].trim();
  const status = item.split(":").slice(1).join(":").trim();
  const good = /✓|available|Ready|reachable|ready/.test(status);
  const bad = /✗|degraded|failed|not available/.test(status);
  return { name, status, good, bad };
}

export default function ClusterInstallLogModal() {
  const target = useCanvasStore((s) => s.clusterLogTarget);
  const close = useCanvasStore((s) => s.closeClusterLog);
  const projectId = useCanvasStore((s) => s.currentProjectId);
  const ocpHealth = useCanvasStore((s) => s.ocpHealth);
  const [log, setLog] = useState("");
  const [loading, setLoading] = useState(false);
  const preRef = useRef<HTMLPreElement>(null);

  useEffect(() => {
    if (!target || !projectId) return;
    let cancelled = false;
    setLog("");
    setLoading(true);
    const fetchLog = async () => {
      try {
        const r = await fetch(
          `/api/v1/projects/${projectId}/ocp/install-log?cluster=${encodeURIComponent(target.clusterKey)}`,
        );
        if (!r.ok || cancelled) return;
        const data = await r.json();
        if (!cancelled) setLog(data.output || "");
      } catch {
        /* transient — keep the last log */
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    fetchLog();
    const timer = setInterval(fetchLog, 4000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [target, projectId]);

  // Auto-scroll to the newest line as the log grows.
  useEffect(() => {
    if (preRef.current) preRef.current.scrollTop = preRef.current.scrollHeight;
  }, [log]);

  if (!target) return null;

  // Derive a one-line status from the newest meaningful log line.
  const lines = log.split("\n").filter((l) => l.trim());
  const logStatus = lines.length ? lines[lines.length - 1] : "waiting for install to start…";

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
      onClick={close}
    >
      <div
        style={{
          background: "var(--pf-t--global--background--color--primary--default)",
          borderRadius: 12,
          padding: 24,
          width: "80vw",
          maxWidth: 900,
          maxHeight: "80vh",
          boxShadow: "0 8px 32px rgba(0,0,0,0.5)",
          border: "1px solid var(--pf-t--global--border--color--default)",
          display: "flex",
          flexDirection: "column",
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
          <h3 style={{ margin: 0 }}>☸ {target.name} — Status &amp; Log</h3>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            {log && (
              <button
                onClick={(e) => {
                  navigator.clipboard.writeText(log);
                  const btn = e.currentTarget;
                  btn.textContent = "Copied!";
                  setTimeout(() => {
                    btn.textContent = "Copy All";
                  }, 1500);
                }}
                style={{
                  background: "rgba(255,255,255,0.08)",
                  border: "1px solid rgba(255,255,255,0.15)",
                  color: "var(--pf-t--global--text--color--regular)",
                  cursor: "pointer",
                  fontSize: 11,
                  padding: "4px 10px",
                  borderRadius: 4,
                }}
              >
                Copy All
              </button>
            )}
            <button
              onClick={close}
              style={{
                background: "transparent",
                border: "none",
                color: "var(--pf-t--global--text--color--regular)",
                cursor: "pointer",
                fontSize: 18,
              }}
            >
              ✕
            </button>
          </div>
        </div>
        {/* Status (left) beside the log (right). */}
        <div style={{ display: "flex", gap: 12, flex: 1, minHeight: 0 }}>
          <div
            style={{
              width: 240,
              flexShrink: 0,
              overflowY: "auto",
              fontSize: 11,
              padding: 10,
              borderRadius: 6,
              background: "rgba(34,211,238,0.06)",
              border: "1px solid rgba(34,211,238,0.2)",
            }}
          >
            <div style={{ fontWeight: 600, marginBottom: 8, color: "var(--troshka-text-dim, #94a3b8)" }}>
              Cluster status
            </div>
            {/* Phase/detail from the health monitor, else the newest log line. */}
            <div
              style={{
                marginBottom: 10,
                color:
                  ocpHealth?.phase === "ready"
                    ? "#4ade80"
                    : ocpHealth?.phase === "error" || ocpHealth?.phase === "timeout"
                      ? "#f87171"
                      : ocpHealth?.phase === "warning"
                        ? "#fbbf24"
                        : "var(--pf-t--global--text--color--regular)",
              }}
            >
              {ocpHealth?.phase === "ready" && "✓ "}
              {(ocpHealth?.phase === "error" || ocpHealth?.phase === "timeout") && "✗ "}
              {ocpHealth?.phase === "warning" && "⚠ "}
              {ocpHealth?.detail || logStatus}
            </div>
            {/* Component checklist (cluster operators / API reachability). */}
            {ocpHealth?.items && ocpHealth.items.length > 0 ? (
              <div style={{ fontSize: 10, lineHeight: 1.7, color: "var(--pf-t--global--text--color--subtle)" }}>
                {ocpHealth.items.map((item, i) => {
                  const { name, status, good, bad } = classifyItem(item);
                  return (
                    <div key={i} style={{ display: "flex", justifyContent: "space-between", gap: 6 }}>
                      <span style={{ whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{name}</span>
                      <span style={{ color: good ? "#4ade80" : bad ? "#f87171" : "#fbbf24", flexShrink: 0 }}>
                        {good ? "✓" : bad ? "✗" : status}
                      </span>
                    </div>
                  );
                })}
              </div>
            ) : (
              <div style={{ fontSize: 10, opacity: 0.5 }}>Health checks appear once the cluster is reachable.</div>
            )}
          </div>
          <pre
            ref={preRef}
            style={{
              fontSize: 11,
              fontFamily: "monospace",
              whiteSpace: "pre-wrap",
              overflowY: "auto",
              flex: 1,
              margin: 0,
              padding: 8,
              background: "rgba(0,0,0,0.2)",
              borderRadius: 6,
              lineHeight: 1.5,
            }}
          >
            {log || (
              <span style={{ opacity: 0.5 }}>
                <span
                  className="project-btn-spinner"
                  style={{ width: 12, height: 12, display: "inline-block", verticalAlign: "middle", marginRight: 6 }}
                />
                {loading ? "Loading install log…" : "No install log yet."}
              </span>
            )}
          </pre>
        </div>
      </div>
    </div>
  );
}
