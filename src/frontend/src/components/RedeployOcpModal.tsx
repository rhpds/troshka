"use client";

import type { OcpClusterStatus, OcpRedeployMode } from "@/lib/redeployOcp";

interface RedeployOcpModalProps {
  clusters: OcpClusterStatus[];
  allReady: boolean;
  onChoose: (mode: OcpRedeployMode) => void;
  onCancel: () => void;
}

export default function RedeployOcpModal({
  clusters,
  allReady,
  onChoose,
  onCancel,
}: RedeployOcpModalProps) {
  const statusLines = clusters
    .map((c) => `• ${c.name}: ${c.status || "unknown"}`)
    .join("\n");

  return (
    <div className="start-order-overlay" onClick={onCancel}>
      <div
        className="start-order-modal"
        style={{ maxWidth: 560 }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="start-order-header">
          <span>Redeploy OpenShift clusters</span>
          <button type="button" onClick={onCancel}>
            &#x2715;
          </button>
        </div>
        <div className="start-order-body" style={{ padding: 16, whiteSpace: "pre-wrap" }}>
          {allReady ? (
            <>
              You have OpenShift cluster(s) in a ready state:
              {"\n\n"}
              {statusLines}
              {"\n\n"}
              <strong>Rebuild</strong> — wipe all disks, clear ready state, and
              reinstall OpenShift from scratch.
              {"\n\n"}
              <strong>Re-cert</strong> — keep existing disks and re-cert in place
              to the best of Troshka&apos;s ability. If the clusters are healthy,
              prefer <em>Save as Pattern</em> instead for a reliable clone.
            </>
          ) : (
            <>
              OpenShift cluster(s) are not all ready:
              {"\n\n"}
              {statusLines}
              {"\n\n"}
              Troshka will <strong>rebuild</strong> them from scratch (wipe all
              disks and reinstall). Re-cert is only available when every cluster
              is ready.
            </>
          )}
        </div>
        <div
          className="start-order-footer"
          style={{ display: "flex", gap: 8, justifyContent: "flex-end", flexWrap: "wrap" }}
        >
          <button type="button" className="start-order-btn" onClick={onCancel}>
            Cancel
          </button>
          {allReady && (
            <button
              type="button"
              className="start-order-btn save"
              onClick={() => onChoose("recert")}
            >
              Re-cert
            </button>
          )}
          <button
            type="button"
            className="start-order-btn delete"
            onClick={() => onChoose("rebuild")}
          >
            Rebuild
          </button>
        </div>
      </div>
    </div>
  );
}
