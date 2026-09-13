"use client";

import React, { useEffect, useRef, useState } from "react";

interface Props {
  runId: string;
  onClose: () => void;
  wsNudge?: unknown;
}

const TERMINAL = new Set(["succeeded", "error", "timeout"]);

export default function WorkloadRunDetailModal({ runId, onClose, wsNudge }: Props) {
  const [log, setLog] = useState("");
  const [status, setStatus] = useState("");
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
        <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 12 }}>
          <h2 style={{ margin: 0 }}>Workload Run — {status || "pending"}</h2>
          <button onClick={onClose}>✕</button>
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
          {log || <span style={{ opacity: 0.5 }}>{loading ? "Loading run log…" : "No log yet."}</span>}
        </pre>
      </div>
    </div>
  );
}
