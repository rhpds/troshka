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
  { label: "Net booting node", re: /Serving via HTTP|Booting nodes|InsertMedia|ForceRestart|Waiting for cluster install/i },
  { label: "Node installing", re: /reached installation stage|to installing|preparing-for-installation|preparing-successful/i },
  { label: "Writing image to disk", re: /Writing image to disk/i },
  { label: "Bootstrap Kube API", re: /Waiting for bootkube|Bootstrap Kube API Initialized/i },
  { label: "Cluster operators", re: /Working towards|waiting for the cluster to initialize|Could not update|Cluster operators/i },
  { label: "Install complete", re: /install complete|Cluster is installed|Install is complete|installation completed/i },
];

type StageState = "done" | "active" | "pending";

/** Elapsed install seconds from the log's first→last "[HH:MM:SS]" timestamps
 *  (handles a single midnight wrap). Null until there are two timestamps. */
function logElapsedSecs(log: string): number | null {
  const ts = [...log.matchAll(/\[(\d{2}):(\d{2}):(\d{2})\]/g)];
  if (ts.length < 2) return null;
  const secs = (m: RegExpMatchArray) => +m[1] * 3600 + +m[2] * 60 + +m[3];
  let d = secs(ts[ts.length - 1]) - secs(ts[0]);
  if (d < 0) d += 86400;
  return d;
}

function fmtElapsed(total: number): string {
  return `${Math.floor(total / 60)}m ${(total % 60).toString().padStart(2, "0")}s`;
}

/** Operators still initializing, from the newest "Cluster operators X, Y are not
 *  available" line. (The "N of M done (P%)" figure openshift-install prints is
 *  intentionally ignored — it oscillates wildly as manifests retry.) */
function parseOperators(log: string): { pending: string[] } {
  let pending: string[] = [];
  for (const line of log.split("\n")) {
    const op = line.match(/Cluster operators? (.+?) (?:is|are) not available/i);
    if (op) {
      pending = op[1]
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
    }
  }
  return { pending };
}

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
  const nodes = useCanvasStore((s) => s.nodes);
  const [log, setLog] = useState("");
  const [loading, setLoading] = useState(false);
  const [revealPw, setRevealPw] = useState(false);
  const [fetchedAt, setFetchedAt] = useState(0);
  // kubeadmin password + kubeconfig availability, polled live from the backend
  // (harvested from the ops pod after install) so credentials appear without a
  // project reload. Null until the first poll returns.
  const [access, setAccess] = useState<{
    kubeadmin_password: string;
    kubeconfig_available: boolean;
    vm_name: string;
  } | null>(null);
  const [, setTick] = useState(0);
  const preRef = useRef<HTMLPreElement>(null);

  useEffect(() => {
    if (!target || !projectId) return;
    let cancelled = false;
    setLog("");
    setAccess(null);
    setLoading(true);
    const fetchLog = async () => {
      try {
        const r = await fetch(
          `/api/v1/projects/${projectId}/ocp/install-log?cluster=${encodeURIComponent(target.clusterKey)}`,
        );
        if (!r.ok || cancelled) return;
        const data = await r.json();
        if (!cancelled) {
          setLog(data.output || "");
          setFetchedAt(Date.now());
          if (data.kubeadmin_password || data.kubeconfig_available) {
            setAccess({
              kubeadmin_password: data.kubeadmin_password || "",
              kubeconfig_available: !!data.kubeconfig_available,
              vm_name: data.vm_name || "",
            });
          }
        }
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

  // Tick every second so the elapsed timer advances live between log polls.
  useEffect(() => {
    if (!target) return;
    const t = setInterval(() => setTick((x) => x + 1), 1000);
    return () => clearInterval(t);
  }, [target]);

  // Auto-scroll to the newest line as the log grows.
  useEffect(() => {
    if (preRef.current) preRef.current.scrollTop = preRef.current.scrollHeight;
  }, [log]);

  if (!target) return null;

  const stages = deriveStages(log);
  const ops = parseOperators(log);
  const installed = stages[stages.length - 1]?.state === "done";
  const failed =
    ocpHealth?.phase === "error" ||
    ocpHealth?.phase === "timeout" ||
    /level=fatal|cluster\(s\) failed/i.test(log);
  // Terminal = complete OR failed: stop advancing the timer either way.
  const terminal = installed || failed;

  // Elapsed = log-derived base + seconds since the last poll (ticks live while
  // installing; frozen at the log's value once the install is complete or failed).
  const baseSecs = logElapsedSecs(log);
  const elapsed =
    baseSecs == null
      ? null
      : fmtElapsed(baseSecs + (terminal || !fetchedAt ? 0 : Math.floor((Date.now() - fetchedAt) / 1000)));

  // kubeadmin password + kubeconfig live on the cluster's member VM nodes; show
  // them here (the palette OCP panel is gone for pod installs) once present.
  const members = nodes.filter(
    (n) => n.type === "vmNode" && (n.data as Record<string, unknown>).clusterId === target.clusterKey,
  );
  const storePw = members
    .map((n) => (n.data as Record<string, unknown>).ocpKubeadminPassword as string | undefined)
    .find(Boolean);
  const storeKubeconfigVm = members.find((n) => (n.data as Record<string, unknown>).ocpKubeconfig);
  // Prefer the live-polled creds (no reload needed); fall back to store state
  // (populated on project load) so an already-deployed cluster still shows them.
  const kubeadminPw = access?.kubeadmin_password || storePw;
  const hasKubeconfig = access?.kubeconfig_available || !!storeKubeconfigVm;

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
          <h3 style={{ margin: 0 }}>
            ☸ {target.name} — Status &amp; Log
            {elapsed && (
              <span style={{ fontSize: 12, fontWeight: 400, color: "var(--troshka-text-dim, #94a3b8)", marginLeft: 8 }}>
                · {elapsed}
              </span>
            )}
          </h3>
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
                <React.Fragment key={s.label}>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
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
                {/* Operators still initializing (from the log) render UNDER the
                    active "Cluster operators" stage, so "Install complete" stays
                    last. One ⟳ line each. */}
                {s.label === "Cluster operators" && s.state === "active" && ops.pending.length > 0 && (
                  <div style={{ fontSize: 10, marginTop: 2, marginBottom: 2, lineHeight: 1.8, paddingLeft: 22 }}>
                    <div style={{ color: "var(--troshka-text-dim, #94a3b8)", marginBottom: 2 }}>Operators pending:</div>
                    {ops.pending.map((op) => (
                      <div key={op} style={{ display: "flex", alignItems: "center", gap: 6, color: "#22d3ee" }}>
                        <span
                          className="project-btn-spinner"
                          style={{ width: 8, height: 8, display: "inline-block", verticalAlign: "middle" }}
                        />
                        {op}
                      </div>
                    ))}
                  </div>
                )}
                </React.Fragment>
              ))}
            </div>
            {/* Access — kubeadmin password + kubeconfig once the cluster is up. */}
            {(kubeadminPw || hasKubeconfig) && (
              <div style={{ marginTop: 12, borderTop: "1px solid rgba(255,255,255,0.1)", paddingTop: 10 }}>
                <div style={{ fontWeight: 600, marginBottom: 6, color: "var(--troshka-text-dim, #94a3b8)" }}>Access</div>
                {kubeadminPw && (
                  <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 4 }}>
                    <span style={{ color: "var(--pf-t--global--text--color--subtle)" }}>kubeadmin</span>
                    <code style={{ fontSize: 11, cursor: "pointer", userSelect: "all" }} onClick={() => setRevealPw((v) => !v)}>
                      {revealPw ? kubeadminPw : "••••••"}
                    </code>
                    <span
                      style={{ cursor: "pointer", fontSize: 10, opacity: 0.6 }}
                      onClick={() => navigator.clipboard.writeText(kubeadminPw)}
                      title="Copy"
                    >
                      Copy
                    </span>
                  </div>
                )}
                {hasKubeconfig && (
                  <span
                    style={{ cursor: "pointer", fontSize: 10, opacity: 0.7, textDecoration: "underline" }}
                    onClick={async () => {
                      // Download live from the backend (deployed_topology) so it
                      // works without the project being reloaded into the store.
                      const vm = access?.vm_name;
                      const kc = vm
                        ? await fetch(
                            `/api/v1/projects/${projectId}/kubeconfig?vm=${encodeURIComponent(vm)}`,
                          )
                            .then((r) => (r.ok ? r.text() : ""))
                            .catch(() => "")
                        : ((storeKubeconfigVm?.data as Record<string, unknown>)?.ocpKubeconfig as string) || "";
                      if (!kc) return;
                      const blob = new Blob([kc], { type: "application/x-yaml" });
                      const url = URL.createObjectURL(blob);
                      const a = document.createElement("a");
                      a.href = url;
                      a.download = `kubeconfig-${target.name}.yaml`;
                      a.click();
                      URL.revokeObjectURL(url);
                    }}
                  >
                    Download kubeconfig
                  </span>
                )}
              </div>
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
