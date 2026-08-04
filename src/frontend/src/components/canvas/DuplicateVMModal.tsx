"use client";

import React, { useState } from "react";

interface Props {
  vmName: string;
  hasUuid: boolean;
  onConfirm: (cloneUuid: boolean) => void;
  onCancel: () => void;
}

export default function DuplicateVMModal({ vmName, hasUuid, onConfirm, onCancel }: Props) {
  const [cloneUuid, setCloneUuid] = useState(false);

  return (
    <div className="start-order-overlay" onClick={onCancel}>
      <div className="start-order-modal" style={{ maxWidth: 460 }} onClick={(e) => e.stopPropagation()}>
        <div className="start-order-header">
          <span>Duplicate VM</span>
          <button onClick={onCancel}>&#x2715;</button>
        </div>
        <div className="start-order-body" style={{ padding: 16 }}>
          <p style={{ fontSize: 13, marginBottom: 12 }}>
            Duplicating <strong>{vmName}</strong>
          </p>
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            <label style={{ display: "flex", alignItems: "flex-start", gap: 8, cursor: "pointer", padding: "8px 10px", borderRadius: 6, border: !cloneUuid ? "1px solid var(--troshka-accent)" : "1px solid var(--troshka-border)", background: !cloneUuid ? "rgba(56,189,248,0.06)" : "transparent", transition: "all 0.15s" }}>
              <input type="radio" name="uuid-mode" checked={!cloneUuid} onChange={() => setCloneUuid(false)} style={{ marginTop: 2 }} />
              <div>
                <div style={{ fontSize: 13, fontWeight: 600 }}>Generate new UUID</div>
                <div style={{ fontSize: 11, color: "var(--troshka-text-dim)", marginTop: 2 }}>
                  The duplicate will have a unique SMBIOS identity. Use this for most cases.
                </div>
              </div>
            </label>
            <label style={{ display: "flex", alignItems: "flex-start", gap: 8, cursor: "pointer", padding: "8px 10px", borderRadius: 6, border: cloneUuid ? "1px solid var(--troshka-accent)" : "1px solid var(--troshka-border)", background: cloneUuid ? "rgba(56,189,248,0.06)" : "transparent", transition: "all 0.15s" }}>
              <input type="radio" name="uuid-mode" checked={cloneUuid} onChange={() => setCloneUuid(true)} style={{ marginTop: 2 }} />
              <div>
                <div style={{ fontSize: 13, fontWeight: 600 }}>Clone UUID{!hasUuid && <span style={{ fontSize: 11, fontWeight: 400, opacity: 0.5 }}> (none set)</span>}</div>
                <div style={{ fontSize: 11, color: "var(--troshka-text-dim)", marginTop: 2 }}>
                  The duplicate will share the same SMBIOS UUID. Use for migration testing or BMC identity cloning.
                </div>
              </div>
            </label>
          </div>
        </div>
        <div className="start-order-footer">
          <button className="start-order-btn cancel" onClick={onCancel}>Cancel</button>
          <button className="start-order-btn save" onClick={() => onConfirm(cloneUuid)}>
            Duplicate
          </button>
        </div>
      </div>
    </div>
  );
}
