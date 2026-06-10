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

interface Host {
  id: string;
  instance_id: string | null;
  instance_type: string | null;
  region: string | null;
  state: string;
  host_type: string;
  total_vcpus: number;
  total_ram_mb: number;
  used_vcpus: number;
  used_ram_mb: number;
  ip_address: string | null;
  agent_status: string;
  storage_size_gb: number;
  storage_used_pct?: number;
  storage_free_gb?: number;
  max_eips: number;
  used_eips: number;
  agent_version: string | null;
  last_health_at: string | null;
  created_at: string;
}

interface RegionSummary {
  region: string;
  total_hosts: number;
  active_hosts: number;
  total_vcpus: number;
  used_vcpus: number;
  total_ram_mb: number;
  used_ram_mb: number;
}

export default function AdminHostsPage() {
  const [hosts, setHosts] = useState<Host[]>([]);
  const [summary, setSummary] = useState<RegionSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [providers, setProviders] = useState<Array<{id: string; name: string; type: string; default_region: string; default_ami: string | null}>>([]);
  const [provisioning, setProvisioning] = useState(false);
  const [showAddForm, setShowAddForm] = useState(false);
  const [newProviderId, setNewProviderId] = useState("");
  const [newInstanceType, setNewInstanceType] = useState("m8i.xlarge");
  const [newRegion, setNewRegion] = useState("");
  const [error, setError] = useState("");
  const [filterRegion, setFilterRegion] = useState("");
  const [cpuRatio, setCpuRatio] = useState(4.0);
  const [ramRatio, setRamRatio] = useState(1.5);

  const [storageInfo, setStorageInfo] = useState<Record<string, { used_pct: number; free_gb: number; total_gb: number }>>({});

  const loadData = () => {
    Promise.all([
      fetch("/api/v1/hosts/").then((r) => r.ok ? r.json() : []),
      fetch("/api/v1/hosts/summary").then((r) => r.ok ? r.json() : []),
      fetch("/api/v1/providers/").then((r) => r.ok ? r.json() : []),
    ]).then(([h, s, p]) => {
      setHosts(Array.isArray(h) ? h : []);
      setSummary(Array.isArray(s) ? s : []);
      setProviders(Array.isArray(p) ? p : []);
      setLoading(false);
    }).catch(() => setLoading(false));
    // Storage fetched separately — SSH calls can be slow and shouldn't block the page
    fetch("/api/v1/hosts/storage").then((r) => r.ok ? r.json() : {}).then((d) => {
      if (d && typeof d === "object") setStorageInfo(d as Record<string, { used_pct: number; free_gb: number; total_gb: number }>);
    }).catch(() => {});
  };

  useEffect(() => {
    loadData();
    fetch("/api/v1/hosts/overcommit").then((r) => r.ok ? r.json() : null).then((d) => {
      if (d) { setCpuRatio(d.cpu_ratio); setRamRatio(d.ram_ratio); }
    }).catch(() => {});
    const interval = setInterval(loadData, 10000);
    return () => clearInterval(interval);
  }, []);

  const addHost = async () => {
    if (!newProviderId) { setError("Select a provider"); return; }
    setError("");
    try {
      const resp = await fetch("/api/v1/hosts/", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          provider_id: newProviderId,
          instance_type: newInstanceType,
          region: newRegion || undefined,
        }),
      });
      if (!resp.ok) {
        const data = await resp.json();
        setError(data.detail || "Failed to provision host");
      } else {
        loadData();
      }
    } catch {
      setError("Failed to connect to server");
    }
  };

  const instanceTypes = [
    { value: "c8i.large",    label: "c8i.large — 2 vCPU / 4 GB — ~$0.085/hr" },
    { value: "c8i.xlarge",   label: "c8i.xlarge — 4 vCPU / 8 GB — ~$0.170/hr" },
    { value: "c8i.2xlarge",  label: "c8i.2xlarge — 8 vCPU / 16 GB — ~$0.340/hr" },
    { value: "c8i.4xlarge",  label: "c8i.4xlarge — 16 vCPU / 32 GB — ~$0.680/hr" },
    { value: "m8i.large",    label: "m8i.large — 2 vCPU / 8 GB — ~$0.096/hr" },
    { value: "m8i.xlarge",   label: "m8i.xlarge — 4 vCPU / 16 GB — ~$0.192/hr" },
    { value: "m8i.2xlarge",  label: "m8i.2xlarge — 8 vCPU / 32 GB — ~$0.384/hr" },
    { value: "m8i.4xlarge",  label: "m8i.4xlarge — 16 vCPU / 64 GB — ~$0.768/hr" },
    { value: "m8i.8xlarge",  label: "m8i.8xlarge — 32 vCPU / 128 GB — ~$1.536/hr" },
    { value: "r8i.large",    label: "r8i.large — 2 vCPU / 16 GB — ~$0.126/hr" },
    { value: "r8i.xlarge",   label: "r8i.xlarge — 4 vCPU / 32 GB — ~$0.252/hr" },
    { value: "r8i.2xlarge",  label: "r8i.2xlarge — 8 vCPU / 64 GB — ~$0.504/hr" },
    { value: "r8i.4xlarge",  label: "r8i.4xlarge — 16 vCPU / 128 GB — ~$1.008/hr" },
    { value: "r8i.8xlarge",  label: "r8i.8xlarge — 32 vCPU / 256 GB — ~$2.016/hr" },
    { value: "r8i.24xlarge", label: "r8i.24xlarge — 96 vCPU / 768 GB — ~$6.048/hr" },
  ];

  const awsRegions = [
    { value: "us-east-1",      label: "US East (N. Virginia)" },
    { value: "us-east-2",      label: "US East (Ohio)" },
    { value: "us-west-1",      label: "US West (N. California)" },
    { value: "us-west-2",      label: "US West (Oregon)" },
    { value: "eu-west-1",      label: "Europe (Ireland)" },
    { value: "eu-west-2",      label: "Europe (London)" },
    { value: "eu-central-1",   label: "Europe (Frankfurt)" },
    { value: "ap-southeast-1", label: "Asia Pacific (Singapore)" },
    { value: "ap-northeast-1", label: "Asia Pacific (Tokyo)" },
  ];

  const [removing, setRemoving] = useState<string | null>(null);

  const removeHost = async (hostId: string, instanceId: string | null) => {
    const projectsResp = await fetch("/api/v1/projects/");
    const projects = projectsResp.ok ? await projectsResp.json() : [];
    const hostProjects = projects.filter((p: Record<string, string>) => p.host_id === hostId && p.state !== "draft");
    let msg = `Remove host ${instanceId || hostId}? This will terminate the EC2 instance.`;
    if (hostProjects.length > 0) {
      const names = hostProjects.map((p: Record<string, string>) => p.name).join(", ");
      msg += `\n\n⚠ ${hostProjects.length} project(s) will be reset to draft and their disk data will be lost: ${names}`;
    }
    if (!window.confirm(msg)) return;
    setRemoving(hostId);
    const resp = await fetch(`/api/v1/hosts/${hostId}`, { method: "DELETE" });
    if (resp.ok) {
      loadData();
    } else {
      const data = await resp.json();
      alert(data.detail || "Failed to remove host");
    }
    setRemoving(null);
  };

  const selectedProvider = providers.find((p) => p.id === newProviderId) as Record<string, any> | undefined;
  const selectedProviderHasAmi = selectedProvider ? !!selectedProvider.default_ami : false;
  const selectedProviderHasVpc = selectedProvider ? !!selectedProvider.vpc_id : false;
  const selectedProviderReady = selectedProviderHasAmi && selectedProviderHasVpc;
  const filteredHosts = filterRegion ? hosts.filter((h) => (h.region || "unknown") === filterRegion) : hosts;

  const stateColors: Record<string, string> = {
    active: "#4ade80",
    provisioning: "#fbbf24",
    draining: "#fbbf24",
    starting: "#fbbf24",
    stopped: "#94a3b8",
    shutting_down: "#fb923c",
    terminating: "#f87171",
    terminated: "#94a3b8",
  };

  const agentColors: Record<string, string> = {
    connected: "#4ade80",
    installed: "#22d3ee",
    installing: "#fbbf24",
    waiting_ssh: "#fbbf24",
    install_failed: "#f87171",
    disconnected: "#f87171",
  };

  const agentLabels: Record<string, string> = {
    connected: "agent: connected",
    installed: "agent: installed",
    installing: "installing agent...",
    waiting_ssh: "waiting for SSH...",
    install_failed: "install failed",
    disconnected: "agent: disconnected",
  };

  const [showKeyFor, setShowKeyFor] = useState<string | null>(null);
  const [keyData, setKeyData] = useState<Record<string, { key_pair_name: string; private_key: string; ssh_command: string | null; public_key?: string }>>({});

  const showKeyPair = async (hostId: string) => {
    if (showKeyFor === hostId) { setShowKeyFor(null); return; }
    if (!keyData[hostId]) {
      const resp = await fetch(`/api/v1/hosts/${hostId}/ssh-key`);
      if (!resp.ok) { alert("No SSH key available for this host"); return; }
      const data = await resp.json();
      setKeyData((prev) => ({ ...prev, [hostId]: data }));
    }
    setShowKeyFor(hostId);
  };

  const copyToClipboard = (text: string, label: string) => {
    navigator.clipboard.writeText(text);
    alert(`${label} copied to clipboard`);
  };

  const [resizeType, setResizeType] = useState<Record<string, string>>({});

  const [poweringHost, setPoweringHost] = useState<string | null>(null);

  const powerHost = async (hostId: string, action: "poweroff" | "poweron", instanceType?: string) => {
    setPoweringHost(hostId);
    try {
      const opts: RequestInit = { method: "POST" };
      if (action === "poweron" && instanceType) {
        opts.headers = { "Content-Type": "application/json" };
        opts.body = JSON.stringify({ instance_type: instanceType });
      }
      const resp = await fetch(`/api/v1/hosts/${hostId}/${action}`, opts);
      if (!resp.ok) {
        const data = await resp.json();
        alert(data.detail || `Failed to ${action}`);
      } else {
        setResizeType((prev) => { const next = { ...prev }; delete next[hostId]; return next; });
      }
      loadData();
    } catch {
      alert("Failed to connect to server");
    }
    setPoweringHost(null);
  };

  const [installing, setInstalling] = useState<string | null>(null);
  const [updating, setUpdating] = useState<string | null>(null);
  const [installOutput, setInstallOutput] = useState<Record<string, string>>({});

  const installAgent = async (hostId: string) => {
    setInstalling(hostId);
    setInstallOutput((prev) => ({ ...prev, [hostId]: "" }));
    try {
      const resp = await fetch(`/api/v1/hosts/${hostId}/install-agent`, { method: "POST" });
      if (!resp.ok) {
        const data = await resp.json();
        setInstallOutput((prev) => ({ ...prev, [hostId]: data.detail || "Install request failed" }));
        setInstalling(null);
        return;
      }
      // Poll until agent connects (install runs in background)
      for (let i = 0; i < 60; i++) {
        await new Promise(r => setTimeout(r, 5000));
        const hostsResp = await fetch("/api/v1/hosts/");
        if (hostsResp.ok) {
          const hostsList = await hostsResp.json();
          const updated = hostsList.find((x: Host) => x.id === hostId);
          if (updated?.agent_status === "connected") {
            setInstallOutput((prev) => ({ ...prev, [hostId]: "" }));
            loadData();
            setInstalling(null);
            return;
          } else if (updated?.agent_status === "install_failed") {
            setInstallOutput((prev) => ({ ...prev, [hostId]: "Agent install failed — check server logs" }));
            setInstalling(null);
            return;
          }
        }
      }
      setInstallOutput((prev) => ({ ...prev, [hostId]: "Install timed out — check host status" }));
    } catch {
      setInstallOutput((prev) => ({ ...prev, [hostId]: "Connection failed" }));
    }
    setInstalling(null);
  };

  if (loading) {
    return <PageSection><Title headingLevel="h1">Loading...</Title></PageSection>;
  }

  return (
    <>
      <PageSection>
        <Toolbar>
          <ToolbarContent>
            <ToolbarItem>
              <Title headingLevel="h1">Host Pool</Title>
            </ToolbarItem>
            <ToolbarItem align={{ default: "alignEnd" }}>
              <Button variant="primary" onClick={() => setShowAddForm(true)}>
                + Add Host
              </Button>
            </ToolbarItem>
          </ToolbarContent>
        </Toolbar>
      </PageSection>

      {error && (
        <PageSection>
          <Alert variant="danger" title={error} />
        </PageSection>
      )}

      {/* Add Host Form */}
      {showAddForm && (
        <PageSection>
          <Card>
            <CardBody>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
                <Title headingLevel="h3" size="md">Provision New Host</Title>
                <Button variant="plain" onClick={() => setShowAddForm(false)} style={{ fontSize: 18, padding: "0 4px" }}>✕</Button>
              </div>
              {providers.length === 0 && (
                <Alert variant="warning" title="No providers configured. Go to Admin > Providers to add one." style={{ marginBottom: 12 }} />
              )}
              <div style={{ display: "flex", gap: 12, alignItems: "end", flexWrap: "wrap" }}>
                <div style={{ minWidth: 180 }}>
                  <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Provider</label>
                  <select
                    style={{ width: "100%", padding: "6px 10px", borderRadius: 6, border: "1px solid var(--pf-t--global--border--color--default)", background: "var(--pf-t--global--background--color--primary--default)", color: "var(--pf-t--global--text--color--regular)", fontSize: 13 }}
                    value={newProviderId}
                    onChange={(e) => {
                      setNewProviderId(e.target.value);
                      const p = providers.find((p) => p.id === e.target.value);
                      if (p) setNewRegion(p.default_region || "");
                    }}
                  >
                    <option value="">Select provider...</option>
                    {providers.filter((p) => p.type === "ec2").map((p) => (
                      <option key={p.id} value={p.id}>{p.name}</option>
                    ))}
                  </select>
                </div>
                {newProviderId && !selectedProviderReady && (
                  <Alert variant="warning" title={
                    !selectedProviderHasAmi && !selectedProviderHasVpc
                      ? "Provider needs setup. Go to Providers and run Discover AMI and Setup VPC."
                      : !selectedProviderHasAmi
                        ? "No AMI set. Go to Providers and click Discover AMI."
                        : "No VPC set. Go to Providers and click Setup VPC."
                  } style={{ width: "100%", flexBasis: "100%" }} />
                )}
                <div style={{ minWidth: 280 }}>
                  <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Instance Type</label>
                  <select
                    style={{ width: "100%", padding: "6px 10px", borderRadius: 6, border: "1px solid var(--pf-t--global--border--color--default)", background: "var(--pf-t--global--background--color--primary--default)", color: "var(--pf-t--global--text--color--regular)", fontSize: 13 }}
                    value={newInstanceType}
                    onChange={(e) => setNewInstanceType(e.target.value)}
                  >
                    {instanceTypes.map((t) => (
                      <option key={t.value} value={t.value}>{t.label}</option>
                    ))}
                  </select>
                </div>
                <div style={{ minWidth: 200 }}>
                  <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Region</label>
                  <select
                    style={{ width: "100%", padding: "6px 10px", borderRadius: 6, border: "1px solid var(--pf-t--global--border--color--default)", background: "var(--pf-t--global--background--color--primary--default)", color: "var(--pf-t--global--text--color--regular)", fontSize: 13 }}
                    value={newRegion}
                    onChange={(e) => setNewRegion(e.target.value)}
                  >
                    {awsRegions.map((r) => (
                      <option key={r.value} value={r.value}>{r.label}</option>
                    ))}
                  </select>
                </div>
                <Button variant="primary" onClick={addHost} isDisabled={!newProviderId || !selectedProviderReady}>
                  Provision Host
                </Button>
              </div>
            </CardBody>
          </Card>
        </PageSection>
      )}

      {/* Region Summary Cards */}
      <PageSection>
        <div style={{ display: "flex", gap: 16, flexWrap: "wrap", marginBottom: 16 }}>
          <Card
            isClickable isSelectable
            onClick={() => setFilterRegion("")}
            style={{ minWidth: 200, borderLeft: !filterRegion ? "3px solid var(--pf-t--global--color--brand--default)" : undefined }}
          >
            <CardBody>
              <div style={{ fontSize: 13, fontWeight: 600 }}>All Regions</div>
              <div style={{ fontSize: 24, fontWeight: 700 }}>{hosts.length}</div>
              <div style={{ fontSize: 11, opacity: 0.6 }}>hosts</div>
            </CardBody>
          </Card>
          {summary.map((s) => (
            <Card
              key={s.region}
              isClickable isSelectable
              onClick={() => setFilterRegion(s.region === filterRegion ? "" : s.region)}
              style={{ minWidth: 200, borderLeft: filterRegion === s.region ? "3px solid var(--pf-t--global--color--brand--default)" : undefined }}
            >
              <CardBody>
                <div style={{ fontSize: 13, fontWeight: 600 }}>{s.region}</div>
                <div style={{ fontSize: 24, fontWeight: 700 }}>{s.active_hosts}<span style={{ fontSize: 14, opacity: 0.5 }}>/{s.total_hosts}</span></div>
                <div style={{ fontSize: 11, opacity: 0.6 }}>active hosts</div>
                <div style={{ fontSize: 11, marginTop: 8 }}>
                  <span>vCPU: {s.used_vcpus}/{(s as unknown as Record<string, number>).alloc_vcpus || s.total_vcpus} <span style={{ opacity: 0.4 }}>({s.total_vcpus} phys)</span></span>
                  <span style={{ marginLeft: 12 }}>RAM: {Math.round(s.used_ram_mb / 1024)}/{Math.round(((s as unknown as Record<string, number>).alloc_ram_mb || s.total_ram_mb) / 1024)} GB <span style={{ opacity: 0.4 }}>({Math.round(s.total_ram_mb / 1024)} phys)</span></span>
                </div>
                <div style={{ height: 4, background: "rgba(255,255,255,0.1)", borderRadius: 2, marginTop: 4 }}>
                  <div style={{
                    height: 4,
                    borderRadius: 2,
                    width: `${(s as unknown as Record<string, number>).alloc_vcpus ? (s.used_vcpus / (s as unknown as Record<string, number>).alloc_vcpus) * 100 : 0}%`,
                    background: (s.used_vcpus / Math.max((s as unknown as Record<string, number>).alloc_vcpus || s.total_vcpus, 1)) > 0.8 ? "#f87171" : "#4ade80",
                  }} />
                </div>
              </CardBody>
            </Card>
          ))}
        </div>
      </PageSection>

      {/* Host List */}
      <PageSection>
        {filteredHosts.length === 0 && (
          <p style={{ opacity: 0.6 }}>No hosts{filterRegion ? ` in ${filterRegion}` : ""}. Click &quot;+ Add Host&quot; to provision one.</p>
        )}
        {filteredHosts.map((h) => (
          <Card key={h.id} style={{ marginBottom: 8 }}>
            <CardBody style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <div style={{ flex: 1 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <strong>{h.instance_id || h.id.slice(0, 8)}</strong>
                  <span style={{ fontSize: 11, padding: "1px 6px", borderRadius: 4, background: `${stateColors[h.state] || "#94a3b8"}22`, color: stateColors[h.state] || "#94a3b8" }}>
                    {h.state}
                  </span>
                  <span style={{ fontSize: 11, padding: "1px 6px", borderRadius: 4, background: `${agentColors[h.agent_status] || "#94a3b8"}22`, color: agentColors[h.agent_status] || "#94a3b8" }}>
                    {(h.agent_status === "waiting_ssh" || h.agent_status === "installing") && "⏳ "}
                    {agentLabels[h.agent_status] || h.agent_status}{h.agent_version && h.agent_status === "connected" ? ` (${h.agent_version})` : ""}
                  </span>
                  {h.agent_status === "connected" && h.last_health_at && (
                    <span style={{ fontSize: 11, opacity: 0.5 }}>
                      health: {Math.round((Date.now() - new Date(h.last_health_at).getTime()) / 1000)}s ago
                    </span>
                  )}
                </div>
                <div style={{ fontSize: 12, opacity: 0.6, marginTop: 4, display: "flex", alignItems: "center", gap: 6 }}>
                  {h.state === "stopped" ? (
                    <>
                      <select
                        style={{ padding: "2px 6px", borderRadius: 4, border: "1px solid var(--pf-t--global--border--color--default)", background: "var(--pf-t--global--background--color--primary--default)", color: "var(--pf-t--global--text--color--regular)", fontSize: 12 }}
                        value={resizeType[h.id] || h.instance_type || ""}
                        onChange={(e) => setResizeType((prev) => ({ ...prev, [h.id]: e.target.value }))}
                      >
                        {instanceTypes.map((t) => (
                          <option key={t.value} value={t.value}>{t.label}</option>
                        ))}
                      </select>
                    </>
                  ) : (
                    <span>{h.instance_type}</span>
                  )}
                  <span>· {h.region} · {h.ip_address || "no IP"} · {h.host_type}</span>
                </div>
              </div>
              <div style={{ display: "flex", gap: 24, marginRight: 16, fontSize: 13 }}>
                <div style={{ textAlign: "center" }}>
                  <div style={{ fontSize: 10, opacity: 0.5, marginBottom: 2 }}>vCPU</div>
                  <div><strong>{h.used_vcpus}</strong>/{Math.round(h.total_vcpus * cpuRatio)}</div>
                  <div style={{ fontSize: 10, opacity: 0.4 }}>{h.total_vcpus} phys · {cpuRatio}:1</div>
                </div>
                <div style={{ textAlign: "center" }}>
                  <div style={{ fontSize: 10, opacity: 0.5, marginBottom: 2 }}>RAM</div>
                  <div><strong>{Math.round(h.used_ram_mb / 1024)}</strong>/{Math.round(h.total_ram_mb * ramRatio / 1024)} GB</div>
                  <div style={{ fontSize: 10, opacity: 0.4 }}>{Math.round(h.total_ram_mb / 1024)} phys · {ramRatio}:1</div>
                </div>
                {(() => { const si = storageInfo[h.id]; return (
                <div style={{ textAlign: "center", color: si && si.used_pct >= 80 ? "#f87171" : undefined }}>
                  <div style={{ fontSize: 10, opacity: 0.5, marginBottom: 2 }}>Storage</div>
                  <div>{si ? <><strong>{si.used_pct}%</strong> of {Math.round(si.total_gb)} GB</> : <span style={{ opacity: 0.4 }}>{h.storage_size_gb} GB</span>}</div>
                  {si && <div style={{ fontSize: 10, opacity: 0.4 }}>{Math.round(si.free_gb)} GB free</div>}
                </div>); })()}
                {h.max_eips > 0 && (
                  <div style={{ textAlign: "center" }}>
                    <div style={{ fontSize: 10, opacity: 0.5, marginBottom: 2 }}>EIPs</div>
                    <div><strong>{h.used_eips || 0}</strong>/{h.max_eips}</div>
                  </div>
                )}
              </div>
              <Button variant="secondary" onClick={() => showKeyPair(h.id)}>
                {showKeyFor === h.id ? "Hide Private Key" : "Show Private Key"}
              </Button>
              {h.state === "active" && (h.agent_status === "disconnected" || h.agent_status === "install_failed") && (
                <Button variant="secondary" onClick={() => {
                  if (!window.confirm(`Install agent on ${h.instance_id}? This will SSH into the host and run the install script.`)) return;
                  installAgent(h.id);
                }} isLoading={installing === h.id} isDisabled={installing === h.id}>
                  {h.agent_status === "install_failed" ? "Retry Install" : "Install Agent"}
                </Button>
              )}
              {(h.agent_status === "waiting_ssh" || h.agent_status === "installing") && (
                <Button variant="secondary" isDisabled isLoading>
                  Installing...
                </Button>
              )}
              {h.state === "active" && h.agent_status === "connected" && (
                <>
                  <Button variant="secondary" isLoading={updating === h.id} isDisabled={updating === h.id} onClick={async (e) => {
                    const force = e.shiftKey;
                    const msg = force
                      ? "FORCE update troshkad? This will kill any running jobs."
                      : "Update troshkad on this host? (Shift+click for force update)";
                    if (!window.confirm(msg)) return;
                    setUpdating(h.id);
                    try {
                      const resp = await fetch(`/api/v1/hosts/${h.id}/update-agent?force=${force}`, { method: "POST" });
                      if (!resp.ok) {
                        const data = await resp.json();
                        alert(data.detail || "Update failed");
                        return;
                      }
                      const data = await resp.json();
                      const targetVersion = data.version;
                      const oldVersion = h.agent_version;
                      if (targetVersion && targetVersion === oldVersion) {
                        alert(`Already up to date (v${oldVersion})`);
                        return;
                      }
                      // Poll: wait for agent to restart (version changes or health recovers)
                      let sawDown = false;
                      for (let i = 0; i < 60; i++) {
                        await new Promise(r => setTimeout(r, 3000));
                        const hostsResp = await fetch("/api/v1/hosts/");
                        if (hostsResp.ok) {
                          const hostsList = await hostsResp.json();
                          const updated = hostsList.find((x: Host) => x.id === h.id);
                          if (!updated || updated.agent_status === "disconnected") {
                            sawDown = true;
                          } else if (sawDown && updated.agent_status === "connected") {
                            alert(`Updated → v${updated.agent_version}`);
                            loadData();
                            return;
                          } else if (updated.agent_version && updated.agent_version !== oldVersion) {
                            alert(`Updated → v${updated.agent_version}`);
                            loadData();
                            return;
                          }
                        }
                      }
                      alert("Update timed out — check host status");
                    } finally {
                      setUpdating(null);
                    }
                  }}>
                    {updating === h.id ? "Updating..." : "Update Agent"}
                  </Button>
                  <Button variant="secondary" isLoading={installing === h.id} isDisabled={installing === h.id} onClick={() => {
                    if (!window.confirm("Reinstall the agent on this host? This re-runs the full install script via SSH.")) return;
                    installAgent(h.id);
                  }}>
                    {installing === h.id ? "Reinstalling..." : "Reinstall Agent"}
                  </Button>
                  <Button variant="secondary" onClick={async () => {
                    if (!window.confirm(`Run garbage collection on ${h.instance_id}? This will sync capacity, clean orphans, and repair networks.`)) return;
                    const resp = await fetch(`/api/v1/hosts/${h.id}/gc`, { method: "POST" });
                    if (resp.ok) {
                      const report = await resp.json();
                      const cap = report.capacity || {};
                      const orphans = report.orphans_found || 0;
                      const cleaned = report.cleanup?.cleaned || 0;
                      const parts = [];
                      if (cap.changed) parts.push(`Capacity synced: ${cap.old?.used_vcpus}→${cap.new?.used_vcpus} vCPUs, ${cap.old?.used_ram_mb}→${cap.new?.used_ram_mb} MB RAM`);
                      else parts.push("Capacity: already in sync");
                      if (orphans > 0) parts.push(`Orphans found: ${orphans}, cleaned: ${cleaned}`);
                      else parts.push("No orphans found");
                      const cacheOrphans = report.orphans?.orphaned_cache?.length || 0;
                      const staleCache = report.orphans?.stale_cache?.length || 0;
                      if (cacheOrphans > 0 || staleCache > 0) parts.push(`Cache cleaned: ${cacheOrphans} orphaned, ${staleCache} stale`);
                      const repaired = report.network_repair?.repaired || 0;
                      if (repaired > 0) parts.push(`Network bridges repaired: ${repaired}`);
                      else parts.push("Network bridges: OK");
                      alert(parts.join("\n"));
                      loadData();
                    } else {
                      alert("GC failed — check server logs");
                    }
                  }}>
                    Clean
                  </Button>
                  <Button variant="danger" isLoading={updating === `wipe-${h.id}`} isDisabled={updating === `wipe-${h.id}`} onClick={async () => {
                    if (!window.confirm("WIPE HOST: This will destroy ALL projects and clean up everything on this host. Are you sure?")) return;
                    if (!window.confirm("FINAL WARNING: All VMs will be destroyed and all projects reset to draft. Continue?")) return;
                    setUpdating(`wipe-${h.id}`);
                    try {
                      const resp = await fetch(`/api/v1/hosts/${h.id}/wipe`, { method: "POST" });
                      if (resp.ok) {
                        const data = await resp.json();
                        alert(`Wiped: ${data.projects_destroyed} destroyed, ${data.projects_reset} reset, ${data.nft_reset?.flushed_chains || 0} nft chains flushed`);
                        loadData();
                      } else {
                        alert("Wipe failed — check server logs");
                      }
                    } finally {
                      setUpdating(null);
                    }
                  }}>
                    {updating === `wipe-${h.id}` ? "Wiping..." : "Wipe Host"}
                  </Button>
                </>
              )}
              {h.state === "active" && (
                <Button variant="secondary" onClick={() => {
                  const msg = h.used_vcpus > 0
                    ? `Power off ${h.instance_id}? This host has ${h.used_vcpus} vCPUs allocated — projects will be unavailable until powered back on.`
                    : `Power off ${h.instance_id}?`;
                  if (!window.confirm(msg)) return;
                  powerHost(h.id, "poweroff");
                }} isDisabled={poweringHost === h.id} isLoading={poweringHost === h.id}>
                  Power Off
                </Button>
              )}
              {h.state === "stopped" && (() => {
                const willResize = resizeType[h.id] && resizeType[h.id] !== h.instance_type;
                return (
                  <Button variant="secondary" onClick={() => {
                    const msg = willResize
                      ? `Resize ${h.instance_id} from ${h.instance_type} → ${resizeType[h.id]} and power on?`
                      : `Power on ${h.instance_id}?`;
                    if (!window.confirm(msg)) return;
                    powerHost(h.id, "poweron", resizeType[h.id] || undefined);
                  }} isLoading={poweringHost === h.id} isDisabled={poweringHost === h.id}>
                    {willResize ? "Resize & Power On" : "Power On"}
                  </Button>
                );
              })()}
              {h.state === "starting" && (
                <Button variant="secondary" isDisabled isLoading>
                  Starting...
                </Button>
              )}
              <Button variant="danger" onClick={() => removeHost(h.id, h.instance_id)} isDisabled={removing === h.id || h.state === "shutting_down"} isLoading={removing === h.id || h.state === "shutting_down"}>
                {(removing === h.id || h.state === "shutting_down") ? "Terminating..." : "Remove"}
              </Button>
            </CardBody>
            {showKeyFor === h.id && keyData[h.id] && (
              <CardBody style={{ borderTop: "1px solid var(--pf-t--global--border--color--default)" }}>
                <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                  {keyData[h.id].ssh_command && (
                    <div>
                      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 4 }}>
                        <label style={{ fontSize: 11, fontWeight: 600 }}>SSH Command</label>
                        <Button variant="plain" onClick={() => copyToClipboard(keyData[h.id].ssh_command!, "SSH command")} style={{ padding: "2px 6px", fontSize: 11 }}>Copy</Button>
                      </div>
                      <code style={{ fontSize: 11, display: "block", padding: 6, background: "rgba(0,0,0,0.2)", borderRadius: 4 }}>{keyData[h.id].ssh_command}</code>
                    </div>
                  )}
                  <div>
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 4 }}>
                      <label style={{ fontSize: 11, fontWeight: 600 }}>Private Key ({keyData[h.id].key_pair_name})</label>
                      <Button variant="plain" onClick={() => copyToClipboard(keyData[h.id].private_key, "Private key")} style={{ padding: "2px 6px", fontSize: 11 }}>Copy</Button>
                    </div>
                    <pre style={{ fontSize: 10, padding: 6, background: "rgba(0,0,0,0.2)", borderRadius: 4, maxHeight: 120, overflowY: "auto", margin: 0, whiteSpace: "pre-wrap", wordBreak: "break-all" }}>{keyData[h.id].private_key}</pre>
                  </div>
                  {keyData[h.id].public_key && (
                    <div>
                      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 4 }}>
                        <label style={{ fontSize: 11, fontWeight: 600 }}>Public Key</label>
                        <Button variant="plain" onClick={() => copyToClipboard(keyData[h.id].public_key!, "Public key")} style={{ padding: "2px 6px", fontSize: 11 }}>Copy</Button>
                      </div>
                      <code style={{ fontSize: 11, display: "block", padding: 6, background: "rgba(0,0,0,0.2)", borderRadius: 4, wordBreak: "break-all" }}>{keyData[h.id].public_key}</code>
                    </div>
                  )}
                </div>
              </CardBody>
            )}
            {installOutput[h.id] && (
              <CardBody style={{ borderTop: "1px solid var(--pf-t--global--border--color--default)", fontSize: 12, fontFamily: "monospace", whiteSpace: "pre-wrap", maxHeight: 150, overflowY: "auto" }}>
                {installOutput[h.id]}
              </CardBody>
            )}
          </Card>
        ))}
      </PageSection>
    </>
  );
}
