"use client";

import React, { useEffect, useRef, useState } from "react";
import { useCanvasStore, type ClusterConfig } from "@/stores/canvasStore";

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

// SNO + deferred workers: control plane installs first; workers join after via
// ``oc adm node-image create`` (see join_deferred_workers.py breadcrumbs).
const WORKER_JOIN_STAGES: { label: string; re: RegExp }[] = [
  { label: "Joining worker nodes", re: /joining \d+ deferred worker/i },
  { label: "Building worker ISO", re: /node-image create for/i },
  { label: "Booting worker nodes", re: /Node ISO URL|net-booting worker/i },
  {
    label: "Worker nodes joined",
    re: /deferred workers joined/i,
  },
  {
    label: "Worker nodes converged",
    re: /deferred workers converged/i,
  },
];

// PATTERN (recert) deploys don't reinstall — the disks are already installed and
// were recerted offline (kubelet-PKI wipe), then the ops pod recerts online. The
// steps are completely different from a fresh agent install, matched against the
// recert block's breadcrumbs (ops_pod_install._recert_cluster_block + the backend
// delivery thread's log lines). Selected when the log carries the "(recert)" marker.
const RECERT_STAGES: { label: string; re: RegExp }[] = [
  { label: "Waiting for control-plane API", re: /waiting for the control-plane API|Waiting for cluster installation to complete/i },
  { label: "Extracting admin kubeconfig", re: /extracting admin kubeconfig|admin kubeconfig received/i },
  { label: "Approving CSRs", re: /approving pending CSRs|approving CSRs:/i },
  { label: "Cluster operators", re: /waiting for cluster operators|waiting on operators/i },
  { label: "Recert complete", re: /install complete/i },
];

export function clusterHasDeferredWorkers(cluster?: ClusterConfig): boolean {
  return cluster?.type === "sno" && (cluster?.workers ?? 0) > 0;
}

function deferredWorkersInLog(log: string): boolean {
  return /joining \d+ deferred worker|node-image create for|deferred workers joined|deferred workers converged|deferred workers Ready:|waiting for worker/i.test(
    log,
  );
}

/** Pattern deploys recert (never reinstall); the ops-pod log carries "(recert)". */
export function stagesFor(
  log: string,
  cluster?: ClusterConfig,
): { label: string; re: RegExp }[] {
  if (/\(recert\)/i.test(log)) return RECERT_STAGES;
  if (clusterHasDeferredWorkers(cluster) || deferredWorkersInLog(log)) {
    const snoComplete = INSTALL_STAGES[INSTALL_STAGES.length - 1];
    return [...INSTALL_STAGES.slice(0, -1), snoComplete, ...WORKER_JOIN_STAGES];
  }
  return INSTALL_STAGES;
}

type StageState = "done" | "active" | "pending" | "failed";

/** True when the install log (or backend status) indicates a terminal failure. */
export function installLogIndicatesFailure(
  log: string,
  clusterStatus?: string | null,
): boolean {
  if (clusterStatus === "error") return true;
  if (/level=fatal|\[.*\] install failed|worker join timed out/i.test(log)) {
    return true;
  }
  if (/error: cannot create pod/i.test(log)) return true;
  if (/\[.*\] restart failed:/i.test(log)) return true;
  // Terminal worker-join failure (not a retry breadcrumb).
  return /\[[^\]]+\] node-image create failed for /i.test(log) && !/retrying in \d+s/i.test(log);
}

/** True when openshift-install wait-for is looping without making install progress. */
export function installLogIndicatesStuck(log: string): boolean {
  // Only after ISO net-boot + wait-for starts — not during create-image or node boot.
  if (!/Waiting for cluster installation to complete/i.test(log)) {
    return false;
  }
  const initWaits = log.match(/Waiting for cluster install to initialize/gi);
  // 15 × 30s ≈ 7.5 min in the init loop; early lines are normal while the node installs.
  return (initWaits?.length ?? 0) >= 15;
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
    // recert block breadcrumb: "waiting on operators: monitoring console" (or
    // "none"). Stop at "(" so a trailing "(console http=200)" from older logs
    // isn't wrongly split into fake operator names.
    const rec = line.match(/waiting on operators:\s*(.+?)\s*(?:\(|$)/i);
    if (rec) {
      const v = rec[1].trim();
      pending = v === "none" ? [] : v.split(/\s+/).filter(Boolean);
    }
  }
  return { pending };
}

/** Latest worker Ready count from poll breadcrumbs. */
export function parseWorkerReady(log: string): { ready: number; expected: number } | null {
  let last: { ready: number; expected: number } | null = null;
  for (const line of log.split("\n")) {
    const m =
      line.match(/deferred workers Ready:\s*(\d+)\/(\d+)/i) ||
      line.match(/worker nodes Ready:\s*(\d+)\/(\d+)/i);
    if (m) last = { ready: parseInt(m[1], 10), expected: parseInt(m[2], 10) };
  }
  return last;
}

export function deriveStages(
  log: string,
  cluster?: ClusterConfig,
  failed = false,
  clusterStatus?: string | null,
): { label: string; state: StageState }[] {
  const stages = stagesFor(log, cluster);
  let last = -1;
  stages.forEach((s, i) => {
    if (s.re.test(log)) last = i;
  });
  const completeIdx = stages.length - 1;
  const workersJoined = /deferred workers joined/i.test(log);
  const workersConverged = /deferred workers converged/i.test(log);
  const workersReadyPoll = /deferred workers Ready:/i.test(log);
  const clusterReady = clusterStatus === "ready";
  return stages.map((s, i) => {
    let state: StageState = "pending";
    if (
      clusterReady &&
      workersJoined &&
      (s.label === "Worker nodes joined" || s.label === "Worker nodes converged")
    ) {
      state = "done";
    } else if (
      s.label === "Worker nodes joined" &&
      workersJoined &&
      !workersConverged
    ) {
      state = "done";
    } else if (
      s.label === "Worker nodes converged" &&
      workersJoined &&
      !workersConverged
    ) {
      state = "active";
    } else if (
      s.label === "Worker nodes joined" &&
      workersReadyPoll &&
      !workersJoined
    ) {
      state = "active";
    } else if (
      s.label === "Booting worker nodes" &&
      workersReadyPoll &&
      !workersJoined
    ) {
      state = "done";
    } else if (i < last || (i === last && last === completeIdx)) {
      state = "done";
    } else if (i === last) {
      state = "active";
    }
    if (failed && state === "active") {
      state = "failed";
    }
    return { label: s.label, state };
  });
}

export default function ClusterInstallLogModal() {
  const target = useCanvasStore((s) => s.clusterLogTarget);
  const close = useCanvasStore((s) => s.closeClusterLog);
  const projectId = useCanvasStore((s) => s.currentProjectId);
  const nodes = useCanvasStore((s) => s.nodes);
  const clusters = useCanvasStore((s) => s.clusters);
  const [log, setLog] = useState("");
  const [clusterStatus, setClusterStatus] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [revealPw, setRevealPw] = useState(false);
  // Timer basis: deployment START (epoch seconds), frozen at the backend's total
  // once terminal. Elapse from deploy start — NOT from log timestamps (recert
  // breadcrumbs have none, and installs should match).
  const [deployStartedAt, setDeployStartedAt] = useState<number | null>(null);
  const [installStartedAt, setInstallStartedAt] = useState<number | null>(null);
  const [installElapsed, setInstallElapsed] = useState<number | null>(null);
  const [controlPlaneUsableElapsed, setControlPlaneUsableElapsed] = useState<number | null>(null);
  // kubeadmin password + kubeconfig availability, polled live from the backend
  // (harvested from the ops pod after install) so credentials appear without a
  // project reload. Null until the first poll returns.
  const [access, setAccess] = useState<{
    kubeadmin_password: string;
    kubeconfig_available: boolean;
    vm_name: string;
  } | null>(null);
  const [restarting, setRestarting] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [, setTick] = useState(0);
  const preRef = useRef<HTMLPreElement>(null);
  const restartGuardRef = useRef<number | null>(null);
  const cancellingRef = useRef(false);

  useEffect(() => {
    if (!target || !projectId) return;
    let cancelled = false;
    setLog("");
    setAccess(null);
    setDeployStartedAt(null);
    setInstallStartedAt(null);
    setInstallElapsed(null);
    setControlPlaneUsableElapsed(null);
    setClusterStatus(null);
    setLoading(true);
    const fetchLog = async () => {
      try {
        const r = await fetch(
          `/api/v1/projects/${projectId}/ocp/install-log?cluster=${encodeURIComponent(target.clusterKey)}`,
        );
        if (!r.ok || cancelled) return;
        const data = await r.json();
        if (!cancelled) {
          // Freeze the log while cancel is in flight — the ops pod may still append
          // for up to ~20s until the worker tears it down.
          if (cancellingRef.current) {
            if (data.cluster_status === "error") {
              cancellingRef.current = false;
              setCancelling(false);
              setClusterStatus("error");
            }
            return;
          }
          // Ignore stale logs briefly after restart — the dying ops pod can still
          // serve the old install.log until the worker recycles it.
          if (restartGuardRef.current) {
            const out = data.output || "";
            const freshAttempt = /starting agent-based install|=== install restart/i.test(
              out,
            );
            if (
              out &&
              !freshAttempt &&
              Date.now() - restartGuardRef.current < 120000
            ) {
              return;
            }
            if (freshAttempt) restartGuardRef.current = null;
          }
          setLog(data.output || "");
          setDeployStartedAt(
            typeof data.deploy_started_at === "number" ? data.deploy_started_at : null,
          );
          setInstallStartedAt(
            typeof data.install_started_at === "number" ? data.install_started_at : null,
          );
          setInstallElapsed(
            typeof data.ocp_install_elapsed === "number" ? data.ocp_install_elapsed : null,
          );
          setControlPlaneUsableElapsed(
            typeof data.ocp_control_plane_usable_elapsed === "number"
              ? data.ocp_control_plane_usable_elapsed
              : null,
          );
          if (typeof data.cluster_status === "string") {
            setClusterStatus(data.cluster_status);
            if (data.cluster_status === "error") {
              cancellingRef.current = false;
              setCancelling(false);
            }
          }
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

  const cluster = clusters.find(
    (c) => c.id === target.clusterKey || c.name === target.clusterKey,
  );
  const failed =
    !restarting && !cancelling && installLogIndicatesFailure(log, clusterStatus);
  const stuck =
    !restarting && !cancelling && !failed && installLogIndicatesStuck(log);
  const stages = deriveStages(log, cluster, failed || cancelling, clusterStatus);
  const ops = parseOperators(log);
  const workerReady = parseWorkerReady(log);
  const installed = stages[stages.length - 1]?.state === "done";
  // Terminal = complete, failed, or cancelling: stop advancing the timer.
  const terminal = installed || failed || cancelling;

  // Prominent header status badge — mirrors the canvas/project-list OCP status
  // so the outcome is obvious from the log view itself, not just the palette.
  // Pattern deploys recert (never reinstall) — surface that distinctly (violet)
  // so it's not mistaken for a fresh install.
  const isRecert = /\(recert\)/i.test(log);
  const statusBadge = cancelling
    ? { label: "Cancelling", fg: "#fbbf24", bg: "rgba(251,191,36,0.14)", bd: "rgba(251,191,36,0.45)" }
    : failed
    ? { label: "Error", fg: "#f87171", bg: "rgba(248,113,113,0.14)", bd: "rgba(248,113,113,0.45)" }
    : stuck
      ? { label: "Stuck", fg: "#fb923c", bg: "rgba(251,146,60,0.14)", bd: "rgba(251,146,60,0.45)" }
    : installed
      ? { label: isRecert ? "Re-Certified" : "Complete", fg: "#4ade80", bg: "rgba(74,222,128,0.14)", bd: "rgba(74,222,128,0.45)" }
      : isRecert
        ? { label: "Re-Certing", fg: "#c084fc", bg: "rgba(192,132,252,0.14)", bd: "rgba(192,132,252,0.45)" }
        : { label: "Installing", fg: "#60a5fa", bg: "rgba(96,165,250,0.14)", bd: "rgba(96,165,250,0.45)" };

  // Prefer per-attempt install_started_at (reset on restart) over project deploy start.
  const timerBase = installStartedAt ?? deployStartedAt;
  const elapsedSecs =
    installElapsed != null
      ? installElapsed
      : timerBase != null
        ? Math.max(0, Math.floor(Date.now() / 1000 - timerBase))
        : null;
  const elapsed = elapsedSecs == null ? null : fmtElapsed(elapsedSecs);

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

  const handleCancelInstall = async () => {
    if (!projectId || !target || cancelling || terminal) return;
    if (!confirm(`Cancel the ${target.name} cluster install? This cannot be undone.`)) {
      return;
    }
    setCancelling(true);
    cancellingRef.current = true;
    setLog(
      (prev) =>
        `${prev}${prev.endsWith("\n") || !prev ? "" : "\n"}[${target.clusterKey}] install cancel requested — log frozen pending teardown\n`,
    );
    try {
      const r = await fetch(
        `/api/v1/projects/${projectId}/ocp/cancel-install?cluster=${encodeURIComponent(target.clusterKey)}`,
        { method: "POST" },
      );
      if (!r.ok) {
        const data = await r.json().catch(() => ({}));
        alert(typeof data.detail === "string" ? data.detail : "Failed to cancel install");
        cancellingRef.current = false;
        setCancelling(false);
        return;
      }
      setClusterStatus("error");
      if (installElapsed == null && timerBase != null) {
        setInstallElapsed(Math.max(0, Math.floor(Date.now() / 1000 - timerBase)));
      }
    } catch {
      alert("Failed to cancel install");
      cancellingRef.current = false;
      setCancelling(false);
    }
  };

  const handleRestartInstall = async () => {
    if (!projectId || !target || restarting) return;
    setRestarting(true);
    setLoading(true);
    try {
      const r = await fetch(
        `/api/v1/projects/${projectId}/ocp/restart-install?cluster=${encodeURIComponent(target.clusterKey)}`,
        { method: "POST" },
      );
      if (!r.ok) {
        const data = await r.json().catch(() => ({}));
        alert(typeof data.detail === "string" ? data.detail : "Failed to restart install");
        return;
      }
      const data = await r.json();
      const startedAt =
        typeof data.install_started_at === "number"
          ? data.install_started_at
          : Math.floor(Date.now() / 1000);
      restartGuardRef.current = Date.now();
      setLog("");
      setAccess(null);
      setClusterStatus("monitoring");
      setInstallStartedAt(startedAt);
      setInstallElapsed(null);
      setControlPlaneUsableElapsed(null);
      // Pull fresh (empty) log + timing immediately instead of waiting for the poll interval.
      try {
        const lr = await fetch(
          `/api/v1/projects/${projectId}/ocp/install-log?cluster=${encodeURIComponent(target.clusterKey)}`,
        );
        if (lr.ok) {
          const fresh = await lr.json();
          setLog(fresh.output || "");
          if (typeof fresh.install_started_at === "number") {
            setInstallStartedAt(fresh.install_started_at);
          }
          if (typeof fresh.cluster_status === "string") {
            setClusterStatus(fresh.cluster_status);
          }
        }
      } catch {
        /* poll will catch up */
      }
    } catch {
      alert("Failed to restart install");
    } finally {
      setRestarting(false);
      setLoading(false);
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
          <h3 style={{ margin: 0, display: "flex", alignItems: "center", gap: 10 }}>
            <span>☸ {target.name} — Status &amp; Log</span>
            <span
              style={{
                fontSize: 12,
                fontWeight: 700,
                letterSpacing: 0.4,
                textTransform: "uppercase",
                padding: "3px 10px",
                borderRadius: 999,
                color: statusBadge.fg,
                background: statusBadge.bg,
                border: `1px solid ${statusBadge.bd}`,
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
              }}
            >
              {!terminal && (
                <span
                  style={{
                    width: 7,
                    height: 7,
                    borderRadius: "50%",
                    background: statusBadge.fg,
                    animation: "pulse 1.4s ease-in-out infinite",
                  }}
                />
              )}
              {statusBadge.label}
            </span>
            {elapsed != null && (
              <span style={{ fontSize: 12, fontWeight: 400, color: "var(--troshka-text-dim, #94a3b8)" }}>
                · {elapsed}
              </span>
            )}
          </h3>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            {!terminal && !isRecert && (
              <button
                onClick={handleCancelInstall}
                disabled={cancelling || restarting}
                style={{
                  background: "rgba(248,113,113,0.14)",
                  border: "1px solid rgba(248,113,113,0.45)",
                  color: "#f87171",
                  cursor: cancelling || restarting ? "wait" : "pointer",
                  fontSize: 11,
                  padding: "4px 10px",
                  borderRadius: 4,
                  fontWeight: 600,
                }}
              >
                {cancelling ? "Cancelling…" : "Cancel build"}
              </button>
            )}
            {(failed || stuck) && !isRecert && (
              <button
                onClick={handleRestartInstall}
                disabled={restarting || cancelling}
                style={{
                  background: "rgba(248,113,113,0.14)",
                  border: "1px solid rgba(248,113,113,0.45)",
                  color: "#f87171",
                  cursor: restarting ? "wait" : "pointer",
                  fontSize: 11,
                  padding: "4px 10px",
                  borderRadius: 4,
                  fontWeight: 600,
                }}
              >
                {restarting ? "Restarting…" : "Restart install"}
              </button>
            )}
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
        {controlPlaneUsableElapsed != null &&
          new RegExp(
            `\\[${target.clusterKey.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\] control-plane-usable`,
            "i",
          ).test(log) && (
          <div
            style={{
              fontSize: 11,
              color: "var(--troshka-text-dim, #94a3b8)",
              marginBottom: 8,
              paddingLeft: 4,
            }}
          >
            minimal control-plane-usable reached at {fmtElapsed(controlPlaneUsableElapsed)}
          </div>
        )}
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
                needed. Each stage: ✓ done, ⟳ active, ✗ failed, ○ pending. */}
            <div style={{ fontSize: 11, lineHeight: 1.9 }}>
              {stages.map((s) => (
                <React.Fragment key={s.label}>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <span style={{ width: 14, flexShrink: 0, textAlign: "center" }}>
                    {s.state === "done" ? (
                      <span style={{ color: "#4ade80" }}>✓</span>
                    ) : s.state === "failed" ? (
                      <span style={{ color: "#f87171" }}>✗</span>
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
                          : s.state === "failed"
                            ? "#f87171"
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
                {(s.label === "Worker nodes joined" ||
                  s.label === "Worker nodes converged") &&
                  s.state === "active" &&
                  workerReady &&
                  workerReady.ready < workerReady.expected && (
                  <div style={{ fontSize: 10, marginTop: 2, marginBottom: 2, lineHeight: 1.8, paddingLeft: 22 }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 6, color: "#22d3ee" }}>
                      <span
                        className="project-btn-spinner"
                        style={{ width: 8, height: 8, display: "inline-block", verticalAlign: "middle" }}
                      />
                      {workerReady.ready}/{workerReady.expected} Ready
                    </div>
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
                {restarting
                  ? "Restarting install…"
                  : loading
                    ? "Loading install log…"
                    : "No install log yet."}
              </span>
            )}
          </pre>
        </div>
      </div>
    </div>
  );
}
