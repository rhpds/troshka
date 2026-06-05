"use client";

import React, { useEffect, useState } from "react";
import {
  Button,
  Card,
  CardBody,
  PageSection,
  Title,
  Alert,
} from "@patternfly/react-core";

interface ApiKey {
  id: string;
  name: string;
  key_prefix: string;
  key?: string;
  is_active: boolean;
  last_used_at: string | null;
  expires_at: string | null;
  created_at: string;
}

export default function SettingsPage() {
  const [keys, setKeys] = useState<ApiKey[]>([]);
  const [newKeyName, setNewKeyName] = useState("");
  const [expiresDays, setExpiresDays] = useState("");
  const [newKey, setNewKey] = useState<string | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    fetch("/api/v1/api-keys/")
      .then((r) => r.json())
      .then((data) => setKeys(Array.isArray(data) ? data : []))
      .catch(() => {});
  }, []);

  const createKey = async () => {
    if (!newKeyName.trim()) {
      setError("Name is required");
      return;
    }
    setError("");
    setNewKey(null);

    const body: Record<string, unknown> = { name: newKeyName.trim() };
    if (expiresDays) body.expires_days = parseInt(expiresDays);

    const resp = await fetch("/api/v1/api-keys/", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

    if (!resp.ok) {
      setError("Failed to create API key");
      return;
    }

    const data = await resp.json();
    setNewKey(data.key);
    setKeys([data, ...keys]);
    setNewKeyName("");
    setExpiresDays("");
  };

  const revokeKey = async (id: string) => {
    if (!window.confirm("Revoke this API key? This cannot be undone.")) return;
    await fetch(`/api/v1/api-keys/${id}`, { method: "DELETE" });
    setKeys(keys.filter((k) => k.id !== id));
  };

  return (
    <>
      <PageSection>
        <Title headingLevel="h1">Settings</Title>
      </PageSection>
      <PageSection>
        <Title headingLevel="h2" size="lg" style={{ marginBottom: 16 }}>API Keys</Title>
        <p style={{ fontSize: 13, opacity: 0.7, marginBottom: 16 }}>
          API keys authenticate external tools and scripts. Keys use the format <code>trk_...</code> and are passed as <code>Authorization: Bearer trk_...</code>
        </p>

        <Card style={{ marginBottom: 16 }}>
          <CardBody>
            <div style={{ display: "flex", gap: 8, alignItems: "end" }}>
              <div style={{ flex: 1 }}>
                <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Name</label>
                <input
                  style={{ width: "100%", padding: "6px 10px", borderRadius: 6, border: "1px solid var(--pf-t--global--border--color--default)", background: "var(--pf-t--global--background--color--primary--default)", color: "var(--pf-t--global--text--color--regular)", fontSize: 13 }}
                  value={newKeyName}
                  onChange={(e) => setNewKeyName(e.target.value)}
                  placeholder="e.g. ansible-collection, ci-pipeline"
                />
              </div>
              <div style={{ flex: "0 0 120px" }}>
                <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Expires</label>
                <select
                  style={{ width: "100%", padding: "6px 10px", borderRadius: 6, border: "1px solid var(--pf-t--global--border--color--default)", background: "var(--pf-t--global--background--color--primary--default)", color: "var(--pf-t--global--text--color--regular)", fontSize: 13 }}
                  value={expiresDays}
                  onChange={(e) => setExpiresDays(e.target.value)}
                >
                  <option value="">Never</option>
                  <option value="30">30 days</option>
                  <option value="90">90 days</option>
                  <option value="365">1 year</option>
                </select>
              </div>
              <Button variant="primary" onClick={createKey}>Create Key</Button>
            </div>
            {error && <Alert variant="danger" title={error} style={{ marginTop: 8 }} />}
          </CardBody>
        </Card>

        {newKey && (
          <Alert variant="success" title="API key created — copy it now, it won't be shown again" style={{ marginBottom: 16 }}>
            <code style={{ fontSize: 14, fontFamily: "monospace", wordBreak: "break-all", display: "block", padding: "8px", background: "rgba(0,0,0,0.2)", borderRadius: 4, marginTop: 8 }}>
              {newKey}
            </code>
            <Button variant="secondary" style={{ marginTop: 8 }} onClick={() => { navigator.clipboard.writeText(newKey); }}>
              Copy to Clipboard
            </Button>
          </Alert>
        )}

        {keys.length === 0 && !newKey && (
          <p style={{ opacity: 0.6 }}>No API keys created yet.</p>
        )}

        {keys.map((k) => (
          <Card key={k.id} style={{ marginBottom: 8 }}>
            <CardBody style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <div>
                <strong>{k.name}</strong>
                <span style={{ marginLeft: 8, fontSize: 12, opacity: 0.6, fontFamily: "monospace" }}>{k.key_prefix}...</span>
                <div style={{ fontSize: 11, opacity: 0.5, marginTop: 2 }}>
                  Created {new Date(k.created_at).toLocaleDateString()}
                  {k.last_used_at && ` · Last used ${new Date(k.last_used_at).toLocaleDateString()}`}
                  {k.expires_at && ` · Expires ${new Date(k.expires_at).toLocaleDateString()}`}
                </div>
              </div>
              <Button variant="danger" onClick={() => revokeKey(k.id)}>Revoke</Button>
            </CardBody>
          </Card>
        ))}
      </PageSection>
      <PageSection>
        <SshKeysSection />
      </PageSection>
    </>
  );
}

function SshKeysSection() {
  const [keys, setSshKeys] = useState<Array<{ id: number; name: string; public_key: string; created_at: string }>>([]);
  const [newName, setNewName] = useState("");
  const [newKey, setNewKey] = useState("");
  const [sshError, setSshError] = useState("");

  const loadKeys = () => {
    fetch("/api/v1/auth/ssh-keys")
      .then((r) => r.ok ? r.json() : [])
      .then((data) => setSshKeys(Array.isArray(data) ? data : []))
      .catch(() => {});
  };

  useEffect(() => { loadKeys(); }, []);

  const addKey = async () => {
    if (!newName.trim() || !newKey.trim()) { setSshError("Name and key are required"); return; }
    setSshError("");
    const resp = await fetch("/api/v1/auth/ssh-keys", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: newName, public_key: newKey }),
    });
    if (resp.ok) {
      setNewName(""); setNewKey("");
      loadKeys();
    } else {
      const data = await resp.json();
      setSshError(data.detail || "Failed to add key");
    }
  };

  const deleteKey = async (id: number) => {
    await fetch(`/api/v1/auth/ssh-keys/${id}`, { method: "DELETE" });
    loadKeys();
  };

  const inputStyle = { width: "100%", padding: "6px 10px", borderRadius: 6, border: "1px solid var(--pf-t--global--border--color--default)", background: "var(--pf-t--global--background--color--primary--default)", color: "var(--pf-t--global--text--color--regular)", fontSize: 13 };

  return (
    <>
      <Title headingLevel="h2" size="lg" style={{ marginBottom: 16 }}>SSH Public Keys</Title>
      <p style={{ fontSize: 13, opacity: 0.7, marginBottom: 16 }}>
        SSH keys are injected into VMs via cloud-init. Add your public keys here, then select them per VM.
      </p>

      <Card style={{ marginBottom: 16 }}>
        <CardBody>
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <div>
              <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Name</label>
              <input style={inputStyle} value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="e.g. Work Laptop, CI Key" />
            </div>
            <div>
              <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Public Key</label>
              <textarea style={{ ...inputStyle, minHeight: 60, fontFamily: "monospace", fontSize: 11 }} value={newKey} onChange={(e) => setNewKey(e.target.value)} placeholder="ssh-ed25519 AAAA... or ssh-rsa AAAA..." />
            </div>
            <Button variant="primary" onClick={addKey} style={{ alignSelf: "flex-start" }}>Add Key</Button>
            {sshError && <Alert variant="danger" title={sshError} />}
          </div>
        </CardBody>
      </Card>

      {keys.length === 0 && <p style={{ opacity: 0.6 }}>No SSH keys added yet.</p>}

      {keys.map((k) => (
        <Card key={k.id} style={{ marginBottom: 8 }}>
          <CardBody style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <div style={{ flex: 1 }}>
              <strong>{k.name}</strong>
              <div style={{ fontSize: 11, opacity: 0.6, fontFamily: "monospace", marginTop: 2, wordBreak: "break-all" }}>
                {k.public_key.length > 80 ? k.public_key.slice(0, 80) + "..." : k.public_key}
              </div>
            </div>
            <Button variant="danger" onClick={() => deleteKey(k.id)}>Delete</Button>
          </CardBody>
        </Card>
      ))}
    </>
  );
}
