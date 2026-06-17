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
  Tooltip,
  Switch,
  Label,
} from "@patternfly/react-core";
import { ExclamationTriangleIcon, ExclamationCircleIcon } from "@patternfly/react-icons";

interface Host {
  id: string;
  provider_id: string | null;
  instance_id: string | null;
  instance_type: string | null;
  region: string | null;
  state: string;
  host_type: string;
  total_vcpus: number;
  total_ram_mb: number;
  used_vcpus: number;
  used_ram_mb: number;
  running_vms: number;
  total_vms: number;
  running_projects: number;
  total_projects: number;
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
  storage_pool_id: string | null;
  storage_warnings?: Array<{mount: string; used_pct: number; level: string}> | null;
  auto_extend_enabled: boolean;
  auto_extend_threshold_pct: number;
  auto_extend_increment_gb: number;
  auto_extend_max_gb: number | null;
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
  const [providers, setProviders] = useState<Array<{id: string; name: string; type: string; default_region: string; default_image: string | null; gcp_zone?: string; azure_location?: string}>>([]);
  const [provisioning, setProvisioning] = useState(false);
  const [showAddForm, setShowAddForm] = useState(false);
  const [newProviderId, setNewProviderId] = useState("");
  const [newInstanceType, setNewInstanceType] = useState("m8i.xlarge");
  const [newRegion, setNewRegion] = useState("");
  const [error, setError] = useState("");
  const [filterRegion, setFilterRegion] = useState("");
  const [filterProvider, setFilterProvider] = useState("");
  const [filterProviderType, setFilterProviderType] = useState("");
  const [filterText, setFilterText] = useState("");
  const [cpuRatio, setCpuRatio] = useState(4.0);
  const [ramRatio, setRamRatio] = useState(1.5);
  const [selectedHosts, setSelectedHosts] = useState<Set<string>>(new Set());

  const [storageInfo, setStorageInfo] = useState<Record<string, { used_pct: number; free_gb: number; total_gb: number }>>({});
  const [pools, setPools] = useState<{id: string; name: string; mode: string; az: string | null; status: string; provider_id: string; worker_host_id: string | null; worker_instance_type: string | null}[]>([]);
  const [selectedPool, setSelectedPool] = useState("");
  const [newCpuCores, setNewCpuCores] = useState(64);
  const [newMemoryGb, setNewMemoryGb] = useState(256);

  const loadData = () => {
    Promise.all([
      fetch("/api/v1/hosts/").then((r) => r.ok ? r.json() : []),
      fetch("/api/v1/hosts/summary").then((r) => r.ok ? r.json() : []),
      fetch("/api/v1/providers/").then((r) => r.ok ? r.json() : []),
      fetch("/api/v1/storage-pools").then((r) => r.ok ? r.json() : []),
    ]).then(([h, s, p, pools]) => {
      setHosts(Array.isArray(h) ? h : []);
      setSummary(Array.isArray(s) ? s : []);
      setProviders(Array.isArray(p) ? p : []);
      setPools(Array.isArray(pools) ? pools : []);
      setLoading(false);
    }).catch(() => setLoading(false));
    fetch("/api/v1/hosts/expected-agent-version").then((r) => r.ok ? r.json() : {}).then((d: { version?: string }) => setExpectedVersion(d.version || ""));
    // Storage fetched separately — SSH calls can be slow and shouldn't block the page
    fetch("/api/v1/hosts/storage").then((r) => r.ok ? r.json() : {}).then((d) => {
      if (d && typeof d === "object") {
        const mapped: Record<string, { used_pct: number; free_gb: number; total_gb: number }> = {};
        for (const [id, info] of Object.entries(d) as [string, any][]) {
          if (info.partitions) {
            const p = info.partitions.find((p: any) => p.mount.includes("troshka")) || info.partitions[0];
            if (p) mapped[id] = { used_pct: p.used_pct, free_gb: Math.round(p.free_bytes / (1024**3) * 10) / 10, total_gb: Math.round(p.total_bytes / (1024**3) * 10) / 10 };
          } else if (info.used_pct !== undefined) {
            mapped[id] = info;
          }
        }
        setStorageInfo(mapped);
      }
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

  const [pendingHosts, setPendingHosts] = useState<Array<{ id: string; instance_type: string; region: string }>>([]);

  const addHost = async () => {
    if (!newProviderId) { setError("Select a provider"); return; }
    setError("");
    const isOcpVirt = selectedProvider?.type === "ocpvirt";
    const instanceType = isOcpVirt ? `${newCpuCores}c-${newMemoryGb}g` : newInstanceType;
    const placeholderId = `pending-${Date.now()}`;
    const placeholder = { id: placeholderId, instance_type: instanceType, region: isOcpVirt ? "ocp-virt" : (newRegion || selectedProvider?.default_region || "") };
    setPendingHosts((prev) => [...prev, placeholder]);
    try {
      const body: Record<string, any> = {
        provider_id: newProviderId,
        instance_type: instanceType,
        storage_pool_id: selectedPool || undefined,
      };
      if (!isOcpVirt) body.region = newRegion || undefined;
      const resp = await fetch("/api/v1/hosts/", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
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
    setPendingHosts((prev) => prev.filter((p) => p.id !== placeholderId));
  };

  const instanceTypesByProvider: Record<string, Array<{value: string; label: string}>> = {
    ec2: [
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
    ],
    gcp: [
      { value: "n2-highmem-8",  label: "n2-highmem-8 — 8 vCPU / 64 GB — ~$0.52/hr" },
      { value: "n2-highmem-16", label: "n2-highmem-16 — 16 vCPU / 128 GB — ~$1.04/hr" },
      { value: "n2-highmem-32", label: "n2-highmem-32 — 32 vCPU / 256 GB — ~$2.08/hr" },
      { value: "n2-highmem-48", label: "n2-highmem-48 — 48 vCPU / 384 GB — ~$3.12/hr" },
      { value: "n2-highmem-64", label: "n2-highmem-64 — 64 vCPU / 512 GB — ~$4.16/hr" },
      { value: "n2-highmem-96", label: "n2-highmem-96 — 96 vCPU / 768 GB — ~$6.24/hr" },
    ],
    azure: [
      { value: "Standard_E8s_v5",  label: "Standard_E8s_v5 — 8 vCPU / 64 GB — ~$0.50/hr" },
      { value: "Standard_E16s_v5", label: "Standard_E16s_v5 — 16 vCPU / 128 GB — ~$1.01/hr" },
      { value: "Standard_E32s_v5", label: "Standard_E32s_v5 — 32 vCPU / 256 GB — ~$2.02/hr" },
      { value: "Standard_E48s_v5", label: "Standard_E48s_v5 — 48 vCPU / 384 GB — ~$3.02/hr" },
      { value: "Standard_E64s_v5", label: "Standard_E64s_v5 — 64 vCPU / 512 GB — ~$4.03/hr" },
      { value: "Standard_E96s_v5", label: "Standard_E96s_v5 — 96 vCPU / 672 GB — ~$6.05/hr" },
    ],
  };
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
    let msg = `Remove host ${instanceId || hostId}? This will terminate the cloud instance.`;
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
  const instanceTypes = instanceTypesByProvider[selectedProvider?.type || "ec2"] || instanceTypesByProvider.ec2;
  const selectedProviderHasImage = selectedProvider ? !!selectedProvider.default_image : false;
  const selectedProviderHasVpc = selectedProvider ? !!selectedProvider.vpc_id : false;
  const selectedProviderReady = selectedProvider?.type === "ocpvirt" || selectedProvider?.type === "gcp" || selectedProvider?.type === "azure" ? true : (selectedProviderHasImage && selectedProviderHasVpc);
  const isOcpVirtHost = (h: Host) => h.instance_type ? /^\d+c-\d+g$/.test(h.instance_type) : false;
  const providerTypeById = Object.fromEntries(providers.map((p) => [p.id, p.type]));
  const filteredHosts = hosts.filter((h) => {
    if (filterRegion && (h.region || "unknown") !== filterRegion) return false;
    if (filterProvider && h.provider_id !== filterProvider) return false;
    if (filterProviderType && providerTypeById[h.provider_id || ""] !== filterProviderType) return false;
    if (filterText) {
      const q = filterText.toLowerCase();
      const haystack = [h.id, h.instance_id, h.instance_type, h.ip_address, h.region, h.host_type, h.agent_status].filter(Boolean).join(" ").toLowerCase();
      if (!haystack.includes(q)) return false;
    }
    return true;
  }).slice().sort((a, b) => (a.storage_pool_id || "").localeCompare(b.storage_pool_id || "") || a.id.localeCompare(b.id));

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
  const [keyData, setKeyData] = useState<Record<string, { key_pair_name: string; private_key: string; ssh_command: string | null; ssh_script_command?: string; public_key?: string }>>({});

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

  const copyToClipboard = (text: string, _label: string) => {
    navigator.clipboard.writeText(text);
  };

  const [resizeType, setResizeType] = useState<Record<string, string>>({});

  const [poweringHosts, setPoweringHosts] = useState<Set<string>>(new Set());

  const powerHost = async (hostId: string, action: "poweroff" | "poweron", instanceType?: string) => {
    setPoweringHosts((prev) => new Set(prev).add(hostId));
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
    setPoweringHosts((prev) => { const next = new Set(prev); next.delete(hostId); return next; });
  };

  const [installing, setInstalling] = useState<string | null>(null);
  const [updating, setUpdating] = useState<string | null>(null);
  const [expectedVersion, setExpectedVersion] = useState("");
  const [installOutput, setInstallOutput] = useState<Record<string, string>>({});
  const [expandedAutoExtend, setExpandedAutoExtend] = useState<Record<string, boolean>>({});
  const [extending, setExtending] = useState<Record<string, boolean>>({});
  const [resizeTarget, setResizeTarget] = useState<Record<string, string>>({});
  const [resizing, setResizing] = useState<Record<string, boolean>>({});

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

  const handleEvacuate = async (hostId: string) => {
    if (!window.confirm("Evacuate all projects from this host? They will be migrated to other hosts in the same pool.")) return;
    setError("");
    const resp = await fetch(`/api/v1/hosts/${hostId}/evacuate`, { method: "POST" });
    if (resp.ok) {
      loadData();
    } else {
      const data = await resp.json();
      setError(data.detail || "Failed to evacuate host");
    }
  };

  const updateAutoExtend = async (
    hostId: string,
    field: string,
    value: boolean | number | null
  ) => {
    const resp = await fetch(`/api/v1/hosts/${hostId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ [field]: value }),
    });
    if (resp.ok) {
      loadData();
    } else {
      const data = await resp.json();
      setError(data.detail || "Failed to update auto-extend settings");
    }
  };

  const handleExtendStorage = async (host: Host) => {
    if (!window.confirm(`Extend storage for host "${host.instance_id || host.id.slice(0, 8)}"? This will increase EBS volume capacity by ${host.auto_extend_increment_gb} GB.`)) return;
    setExtending({ ...extending, [host.id]: true });
    const resp = await fetch(`/api/v1/hosts/${host.id}/extend-storage`, {
      method: "POST",
    });
    setExtending({ ...extending, [host.id]: false });
    if (resp.ok) {
      loadData();
    } else {
      const data = await resp.json();
      setError(data.detail || "Failed to extend storage");
    }
  };

  const handleResizeStorage = async (host: Host) => {
    const sizeGb = parseInt(resizeTarget[host.id]);
    if (!sizeGb || sizeGb <= host.storage_size_gb) {
      setError(`New size must be larger than current (${host.storage_size_gb} GB)`);
      return;
    }
    if (!window.confirm(`Resize storage for host "${host.instance_id || host.id.slice(0, 8)}" to ${sizeGb} GB? (currently ${host.storage_size_gb} GB)`)) return;
    setResizing({ ...resizing, [host.id]: true });
    try {
      const resp = await fetch(`/api/v1/hosts/${host.id}/resize-storage`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ size_gb: sizeGb }),
      });
      if (resp.ok) {
        const data = await resp.json();
        setResizeTarget({ ...resizeTarget, [host.id]: "" });
        alert(`Storage resized: ${data.old_size_gb} GB → ${data.new_size_gb} GB`);
        loadData();
      } else {
        const data = await resp.json();
        setError(data.detail || "Failed to resize storage");
      }
    } finally {
      setResizing({ ...resizing, [host.id]: false });
    }
  };

  if (loading) {
    return <PageSection><Title headingLevel="h1">Loading...</Title></PageSection>;
  }

  const inputStyle = {
    width: "100%",
    padding: "6px 10px",
    borderRadius: 6,
    border: "1px solid var(--pf-t--global--border--color--default)",
    background: "var(--pf-t--global--background--color--primary--default)",
    color: "var(--pf-t--global--text--color--regular)",
    fontSize: 13,
  };

  return (
    <>
      <PageSection>
        <Toolbar>
          <ToolbarContent>
            <ToolbarItem>
              <Title headingLevel="h1">Host Pool</Title>
            </ToolbarItem>
            <ToolbarItem>
              <input
                type="text"
                placeholder="Search hosts..."
                value={filterText}
                onChange={(e) => setFilterText(e.target.value)}
                style={{ ...inputStyle, width: "auto", minWidth: 180 }}
              />
            </ToolbarItem>
            <ToolbarItem>
              <select value={filterProviderType} onChange={(e) => setFilterProviderType(e.target.value)} style={{ ...inputStyle, width: "auto" }}>
                <option value="">All types</option>
                {[...new Set(providers.filter((p) => p.type === "ec2" || p.type === "ocpvirt" || p.type === "gcp" || p.type === "azure").map((p) => p.type))].map((t) => (
                  <option key={t} value={t}>{t}</option>
                ))}
              </select>
            </ToolbarItem>
            <ToolbarItem>
              <select value={filterProvider} onChange={(e) => setFilterProvider(e.target.value)} style={{ ...inputStyle, width: "auto" }}>
                <option value="">All providers</option>
                {providers.filter((p) => p.type === "ec2" || p.type === "ocpvirt" || p.type === "gcp" || p.type === "azure").map((p) => (
                  <option key={p.id} value={p.id}>{p.name} ({p.type})</option>
                ))}
              </select>
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
                      if (p) setNewRegion(p.default_region || p.gcp_zone || p.azure_location || "");
                      if (p) {
                        const types = instanceTypesByProvider[p.type] || instanceTypesByProvider.ec2;
                        setNewInstanceType(types[0]?.value || "");
                        const matchingPool = pools.find((pl) => pl.status === "available" && pl.provider_id === e.target.value && (p.type !== "ocpvirt" || pl.mode !== "shared-fsx"));
                        setSelectedPool(matchingPool?.id || "");
                      }
                    }}
                  >
                    <option value="">Select provider...</option>
                    {providers.filter((p) => p.type === "ec2" || p.type === "ocpvirt" || p.type === "gcp" || p.type === "azure").map((p) => (
                      <option key={p.id} value={p.id}>{p.name}</option>
                    ))}
                  </select>
                </div>
                {newProviderId && !selectedProviderReady && selectedProvider && selectedProvider.type === "ec2" && (
                  <Alert variant="warning" title={
                    !selectedProviderHasImage && !selectedProviderHasVpc
                      ? "Provider needs setup. Go to Providers and select an image and Setup VPC."
                      : !selectedProviderHasImage
                        ? "No image set. Go to Providers and select an image."
                        : "No VPC set. Go to Providers and click Setup VPC."
                  } style={{ width: "100%", flexBasis: "100%" }} />
                )}
                {!newProviderId ? null : selectedProvider?.type === "ocpvirt" ? (
                  <>
                    <div style={{ minWidth: 120 }}>
                      <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>CPU Cores</label>
                      <input
                        style={{ width: "100%", padding: "6px 10px", borderRadius: 6, border: "1px solid var(--pf-t--global--border--color--default)", background: "var(--pf-t--global--background--color--primary--default)", color: "var(--pf-t--global--text--color--regular)", fontSize: 13 }}
                        type="number"
                        value={newCpuCores}
                        onChange={(e) => setNewCpuCores(parseInt(e.target.value) || 1)}
                        min={1}
                      />
                    </div>
                    <div style={{ minWidth: 120 }}>
                      <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Memory GB</label>
                      <input
                        style={{ width: "100%", padding: "6px 10px", borderRadius: 6, border: "1px solid var(--pf-t--global--border--color--default)", background: "var(--pf-t--global--background--color--primary--default)", color: "var(--pf-t--global--text--color--regular)", fontSize: 13 }}
                        type="number"
                        value={newMemoryGb}
                        onChange={(e) => setNewMemoryGb(parseInt(e.target.value) || 1)}
                        min={1}
                      />
                    </div>
                    <div style={{ display: "flex", gap: 6, alignItems: "flex-end" }}>
                      <Button variant="tertiary" size="sm" onClick={() => { setNewCpuCores(8); setNewMemoryGb(32); }}>8c / 32G</Button>
                      <Button variant="tertiary" size="sm" onClick={() => { setNewCpuCores(64); setNewMemoryGb(256); }}>64c / 256G</Button>
                      <Button variant="tertiary" size="sm" onClick={() => { setNewCpuCores(128); setNewMemoryGb(512); }}>128c / 512G</Button>
                    </div>
                  </>
                ) : (
                  <>
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
                    {selectedProvider?.type === "ec2" ? (
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
                    ) : (
                      <div style={{ minWidth: 150 }}>
                        <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Zone</label>
                        <input
                          style={{ width: "100%", padding: "6px 10px", borderRadius: 6, border: "1px solid var(--pf-t--global--border--color--default)", background: "var(--pf-t--global--background--color--primary--default)", color: "var(--pf-t--global--text--color--regular)", fontSize: 13 }}
                          value={newRegion}
                          disabled
                        />
                      </div>
                    )}
                  </>
                )}
                {newProviderId && <div style={{ minWidth: 220 }}>
                  <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Storage Pool</label>
                  <select
                    style={{ width: "100%", padding: "6px 10px", borderRadius: 6, border: "1px solid var(--pf-t--global--border--color--default)", background: "var(--pf-t--global--background--color--primary--default)", color: "var(--pf-t--global--text--color--regular)", fontSize: 13 }}
                    value={selectedPool}
                    onChange={(e) => setSelectedPool(e.target.value)}
                  >
                    <option value="">None (local storage)</option>
                    {pools.filter(p => p.status === "available" && (selectedProvider?.type !== "ocpvirt" || p.mode !== "shared-fsx")).map((p) => (
                      <option key={p.id} value={p.id}>{p.name} ({p.mode}{p.az ? `, ${p.az}` : ""})</option>
                    ))}
                  </select>
                </div>}
                {newProviderId && <Button variant="primary" onClick={addHost} isDisabled={!selectedProviderReady}>
                  Provision Host
                </Button>}
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
                {(() => {
                  const allocVcpus = (s as unknown as Record<string, number>).alloc_vcpus || s.total_vcpus;
                  const allocRamMb = (s as unknown as Record<string, number>).alloc_ram_mb || s.total_ram_mb;
                  const cpuPct = allocVcpus ? (s.used_vcpus / allocVcpus) * 100 : 0;
                  const ramPct = allocRamMb ? (s.used_ram_mb / allocRamMb) * 100 : 0;
                  return (<>
                    <div style={{ fontSize: 11, marginTop: 8 }}>
                      vCPU: {s.used_vcpus}/{allocVcpus} <span style={{ opacity: 0.4 }}>({s.total_vcpus} phys)</span>
                    </div>
                    <div style={{ height: 4, background: "rgba(255,255,255,0.1)", borderRadius: 2, marginTop: 2 }}>
                      <div style={{ height: 4, borderRadius: 2, width: `${cpuPct}%`, background: cpuPct > 80 ? "#f87171" : "#4ade80" }} />
                    </div>
                    <div style={{ fontSize: 11, marginTop: 6 }}>
                      RAM: {Math.round(s.used_ram_mb / 1024)}/{Math.round(allocRamMb / 1024)} GB <span style={{ opacity: 0.4 }}>({Math.round(s.total_ram_mb / 1024)} phys)</span>
                    </div>
                    <div style={{ height: 4, background: "rgba(255,255,255,0.1)", borderRadius: 2, marginTop: 2 }}>
                      <div style={{ height: 4, borderRadius: 2, width: `${ramPct}%`, background: ramPct > 80 ? "#f87171" : "#4ade80" }} />
                    </div>
                  </>);
                })()}
              </CardBody>
            </Card>
          ))}
        </div>
      </PageSection>

      {/* Host List */}
      <PageSection>
        {filteredHosts.length > 0 && (() => {
          const selected = filteredHosts.filter((h) => selectedHosts.has(h.id));
          const allSelected = selected.length === filteredHosts.length && filteredHosts.length > 0;
          const someSelected = selected.length > 0;
          const allActive = someSelected && selected.every((h) => h.state === "active");
          const allConnected = someSelected && selected.every((h) => h.agent_status === "connected");
          const allStopped = someSelected && selected.every((h) => h.state === "stopped");
          const allActiveConnected = allActive && allConnected;
          return (
            <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 8, flexWrap: "wrap" }}>
              <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 13, cursor: "pointer" }}>
                <input
                  type="checkbox"
                  checked={allSelected}
                  onChange={() => {
                    if (allSelected) setSelectedHosts(new Set());
                    else setSelectedHosts(new Set(filteredHosts.map((h) => h.id)));
                  }}
                />
                {someSelected ? `${selected.length} of ${filteredHosts.length} selected` : "Select all"}
              </label>
              {someSelected && (
                <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                  {allActiveConnected && (
                    <>
                      <Button variant="secondary" size="sm" onClick={async () => {
                        if (!window.confirm(`Update agent on ${selected.length} host(s)?`)) return;
                        for (const h of selected) {
                          fetch(`/api/v1/hosts/${h.id}/update-agent`, { method: "POST" });
                        }
                        setSelectedHosts(new Set());
                        loadData();
                      }}>Update Agent ({selected.length})</Button>
                      <Button variant="secondary" size="sm" onClick={async () => {
                        if (!window.confirm(`Run GC on ${selected.length} host(s)?`)) return;
                        for (const h of selected) {
                          fetch(`/api/v1/hosts/${h.id}/gc`, { method: "POST" });
                        }
                        setSelectedHosts(new Set());
                        loadData();
                      }}>Clean ({selected.length})</Button>
                    </>
                  )}
                  {allActive && (
                    <Button variant="secondary" size="sm" onClick={async () => {
                      if (!window.confirm(`Power off ${selected.length} host(s)?`)) return;
                      for (const h of selected) {
                        fetch(`/api/v1/hosts/${h.id}/poweroff`, { method: "POST" });
                      }
                      setSelectedHosts(new Set());
                      loadData();
                    }}>Power Off ({selected.length})</Button>
                  )}
                  {allStopped && (
                    <Button variant="secondary" size="sm" onClick={async () => {
                      if (!window.confirm(`Power on ${selected.length} host(s)?`)) return;
                      for (const h of selected) {
                        fetch(`/api/v1/hosts/${h.id}/poweron`, { method: "POST" });
                      }
                      setSelectedHosts(new Set());
                      loadData();
                    }}>Power On ({selected.length})</Button>
                  )}
                  <Button variant="danger" size="sm" onClick={async () => {
                    if (!window.confirm(`Remove ${selected.length} host(s)? This will terminate all EC2 instances.`)) return;
                    for (const h of selected) {
                      fetch(`/api/v1/hosts/${h.id}`, { method: "DELETE" });
                    }
                    setSelectedHosts(new Set());
                    loadData();
                  }}>Remove ({selected.length})</Button>
                </div>
              )}
            </div>
          );
        })()}
        {pendingHosts.map((ph) => (
          <Card key={ph.id} style={{ marginBottom: 8, opacity: 0.6 }}>
            <CardBody style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <div style={{ flex: 1 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <strong>provisioning...</strong>
                  <span style={{ fontSize: 11, padding: "1px 6px", borderRadius: 4, background: "#fbbf2422", color: "#fbbf24" }}>
                    provisioning
                  </span>
                </div>
                <div style={{ fontSize: 12, opacity: 0.6, marginTop: 4 }}>
                  {ph.instance_type} · {ph.region} · provisioning...
                </div>
              </div>
              <Button variant="plain" isDisabled isLoading>Provisioning...</Button>
            </CardBody>
          </Card>
        ))}
        {filteredHosts.length === 0 && pendingHosts.length === 0 && (
          <p style={{ opacity: 0.6 }}>No hosts{filterRegion ? ` in ${filterRegion}` : ""}. Click &quot;+ Add Host&quot; to provision one.</p>
        )}
        {filteredHosts.map((h) => (
          <Card key={h.id} style={{ marginBottom: 8 }}>
            {/* Row 1: Host info + stats */}
            <CardBody style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
              <input
                type="checkbox"
                checked={selectedHosts.has(h.id)}
                onChange={() => setSelectedHosts((prev) => {
                  const next = new Set(prev);
                  if (next.has(h.id)) next.delete(h.id); else next.add(h.id);
                  return next;
                })}
                style={{ width: 18, height: 18, minWidth: 18, marginRight: 8, cursor: "pointer", marginTop: 2 }}
              />
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
                  {(() => {
                    const localWarnings = (h.storage_warnings || []).filter((w: any) =>
                      !h.storage_pool_id || !w.mount.includes("/shared")
                    );
                    return localWarnings.length > 0 && (
                    <Tooltip
                      content={
                        <div>
                          {localWarnings.map((w: any, i: number) => (
                            <div key={i}>
                              {w.mount}: {w.used_pct}% used ({w.level})
                            </div>
                          ))}
                        </div>
                      }
                    >
                      {localWarnings.some((w: any) => w.level === "critical") ? (
                        <ExclamationCircleIcon style={{ color: "var(--pf-t--global--color--status--danger--default)", marginLeft: 8 }} />
                      ) : (
                        <ExclamationTriangleIcon style={{ color: "var(--pf-t--global--color--status--warning--default)", marginLeft: 8 }} />
                      )}
                    </Tooltip>
                  ); })()}
                  {h.agent_status === "connected" && h.last_health_at && (
                    <span style={{ fontSize: 11, opacity: 0.5 }}>
                      health: {Math.round((Date.now() - new Date(h.last_health_at).getTime()) / 1000)}s ago
                    </span>
                  )}
                </div>
                <div style={{ fontSize: 12, opacity: 0.6, marginTop: 4, display: "flex", alignItems: "center", gap: 6 }}>
                  {h.state === "stopped" && !isOcpVirtHost(h) ? (
                    <select
                      style={{ padding: "2px 6px", borderRadius: 4, border: "1px solid var(--pf-t--global--border--color--default)", background: "var(--pf-t--global--background--color--primary--default)", color: "var(--pf-t--global--text--color--regular)", fontSize: 12 }}
                      value={resizeType[h.id] || h.instance_type || ""}
                      onChange={(e) => setResizeType((prev) => ({ ...prev, [h.id]: e.target.value }))}
                    >
                      {instanceTypes.map((t) => (
                        <option key={t.value} value={t.value}>{t.label}</option>
                      ))}
                    </select>
                  ) : (
                    <span>{h.instance_type}</span>
                  )}
                  <span>{(() => { const prov = providers.find(p => p.id === h.provider_id); return prov ? `· ${prov.name} (${prov.type})` : ""; })()} · {h.ip_address || "no IP"}{isOcpVirtHost(h) ? ":22000" : ""} {h.host_type === "pattern_buffer" ? <> · <Label color="purple" isCompact>pattern buffer</Label></> : ""}</span>
                </div>
              </div>
              <div style={{ display: "flex", gap: 24, fontSize: 13 }}>
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
                {(() => { const si = storageInfo[h.id]; const optimizing = si && (h.storage_size_gb - Math.round(si.total_gb)) > Math.round(h.storage_size_gb * 0.05); return (
                <div style={{ textAlign: "center", color: si && si.used_pct >= 80 ? "#f87171" : undefined }}>
                  <div style={{ fontSize: 10, opacity: 0.5, marginBottom: 2 }}>Storage</div>
                  <div>{si ? <><strong>{si.used_pct}%</strong> of {Math.round(si.total_gb)} GB</> : <span style={{ opacity: 0.4 }}>{h.storage_size_gb} GB</span>}</div>
                  {si && <div style={{ fontSize: 10, opacity: 0.4 }}>{Math.round(si.free_gb)} GB free</div>}
                  {optimizing && <div style={{ fontSize: 10, color: "#facc15" }}>Optimizing → {h.storage_size_gb} GB</div>}
                </div>); })()}
                <div style={{ textAlign: "center" }}>
                  <div style={{ fontSize: 10, opacity: 0.5, marginBottom: 2 }}>VMs</div>
                  <div><strong>{h.running_vms || 0}</strong>/{h.total_vms || 0}</div>
                </div>
                <div style={{ textAlign: "center" }}>
                  <div style={{ fontSize: 10, opacity: 0.5, marginBottom: 2 }}>Projects</div>
                  <div><strong>{h.running_projects || 0}</strong>/{h.total_projects || 0}</div>
                </div>
                {h.max_eips > 0 && (
                  <div style={{ textAlign: "center" }}>
                    <div style={{ fontSize: 10, opacity: 0.5, marginBottom: 2 }}>EIPs</div>
                    <div><strong>{h.used_eips || 0}</strong>/{h.max_eips}</div>
                  </div>
                )}
              </div>
            </CardBody>
            {/* Row 2: Action buttons */}
            <CardBody style={{ borderTop: "1px solid var(--pf-t--global--border--color--default)", display: "flex", gap: 8, flexWrap: "wrap", paddingTop: 8, paddingBottom: 8 }}>
              <Button variant="secondary" onClick={() => showKeyPair(h.id)}>
                {showKeyFor === h.id ? "Hide Access Info" : "Host Access Info"}
              </Button>
              {(() => {
                const hostBusy = installing === h.id || updating === h.id || h.agent_status === "waiting_ssh" || h.agent_status === "installing";
                return (<>
              {h.state === "active" && (h.agent_status === "disconnected" || h.agent_status === "install_failed") && (
                <Button variant="secondary" onClick={() => {
                  if (!window.confirm(`Install agent on ${h.instance_id}? This will SSH into the host and run the install script.`)) return;
                  installAgent(h.id);
                }} isLoading={installing === h.id} isDisabled={hostBusy}>
                  {h.agent_status === "install_failed" ? "Retry Install" : "Install Agent"}
                </Button>
              )}
              {(h.agent_status === "waiting_ssh" || h.agent_status === "installing") && (
                <Button variant="secondary" isDisabled isLoading>
                  Installing...
                </Button>
              )}
              {h.state === "active" && h.agent_status === "connected" && h.storage_pool_id && h.host_type !== "pattern_buffer" && (
                <Button variant="secondary" isDisabled={hostBusy} onClick={() => handleEvacuate(h.id)}>
                  Evacuate
                </Button>
              )}
              {h.state === "active" && h.agent_status === "connected" && (
                <>
                  {expectedVersion && h.agent_version && h.agent_version !== expectedVersion && <Button variant="primary"
                          isLoading={updating === h.id} isDisabled={hostBusy} onClick={async (e) => {
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
                  </Button>}
                  <Button variant="secondary" isLoading={installing === h.id} isDisabled={hostBusy} onClick={() => {
                    if (!window.confirm("Reinstall the agent on this host? This re-runs the full install script via SSH.")) return;
                    installAgent(h.id);
                  }}>
                    {installing === h.id ? "Reinstalling..." : "Reinstall Agent"}
                  </Button>
                  {h.host_type !== "pattern_buffer" && <Button variant="secondary" isLoading={updating === `gc-${h.id}`} isDisabled={hostBusy || updating === `gc-${h.id}`} onClick={async () => {
                    if (!window.confirm(`Run garbage collection on ${h.instance_id}? This will sync capacity, clean orphans, and repair networks.`)) return;
                    setUpdating(`gc-${h.id}`);
                    try {
                      const resp = await fetch(`/api/v1/hosts/${h.id}/gc`, { method: "POST" });
                      if (resp.ok) {
                        const report = await resp.json();
                        const cap = report.capacity || {};
                        const orphans = report.orphans_found || 0;
                        const cleaned = report.cleanup?.cleaned || 0;
                        const parts = [];
                        if (cap.changed) parts.push(`Capacity synced: ${cap.old?.used_vcpus}→${cap.new?.used_vcpus} vCPUs, ${cap.old?.used_ram_mb}→${cap.new?.used_ram_mb} MB RAM`);
                        if (orphans > 0) {
                          parts.push(`Orphans found: ${orphans}, cleaned: ${cleaned}`);
                          if (report.cleanup?.output) parts.push(report.cleanup.output);
                          if (orphans > cleaned) parts.push(`Warning: ${orphans - cleaned} orphan(s) could not be cleaned`);
                        }
                        const cacheCleaned = report.cleanup?.cache_cleaned || 0;
                        if (cacheCleaned > 0) parts.push(`Cache cleaned: ${cacheCleaned} items`);
                        const repaired = report.network_repair?.repaired || 0;
                        if (repaired > 0) parts.push(`Network bridges repaired: ${repaired}`);
                        const s3 = report.s3_cleanup;
                        if (s3?.deleted > 0) parts.push(`S3 cleaned: ${s3.deleted} objects (${s3.deleted_gb || 0} GB)`);
                        if (report.shared_cache_entries_cleaned) parts.push(`Shared cache entries cleaned: ${report.shared_cache_entries_cleaned}`);
                        if (parts.length === 0) parts.push("Nothing to do — host is clean");
                        const fullMsg = parts.join("\n");
                        const copy = window.confirm(fullMsg + "\n\nClick OK to copy to clipboard.");
                        if (copy) navigator.clipboard.writeText(fullMsg).catch(() => {});
                        loadData();
                      } else {
                        alert("GC failed — check server logs");
                      }
                    } finally {
                      setUpdating(null);
                    }
                  }}>
                    Clean
                  </Button>}
                  {h.host_type !== "pattern_buffer" && <Button variant="danger" isLoading={updating === `wipe-${h.id}`} isDisabled={hostBusy || updating === `wipe-${h.id}`} onClick={async () => {
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
                  </Button>}
                </>
              )}
              </>); })()}
              {h.state === "active" && (
                <Button variant="secondary" onClick={() => {
                  const msg = h.used_vcpus > 0
                    ? `Power off ${h.instance_id}? This host has ${h.used_vcpus} vCPUs allocated — projects will be unavailable until powered back on.`
                    : `Power off ${h.instance_id}?`;
                  if (!window.confirm(msg)) return;
                  powerHost(h.id, "poweroff");
                }} isDisabled={installing === h.id || updating === h.id || poweringHosts.has(h.id)} isLoading={poweringHosts.has(h.id)}>
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
                  }} isLoading={poweringHosts.has(h.id)} isDisabled={poweringHosts.has(h.id)}>
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
              {h.state === "active" && h.agent_status === "connected" && h.host_type !== "pattern_buffer" && !isOcpVirtHost(h) && (
                <>
                  <span style={{ borderLeft: "1px solid var(--pf-t--global--border--color--default)", height: 24, margin: "0 4px" }} />
                  <div style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                    <input
                      style={{ ...inputStyle, width: 80, padding: "5px 8px", fontSize: 12 }}
                      type="number"
                      value={resizeTarget[h.id] || ""}
                      onChange={(e) => setResizeTarget({ ...resizeTarget, [h.id]: e.target.value })}
                      placeholder={`${h.storage_size_gb} GB`}
                      min={h.storage_size_gb + 1}
                    />
                    <Button
                      variant="secondary"
                      onClick={() => handleResizeStorage(h)}
                      isLoading={resizing[h.id]}
                      isDisabled={resizing[h.id] || !resizeTarget[h.id] || parseInt(resizeTarget[h.id]) <= h.storage_size_gb}
                    >
                      Resize
                    </Button>
                  </div>
                  <span style={{ borderLeft: "1px solid var(--pf-t--global--border--color--default)", height: 24, margin: "0 4px" }} />
                  <Switch
                    label={`Auto-extend${h.auto_extend_enabled ? ` (${h.auto_extend_threshold_pct}% → +${h.auto_extend_increment_gb} GB)` : ""}`}
                    isChecked={h.auto_extend_enabled}
                    onChange={(_, checked) => updateAutoExtend(h.id, "auto_extend_enabled", checked)}
                    style={{ fontSize: 12 }}
                  />
                  {h.auto_extend_enabled && (
                    <Button
                      variant="plain"
                      size="sm"
                      onClick={() => setExpandedAutoExtend({ ...expandedAutoExtend, [h.id]: !expandedAutoExtend[h.id] })}
                      style={{ padding: "2px 6px", fontSize: 11 }}
                    >
                      {expandedAutoExtend[h.id] ? "▲" : "▼"}
                    </Button>
                  )}
                </>
              )}
            </CardBody>
            {h.state === "active" && h.agent_status === "connected" && h.auto_extend_enabled && expandedAutoExtend[h.id] && (
              <CardBody style={{ borderTop: "1px solid var(--pf-t--global--border--color--default)" }}>
                <div style={{ display: "flex", gap: 16, flexWrap: "wrap", maxWidth: 600 }}>
                  <div>
                    <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Threshold (%)</label>
                    <input
                      style={{ ...inputStyle, width: 80 }}
                      type="number"
                      value={h.auto_extend_threshold_pct}
                      onChange={(e) => {
                        const val = parseInt(e.target.value) || 80;
                        if (val >= 50 && val <= 95) updateAutoExtend(h.id, "auto_extend_threshold_pct", val);
                      }}
                      min={50}
                      max={95}
                    />
                  </div>
                  <div>
                    <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Increment (GB)</label>
                    <input
                      style={{ ...inputStyle, width: 80 }}
                      type="number"
                      value={h.auto_extend_increment_gb}
                      onChange={(e) => updateAutoExtend(h.id, "auto_extend_increment_gb", parseInt(e.target.value) || 100)}
                      min={10}
                    />
                  </div>
                  <div>
                    <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Max (GB)</label>
                    <input
                      style={{ ...inputStyle, width: 100 }}
                      type="number"
                      value={h.auto_extend_max_gb || ""}
                      onChange={(e) => updateAutoExtend(h.id, "auto_extend_max_gb", e.target.value ? parseInt(e.target.value) : null)}
                      placeholder="No limit"
                    />
                  </div>
                  <div style={{ display: "flex", alignItems: "flex-end" }}>
                    <Button variant="primary" size="sm" onClick={() => handleExtendStorage(h)} isLoading={extending[h.id]} isDisabled={extending[h.id]}>
                      Extend Now (+{h.auto_extend_increment_gb} GB)
                    </Button>
                  </div>
                </div>
              </CardBody>
            )}
            {showKeyFor === h.id && keyData[h.id] && (
              <CardBody style={{ borderTop: "1px solid var(--pf-t--global--border--color--default)" }}>
                <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                  {keyData[h.id].ssh_script_command && (
                    <div>
                      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 4 }}>
                        <label style={{ fontSize: 11, fontWeight: 600 }}>SSH Script</label>
                        <Button variant="plain" onClick={() => copyToClipboard(keyData[h.id].ssh_script_command!, "SSH script command")} style={{ padding: "2px 6px", fontSize: 11 }}>Copy</Button>
                      </div>
                      <code style={{ fontSize: 11, display: "block", padding: 6, background: "rgba(0,0,0,0.2)", borderRadius: 4 }}>{keyData[h.id].ssh_script_command}</code>
                    </div>
                  )}
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
