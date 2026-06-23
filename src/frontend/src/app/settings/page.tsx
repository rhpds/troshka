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

  // Red Hat Offline Token
  const [rhTokenMasked, setRhTokenMasked] = useState("");
  const [hasRhToken, setHasRhToken] = useState(false);
  const [rhTokenInput, setRhTokenInput] = useState("");
  const [rhTokenSaving, setRhTokenSaving] = useState(false);
  const [rhTokenEdit, setRhTokenEdit] = useState(false);

  // OCP Pull Secret
  const [pullSecretMasked, setPullSecretMasked] = useState("");
  const [hasPullSecret, setHasPullSecret] = useState(false);
  const [pullSecretInput, setPullSecretInput] = useState("");
  const [pullSecretSaving, setPullSecretSaving] = useState(false);
  const [pullSecretEdit, setPullSecretEdit] = useState(false);

  // Registry Credentials
  const [registryCreds, setRegistryCreds] = useState<
    Array<{ id: string; name: string; registry: string; username: string }>
  >([]);
  const [showAddCred, setShowAddCred] = useState(false);
  const [editCredId, setEditCredId] = useState<string | null>(null);
  const [credForm, setCredForm] = useState({ name: "", registry: "", username: "", password: "" });

  useEffect(() => {
    fetch("/api/v1/api-keys/")
      .then((r) => r.json())
      .then((data) => setKeys(Array.isArray(data) ? data : []))
      .catch(() => {});
    fetch("/api/v1/auth/rh-offline-token")
      .then((r) => r.json())
      .then((data) => { setHasRhToken(data.has_token); setRhTokenMasked(data.masked || ""); })
      .catch(() => {});
    fetch("/api/v1/auth/ocp-pull-secret")
      .then((r) => r.json())
      .then((data) => { setHasPullSecret(data.has_secret); setPullSecretMasked(data.masked || ""); })
      .catch(() => {});
    fetch("/api/v1/auth/registry-credentials")
      .then((r) => r.json())
      .then((data) => setRegistryCreds(data))
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

  const fetchCreds = () => {
    fetch("/api/v1/auth/registry-credentials")
      .then((r) => r.json())
      .then((data) => setRegistryCreds(data))
      .catch(() => {});
  };

  const saveCred = async () => {
    const method = editCredId ? "PUT" : "POST";
    const url = editCredId
      ? `/api/v1/auth/registry-credentials/${editCredId}`
      : "/api/v1/auth/registry-credentials";
    const resp = await fetch(url, {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(credForm),
    });
    if (resp.ok) {
      setShowAddCred(false);
      setEditCredId(null);
      setCredForm({ name: "", registry: "", username: "", password: "" });
      fetchCreds();
    }
  };

  const deleteCred = async (id: string) => {
    await fetch(`/api/v1/auth/registry-credentials/${id}`, { method: "DELETE" });
    fetchCreds();
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
      <PageSection>
        <Title headingLevel="h2" style={{ marginBottom: 12 }}>Red Hat Offline Token</Title>
        <p style={{ fontSize: 13, opacity: 0.7, marginBottom: 12 }}>
          Required for building custom host images via Image Builder. Generate a token at{" "}
          <a href="https://access.redhat.com/management/api" target="_blank" rel="noreferrer" style={{ color: "#3b82f6" }}>access.redhat.com/management/api</a>.
        </p>
        {hasRhToken && !rhTokenEdit ? (
          <Card>
            <CardBody style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <div style={{ fontSize: 11, fontFamily: "monospace", opacity: 0.6, wordBreak: "break-all" }}>{rhTokenMasked}</div>
              <div style={{ display: "flex", gap: 8 }}>
                <Button variant="secondary" onClick={() => setRhTokenEdit(true)}>Replace</Button>
                <Button variant="danger" onClick={async () => { await fetch("/api/v1/auth/rh-offline-token", { method: "DELETE" }); setHasRhToken(false); setRhTokenMasked(""); }}>Delete</Button>
              </div>
            </CardBody>
          </Card>
        ) : (
          <Card>
            <CardBody>
              <input type="password" style={{ width: "100%", padding: "8px 10px", borderRadius: 6, fontSize: 12, fontFamily: "monospace", border: "1px solid var(--pf-t--global--border--color--default)", background: "var(--pf-t--global--background--color--primary--default)", color: "var(--pf-t--global--text--color--regular)" }} value={rhTokenInput} onChange={(e) => setRhTokenInput(e.target.value)} placeholder="eyJhbGci..." />
              <div style={{ display: "flex", gap: 8, marginTop: 8, justifyContent: "flex-end" }}>
                {rhTokenEdit && <Button variant="secondary" onClick={() => { setRhTokenEdit(false); setRhTokenInput(""); }}>Cancel</Button>}
                <Button variant="primary" isDisabled={!rhTokenInput.trim() || rhTokenSaving} onClick={async () => {
                  setRhTokenSaving(true);
                  const resp = await fetch("/api/v1/auth/rh-offline-token", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ offline_token: rhTokenInput }) });
                  if (resp.ok) { setHasRhToken(true); setRhTokenEdit(false); setRhTokenInput(""); const data = await fetch("/api/v1/auth/rh-offline-token").then(r => r.json()); setRhTokenMasked(data.masked || ""); }
                  else { const err = await resp.json().catch(() => ({ detail: "Save failed" })); alert(err.detail || "Save failed"); }
                  setRhTokenSaving(false);
                }}>{rhTokenSaving ? "Saving..." : "Save Token"}</Button>
              </div>
            </CardBody>
          </Card>
        )}
      </PageSection>
      <PageSection>
        <Title headingLevel="h2" style={{ marginBottom: 12 }}>OCP Pull Secret</Title>
        <p style={{ fontSize: 13, opacity: 0.7, marginBottom: 12 }}>
          Required for OpenShift installation. Get yours from{" "}
          <a href="https://console.redhat.com/openshift/install/pull-secret" target="_blank" rel="noreferrer" style={{ color: "#3b82f6" }}>console.redhat.com</a>.
        </p>
        {hasPullSecret && !pullSecretEdit ? (
          <Card>
            <CardBody style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <div style={{ fontSize: 11, fontFamily: "monospace", opacity: 0.6, wordBreak: "break-all" }}>{pullSecretMasked}</div>
              <div style={{ display: "flex", gap: 8 }}>
                <Button variant="secondary" onClick={() => setPullSecretEdit(true)}>Replace</Button>
                <Button variant="danger" onClick={async () => { await fetch("/api/v1/auth/ocp-pull-secret", { method: "DELETE" }); setHasPullSecret(false); setPullSecretMasked(""); }}>Delete</Button>
              </div>
            </CardBody>
          </Card>
        ) : (
          <Card>
            <CardBody>
              <textarea style={{ width: "100%", minHeight: 80, padding: "8px 10px", borderRadius: 6, fontSize: 12, fontFamily: "monospace", resize: "vertical", border: "1px solid var(--pf-t--global--border--color--default)", background: "var(--pf-t--global--background--color--primary--default)", color: "var(--pf-t--global--text--color--regular)" }} value={pullSecretInput} onChange={(e) => setPullSecretInput(e.target.value)} placeholder='{"auths":{"cloud.openshift.com":...}}' />
              <div style={{ display: "flex", gap: 8, marginTop: 8, justifyContent: "flex-end" }}>
                {pullSecretEdit && <Button variant="secondary" onClick={() => { setPullSecretEdit(false); setPullSecretInput(""); }}>Cancel</Button>}
                <Button variant="primary" isDisabled={!pullSecretInput.trim() || pullSecretSaving} onClick={async () => {
                  setPullSecretSaving(true);
                  const resp = await fetch("/api/v1/auth/ocp-pull-secret", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ pull_secret: pullSecretInput }) });
                  if (resp.ok) { setHasPullSecret(true); setPullSecretEdit(false); setPullSecretInput(""); const data = await fetch("/api/v1/auth/ocp-pull-secret").then(r => r.json()); setPullSecretMasked(data.masked || ""); }
                  else { const err = await resp.json().catch(() => ({ detail: "Save failed" })); alert(err.detail || "Save failed"); }
                  setPullSecretSaving(false);
                }}>{pullSecretSaving ? "Saving..." : "Save Pull Secret"}</Button>
              </div>
            </CardBody>
          </Card>
        )}
      </PageSection>
      <PageSection>
        <Title headingLevel="h2" style={{ marginBottom: 12 }}>Registry Credentials</Title>
        <p style={{ fontSize: 13, opacity: 0.7, marginBottom: 12 }}>
          Container registry credentials for pulling private images. Referenced by name in container nodes.
        </p>
        {registryCreds.length > 0 && (
          <table style={{ width: "100%", borderCollapse: "collapse", marginBottom: 12, border: "1px solid var(--pf-t--global--border--color--default)" }}>
            <thead>
              <tr style={{ borderBottom: "1px solid var(--pf-t--global--border--color--default)" }}>
                <th style={{ textAlign: "left", padding: "6px 8px", fontSize: 12 }}>Name</th>
                <th style={{ textAlign: "left", padding: "6px 8px", fontSize: 12 }}>Registry</th>
                <th style={{ textAlign: "left", padding: "6px 8px", fontSize: 12 }}>Username</th>
                <th style={{ textAlign: "right", padding: "6px 8px", fontSize: 12 }}></th>
              </tr>
            </thead>
            <tbody>
              {registryCreds.map((c) => (
                <tr key={c.id} style={{ borderBottom: "1px solid var(--pf-t--global--border--color--default)" }}>
                  <td style={{ padding: "6px 8px", fontSize: 13 }}>{c.name}</td>
                  <td style={{ padding: "6px 8px", fontSize: 13, fontFamily: "monospace" }}>{c.registry}</td>
                  <td style={{ padding: "6px 8px", fontSize: 13 }}>{c.username}</td>
                  <td style={{ padding: "6px 8px", textAlign: "right" }}>
                    <Button
                      variant="secondary"
                      size="sm"
                      onClick={() => {
                        setEditCredId(c.id);
                        setCredForm({ name: c.name, registry: c.registry, username: c.username, password: "" });
                        setShowAddCred(true);
                      }}
                    >
                      Edit
                    </Button>
                    <Button
                      variant="danger"
                      size="sm"
                      style={{ marginLeft: 6 }}
                      onClick={() => deleteCred(c.id)}
                    >
                      Delete
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {showAddCred ? (
          <Card>
            <CardBody>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8, marginBottom: 8 }}>
                <div>
                  <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Name</label>
                  <input
                    style={{ width: "100%", padding: "6px 10px", borderRadius: 6, border: "1px solid var(--pf-t--global--border--color--default)", background: "var(--pf-t--global--background--color--primary--default)", color: "var(--pf-t--global--text--color--regular)", fontSize: 13 }}
                    placeholder="e.g., Quay.io prod"
                    value={credForm.name}
                    onChange={(e) => setCredForm({ ...credForm, name: e.target.value })}
                  />
                </div>
                <div>
                  <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Registry</label>
                  <input
                    style={{ width: "100%", padding: "6px 10px", borderRadius: 6, border: "1px solid var(--pf-t--global--border--color--default)", background: "var(--pf-t--global--background--color--primary--default)", color: "var(--pf-t--global--text--color--regular)", fontSize: 13 }}
                    placeholder="e.g., quay.io"
                    value={credForm.registry}
                    onChange={(e) => setCredForm({ ...credForm, registry: e.target.value })}
                  />
                </div>
                <div>
                  <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Username</label>
                  <input
                    style={{ width: "100%", padding: "6px 10px", borderRadius: 6, border: "1px solid var(--pf-t--global--border--color--default)", background: "var(--pf-t--global--background--color--primary--default)", color: "var(--pf-t--global--text--color--regular)", fontSize: 13 }}
                    value={credForm.username}
                    onChange={(e) => setCredForm({ ...credForm, username: e.target.value })}
                  />
                </div>
                <div>
                  <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Password</label>
                  <input
                    style={{ width: "100%", padding: "6px 10px", borderRadius: 6, border: "1px solid var(--pf-t--global--border--color--default)", background: "var(--pf-t--global--background--color--primary--default)", color: "var(--pf-t--global--text--color--regular)", fontSize: 13 }}
                    type="password"
                    placeholder={editCredId ? "(unchanged)" : ""}
                    value={credForm.password}
                    onChange={(e) => setCredForm({ ...credForm, password: e.target.value })}
                  />
                </div>
              </div>
              <div style={{ display: "flex", gap: 8 }}>
                <Button variant="primary" onClick={saveCred}>
                  {editCredId ? "Update" : "Add"}
                </Button>
                <Button
                  variant="secondary"
                  onClick={() => {
                    setShowAddCred(false);
                    setEditCredId(null);
                    setCredForm({ name: "", registry: "", username: "", password: "" });
                  }}
                >
                  Cancel
                </Button>
              </div>
            </CardBody>
          </Card>
        ) : (
          <Button variant="primary" onClick={() => setShowAddCred(true)}>
            + Add Registry Credential
          </Button>
        )}
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
