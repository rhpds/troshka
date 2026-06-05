"use client";

import React, { useEffect, useState } from "react";
import {
  Button,
  Card,
  CardBody,
  PageSection,
  Title,
  Alert,
  Toolbar,
  ToolbarContent,
  ToolbarItem,
} from "@patternfly/react-core";

interface ProviderInfo {
  id: string;
  name: string;
  type: string;
  default_region: string | null;
  state: string;
  has_credentials: boolean;
  host_count: number;
  created_at: string;
}

export default function AdminProvidersPage() {
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [showAdd, setShowAdd] = useState(false);
  const [error, setError] = useState("");
  const [testResult, setTestResult] = useState<Record<string, string>>({});
  const [amiResult, setAmiResult] = useState<Record<string, string>>({});

  const [name, setName] = useState("");
  const [type, setType] = useState("ec2");
  const [region, setRegion] = useState("us-east-1");
  const [accessKey, setAccessKey] = useState("");
  const [secretKey, setSecretKey] = useState("");
  const [editId, setEditId] = useState<string | null>(null);
  const [editName, setEditName] = useState("");
  const [editRegion, setEditRegion] = useState("");
  const [editAccessKey, setEditAccessKey] = useState("");
  const [editSecretKey, setEditSecretKey] = useState("");

  const loadProviders = () => {
    fetch("/api/v1/providers/")
      .then((r) => r.ok ? r.json() : [])
      .then((data) => { setProviders(Array.isArray(data) ? data : []); setLoading(false); })
      .catch(() => setLoading(false));
  };

  useEffect(() => { loadProviders(); }, []);

  const createProvider = async () => {
    if (!name.trim() || !accessKey.trim() || !secretKey.trim()) {
      setError("All fields are required");
      return;
    }
    setError("");
    const resp = await fetch("/api/v1/providers/", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, type, default_region: region, access_key_id: accessKey, secret_access_key: secretKey }),
    });
    if (resp.ok) {
      setShowAdd(false);
      setName(""); setAccessKey(""); setSecretKey("");
      loadProviders();
    } else {
      const data = await resp.json();
      setError(data.detail || "Failed to create provider");
    }
  };

  const testProvider = async (id: string) => {
    setTestResult((prev) => ({ ...prev, [id]: "testing..." }));
    const resp = await fetch(`/api/v1/providers/${id}/test`, { method: "POST" });
    if (resp.ok) {
      const data = await resp.json();
      setTestResult((prev) => ({ ...prev, [id]: `OK — Account: ${data.account}` }));
    } else {
      setTestResult((prev) => ({ ...prev, [id]: "FAILED" }));
    }
  };

  const startEdit = (p: ProviderInfo) => {
    setEditId(p.id);
    setEditName(p.name);
    setEditRegion(p.default_region || "us-east-1");
    setEditAccessKey("");
    setEditSecretKey("");
  };

  const saveEdit = async () => {
    if (!editId) return;
    const body: Record<string, string> = {};
    if (editName) body.name = editName;
    if (editRegion) body.default_region = editRegion;
    if (editAccessKey) body.access_key_id = editAccessKey;
    if (editSecretKey) body.secret_access_key = editSecretKey;

    const resp = await fetch(`/api/v1/providers/${editId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (resp.ok) {
      setEditId(null);
      loadProviders();
    } else {
      const data = await resp.json();
      setError(data.detail || "Failed to update");
    }
  };

  const discoverAmi = async (id: string) => {
    setAmiResult((prev) => ({ ...prev, [id]: "discovering..." }));
    const resp = await fetch(`/api/v1/providers/${id}/discover-ami`, { method: "POST" });
    if (resp.ok) {
      const data = await resp.json();
      setAmiResult((prev) => ({ ...prev, [id]: `${data.ami_id} (${data.name})` }));
      loadProviders();
    } else {
      setAmiResult((prev) => ({ ...prev, [id]: "FAILED — no AMI found" }));
    }
  };

  const deleteProvider = async (id: string) => {
    if (!window.confirm("Delete this provider?")) return;
    const resp = await fetch(`/api/v1/providers/${id}`, { method: "DELETE" });
    if (resp.ok) {
      loadProviders();
    } else {
      const data = await resp.json();
      alert(data.detail || "Failed to delete");
    }
  };

  const regions = [
    { value: "us-east-1", label: "US East (N. Virginia)" },
    { value: "us-east-2", label: "US East (Ohio)" },
    { value: "us-west-1", label: "US West (N. California)" },
    { value: "us-west-2", label: "US West (Oregon)" },
    { value: "eu-west-1", label: "Europe (Ireland)" },
    { value: "eu-west-2", label: "Europe (London)" },
    { value: "eu-central-1", label: "Europe (Frankfurt)" },
    { value: "ap-southeast-1", label: "Asia Pacific (Singapore)" },
    { value: "ap-northeast-1", label: "Asia Pacific (Tokyo)" },
  ];

  const inputStyle = { width: "100%", padding: "6px 10px", borderRadius: 6, border: "1px solid var(--pf-t--global--border--color--default)", background: "var(--pf-t--global--background--color--primary--default)", color: "var(--pf-t--global--text--color--regular)", fontSize: 13 };

  if (loading) return <PageSection><Title headingLevel="h1">Loading...</Title></PageSection>;

  return (
    <>
      <PageSection>
        <Toolbar>
          <ToolbarContent>
            <ToolbarItem><Title headingLevel="h1">Providers</Title></ToolbarItem>
            <ToolbarItem align={{ default: "alignEnd" }}>
              <Button variant="primary" onClick={() => setShowAdd(!showAdd)}>
                {showAdd ? "Cancel" : "+ Add Provider"}
              </Button>
            </ToolbarItem>
          </ToolbarContent>
        </Toolbar>
      </PageSection>

      {error && <PageSection><Alert variant="danger" title={error} /></PageSection>}

      {showAdd && (
        <PageSection>
          <Card>
            <CardBody>
              <Title headingLevel="h3" size="md" style={{ marginBottom: 12 }}>New Provider</Title>
              <div style={{ display: "flex", flexDirection: "column", gap: 10, maxWidth: 500 }}>
                <div>
                  <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Name</label>
                  <input style={inputStyle} value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. AWS Production" />
                </div>
                <div style={{ display: "flex", gap: 10 }}>
                  <div style={{ flex: 1 }}>
                    <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Type</label>
                    <select style={inputStyle} value={type} onChange={(e) => setType(e.target.value)}>
                      <option value="ec2">AWS EC2</option>
                      <option value="ocp_virt">OCP Virtualization</option>
                    </select>
                  </div>
                  <div style={{ flex: 1 }}>
                    <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Default Region</label>
                    <select style={inputStyle} value={region} onChange={(e) => setRegion(e.target.value)}>
                      {regions.map((r) => <option key={r.value} value={r.value}>{r.label}</option>)}
                    </select>
                  </div>
                </div>
                <div>
                  <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Access Key ID</label>
                  <input style={{ ...inputStyle, fontFamily: "monospace" }} value={accessKey} onChange={(e) => setAccessKey(e.target.value)} placeholder="AKIA..." />
                </div>
                <div>
                  <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Secret Access Key</label>
                  <input style={{ ...inputStyle, fontFamily: "monospace" }} type="password" value={secretKey} onChange={(e) => setSecretKey(e.target.value)} placeholder="Secret key" />
                </div>
                <Button variant="primary" onClick={createProvider} style={{ alignSelf: "flex-start" }}>Create Provider</Button>
              </div>
            </CardBody>
          </Card>
        </PageSection>
      )}

      <PageSection>
        {providers.length === 0 && !showAdd && (
          <p style={{ opacity: 0.6 }}>No providers configured. Add one to start provisioning hosts.</p>
        )}
        {providers.map((p) => (
          <Card key={p.id} style={{ marginBottom: 8 }}>
            <CardBody>
              {editId === p.id ? (
                <div style={{ display: "flex", flexDirection: "column", gap: 10, maxWidth: 500 }}>
                  <div>
                    <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Name</label>
                    <input style={inputStyle} value={editName} onChange={(e) => setEditName(e.target.value)} />
                  </div>
                  <div>
                    <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Default Region</label>
                    <select style={inputStyle} value={editRegion} onChange={(e) => setEditRegion(e.target.value)}>
                      {regions.map((r) => <option key={r.value} value={r.value}>{r.label}</option>)}
                    </select>
                  </div>
                  <div>
                    <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Access Key ID <span style={{ opacity: 0.5 }}>(leave blank to keep current)</span></label>
                    <input style={{ ...inputStyle, fontFamily: "monospace" }} value={editAccessKey} onChange={(e) => setEditAccessKey(e.target.value)} placeholder="Leave blank to keep current" />
                  </div>
                  <div>
                    <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Secret Access Key <span style={{ opacity: 0.5 }}>(leave blank to keep current)</span></label>
                    <input style={{ ...inputStyle, fontFamily: "monospace" }} type="password" value={editSecretKey} onChange={(e) => setEditSecretKey(e.target.value)} placeholder="Leave blank to keep current" />
                  </div>
                  <div style={{ display: "flex", gap: 8 }}>
                    <Button variant="primary" onClick={saveEdit}>Save</Button>
                    <Button variant="secondary" onClick={() => setEditId(null)}>Cancel</Button>
                  </div>
                </div>
              ) : (
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <div style={{ flex: 1 }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                      <strong>{p.name}</strong>
                      <span style={{ fontSize: 11, padding: "1px 6px", borderRadius: 4, background: p.type === "ec2" ? "rgba(251,146,60,0.15)" : "rgba(108,99,255,0.15)", color: p.type === "ec2" ? "#fb923c" : "#a78bfa" }}>
                        {p.type === "ec2" ? "AWS EC2" : "OCP Virt"}
                      </span>
                      <span style={{ fontSize: 11, padding: "1px 6px", borderRadius: 4, background: p.state === "active" ? "rgba(74,222,128,0.15)" : "rgba(148,163,184,0.15)", color: p.state === "active" ? "#4ade80" : "#94a3b8" }}>
                        {p.state}
                      </span>
                      {p.has_credentials && <span style={{ fontSize: 11, color: "#4ade80" }}>🔑</span>}
                    </div>
                    <div style={{ fontSize: 12, opacity: 0.6, marginTop: 4 }}>
                      {p.default_region} · {p.host_count} host{p.host_count !== 1 ? "s" : ""}
                      {(p as Record<string, unknown>).default_ami && (
                        <span> · AMI: <code style={{ fontSize: 11 }}>{(p as Record<string, unknown>).default_ami as string}</code></span>
                      )}
                      {!(p as Record<string, unknown>).default_ami && (
                        <span style={{ color: "#fbbf24" }}> · ⚠ No AMI set</span>
                      )}
                    </div>
                    {testResult[p.id] && (
                      <div style={{ fontSize: 11, marginTop: 4, color: testResult[p.id].startsWith("OK") ? "#4ade80" : "#f87171" }}>
                        {testResult[p.id]}
                      </div>
                    )}
                    {amiResult[p.id] && (
                      <div style={{ fontSize: 11, marginTop: 4, color: amiResult[p.id].startsWith("ami-") ? "#4ade80" : "#f87171" }}>
                        AMI: {amiResult[p.id]}
                      </div>
                    )}
                  </div>
                  <div style={{ display: "flex", gap: 8 }}>
                    <Button variant="secondary" onClick={() => startEdit(p)}>Edit</Button>
                    <Button variant="secondary" onClick={() => discoverAmi(p.id)}>Discover AMI</Button>
                    <Button variant="secondary" onClick={() => testProvider(p.id)}>Test</Button>
                    <Button variant="danger" onClick={() => deleteProvider(p.id)} isDisabled={p.host_count > 0}>
                      {p.host_count > 0 ? "Has Hosts" : "Delete"}
                    </Button>
                  </div>
                </div>
              )}
            </CardBody>
          </Card>
        ))}
      </PageSection>
    </>
  );
}
