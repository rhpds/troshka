"use client";

import { useEffect, useState } from "react";
import {
  PageSection,
  Title,
  Button,
  Card,
  CardBody,
  Alert,
  Switch,
} from "@patternfly/react-core";

interface StoragePool {
  id: string;
  name: string;
  mode: string;
  az: string | null;
  subnet_id: string | null;
  fsx_filesystem_id: string | null;
  fsx_dns_name: string | null;
  fsx_throughput_mbps: number | null;
  fsx_storage_gb: number | null;
  nfs_endpoint: string | null;
  status: string;
  provider_id: string;
  host_count: number;
  created_at: string;
  auto_extend_enabled: boolean;
  auto_extend_threshold_pct: number;
  auto_extend_increment_gb: number;
  auto_extend_max_gb: number | null;
  worker_host_id: string | null;
  worker_instance_type: string | null;
}

interface Provider {
  id: string;
  name: string;
  type: string;
}

const statusColors: Record<string, string> = {
  available: "var(--troshka-green)",
  creating: "var(--troshka-yellow, #f0ab00)",
  error: "var(--troshka-red)",
  deleting: "var(--troshka-yellow, #f0ab00)",
};

const modeLabels: Record<string, string> = {
  local: "Local EBS",
  "shared-fsx": "FSx OpenZFS",
  "shared-byo": "BYO NFS",
};

const inputStyle = {
  width: "100%",
  padding: "6px 10px",
  borderRadius: 6,
  border: "1px solid var(--pf-t--global--border--color--default)",
  background: "var(--pf-t--global--background--color--primary--default)",
  color: "var(--pf-t--global--text--color--regular)",
  fontSize: 13,
};

export default function StoragePoolsPage() {
  const [pools, setPools] = useState<StoragePool[]>([]);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [error, setError] = useState("");
  const [showCreate, setShowCreate] = useState(false);

  const [newName, setNewName] = useState("");
  const [newMode, setNewMode] = useState("shared-fsx");
  const [newProviderId, setNewProviderId] = useState("");
  const [newAz, setNewAz] = useState("");
  const [newThroughput, setNewThroughput] = useState(160);
  const [newStorageGb, setNewStorageGb] = useState(128);
  const [newNfsEndpoint, setNewNfsEndpoint] = useState("");
  const [creating, setCreating] = useState(false);
  const [availableAzs, setAvailableAzs] = useState<string[]>([]);

  const [editId, setEditId] = useState<string | null>(null);
  const [editThroughput, setEditThroughput] = useState(160);
  const [editStorageGb, setEditStorageGb] = useState(128);
  const [editNfsEndpoint, setEditNfsEndpoint] = useState("");
  const [editAutoExtend, setEditAutoExtend] = useState(false);
  const [editThresholdPct, setEditThresholdPct] = useState(80);
  const [editIncrementGb, setEditIncrementGb] = useState(64);
  const [editMaxGb, setEditMaxGb] = useState<number | null>(null);
  const [saving, setSaving] = useState(false);

  const [extending, setExtending] = useState<Record<string, boolean>>({});
  const [extendTarget, setExtendTarget] = useState<Record<string, string>>({});
  const [expandedAutoExtend, setExpandedAutoExtend] = useState<Record<string, boolean>>({});
  const [poolUsage, setPoolUsage] = useState<Record<string, { used_gb: number; total_gb: number; used_pct: number }>>({});

  const loadData = () => {
    Promise.all([
      fetch("/api/v1/storage-pools/").then((r) => (r.ok ? r.json() : [])),
      fetch("/api/v1/providers/").then((r) => (r.ok ? r.json() : [])),
    ]).then(([p, prov]) => {
      setPools(p);
      setProviders(prov);
    });
    fetch("/api/v1/hosts/").then((r) => r.ok ? r.json() : []).then((hosts: any[]) => {
      fetch("/api/v1/hosts/storage").then((r) => r.ok ? r.json() : {}).then((storage: any) => {
        const usage: Record<string, { used_gb: number; total_gb: number; used_pct: number }> = {};
        for (const h of hosts) {
          if (!h.storage_pool_id || usage[h.storage_pool_id]) continue;
          const info = storage[h.id];
          if (!info) continue;
          const parts = info.partitions;
          if (parts) {
            const shared = parts.find((p: any) => p.mount.includes("shared"));
            if (shared) {
              usage[h.storage_pool_id] = {
                used_gb: Math.round(shared.used_bytes / (1024**3) * 10) / 10,
                total_gb: Math.round(shared.total_bytes / (1024**3) * 10) / 10,
                used_pct: shared.used_pct,
              };
            }
          }
        }
        setPoolUsage(usage);
      }).catch(() => {});
    }).catch(() => {});
  };

  const pollUntilSettled = () => {
    const settled = ["available", "error"];
    const poll = setInterval(() => {
      fetch("/api/v1/storage-pools/").then((r) => r.ok ? r.json() : []).then((data: StoragePool[]) => {
        setPools(data);
        if (data.every((p) => settled.includes(p.status))) clearInterval(poll);
      }).catch(() => {});
    }, 2000);
  };

  useEffect(() => {
    loadData();
    const interval = setInterval(loadData, 10000);
    return () => clearInterval(interval);
  }, []);

  const fetchAzs = async (providerId: string) => {
    if (!providerId) { setAvailableAzs([]); return; }
    const resp = await fetch(`/api/v1/providers/${providerId}/availability-zones`);
    if (resp.ok) {
      setAvailableAzs(await resp.json());
    }
  };

  const handleProviderChange = (providerId: string) => {
    setNewProviderId(providerId);
    setNewAz("");
    fetchAzs(providerId);
  };

  const handleModeChange = (mode: string) => {
    setNewMode(mode);
    setNewAz("");
    setAvailableAzs([]);
    if (mode === "shared-fsx" && newProviderId) fetchAzs(newProviderId);
  };

  const handleCreate = async () => {
    setError("");
    if (!newName.trim()) { setError("Name is required"); return; }
    if (newMode === "shared-fsx" && !newProviderId) { setError("Provider is required for FSx pools"); return; }
    if (newMode === "shared-fsx" && !newAz) { setError("AZ is required for FSx pools"); return; }
    if (newMode === "shared-byo" && !newNfsEndpoint) { setError("NFS endpoint is required"); return; }

    // BYO NFS: auto-select first EC2 provider (needed for SG rules)
    const providerId = newProviderId || providers.find((p) => p.type === "ec2")?.id;
    if (!providerId) { setError("No EC2 provider configured"); return; }

    setCreating(true);
    const resp = await fetch("/api/v1/storage-pools", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: newName.trim(),
        mode: newMode,
        provider_id: providerId,
        az: newAz || null,
        fsx_throughput_mbps: newMode === "shared-fsx" ? newThroughput : null,
        fsx_storage_gb: newMode === "shared-fsx" ? newStorageGb : null,
        nfs_endpoint: newMode === "shared-byo" ? newNfsEndpoint : null,
      }),
    });
    setCreating(false);

    if (resp.ok) {
      setShowCreate(false);
      setNewName("");
      setNewMode("shared-fsx");
      setNewAz("");
      loadData();
      pollUntilSettled();
    } else {
      const data = await resp.json();
      setError(data.detail || "Failed to create pool");
    }
  };

  const startEdit = (pool: StoragePool) => {
    setEditId(pool.id);
    setEditThroughput(pool.fsx_throughput_mbps || 160);
    setEditStorageGb(pool.fsx_storage_gb || 128);
    setEditNfsEndpoint(pool.nfs_endpoint || "");
    setEditAutoExtend(pool.auto_extend_enabled);
    setEditThresholdPct(pool.auto_extend_threshold_pct);
    setEditIncrementGb(pool.auto_extend_increment_gb);
    setEditMaxGb(pool.auto_extend_max_gb);
  };

  const cancelEdit = () => {
    setEditId(null);
  };

  const saveEdit = async (pool: StoragePool) => {
    setError("");
    if (pool.mode === "shared-fsx") {
      if (editThroughput < 160) { setError("Throughput must be at least 160 MBps"); return; }
      if (editStorageGb < (pool.fsx_storage_gb || 64)) { setError("Storage can only grow, not shrink"); return; }
      const minGrow = Math.ceil((pool.fsx_storage_gb || 64) * 1.1);
      if (editStorageGb > (pool.fsx_storage_gb || 0) && editStorageGb < minGrow) {
        setError(`Storage increase must be at least 10% (minimum ${minGrow} GB)`); return;
      }
    }
    if (pool.mode === "shared-byo") {
      if (!editNfsEndpoint.trim()) { setError("NFS endpoint is required"); return; }
      if (!editNfsEndpoint.includes(":")) { setError("NFS endpoint must be in host:/path format"); return; }
    }
    setSaving(true);
    const body: Record<string, unknown> = {};
    if (pool.mode === "shared-fsx") {
      body.fsx_throughput_mbps = editThroughput;
      body.fsx_storage_gb = editStorageGb;
    }
    if (pool.mode === "shared-byo") {
      body.nfs_endpoint = editNfsEndpoint;
    }
    body.auto_extend_enabled = editAutoExtend;
    body.auto_extend_threshold_pct = editThresholdPct;
    body.auto_extend_increment_gb = editIncrementGb;
    body.auto_extend_max_gb = editMaxGb;
    const resp = await fetch(`/api/v1/storage-pools/${pool.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    setSaving(false);
    if (resp.ok) {
      setEditId(null);
      loadData();
    } else {
      const data = await resp.json();
      setError(data.detail || "Failed to update pool");
    }
  };

  const handleDelete = async (pool: StoragePool) => {
    if (!window.confirm(`Delete storage pool "${pool.name}"? This will also delete the FSx filesystem if applicable.`)) return;
    const resp = await fetch(`/api/v1/storage-pools/${pool.id}`, { method: "DELETE" });
    if (resp.ok) {
      loadData();
    } else {
      const data = await resp.json();
      setError(data.detail || "Failed to delete pool");
    }
  };

  const handleExtendNow = async (pool: StoragePool) => {
    const newSize = (pool.fsx_storage_gb || 0) + pool.auto_extend_increment_gb;
    if (!window.confirm(`Extend storage for pool "${pool.name}"?\n\n${pool.fsx_storage_gb} GB → ${newSize} GB (+${pool.auto_extend_increment_gb} GB)\n\nNote: FSx only allows one extend every 6 hours. The host may take a few minutes to see the new capacity.`)) return;
    setExtending({ ...extending, [pool.id]: true });
    const resp = await fetch(`/api/v1/storage-pools/${pool.id}/extend`, {
      method: "POST",
    });
    if (!resp.ok) {
      setExtending({ ...extending, [pool.id]: false });
      const data = await resp.json();
      setError(data.detail || "Failed to extend storage");
      return;
    }
    const data = await resp.json();
    const targetGb = data.new_size_gb || newSize;
    const poll = setInterval(async () => {
      try {
        const r = await fetch("/api/v1/hosts/storage");
        if (!r.ok) return;
        const storage = await r.json();
        for (const [, info] of Object.entries(storage) as [string, any][]) {
          const parts = info.partitions;
          if (!parts) continue;
          const shared = parts.find((p: any) => p.mount.includes("shared"));
          if (shared && shared.total_bytes / (1024**3) >= targetGb - 1) {
            clearInterval(poll);
            setExtending((prev) => ({ ...prev, [pool.id]: false }));
            loadData();
            return;
          }
        }
        loadData();
      } catch {}
    }, 5000);
    setTimeout(() => {
      clearInterval(poll);
      setExtending((prev) => ({ ...prev, [pool.id]: false }));
      loadData();
    }, 120000);
  };

  const handleResizePool = async (pool: StoragePool) => {
    const targetGb = parseInt(extendTarget[pool.id]);
    const currentGb = pool.fsx_storage_gb || 0;
    if (!targetGb || targetGb <= currentGb) {
      setError(`New size must be larger than current (${currentGb} GB)`);
      return;
    }
    const minGrow = Math.ceil(currentGb * 1.1);
    if (targetGb < minGrow) {
      setError(`FSx requires at least 10% growth (minimum ${minGrow} GB)`);
      return;
    }
    if (!window.confirm(`Resize pool "${pool.name}" to ${targetGb} GB? (currently ${currentGb} GB)\n\nNote: FSx only allows one resize every 6 hours.`)) return;
    setExtending({ ...extending, [pool.id]: true });
    const incrementGb = targetGb - currentGb;
    const resp = await fetch(`/api/v1/storage-pools/${pool.id}/extend`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ increment_gb: incrementGb }),
    });
    setExtending({ ...extending, [pool.id]: false });
    if (resp.ok) {
      setExtendTarget({ ...extendTarget, [pool.id]: "" });
      alert(`Pool storage resized: ${currentGb} GB → ${targetGb} GB`);
      loadData();
    } else {
      const data = await resp.json();
      setError(data.detail || "Failed to resize pool storage");
    }
  };

  const updatePoolField = async (poolId: string, field: string, value: boolean | number | null) => {
    const resp = await fetch(`/api/v1/storage-pools/${poolId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ [field]: value }),
    });
    if (resp.ok) {
      loadData();
    } else {
      const data = await resp.json();
      setError(data.detail || "Failed to update pool settings");
    }
  };

  return (
    <PageSection>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
        <Title headingLevel="h1">Storage Pools</Title>
        <Button variant="primary" onClick={() => setShowCreate(!showCreate)}>
          {showCreate ? "Cancel" : "Create Pool"}
        </Button>
      </div>

      {error && <Alert variant="danger" title={error} style={{ marginBottom: 16 }} />}

      {showCreate && (
        <Card style={{ marginBottom: 16 }}>
          <CardBody>
            <div style={{ display: "flex", flexDirection: "column", gap: 12, maxWidth: 500 }}>
              <div>
                <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Mode</label>
                <select style={inputStyle} value={newMode} onChange={(e) => handleModeChange(e.target.value)}>
                  <option value="shared-fsx">FSx OpenZFS (Managed NFS)</option>
                  <option value="shared-byo">BYO NFS</option>
                </select>
              </div>
              <div>
                <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Name</label>
                <input style={inputStyle} value={newName} onChange={(e) => setNewName(e.target.value)}
                       placeholder="e.g. prod-east-1b" />
              </div>
              {newMode === "shared-fsx" && (
                <div>
                  <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Provider</label>
                  <select style={inputStyle} value={newProviderId} onChange={(e) => handleProviderChange(e.target.value)}>
                    <option value="">Select provider...</option>
                    {providers.filter((p) => p.type === "ec2").map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
                  </select>
                </div>
              )}
              {newMode === "shared-fsx" && (
                <div>
                  <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Availability Zone</label>
                  <select style={inputStyle} value={newAz} onChange={(e) => setNewAz(e.target.value)}>
                    <option value="">{availableAzs.length ? "Select AZ..." : "Select a provider first"}</option>
                    {availableAzs.map((az) => <option key={az} value={az}>{az}</option>)}
                  </select>
                </div>
              )}
              {newMode === "shared-fsx" && (
                <>
                  <div>
                    <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Throughput (MBps)</label>
                    <input style={inputStyle} type="number" value={newThroughput}
                           onChange={(e) => setNewThroughput(parseInt(e.target.value) || 160)} min={160} />
                  </div>
                  <div>
                    <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Storage (GB)</label>
                    <input style={inputStyle} type="number" value={newStorageGb}
                           onChange={(e) => setNewStorageGb(parseInt(e.target.value) || 64)} min={64} />
                  </div>
                </>
              )}
              {newMode === "shared-byo" && (
                <div>
                  <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>NFS Endpoint</label>
                  <input style={inputStyle} value={newNfsEndpoint} onChange={(e) => setNewNfsEndpoint(e.target.value)}
                         placeholder="10.0.1.50:/exports/troshka" />
                </div>
              )}
              <Button variant="primary" onClick={handleCreate} isLoading={creating} isDisabled={creating}>
                Create Pool
              </Button>
            </div>
          </CardBody>
        </Card>
      )}

      {pools.length === 0 && !showCreate && (
        <Card><CardBody style={{ textAlign: "center", padding: 40, color: "var(--pf-t--global--text--color--subtle)" }}>
          No storage pools configured. Create one to get started.
        </CardBody></Card>
      )}

      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        {pools.map((pool) => (
          <Card key={pool.id}>
            <CardBody>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
                <div style={{ flex: 1 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
                    <span style={{ fontWeight: 600, fontSize: 15 }}>{pool.name}</span>
                    <span style={{
                      fontSize: 11, padding: "2px 8px", borderRadius: 10,
                      background: statusColors[pool.status] || "gray", color: "#fff",
                    }}>{pool.status}</span>
                    <span style={{
                      fontSize: 11, padding: "2px 8px", borderRadius: 10,
                      border: "1px solid var(--pf-t--global--border--color--default)",
                    }}>{modeLabels[pool.mode] || pool.mode}</span>
                  </div>
                  {editId === pool.id ? (
                    <div style={{ display: "flex", flexDirection: "column", gap: 10, marginTop: 8, maxWidth: 400 }}>
                      {pool.mode === "shared-fsx" && (
                        <>
                          <div>
                            <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Throughput (MBps)</label>
                            <input style={inputStyle} type="number" value={editThroughput}
                                   onChange={(e) => setEditThroughput(parseInt(e.target.value) || 160)} min={160} />
                          </div>
                          <div>
                            <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Storage (GB)</label>
                            <input style={inputStyle} type="number" value={editStorageGb}
                                   onChange={(e) => setEditStorageGb(parseInt(e.target.value) || 64)}
                                   min={pool.fsx_storage_gb || 64} />
                          </div>
                        </>
                      )}
                      {pool.mode === "shared-byo" && (
                        <div>
                          <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>NFS Endpoint</label>
                          <input style={inputStyle} value={editNfsEndpoint}
                                 onChange={(e) => setEditNfsEndpoint(e.target.value)}
                                 placeholder="10.0.1.50:/exports/troshka" />
                        </div>
                      )}
                      {pool.mode === "shared-fsx" && (
                        <>
                          <div style={{ marginTop: 12, borderTop: "1px solid var(--pf-t--global--border--color--default)", paddingTop: 10 }}>
                            <label style={{ fontSize: 12, fontWeight: 600, display: "block", marginBottom: 8 }}>Auto-Extend</label>
                          </div>
                          <Switch
                            label="Auto-extend enabled"
                            isChecked={editAutoExtend}
                            onChange={(_, checked) => setEditAutoExtend(checked)}
                          />
                          <div>
                            <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Threshold (%)</label>
                            <input style={inputStyle} type="number" value={editThresholdPct}
                                   onChange={(e) => setEditThresholdPct(parseInt(e.target.value) || 80)}
                                   min={50} max={95} />
                          </div>
                          <div>
                            <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Increment (GB)</label>
                            <input style={inputStyle} type="number" value={editIncrementGb}
                                   onChange={(e) => setEditIncrementGb(parseInt(e.target.value) || 64)}
                                   min={64} />
                          </div>
                          <div>
                            <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Max (GB, optional)</label>
                            <input style={inputStyle} type="number" value={editMaxGb || ""}
                                   onChange={(e) => setEditMaxGb(e.target.value ? parseInt(e.target.value) : null)}
                                   placeholder="No limit" />
                          </div>
                        </>
                      )}
                      <div style={{ display: "flex", gap: 8 }}>
                        <Button variant="primary" size="sm" onClick={() => saveEdit(pool)}
                                isLoading={saving} isDisabled={saving}>Save</Button>
                        <Button variant="secondary" size="sm" onClick={cancelEdit}>Cancel</Button>
                      </div>
                    </div>
                  ) : (
                    <>
                      <div style={{ fontSize: 12, color: "var(--pf-t--global--text--color--subtle)", display: "flex", gap: 16, flexWrap: "wrap" }}>
                        {pool.az && <span>AZ: {pool.az}</span>}
                        <span>Hosts: {pool.host_count}</span>
                        {pool.fsx_filesystem_id && <span>FSx: {pool.fsx_filesystem_id}</span>}
                        {pool.fsx_throughput_mbps && <span>Throughput: {pool.fsx_throughput_mbps} MBps</span>}
                        {pool.fsx_storage_gb && <span style={poolUsage[pool.id]?.used_pct >= 80 ? { color: "#f87171", fontWeight: 600 } : undefined}>Storage: {poolUsage[pool.id] ? `${poolUsage[pool.id].used_gb} / ${pool.fsx_storage_gb} GB (${poolUsage[pool.id].used_pct}%)` : `${pool.fsx_storage_gb} GB`}</span>}
                        {pool.nfs_endpoint && <span>NFS: {pool.nfs_endpoint}</span>}
                      </div>
                      {pool.mode === "shared-fsx" && pool.status === "available" && (
                        <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 8, flexWrap: "wrap" }}>
                          <div style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                            <input
                              style={{ ...inputStyle, width: 80, padding: "5px 8px", fontSize: 12 }}
                              type="number"
                              value={extendTarget[pool.id] || ""}
                              onChange={(e) => setExtendTarget({ ...extendTarget, [pool.id]: e.target.value })}
                              placeholder={`${pool.fsx_storage_gb} GB`}
                              min={(pool.fsx_storage_gb || 0) + 1}
                            />
                            <Button
                              variant="secondary"
                              onClick={() => handleResizePool(pool)}
                              isLoading={extending[pool.id]}
                              isDisabled={extending[pool.id] || !extendTarget[pool.id] || parseInt(extendTarget[pool.id]) <= (pool.fsx_storage_gb || 0)}
                            >
                              Resize
                            </Button>
                          </div>
                          <span style={{ borderLeft: "1px solid var(--pf-t--global--border--color--default)", height: 24 }} />
                          <Switch
                            label={`Auto-extend${pool.auto_extend_enabled ? ` (${pool.auto_extend_threshold_pct}% → +${pool.auto_extend_increment_gb} GB)` : ""}`}
                            isChecked={pool.auto_extend_enabled}
                            onChange={(_, checked) => updatePoolField(pool.id, "auto_extend_enabled", checked)}
                            style={{ fontSize: 12 }}
                          />
                          {pool.auto_extend_enabled && (
                            <Button
                              variant="plain"
                              size="sm"
                              onClick={() => setExpandedAutoExtend({ ...expandedAutoExtend, [pool.id]: !expandedAutoExtend[pool.id] })}
                              style={{ padding: "2px 6px", fontSize: 11 }}
                            >
                              {expandedAutoExtend[pool.id] ? "▲" : "▼"}
                            </Button>
                          )}
                        </div>
                      )}
                      {pool.mode === "shared-fsx" && pool.auto_extend_enabled && expandedAutoExtend[pool.id] && (
                        <div style={{ display: "flex", gap: 16, flexWrap: "wrap", maxWidth: 600, marginTop: 8 }}>
                          <div>
                            <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Threshold (%)</label>
                            <input
                              style={{ ...inputStyle, width: 80 }}
                              type="number"
                              value={pool.auto_extend_threshold_pct}
                              onChange={(e) => {
                                const val = parseInt(e.target.value) || 80;
                                if (val >= 50 && val <= 95) updatePoolField(pool.id, "auto_extend_threshold_pct", val);
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
                              value={pool.auto_extend_increment_gb}
                              onChange={(e) => updatePoolField(pool.id, "auto_extend_increment_gb", parseInt(e.target.value) || 64)}
                              min={64}
                            />
                          </div>
                          <div>
                            <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Max (GB)</label>
                            <input
                              style={{ ...inputStyle, width: 100 }}
                              type="number"
                              value={pool.auto_extend_max_gb || ""}
                              onChange={(e) => updatePoolField(pool.id, "auto_extend_max_gb", e.target.value ? parseInt(e.target.value) : null)}
                              placeholder="No limit"
                            />
                          </div>
                          <div style={{ display: "flex", alignItems: "flex-end" }}>
                            <Button variant="primary" size="sm" onClick={() => handleExtendNow(pool)} isLoading={extending[pool.id]} isDisabled={extending[pool.id]}>
                              Extend Now (+{pool.auto_extend_increment_gb} GB)
                            </Button>
                          </div>
                        </div>
                      )}
                    </>
                  )}
                </div>
                <div style={{ display: "flex", gap: 6, flexDirection: "column", alignItems: "flex-end" }}>
                  <div style={{ fontSize: 11, marginBottom: 4 }}>
                    {pool.worker_host_id ? (
                      <span style={{ color: "#4ade80" }}>Pattern Buffer: active</span>
                    ) : (
                      <span style={{ opacity: 0.5 }}>No Pattern Buffer</span>
                    )}
                  </div>
                  <div style={{ display: "flex", gap: 6 }}>
                    {pool.status === "available" && (
                      <Button variant="secondary" size="sm" onClick={() => {
                        fetch(`/api/v1/storage-pools/${pool.id}/pattern-buffer`, { method: "POST" });
                        setTimeout(loadData, 3000);
                      }}>
                        {pool.worker_host_id ? "Replace" : "Add"} Pattern Buffer
                      </Button>
                    )}
                    {editId !== pool.id && pool.status === "available" && (
                      <Button variant="secondary" size="sm" onClick={() => startEdit(pool)}>Edit</Button>
                    )}
                    <Button variant="danger" size="sm" onClick={() => handleDelete(pool)}
                            isDisabled={pool.host_count > 0 || pool.status === "creating"}>
                      Delete
                    </Button>
                  </div>
                </div>
              </div>
            </CardBody>
          </Card>
        ))}
      </div>
    </PageSection>
  );
}
