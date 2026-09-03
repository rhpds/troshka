"use client";

import React, { useEffect, useRef, useState } from "react";
import { useCanvasStore } from "@/stores/canvasStore";

/**
 * Per-cluster OCP install log + status modal, opened from a cluster box's
 * "Log" button (store `clusterLogTarget`). Polls
 * `GET /projects/{id}/ocp/install-log?cluster=<key>` while open so the log and
 * derived status update live during the (long) agent-based install.
 */
// Ordered agent-based install stages, each recognised by a marker in the ops-pod
// install log. Derived from the log (not the bastion health monitor, which does
// not run for pod/bastionless installs), so the checklist progresses live from
// "building ISO" all the way to "install complete".
const INSTALL_STAGES: { label: string; re: RegExp }[] = [
  { label: "Fetching release image", re: /Fetching image from OCP release|Extracting base ISO|internal constant for release image/i },
  { label: "Building agent ISO", re: /Fetching Agent Installer ISO|Generating.*ISO/i },
  { label: "Agent ISO ready", re: /Generated ISO|Agent ISO created/i },
  { label: "Booting node (Redfish)", re: /Serving via HTTP|Booting nodes|InsertMedia|ForceRestart|Waiting for cluster install/i },
  { label: "Node installing", re: /reached installation stage|to installing|preparing-for-installation|preparing-successful/i },
  { label: "Writing image to disk", re: /Writing image to disk/i },
  { label: "Bootstrap Kube API", re: /Waiting for bootkube|Bootstrap Kube API Initialized/i },
  { label: "Cluster operators", re: /Working towards|waiting for the cluster to initialize|Could not update|Cluster operators/i },
  { label: "Install complete", re: /install complete|Cluster is installed|Install is complete|installation completed/i },
];

type StageState = "done" | "active" | "pending";

function deriveStages(log: string): { label: string; state: StageState }[] {
  let last = -1;
  INSTALL_STAGES.forEach((s, i) => {
    if (s.re.test(log)) last = i;
  });
  const completeIdx = INSTALL_STAGES.length - 1;
  return INSTALL_STAGES.map((s, i) => {
    let state: StageState = "pending";
    if (i < last || (i === last && last === completeIdx)) state = "done";
    else if (i === last) state = "active";
    return { label: s.label, state };
  });
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
  const stages = deriveStages(log);

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
              Install progress
            </div>
            {/* Install stages derived from the log — no bastion/cluster access
                needed. Each stage: ✓ done, ⟳ active, ○ pending. */}
            <div style={{ fontSize: 11, lineHeight: 1.9 }}>
              {stages.map((s) => (
                <div key={s.label} style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <span style={{ width: 14, flexShrink: 0, textAlign: "center" }}>
                    {s.state === "done" ? (
                      <span style={{ color: "#4ade80" }}>✓</span>
                    ) : s.state === "active" ? (
                      <span
                        className="project-btn-spinner"
                        style={{ width: 10, height: 10, display: "inline-block", verticalAlign: "middle" }}
                      />
                    ) : (
                      <span style={{ color: "var(--troshka-text-dim, #64748b)" }}>○</span>
                    )}
                  </span>
                  <span
                    style={{
                      color:
                        s.state === "done"
                          ? "var(--pf-t--global--text--color--regular)"
                          : s.state === "active"
                            ? "#22d3ee"
                            : "var(--pf-t--global--text--color--subtle)",
                    }}
                  >
                    {s.label}
                  </span>
                </div>
              ))}
            </div>
            {/* When the bastion-based health monitor has data (bastion installs),
                surface its summary line too. */}
            {ocpHealth?.detail && (
              <div style={{ fontSize: 10, marginTop: 10, opacity: 0.7 }}>{ocpHealth.detail}</div>
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
