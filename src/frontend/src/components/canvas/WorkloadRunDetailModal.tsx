"use client";

import React, { useEffect, useRef, useState } from "react";

const TERMINAL = new Set(["succeeded", "error", "timeout"]);

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
  wsNudge?: unknown;
};

export function WorkloadRunLogPanel({
  runId,
  standalone = false,
  onClose,
  onPopOut,
  wsNudge,
}: WorkloadRunLogPanelProps) {
  const [log, setLog] = useState("");
  const [status, setStatus] = useState("");
  const [roleFqcn, setRoleFqcn] = useState<string | null>(null);
  const [catalogItem, setCatalogItem] = useState<string | null>(null);
  const [createdAt, setCreatedAt] = useState<string | null>(null);
  const [startedAt, setStartedAt] = useState<string | null>(null);
  const [endedAt, setEndedAt] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const preRef = useRef<HTMLPreElement>(null);
  const statusRef = useRef("");

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    const fetchLog = async () => {
      try {
        const r = await fetch(`/api/v1/workloads/${runId}/log`);
        if (!r.ok || cancelled) return;
        const data = await r.json();
        if (!cancelled) {
          setLog(data.log || "");
          setStatus(data.status || "");
          statusRef.current = data.status || "";
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
    document.title = status ? `${title} — ${status}` : title;
  }, [standalone, roleFqcn, catalogItem, status]);

  const title = shortWorkloadName(roleFqcn, catalogItem);
  const whenLabel = endedAt
    ? `${formatWhen(startedAt || createdAt)} → ${formatWhen(endedAt)}`
    : formatWhen(startedAt || createdAt);

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
            <span style={{ textTransform: "capitalize" }}>{status || "pending"}</span>
            {" · "}
            {whenLabel}
          </div>
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
  wsNudge?: unknown;
}

export default function WorkloadRunDetailModal({ runId, onClose, wsNudge }: ModalProps) {
  const handlePopOut = () => {
    openWorkloadRunLogWindow(runId);
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
          runId={runId}
          wsNudge={wsNudge}
          onClose={onClose}
          onPopOut={handlePopOut}
        />
      </div>
    </div>
  );
}
