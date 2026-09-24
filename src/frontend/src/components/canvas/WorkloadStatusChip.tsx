"use client";

import React from "react";

interface Props {
  label: string;
  onClick: () => void;
}

/** Compact action-bar chip for an in-flight workload run. */
export default function WorkloadStatusChip({ label, onClick }: Props) {
  return (
    <button
      type="button"
      className="project-publish-btn"
      onClick={onClick}
      title="Open live workload log"
      style={{
        opacity: 0.95,
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        maxWidth: 320,
      }}
    >
      <span className="project-btn-spinner" style={{ width: 10, height: 10, flexShrink: 0 }} />
      <span
        style={{
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
          fontSize: 12,
        }}
      >
        {label}
      </span>
    </button>
  );
}
