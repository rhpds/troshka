"use client";

import React, { useEffect, useState } from "react";
import { Button } from "@patternfly/react-core";

interface SharePatternModalProps {
  patternId: string;
  patternName: string;
  visibility: string;
  /** Owner-only per-user sharing; admins who aren't the owner get the public toggle only. */
  isOwner: boolean;
  onClose: () => void;
  onChanged: () => void;
}

export default function SharePatternModal({
  patternId,
  patternName,
  visibility,
  isOwner,
  onClose,
  onChanged,
}: SharePatternModalProps) {
  const [isPublic, setIsPublic] = useState(visibility === "public");
  const [savingPublic, setSavingPublic] = useState(false);
  const [shares, setShares] = useState<string[]>([]);
  const [email, setEmail] = useState("");
  const [busyEmail, setBusyEmail] = useState(false);
  const [error, setError] = useState("");

  const loadShares = () => {
    if (!isOwner) return;
    fetch(`/api/v1/patterns/${patternId}/shares`)
      .then((r) => (r.ok ? r.json() : { shared_with: [] }))
      .then((d) => setShares(Array.isArray(d.shared_with) ? d.shared_with : []))
      .catch(() => {});
  };

  useEffect(loadShares, [patternId, isOwner]);

  const inputStyle = {
    width: "100%",
    padding: "6px 10px",
    borderRadius: 6,
    border: "1px solid var(--pf-t--global--border--color--default)",
    background: "var(--pf-t--global--background--color--primary--default)",
    color: "var(--pf-t--global--text--color--regular)",
    fontSize: 13,
  };

  const togglePublic = async (next: boolean) => {
    setSavingPublic(true);
    setError("");
    setIsPublic(next);
    try {
      const resp = await fetch(`/api/v1/patterns/${patternId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ visibility: next ? "public" : "private" }),
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({ detail: "Update failed" }));
        setError(err.detail || "Update failed");
        setIsPublic(!next);
      } else {
        onChanged();
      }
    } catch {
      setError("Failed to connect to server");
      setIsPublic(!next);
    }
    setSavingPublic(false);
  };

  const addShare = async () => {
    const target = email.trim();
    if (!target) return;
    setBusyEmail(true);
    setError("");
    try {
      const resp = await fetch(`/api/v1/patterns/${patternId}/share`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_email: target }),
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({ detail: "Share failed" }));
        setError(err.detail || "Share failed");
      } else {
        setEmail("");
        loadShares();
        onChanged();
      }
    } catch {
      setError("Failed to connect to server");
    }
    setBusyEmail(false);
  };

  const removeShare = async (target: string) => {
    setBusyEmail(true);
    setError("");
    try {
      const resp = await fetch(
        `/api/v1/patterns/${patternId}/share/${encodeURIComponent(target)}`,
        { method: "DELETE" },
      );
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({ detail: "Remove failed" }));
        setError(err.detail || "Remove failed");
      } else {
        loadShares();
        onChanged();
      }
    } catch {
      setError("Failed to connect to server");
    }
    setBusyEmail(false);
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
      <div
        style={{
          background: "var(--pf-t--global--background--color--primary--default)",
          borderRadius: 12,
          padding: 24,
          width: 460,
          maxWidth: "90vw",
          boxShadow: "0 8px 32px rgba(0,0,0,0.5)",
          border: "1px solid var(--pf-t--global--border--color--default)",
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <h2 style={{ marginTop: 0, marginBottom: 4 }}>Share Pattern</h2>
        <p style={{ fontSize: 13, opacity: 0.7, marginBottom: 16 }}>{patternName}</p>

        {/* Public toggle */}
        <label
          style={{
            fontSize: 13,
            display: "flex",
            alignItems: "center",
            gap: 8,
            cursor: savingPublic ? "wait" : "pointer",
          }}
        >
          <input
            type="checkbox"
            checked={isPublic}
            disabled={savingPublic}
            onChange={(e) => togglePublic(e.target.checked)}
          />
          <span>
            <strong>Visible to everyone</strong>
            <span style={{ display: "block", fontSize: 12, opacity: 0.6 }}>
              Any user can see and deploy this pattern.
            </span>
          </span>
        </label>

        {/* Per-user sharing (owner only) */}
        {isOwner && (
          <div
            style={{
              borderTop: "1px solid var(--pf-t--global--border--color--default)",
              paddingTop: 16,
              marginTop: 16,
            }}
          >
            <div style={{ fontSize: 12, opacity: 0.7, marginBottom: 8 }}>
              Share with specific users
            </div>
            <div style={{ display: "flex", gap: 8, marginBottom: 8 }}>
              <input
                style={inputStyle}
                type="email"
                value={email}
                placeholder="user@example.com"
                disabled={busyEmail}
                onChange={(e) => setEmail(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") addShare();
                }}
              />
              <Button
                variant="secondary"
                size="sm"
                isDisabled={busyEmail || !email.trim()}
                onClick={addShare}
              >
                Add
              </Button>
            </div>
            {shares.length === 0 ? (
              <div style={{ fontSize: 12, opacity: 0.5 }}>Not shared with anyone yet.</div>
            ) : (
              <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                {shares.map((s) => (
                  <div
                    key={s}
                    style={{
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "space-between",
                      fontSize: 13,
                      padding: "4px 8px",
                      borderRadius: 6,
                      background: "var(--pf-t--global--background--color--secondary--default)",
                    }}
                  >
                    <span>{s}</span>
                    <button
                      onClick={() => removeShare(s)}
                      disabled={busyEmail}
                      title="Remove"
                      style={{
                        border: "none",
                        background: "transparent",
                        color: "var(--pf-t--global--text--color--subtle)",
                        cursor: busyEmail ? "not-allowed" : "pointer",
                        fontSize: 16,
                        lineHeight: 1,
                      }}
                    >
                      &times;
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {error && (
          <div style={{ color: "#f87171", fontSize: 12, marginTop: 12 }}>{error}</div>
        )}

        <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 20 }}>
          <Button variant="primary" onClick={onClose}>
            Done
          </Button>
        </div>
      </div>
    </div>
  );
}
