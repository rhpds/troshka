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
  default_ami: string | null;
  vpc_id: string | null;
  subnet_id: string | null;
  security_group_id: string | null;
  state: string;
  has_credentials: boolean;
  host_count: number;
  created_at: string;
  console_base_domain?: string;
  console_nameservers?: string[];
  console_configured?: boolean;
}

export default function AdminProvidersPage() {
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [showAdd, setShowAdd] = useState(false);
  const [error, setError] = useState("");
  const [testResult, setTestResult] = useState<Record<string, string>>({});
  const [amiResult, setAmiResult] = useState<Record<string, string>>({});
  const [amiOptions, setAmiOptions] = useState<Record<string, Array<{type: string; label: string; ami_id: string; name: string; created: string}>>>({});
  const [consoleDomain, setConsoleDomain] = useState<Record<string, string>>({});
  const [consoleSetupResult, setConsoleSetupResult] = useState<Record<string, string>>({});
  const [settingUpConsole, setSettingUpConsole] = useState<string | null>(null);

  const [name, setName] = useState("");
  const [type, setType] = useState("ec2");
  const [region, setRegion] = useState("us-east-1");
  const [accessKey, setAccessKey] = useState("");
  const [secretKey, setSecretKey] = useState("");
  const [editId, setEditId] = useState<string | null>(null);
  const [editName, setEditName] = useState("");
  const [editRegion, setEditRegion] = useState("");
  const [s3Bucket, setS3Bucket] = useState("troshka-images");
  const [editAccessKey, setEditAccessKey] = useState("");
  const [editSecretKey, setEditSecretKey] = useState("");

  const loadProviders = () => {
    fetch("/api/v1/providers/")
      .then((r) => r.ok ? r.json() : [])
      .then((data) => { setProviders(Array.isArray(data) ? data : []); setLoading(false); })
      .catch(() => setLoading(false));
  };

  useEffect(() => {
    loadProviders();
    const interval = setInterval(loadProviders, 10000);
    return () => clearInterval(interval);
  }, []);

  const createProvider = async () => {
    if (!name.trim() || !accessKey.trim() || !secretKey.trim()) {
      setError("All fields are required");
      return;
    }
    setError("");
    const resp = await fetch("/api/v1/providers/", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name, type, default_region: region,
        access_key_id: accessKey, secret_access_key: secretKey,
        ...(type === "s3" ? { bucket: s3Bucket } : {}),
      }),
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
      if (data.message) {
        setTestResult((prev) => ({ ...prev, [id]: data.message }));
      } else if (data.bucket) {
        setTestResult((prev) => ({ ...prev, [id]: `OK — Bucket: ${data.bucket}` }));
      } else {
        setTestResult((prev) => ({ ...prev, [id]: `OK — Account: ${data.account}` }));
      }
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
    setAmiOptions((prev) => ({ ...prev, [id]: [] }));
    const resp = await fetch(`/api/v1/providers/${id}/discover-ami`);
    if (resp.ok) {
      const data = await resp.json();
      if (data.amis && data.amis.length > 0) {
        setAmiOptions((prev) => ({ ...prev, [id]: data.amis }));
        setAmiResult((prev) => ({ ...prev, [id]: `Found ${data.amis.length} AMI(s) in ${data.region}` }));
      } else {
        setAmiResult((prev) => ({ ...prev, [id]: "No AMIs found in this region" }));
      }
    } else {
      setAmiResult((prev) => ({ ...prev, [id]: "FAILED — check credentials and region" }));
    }
  };

  const selectAmi = async (providerId: string, amiId: string) => {
    const resp = await fetch(`/api/v1/providers/${providerId}/set-ami?ami_id=${amiId}`, { method: "POST" });
    if (resp.ok) {
      setAmiOptions((prev) => ({ ...prev, [providerId]: [] }));
      setAmiResult((prev) => ({ ...prev, [providerId]: `Set to ${amiId}` }));
      loadProviders();
    }
  };

  const [vpcOptions, setVpcOptions] = useState<Record<string, Array<{vpc_id: string; name: string; cidr: string; is_default: boolean; subnets: Array<{subnet_id: string; az: string; cidr: string; public: boolean}>}>>>({});

  const discoverVpcs = async (id: string) => {
    setAmiResult((prev) => ({ ...prev, [id]: "Discovering VPCs..." }));
    const resp = await fetch(`/api/v1/providers/${id}/discover-vpcs`);
    if (resp.ok) {
      const data = await resp.json();
      const vpcs = data.vpcs || [];
      if (vpcs.length === 0) {
        setAmiResult((prev) => ({ ...prev, [id]: "No troshka VPC found — creating one..." }));
        await createVpc(id, true);
        return;
      }
      setVpcOptions((prev) => ({ ...prev, [id]: vpcs }));
      setAmiResult((prev) => ({ ...prev, [id]: `Found ${vpcs.length} VPC(s)` }));
    } else {
      setAmiResult((prev) => ({ ...prev, [id]: "VPC discovery failed" }));
    }
  };

  const setupInfra = async (providerId: string, vpcId: string, subnetId: string) => {
    const resp = await fetch(`/api/v1/providers/${providerId}/setup-infra?vpc_id=${vpcId}&subnet_id=${subnetId}`, { method: "POST" });
    if (resp.ok) {
      setVpcOptions((prev) => ({ ...prev, [providerId]: [] }));
      loadProviders();
    } else {
      const data = await resp.json();
      setError(data.detail || "Setup failed");
    }
  };

  const createVpc = async (id: string, skipConfirm = false) => {
    if (!skipConfirm && !window.confirm("Create a new VPC (10.100.0.0/16) with a public subnet, internet gateway, and security group?")) return;
    setAmiResult((prev) => ({ ...prev, [id]: "Creating VPC..." }));
    const resp = await fetch(`/api/v1/providers/${id}/create-vpc`, { method: "POST" });
    if (resp.ok) {
      const data = await resp.json();
      setVpcOptions((prev) => ({ ...prev, [id]: [] }));
      setAmiResult((prev) => ({ ...prev, [id]: `VPC created: ${data.vpc_id}` }));
      loadProviders();
    } else {
      const data = await resp.json();
      setAmiResult((prev) => ({ ...prev, [id]: `Failed: ${data.detail || "unknown error"}` }));
    }
  };

  const setupConsole = async (providerId: string) => {
    const domain = consoleDomain[providerId]?.trim();
    if (!domain) return;
    setSettingUpConsole(providerId);
    setConsoleSetupResult((prev) => ({ ...prev, [providerId]: "" }));
    try {
      const resp = await fetch(`/api/v1/providers/${providerId}/setup-console`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ base_domain: domain }),
      });
      const data = await resp.json();
      if (!resp.ok) {
        setConsoleSetupResult((prev) => ({ ...prev, [providerId]: data.detail || "Setup failed" }));
      } else {
        setConsoleSetupResult((prev) => ({ ...prev, [providerId]: "Console configured" }));
        loadProviders();
      }
    } catch {
      setConsoleSetupResult((prev) => ({ ...prev, [providerId]: "Connection failed" }));
    }
    setSettingUpConsole(null);
  };

  const removeConsole = async (providerId: string) => {
    if (!confirm("Remove console DNS? This will delete the hosted zone and all DNS records.")) return;
    try {
      const resp = await fetch(`/api/v1/providers/${providerId}/console`, { method: "DELETE" });
      if (resp.ok) loadProviders();
    } catch { /* ignore */ }
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
                    <select style={inputStyle} value={type} onChange={(e) => {
                      setType(e.target.value);
                      if (e.target.value === "s3") setRegion("us-east-1");
                    }}>
                      <option value="ec2">AWS EC2</option>
                      <option value="ocp_virt">OCP Virtualization</option>
                      <option value="s3">S3 Storage</option>
                    </select>
                  </div>
                  <div style={{ flex: 1 }}>
                    <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Default Region</label>
                    <select style={inputStyle} value={region} onChange={(e) => setRegion(e.target.value)}>
                      {regions.map((r) => <option key={r.value} value={r.value}>{r.label}</option>)}
                    </select>
                  </div>
                </div>
                {type === "s3" && (
                  <div>
                    <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>S3 Bucket</label>
                    <input style={inputStyle} value={s3Bucket} onChange={(e) => setS3Bucket(e.target.value)} placeholder="troshka-images" />
                  </div>
                )}
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
                <div>
                  <div>
                    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                      <strong>{p.name}</strong>
                      <span style={{ fontSize: 11, padding: "1px 6px", borderRadius: 4, background: p.type === "ec2" ? "rgba(251,146,60,0.15)" : p.type === "s3" ? "rgba(74,222,128,0.15)" : "rgba(108,99,255,0.15)", color: p.type === "ec2" ? "#fb923c" : p.type === "s3" ? "#4ade80" : "#a78bfa" }}>
                        {p.type === "ec2" ? "AWS EC2" : p.type === "s3" ? "S3 Storage" : "OCP Virt"}
                      </span>
                      <span style={{ fontSize: 11, padding: "1px 6px", borderRadius: 4, background: p.state === "active" ? "rgba(74,222,128,0.15)" : "rgba(148,163,184,0.15)", color: p.state === "active" ? "#4ade80" : "#94a3b8" }}>
                        {p.state}
                      </span>
                      {p.has_credentials && <span style={{ fontSize: 11, color: "#4ade80" }}>🔑</span>}
                    </div>
                    <div style={{ fontSize: 12, opacity: 0.6, marginTop: 4 }}>
                      {p.default_region}
                      {p.type !== "s3" && <span> · {p.host_count} host{p.host_count !== 1 ? "s" : ""}</span>}
                      {p.type !== "s3" && (
                        p.default_ami
                          ? <span> · AMI: <code style={{ fontSize: 11 }}>{p.default_ami}</code></span>
                          : <span style={{ color: "#fbbf24" }}> · ⚠ No AMI</span>
                      )}
                      {p.type !== "s3" && (
                        p.vpc_id
                          ? <span> · VPC: <code style={{ fontSize: 11 }}>{p.vpc_id}</code></span>
                          : <span style={{ color: "#fbbf24" }}> · ⚠ No VPC</span>
                      )}
                      {p.type !== "s3" && (
                        p.console_configured
                          ? <span> · Console: <code style={{ fontSize: 11 }}>{p.console_base_domain}</code></span>
                          : null
                      )}
                    </div>
                    {testResult[p.id] && (
                      <div style={{ fontSize: 11, marginTop: 4, color: testResult[p.id].includes("FAILED") || testResult[p.id].includes("Failed") ? "#f87171" : testResult[p.id].includes("does not exist") || testResult[p.id].includes("no access") ? "#fbbf24" : "#4ade80" }}>
                        {testResult[p.id]}
                      </div>
                    )}
                    {amiResult[p.id] && (
                      <div style={{ fontSize: 11, marginTop: 4, color: amiResult[p.id].includes("FAILED") ? "#f87171" : "#4ade80" }}>
                        {amiResult[p.id]}
                      </div>
                    )}
                    {amiOptions[p.id] && amiOptions[p.id].length > 0 && (
                      <div style={{ marginTop: 8, display: "flex", flexDirection: "column", gap: 6 }}>
                        {amiOptions[p.id].map((ami) => (
                          <div key={ami.ami_id} style={{ display: "flex", alignItems: "center", justifyContent: "space-between", background: "var(--pf-t--global--background--color--secondary--default)", padding: "8px 12px", borderRadius: 6 }}>
                            <div>
                              <div style={{ fontSize: 12, fontWeight: 600 }}>{ami.label}</div>
                              <div style={{ fontSize: 11, opacity: 0.7, fontFamily: "monospace" }}>{ami.ami_id}</div>
                              <div style={{ fontSize: 10, opacity: 0.5 }}>{ami.name} · {new Date(ami.created).toLocaleDateString()}</div>
                            </div>
                            <Button variant="secondary" onClick={() => selectAmi(p.id, ami.ami_id)}>
                              Select
                            </Button>
                          </div>
                        ))}
                      </div>
                    )}
                    {vpcOptions[p.id] && vpcOptions[p.id].length > 0 && (
                      <div style={{ marginTop: 8, display: "flex", flexDirection: "column", gap: 6 }}>
                        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                          <div style={{ fontSize: 12, fontWeight: 600 }}>Select VPC and Subnet</div>
                          <Button variant="secondary" onClick={() => createVpc(p.id)}>+ Create New VPC</Button>
                        </div>
                        {vpcOptions[p.id].map((vpc) => (
                          <div key={vpc.vpc_id} style={{ background: "var(--pf-t--global--background--color--secondary--default)", padding: "8px 12px", borderRadius: 6 }}>
                            <div style={{ fontSize: 12, fontWeight: 600 }}>
                              {vpc.name} {vpc.is_default && <span style={{ fontSize: 10, color: "#4ade80" }}>(default)</span>}
                            </div>
                            <div style={{ fontSize: 11, opacity: 0.7, fontFamily: "monospace" }}>{vpc.vpc_id} · {vpc.cidr}</div>
                            <div style={{ marginTop: 6, display: "flex", flexDirection: "column", gap: 4 }}>
                              {vpc.subnets.map((s) => (
                                <div key={s.subnet_id} style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "4px 8px", background: "rgba(0,0,0,0.15)", borderRadius: 4 }}>
                                  <div style={{ fontSize: 11 }}>
                                    <code>{s.subnet_id}</code> · {s.az} · {s.cidr} {s.public && <span style={{ color: "#4ade80" }}>public</span>}
                                  </div>
                                  <Button variant="secondary" onClick={() => setupInfra(p.id, vpc.vpc_id, s.subnet_id)} style={{ padding: "2px 8px", fontSize: 11 }}>
                                    Use
                                  </Button>
                                </div>
                              ))}
                            </div>
                          </div>
                        ))}
                      </div>
                    )}
                    {consoleDomain[p.id] !== undefined && !p.console_configured && (
                      <Card style={{ marginTop: 12 }}>
                        <CardBody>
                          <div style={{ fontWeight: 600, marginBottom: 8 }}>Setup Console DNS</div>
                          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                            <input
                              style={inputStyle}
                              placeholder="e.g., troshka.dev.rhdp.net"
                              value={consoleDomain[p.id] || ""}
                              onChange={(e) => setConsoleDomain((prev) => ({ ...prev, [p.id]: e.target.value }))}
                            />
                            <Button
                              variant="primary"
                              isLoading={settingUpConsole === p.id}
                              isDisabled={!consoleDomain[p.id]?.trim() || settingUpConsole === p.id}
                              onClick={() => setupConsole(p.id)}
                            >
                              Create
                            </Button>
                            <Button variant="plain" onClick={() => setConsoleDomain((prev) => { const n = { ...prev }; delete n[p.id]; return n; })}>
                              Cancel
                            </Button>
                          </div>
                          {consoleSetupResult[p.id] && (
                            <div style={{ marginTop: 8, fontSize: 13, color: consoleSetupResult[p.id].includes("failed") ? "#ef4444" : "#22c55e" }}>
                              {consoleSetupResult[p.id]}
                            </div>
                          )}
                        </CardBody>
                      </Card>
                    )}
                    {p.console_configured && p.console_nameservers && (
                      <details style={{ marginTop: 12 }}>
                        <summary style={{ cursor: "pointer", fontSize: 13, color: "var(--pf-t--global--text--color--subtle)" }}>
                          Console DNS Domain: <code style={{ fontSize: 11 }}>{p.console_base_domain}</code>
                        </summary>
                        <Card style={{ marginTop: 6 }}>
                          <CardBody>
                            <div style={{ fontSize: 12, color: "var(--pf-t--global--text--color--subtle)", marginBottom: 8 }}>
                              Add NS records for <code>{p.console_base_domain}</code> in your parent zone pointing to:
                            </div>
                            <div style={{ fontSize: 12, fontFamily: "monospace", marginBottom: 8 }}>
                              {p.console_nameservers.map((ns: string) => <div key={ns}>{ns}</div>)}
                            </div>
                            <Button variant="danger" onClick={() => removeConsole(p.id)}>Remove Console DNS</Button>
                          </CardBody>
                        </Card>
                      </details>
                    )}
                  </div>
                </div>
              )}
            </CardBody>
            {editId !== p.id && (
              <CardBody style={{ borderTop: "1px solid var(--pf-t--global--border--color--default)", display: "flex", gap: 8, flexWrap: "wrap", paddingTop: 8, paddingBottom: 8 }}>
                    <Button variant="secondary" onClick={() => startEdit(p)}>Edit</Button>
                    {p.type === "s3" && (
                      <Button variant="secondary" onClick={async () => {
                        const resp = await fetch(`/api/v1/providers/${p.id}/create-bucket`, { method: "POST" });
                        const data = await resp.json();
                        if (resp.ok) {
                          setTestResult((prev) => ({ ...prev, [p.id]: `Bucket: ${data.bucket} (${data.status})` }));
                        } else {
                          setTestResult((prev) => ({ ...prev, [p.id]: `Failed: ${data.detail}` }));
                        }
                      }}>Create Bucket</Button>
                    )}
                    {p.type === "s3" && (
                      <Button variant="secondary" onClick={async () => {
                        setTestResult((prev) => ({ ...prev, [p.id]: "Scanning S3..." }));
                        const resp = await fetch("/api/v1/library/scan-s3", { method: "POST" });
                        if (resp.ok) {
                          const data = await resp.json();
                          const parts = [`${data.imported} library item(s)`];
                  if (data.snapshots) parts.push(`${data.snapshots} snapshot(s)`);
                  if (data.patterns) parts.push(`${data.patterns} pattern(s)`);
                  setTestResult((prev) => ({ ...prev, [p.id]: `Imported: ${parts.join(", ")}` }));
                        } else {
                          const data = await resp.json().catch(() => ({}));
                          setTestResult((prev) => ({ ...prev, [p.id]: `Scan failed: ${data.detail || "unknown error"}` }));
                        }
                      }}>Scan S3</Button>
                    )}
                    {p.type !== "s3" && <Button variant="secondary" onClick={() => discoverAmi(p.id)}>Discover AMI</Button>}
                    {p.type !== "s3" && !(p.vpc_id && p.subnet_id && p.security_group_id) && <Button variant="secondary" onClick={() => discoverVpcs(p.id)}>Setup VPC</Button>}
                    {p.type !== "s3" && p.vpc_id && !p.console_configured && (
                      <Button variant="secondary" onClick={() => setConsoleDomain((prev) => ({ ...prev, [p.id]: prev[p.id] || "" }))}>
                        Setup Console
                      </Button>
                    )}
                    <Button variant="secondary" onClick={() => testProvider(p.id)}>Test</Button>
                    {p.type === "ec2" && p.state === "active" && (
                      <Button variant="secondary" onClick={async () => {
                        const resp = await fetch(`/api/v1/providers/${p.id}/gc`, { method: "POST" });
                        if (resp.ok) {
                          const report = await resp.json();
                          const parts: string[] = [];
                          if (report.eips_released > 0) parts.push(`Released ${report.eips_released} orphan EIPs`);
                          if (report.sg_rules_removed > 0) parts.push(`Removed ${report.sg_rules_removed} stale SG rules`);
                          if (report.stale_db_rows_deleted > 0) parts.push(`Cleaned ${report.stale_db_rows_deleted} stale DB records`);
                          if (parts.length === 0) parts.push("No orphans found");
                          alert(parts.join("\n"));
                        } else {
                          alert("Provider GC failed — check server logs");
                        }
                      }}>
                        Clean
                      </Button>
                    )}
                    <Button variant="danger" onClick={() => deleteProvider(p.id)} isDisabled={p.host_count > 0}>
                      {p.host_count > 0 ? "Has Hosts" : "Delete"}
                    </Button>
              </CardBody>
            )}
          </Card>
        ))}
      </PageSection>
    </>
  );
}
