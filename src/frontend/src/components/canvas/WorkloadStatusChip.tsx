"use client";

import React from "react";

interface Props {
  headline: string;
  detail: string;
  /** inflight = spinner; failed = Retry for unfinished chain */
  variant?: "inflight" | "failed";
  onClick: () => void;
  onRetry?: () => void;
  retrying?: boolean;
}

/** Compact action-bar chip for an in-flight or failed chain workload. */
export default function WorkloadStatusChip({
  headline,
  detail,
  variant = "inflight",
  onClick,
  onRetry,
  retrying = false,
}: Props) {
  const failed = variant === "failed";
  const title = failed
    ? `${headline} — ${detail} (interrupted/failed — retry to continue the chain)`
    : `${headline} — ${detail}`;

  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 4,
      }}
    >
      <button
        type="button"
        className="project-publish-btn workload-status-chip"
        onClick={onClick}
        title={title}
        data-testid="workload-status-chip"
        style={{
          opacity: 0.95,
          display: "inline-flex",
          alignItems: "center",
          gap: 6,
          whiteSpace: "nowrap",
          ...(failed
            ? {
                borderColor: "rgba(248, 113, 113, 0.55)",
                color: "var(--pf-t--global--color--status--danger--default, #f87171)",
              }
            : {}),
        }}
      >
        {!failed && (
          <span className="project-btn-spinner" style={{ width: 10, height: 10, flexShrink: 0 }} />
        )}
        <span style={{ fontSize: 12 }}>
          {headline}
          <span style={{ opacity: 0.85 }}>
            {" "}
            · {failed ? "Interrupted" : detail}
          </span>
        </span>
      </button>
      {failed && onRetry && (
        <button
          type="button"
          className="project-publish-btn"
          data-testid="workload-chain-retry"
          onClick={(e) => {
            e.stopPropagation();
            onRetry();
          }}
          disabled={retrying}
          title="Retry this workload and continue the remaining unfinished roles"
          style={{
            opacity: retrying ? 0.6 : 0.95,
            background: "rgba(59, 130, 246, 0.25)",
            border: "1px solid rgba(59, 130, 246, 0.5)",
            whiteSpace: "nowrap",
            fontSize: 12,
          }}
        >
          {retrying ? "Retrying…" : "Retry"}
        </button>
      )}
    </span>
  );
}
