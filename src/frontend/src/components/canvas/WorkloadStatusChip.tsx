"use client";

import React from "react";

interface Props {
  headline: string;
  detail: string;
  onClick: () => void;
}

/** Compact action-bar chip for an in-flight workload run. */
export default function WorkloadStatusChip({ headline, detail, onClick }: Props) {
  const title = `${headline} — ${detail}`;
  return (
    <button
      type="button"
      className="project-publish-btn workload-status-chip"
      onClick={onClick}
      title={title}
      style={{
        opacity: 0.95,
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        whiteSpace: "nowrap",
      }}
    >
      <span className="project-btn-spinner" style={{ width: 10, height: 10, flexShrink: 0 }} />
      <span style={{ fontSize: 12 }}>
        {headline}
        <span style={{ opacity: 0.85 }}> · {detail}</span>
      </span>
    </button>
  );
}
