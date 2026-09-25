"use client";

import React, { useEffect, useRef, useState } from "react";

const TERMINAL = new Set(["succeeded", "error", "timeout", "cancelled"]);
const RETRYABLE = new Set(["error", "timeout", "cancelled"]);
const CANCELLABLE = new Set(["pending", "queued", "running"]);

export function openWorkloadRunLogWindow(runId: string, titleHint?: string): void {
  const winName = `workloadlog_${runId.replace(/-/g, "")}`;
  const q = new URLSearchParams({ run: runId });
  if (titleHint) q.set("name", titleHint);
  window.open(
    `/console/workload-log?${q.toString()}`,
    winName,
    "width=1100,height=800,menubar=no,toolbar=no,location=no",
  );
}

function shortWorkloadName(roleFqcn: string | null, catalogItem: string | null): string {
  const raw = (roleFqcn || catalogItem || "").trim();
  if (!raw) return "Workload Run";
  const parts = raw.split(".");
  return parts[parts.length - 1] || raw;
}

function formatWhen(iso: string | null | undefined): string {
  if (!iso) return "—";
  return iso.slice(0, 19).replace("T", " ");
}

/** Human label: Interrupted (never started / worker lost) vs Failed vs raw status. */
export function formatWorkloadStatusLabel(
  status: string,
  startedAt: string | null | undefined,
  error: string | null | undefined,
): string {
  const s = (status || "pending").toLowerCase();
  if (s === "error") {
    const err = (error || "").toLowerCase();
    if (err.includes("interrupted") || err.includes("superseded")) {
      return "Interrupted";
    }
    if (!startedAt) {
      return "Interrupted";
    }
    return "Failed";
  }
  if (s === "cancelled") return "Cancelled";
  if (s === "timeout") return "Timed out";
  return s;
}

const headerBtnStyle: React.CSSProperties = {
  background: "rgba(255,255,255,0.08)",
  border: "1px solid rgba(255,255,255,0.15)",
  color: "var(--pf-t--global--text--color--regular)",
  cursor: "pointer",
  fontSize: 11,
  padding: "4px 10px",
  borderRadius: 4,
};

export type WorkloadRunLogPanelProps = {
  runId: string;
  standalone?: boolean;
  onClose?: () => void;
  onPopOut?: () => void;
  onRunIdChange?: (runId: string) => void;
  wsNudge?: unknown;
};

export function WorkloadRunLogPanel({
  runId: initialRunId,
  standalone = false,
  onClose,
  onPopOut,
  onRunIdChange,
  wsNudge,
}: WorkloadRunLogPanelProps) {
  const [runId, setRunId] = useState(initialRunId);
  const [log, setLog] = useState("");
  const [status, setStatus] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [roleFqcn, setRoleFqcn] = useState<string | null>(null);
  const [catalogItem, setCatalogItem] = useState<string | null>(null);
  const [createdAt, setCreatedAt] = useState<string | null>(null);
  const [startedAt, setStartedAt] = useState<string | null>(null);
  const [endedAt, setEndedAt] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [retryError, setRetryError] = useState<string | null>(null);
  const preRef = useRef<HTMLPreElement>(null);
  const statusRef = useRef("");

  useEffect(() => {
    setRunId(initialRunId);
  }, [initialRunId]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setRetryError(null);
    const fetchLog = async () => {
      try {
        const r = await fetch(`/api/v1/workloads/${runId}/log`);
        if (!r.ok || cancelled) return;
        const data = await r.json();
        if (!cancelled) {
          setLog(data.log || "");
          setStatus(data.status || "");
          statusRef.current = data.status || "";
          setError(data.error ?? null);
          setRoleFqcn(data.role_fqcn ?? null);
          setCatalogItem(data.catalog_item ?? null);
          setCreatedAt(data.created_at ?? null);
          setStartedAt(data.started_at ?? null);
          setEndedAt(data.ended_at ?? null);
        }
      } catch {
        /* transient — keep last log */
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    fetchLog();
    const timer = setInterval(() => {
      if (TERMINAL.has(statusRef.current)) {
        clearInterval(timer);
        return;
      }
      fetchLog();
    }, 3000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [runId, (wsNudge as { run_id?: string } | null | undefined)?.run_id === runId ? wsNudge : null]);

  useEffect(() => {
    if (preRef.current) preRef.current.scrollTop = preRef.current.scrollHeight;
  }, [log]);

  useEffect(() => {
    if (!standalone) return;
    const title = shortWorkloadName(roleFqcn, catalogItem);
    const label = formatWorkloadStatusLabel(status, startedAt, error);
    document.title = status ? `${title} — ${label}` : title;
  }, [standalone, roleFqcn, catalogItem, status, startedAt, error]);

  const handleRetry = async () => {
    setRetrying(true);
    setRetryError(null);
    try {
      const r = await fetch(`/api/v1/workloads/${runId}/retry`, { method: "POST" });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) {
        setRetryError(typeof data.detail === "string" ? data.detail : "Retry failed");
        return;
      }
      const newId = data.id as string;
      setLog("");
      setStatus(data.status || "pending");
      statusRef.current = data.status || "pending";
      setError(null);
      setStartedAt(null);
      setEndedAt(null);
      setRunId(newId);
      onRunIdChange?.(newId);
    } catch {
      setRetryError("Retry failed");
    } finally {
      setRetrying(false);
    }
  };

  const handleCancel = async () => {
    setCancelling(true);
    setRetryError(null);
    try {
      const r = await fetch(`/api/v1/workloads/${runId}/cancel`, { method: "POST" });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) {
        setRetryError(typeof data.detail === "string" ? data.detail : "Cancel failed");
        return;
      }
      setStatus(data.status || "cancelled");
      statusRef.current = data.status || "cancelled";
      setError("Cancelled by user");
      setEndedAt(new Date().toISOString());
      onRunIdChange?.(runId);
    } catch {
      setRetryError("Cancel failed");
    } finally {
      setCancelling(false);
    }
  };

  const title = shortWorkloadName(roleFqcn, catalogItem);
  const statusLabel = formatWorkloadStatusLabel(status, startedAt, error);
  const whenLabel = endedAt
    ? `${formatWhen(startedAt || createdAt)} → ${formatWhen(endedAt)}`
    : formatWhen(startedAt || createdAt);
  const canRetry = RETRYABLE.has(status);
  const canCancel = CANCELLABLE.has(status);

  return (
    <div
      style={{
        background: "var(--pf-t--global--background--color--primary--default)",
        borderRadius: standalone ? 0 : 12,
        padding: 24,
        width: standalone ? "100%" : "80vw",
        maxWidth: standalone ? "none" : 900,
        height: standalone ? "100vh" : undefined,
        maxHeight: standalone ? "none" : "80vh",
        boxShadow: standalone ? "none" : "0 8px 32px rgba(0,0,0,0.5)",
        border: standalone ? "none" : "1px solid var(--pf-t--global--border--color--default)",
        display: "flex",
        flexDirection: "column",
        boxSizing: "border-box",
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 12, gap: 12 }}>
        <div style={{ minWidth: 0 }}>
          <h2 style={{ margin: 0, fontSize: 18, wordBreak: "break-word" }}>{title}</h2>
          <div style={{ marginTop: 4, fontSize: 12, opacity: 0.75 }}>
            <span
              data-testid="workload-run-status"
              style={{
                textTransform: statusLabel === status ? "capitalize" : undefined,
                color:
                  status === "error" || status === "timeout" || status === "cancelled"
                    ? "var(--pf-t--global--color--status--danger--default, #f87171)"
                    : undefined,
              }}
            >
              {statusLabel || "pending"}
            </span>
            {" · "}
            {whenLabel}
          </div>
          {error && (
            <div
              data-testid="workload-run-error"
              style={{
                marginTop: 6,
                fontSize: 12,
                color: "var(--pf-t--global--color--status--danger--default, #f87171)",
                opacity: 0.95,
                wordBreak: "break-word",
              }}
            >
              {error}
            </div>
          )}
          {retryError && (
            <div style={{ marginTop: 4, fontSize: 12, color: "#f87171" }}>{retryError}</div>
          )}
          {(roleFqcn || catalogItem) && title !== (roleFqcn || catalogItem) && (
            <div
              data-testid="workload-run-fqcn"
              style={{ marginTop: 2, fontSize: 11, opacity: 0.55, wordBreak: "break-all" }}
            >
              {roleFqcn || catalogItem}
            </div>
          )}
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "flex-start", flexShrink: 0 }}>
          {canCancel && (
            <button
              data-testid="workload-run-cancel"
              onClick={handleCancel}
              disabled={cancelling}
              title="Stop this run and re-enable project actions"
              style={{
                ...headerBtnStyle,
                background: "rgba(220, 38, 38, 0.2)",
                border: "1px solid rgba(248, 113, 113, 0.5)",
                color: "var(--pf-t--global--color--status--danger--default, #f87171)",
                opacity: cancelling ? 0.6 : 1,
              }}
            >
              {cancelling ? "Cancelling…" : "Cancel"}
            </button>
          )}
          {canRetry && (
            <button
              data-testid="workload-run-retry"
              onClick={handleRetry}
              disabled={retrying}
              title="Start a new run with the same parameters"
              style={{
                ...headerBtnStyle,
                background: "rgba(59, 130, 246, 0.25)",
                border: "1px solid rgba(59, 130, 246, 0.5)",
                opacity: retrying ? 0.6 : 1,
              }}
            >
              {retrying ? "Retrying…" : "Retry"}
            </button>
          )}
          {onPopOut && (
            <button onClick={onPopOut} title="Open in a separate window" style={headerBtnStyle}>
              Pop out
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
              style={headerBtnStyle}
            >
              Copy All
            </button>
          )}
          {onClose && (
            <button
              onClick={onClose}
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
          )}
        </div>
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
          minHeight: 0,
        }}
      >
        {log || <span style={{ opacity: 0.5 }}>{loading ? "Loading run log…" : "No log yet."}</span>}
      </pre>
    </div>
  );
}

interface ModalProps {
  runId: string;
  onClose: () => void;
  onRunIdChange?: (runId: string) => void;
  wsNudge?: unknown;
}

export default function WorkloadRunDetailModal({
  runId,
  onClose,
  onRunIdChange,
  wsNudge,
}: ModalProps) {
  const [activeRunId, setActiveRunId] = useState(runId);
  useEffect(() => setActiveRunId(runId), [runId]);

  const handlePopOut = () => {
    openWorkloadRunLogWindow(activeRunId);
    onClose();
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
      onClick={onClose}
    >
      <div onClick={(e) => e.stopPropagation()}>
        <WorkloadRunLogPanel
          runId={activeRunId}
          wsNudge={wsNudge}
          onClose={onClose}
          onPopOut={handlePopOut}
          onRunIdChange={(id) => {
            setActiveRunId(id);
            onRunIdChange?.(id);
          }}
        />
      </div>
    </div>
  );
}
