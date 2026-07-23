"use client";

import React, { useState, useMemo, useEffect } from "react";

interface BulkDeployModalProps {
  patternId: string;
  onClose: () => void;
  onDeployed: (count: number) => void;
}

export default function BulkDeployModal({ patternId, onClose, onDeployed }: BulkDeployModalProps) {
  const [count, setCount] = useState<number | "">(5);
  const [nameTemplate, setNameTemplate] = useState("lab-{n}");
  const [autoDeploy, setAutoDeploy] = useState(false);
  const [deploying, setDeploying] = useState(false);
  const [error, setError] = useState("");
  const [guidTemplate, setGuidTemplate] = useState("");
  const [domain, setDomain] = useState("");
  const [dnsProviderId, setDnsProviderId] = useState("");
  const [dnsProviders, setDnsProviders] = useState<Array<{id: string; name: string}>>([]);

  useEffect(() => {
    fetch("/api/v1/dns-providers")
      .then(r => r.ok ? r.json() : [])
      .then(data => setDnsProviders(Array.isArray(data) ? data : []))
      .catch(() => {});
  }, []);

  const inputStyle = {
    width: "100%",
    padding: "6px 10px",
    borderRadius: 6,
    border: "1px solid var(--pf-t--global--border--color--default)",
    background: "var(--pf-t--global--background--color--primary--default)",
    color: "var(--pf-t--global--text--color--regular)",
    fontSize: 13,
  };

  const previewNames = useMemo(() => {
    const names: string[] = [];
    const c = count || 0;
    const limit = Math.min(c, 3);
    for (let i = 1; i <= limit; i++) {
      names.push(nameTemplate.replace(/\{n\}/g, String(i)));
    }
    if (c > 3) names.push("...");
    return names;
  }, [count, nameTemplate]);

  const handleDeploy = async () => {
    if (!count || count < 1 || count > 500) { setError("Count must be between 1 and 500"); return; }
    setDeploying(true);
    setError("");
    try {
      const resp = await fetch(`/api/v1/patterns/${patternId}/bulk-deploy`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          count,
          name_template: nameTemplate,
          auto_deploy: autoDeploy,
          ...(guidTemplate ? { guid_template: guidTemplate } : {}),
          ...(domain ? { domain } : {}),
          ...(dnsProviderId ? { dns_provider_id: dnsProviderId } : {}),
        }),
      });
      if (resp.ok) {
        const data = await resp.json();
        onDeployed(data.created || count);
      } else {
        const err = await resp.json().catch(() => ({ detail: "Bulk deploy failed" }));
        setError(err.detail || "Bulk deploy failed");
      }
    } catch {
      setError("Failed to connect to server");
    }
    setDeploying(false);
  };

  return (
    <div style={{
      position: "fixed", inset: 0, zIndex: 10000,
      display: "flex", alignItems: "center", justifyContent: "center",
      background: "rgba(0,0,0,0.6)",
    }} onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div style={{
        background: "var(--pf-t--global--background--color--primary--default)",
        borderRadius: 12, padding: 24, width: 460, maxWidth: "90vw",
        boxShadow: "0 8px 32px rgba(0,0,0,0.5)",
        border: "1px solid var(--pf-t--global--border--color--default)",
      }}>
        <h2 style={{ marginTop: 0, marginBottom: 16 }}>Bulk Deploy Pattern</h2>

        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <div>
            <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Number of Projects</label>
            <input
              style={inputStyle}
              type="number"
              min={1}
              max={500}
              value={count}
              onChange={(e) => {
                const v = e.target.value;
                if (v === "") { setCount(""); return; }
                const n = parseInt(v);
                if (!isNaN(n)) setCount(Math.min(500, n));
              }}
              onBlur={() => { if (count === "" || count < 1) setCount(1); }}
            />
          </div>
          <div>
            <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>
              Naming Template
              <span style={{ opacity: 0.5, marginLeft: 8 }}>{"{n}"} = sequence number</span>
            </label>
            <input
              style={inputStyle}
              value={nameTemplate}
              onChange={(e) => setNameTemplate(e.target.value)}
              placeholder="lab-{n}"
            />
          </div>
          <div>
            <label style={{ fontSize: 12, display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
              <input
                type="checkbox"
                checked={autoDeploy}
                onChange={(e) => setAutoDeploy(e.target.checked)}
              />
              Auto-deploy after creation
            </label>
          </div>

          <div style={{ borderTop: "1px solid var(--pf-t--global--border--color--default)", paddingTop: 12 }}>
            <div style={{ fontSize: 11, color: "var(--pf-t--global--text--color--subtle)", marginBottom: 8 }}>DNS Integration (optional)</div>
            <div style={{ display: "flex", gap: 8, marginBottom: 8 }}>
              <div style={{ flex: 1 }}>
                <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>
                  GUID Template
                  <span style={{ opacity: 0.5, marginLeft: 8 }}>{"{n}"} = seq</span>
                </label>
                <input style={inputStyle} value={guidTemplate} onChange={(e) => setGuidTemplate(e.target.value)} placeholder="lab-{n}" />
              </div>
              <div style={{ flex: 2 }}>
                <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Domain</label>
                <input style={inputStyle} value={domain} onChange={(e) => setDomain(e.target.value)} placeholder="sandbox.example.com" />
              </div>
            </div>
            {dnsProviders.length > 0 && (
              <div>
                <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>DNS Provider</label>
                <select style={inputStyle} value={dnsProviderId} onChange={(e) => setDnsProviderId(e.target.value)}>
                  <option value="">None</option>
                  {dnsProviders.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
                </select>
              </div>
            )}
          </div>

          <div style={{
            padding: "8px 12px", borderRadius: 6,
            background: "rgba(148,163,184,0.08)", border: "1px solid rgba(148,163,184,0.15)",
            fontSize: 12, opacity: 0.8,
          }}>
            <div style={{ marginBottom: 4, fontWeight: 500 }}>Preview:</div>
            {previewNames.map((name, i) => (
              <div key={i} style={{ paddingLeft: 8 }}>{name}</div>
            ))}
            {!!count && count > 0 && <div style={{ marginTop: 4, opacity: 0.6 }}>{count} project{count !== 1 ? "s" : ""} total</div>}
          </div>

          {error && <div style={{ color: "#f87171", fontSize: 13 }}>{error}</div>}

          <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginTop: 4 }}>
            <button
              onClick={onClose}
              style={{ ...inputStyle, width: "auto", cursor: "pointer", padding: "6px 16px" }}
            >
              Cancel
            </button>
            <button
              onClick={handleDeploy}
              disabled={deploying}
              style={{
                ...inputStyle, width: "auto", cursor: deploying ? "wait" : "pointer",
                padding: "6px 16px", background: "rgba(74,222,128,0.15)",
                borderColor: "#4ade80", color: "#4ade80", opacity: deploying ? 0.6 : 1,
              }}
            >
              {deploying ? "Creating..." : `Create ${count} Project${count !== 1 ? "s" : ""}`}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
