"use client";

import LabeledField from "@/components/LabeledField";
import ModalBackdrop from "@/components/ModalBackdrop";
import { useEffect, useRef, useState } from "react";
import AlertModal from "@/components/AlertModal";
import { appConfirm } from "@/lib/confirm";
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
  azure_files_capacity_gb: number | null;
  azure_files_throughput: number | null;
  azure_storage_account: string | null;
  azure_file_share_name: string | null;
  azure_file_share_url: string | null;
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
  worker_status: string | null;
  worker_error: string | null;
  worker_ip: string | null;
  worker_private_ip: string | null;
  worker_instance_id: string | null;
  worker_agent_version: string | null;
  pb_auto_sleep_minutes: number;
  pb_last_activity_at: string | null;
}

interface Provider {
  id: string;
  name: string;
  type: string;
  gcp_zone?: string | null;
  azure_location?: string | null;
}

const statusColors: Record<string, string> = {
  available: "var(--troshka-green)",
  creating: "var(--troshka-yellow, #f0ab00)",
  error: "var(--troshka-red)",
  deleting: "var(--troshka-yellow, #f0ab00)",
};

const modeLabels: Record<string, string> = {
  local: "Local (All Providers)",
  "shared-fsx": "FSx OpenZFS (AWS)",
  "shared-byo": "BYO NFS (All Providers)",
  "shared-ceph-nfs": "Ceph-NFS (OCP Virt)",
  "shared-azure-files": "Azure Files NFS (Azure)",
};

function poolAutoExtendLabel(pool: StoragePool) {
  if (!pool.auto_extend_enabled) return "Auto-extend";
  return `Auto-extend (${pool.auto_extend_threshold_pct}% → +${pool.auto_extend_increment_gb} GB)`;
}

function poolStorageGb(pool: StoragePool): number | null {
  return pool.fsx_storage_gb || pool.azure_files_capacity_gb || null;
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


function retainedBufferActions(previous: Record<string, string>, pools: StoragePool[]) {
  const next: Record<string, string> = {};
  for (const [id, action] of Object.entries(previous)) {
    const pool = pools.find(p => p.id === id);
    if (pool && ((action === "waking" && pool.worker_status === "stopped") ||
      (action === "sleeping" && pool.worker_status === "connected"))) next[id] = action;
  }
  return next;
}

interface StorageHost { id: string; storage_pool_id?: string; agent_version?: string }
interface StoragePartition { mount: string; used_bytes: number; total_bytes: number; used_pct: number }
type HostStorage = Record<string, { partitions?: StoragePartition[] }>;

function collectPoolUsage(hosts: StorageHost[], storage: HostStorage) {
  const usage: Record<string, { used_gb: number; total_gb: number; used_pct: number }> = {};
  for (const host of hosts) {
    if (!host.storage_pool_id || usage[host.storage_pool_id]) continue;
    const shared = storage[host.id]?.partitions?.find(part => part.mount.includes("shared"));
    if (!shared) continue;
    usage[host.storage_pool_id] = {
      used_gb: Math.round(shared.used_bytes / (1024**3) * 10) / 10,
      total_gb: Math.round(shared.total_bytes / (1024**3) * 10) / 10,
      used_pct: shared.used_pct,
    };
  }
  return usage;
}

const settledStatuses = new Set(["available", "error"]);

function isSettledPool(pool: StoragePool) {
  return settledStatuses.has(pool.status);
}

function defaultProviderType(mode: string) {
  return ({ "shared-ceph-nfs": "ocpvirt", "shared-netapp": "gcp", "shared-azure-files": "azure" } as Record<string, string>)[mode] || "ec2";
}

function providerSupportsMode(provider: Provider, mode: string) {
  if (mode === "shared-fsx") return provider.type === "ec2";
  if (mode === "shared-azure-files") return provider.type === "azure";
  return provider.type !== "s3";
}

function createPoolError(name: string, mode: string, providerId: string, az: string, nfs: string) {
  if (!name.trim()) return "Name is required";
  const names: Record<string, string> = { local: "local", "shared-fsx": "FSx", "shared-ceph-nfs": "Ceph-NFS", "shared-netapp": "NetApp Volumes", "shared-azure-files": "Azure Files" };
  if (names[mode] && !providerId) return `Provider is required for ${names[mode]} pools`;
  const zones: Record<string, string> = { "shared-fsx": "AZ is required for FSx pools", "shared-netapp": "Zone is required for NetApp Volumes pools", "shared-azure-files": "Location is required for Azure Files pools" };
  if (zones[mode] && !az) return zones[mode];
  if (mode === "shared-byo" && !nfs) return "NFS endpoint is required";
  return "";
}

function validatePoolEdit(pool: StoragePool, throughput: number, size: number, nfs: string) {
  if (pool.mode === "shared-byo") {
    if (!nfs.trim()) return "NFS endpoint is required";
    if (!nfs.includes(":")) return "NFS endpoint must be in host:/path format";
  }
  if (pool.mode !== "shared-fsx" && pool.mode !== "shared-azure-files") return "";
  const minThroughput = pool.mode === "shared-fsx" ? 160 : 100;
  if (throughput < minThroughput) return `Throughput must be at least ${minThroughput} MBps`;
  const minStorage = pool.mode === "shared-fsx" ? 64 : 100;
  if (size < (poolStorageGb(pool) || minStorage)) return "Storage can only grow, not shrink";
  const minGrow = Math.ceil((poolStorageGb(pool) || minStorage) * 1.1);
  if (size > (poolStorageGb(pool) || 0) && size < minGrow) return `Storage increase must be at least 10% (minimum ${minGrow} GB)`;
  return "";
}

export default function StoragePoolsPage() {
  const [pools, setPools] = useState<StoragePool[]>([]);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [error, setError] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [alertMsg, setAlertMsg] = useState<string | null>(null);

  const [newName, setNewName] = useState("");
  const [newMode, setNewMode] = useState("shared-fsx");
  const [newProviderId, setNewProviderId] = useState("");
  const [newAz, setNewAz] = useState("");
  const [newThroughput, setNewThroughput] = useState(160);
  const [newStorageGb, setNewStorageGb] = useState(128);
  const [newNfsEndpoint, setNewNfsEndpoint] = useState("");
  const [newStorageQuotaGb, setNewStorageQuotaGb] = useState(500);
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

  const [pbPoolId, setPbPoolId] = useState<string | null>(null);
  const [pbAction, setPbAction] = useState<Record<string, string>>({});
  const [pbError, setPbError] = useState<string | null>(null);
  const pbErrorDismissedRef = useRef<Set<string>>(new Set());
  const [pbErrorPoolId, setPbErrorPoolId] = useState<string | null>(null);
  const [expectedAgentVersion, setExpectedAgentVersion] = useState("");
  const [pbInstanceType, setPbInstanceType] = useState("");
  const pbTypesByProvider: Record<string, Array<{value: string; label: string}>> = {
    ec2: [
      { value: "i4i.large", label: "i4i.large — 2 vCPU / 16 GB / 468 GB NVMe — ~$0.31/hr" },
      { value: "i4i.xlarge", label: "i4i.xlarge — 4 vCPU / 32 GB / 937 GB NVMe — ~$0.62/hr" },
      { value: "i4i.2xlarge", label: "i4i.2xlarge — 8 vCPU / 64 GB / 1.8 TB NVMe — ~$1.25/hr" },
      { value: "c6id.large", label: "c6id.large — 2 vCPU / 4 GB / 118 GB NVMe — ~$0.13/hr" },
      { value: "c6id.xlarge", label: "c6id.xlarge — 4 vCPU / 8 GB / 237 GB NVMe — ~$0.25/hr" },
    ],
    gcp: [
      { value: "e2-standard-2", label: "e2-standard-2 — 2 vCPU / 8 GB — ~$0.07/hr" },
      { value: "e2-standard-4", label: "e2-standard-4 — 4 vCPU / 16 GB — ~$0.13/hr" },
      { value: "e2-standard-8", label: "e2-standard-8 — 8 vCPU / 32 GB — ~$0.27/hr" },
    ],
    azure: [
      { value: "Standard_E2s_v5", label: "Standard_E2s_v5 — 2 vCPU / 16 GB — ~$0.13/hr" },
      { value: "Standard_E4s_v5", label: "Standard_E4s_v5 — 4 vCPU / 32 GB — ~$0.25/hr" },
    ],
    ocpvirt: [
      { value: "4c-8g", label: "4c-8g — 4 vCPU / 8 GB" },
      { value: "8c-16g", label: "8c-16g — 8 vCPU / 16 GB" },
    ],
  };

  const loadData = () => {
    Promise.all([
      fetch("/api/v1/storage-pools/").then((r) => (r.ok ? r.json() : [])),
      fetch("/api/v1/providers/").then((r) => (r.ok ? r.json() : [])),
    ]).then(([p, prov]) => {
      setPools(p);
      setProviders(prov);
      const errPool = p.find((pp: StoragePool) => pp.worker_error && !pbErrorDismissedRef.current.has(pp.id));
      if (errPool) {
        setPbError(errPool.worker_error);
        setPbErrorPoolId(errPool.id);
        pbErrorDismissedRef.current.add(errPool.id);
      }
      setPbAction(prev => retainedBufferActions(prev, p));
    });
    fetch("/api/v1/hosts/").then((r) => r.ok ? r.json() : []).then((hosts: StorageHost[]) => {
      const versions = hosts.map(h => h.agent_version).filter(Boolean);
      if (versions.length > 0) setExpectedAgentVersion(versions[0]!);
      fetch("/api/v1/hosts/storage").then((r) => r.ok ? r.json() : {}).then((storage: HostStorage) => {
        setPoolUsage(collectPoolUsage(hosts, storage));
      }).catch(() => {});
    }).catch(() => {});
  };

  const pollUntilSettled = () => {
    const poll = setInterval(() => {
      fetch("/api/v1/storage-pools/").then((r) => r.ok ? r.json() : []).then((data: StoragePool[]) => {
        setPools(data);
        if (data.every(isSettledPool)) clearInterval(poll);
      }).catch(() => {});
    }, 2000);
  };

  useEffect(() => {
    loadData();
    const interval = setInterval(loadData, 10000);
    return () => clearInterval(interval);
  }, []);

  useEffect(() => {
    const provisioning = pools.some(
      (p) => p.worker_status && !["connected"].includes(p.worker_status)
    );
    if (!provisioning) return;
    const fast = setInterval(loadData, 3000);
    return () => clearInterval(fast);
  }, [pools]);

  const fetchAzs = async (providerId: string) => {
    if (!providerId) { setAvailableAzs([]); return; }
    const prov = providers.find((p) => p.id === providerId);
    if (prov?.type === "gcp" && prov.gcp_zone) {
      setAvailableAzs([prov.gcp_zone]);
      setNewAz(prov.gcp_zone);
      return;
    }
    if (prov?.type === "azure" && prov.azure_location) {
      setAvailableAzs([prov.azure_location]);
      setNewAz(prov.azure_location);
      return;
    }
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
    if (mode === "shared-netapp") setNewStorageGb(1024);
    else if (mode === "shared-fsx") setNewStorageGb(128);
    if ((mode === "shared-fsx" || mode === "shared-netapp" || mode === "shared-azure-files") && newProviderId) fetchAzs(newProviderId);
    if (mode === "shared-ceph-nfs") setNewProviderId("");
  };

  const handleCreate = async () => {
    setError("");
    const validationError = createPoolError(newName, newMode, newProviderId, newAz, newNfsEndpoint);
    if (validationError) { setError(validationError); return; }

    const autoProviderType = defaultProviderType(newMode);
    const providerId = newProviderId || providers.find((p) => p.type === autoProviderType)?.id;
    if (!providerId) { setError(`No ${autoProviderType} provider configured`); return; }

    setCreating(true);
    const body: Record<string, unknown> = {
      name: newName.trim(),
      mode: newMode,
      provider_id: providerId,
      az: newAz || null,
      fsx_throughput_mbps: newMode === "shared-fsx" ? newThroughput : null,
      fsx_storage_gb: ({ "shared-fsx": newStorageGb, "shared-ceph-nfs": newStorageQuotaGb } as Record<string, number>)[newMode] ?? null,
      netapp_capacity_gb: newMode === "shared-netapp" ? newStorageGb : null,
      azure_files_capacity_gb: newMode === "shared-azure-files" ? newStorageGb : null,
      azure_files_throughput: newMode === "shared-azure-files" ? newThroughput : null,
      nfs_endpoint: newMode === "shared-byo" ? newNfsEndpoint : null,
    };
    const resp = await fetch("/api/v1/storage-pools", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
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
    setEditStorageGb(poolStorageGb(pool) || 128);
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
    const validationError = validatePoolEdit(pool, editThroughput, editStorageGb, editNfsEndpoint);
    if (validationError) { setError(validationError); return; }
    setSaving(true);
    const body: Record<string, unknown> = {};
    if (pool.mode === "shared-fsx" || pool.mode === "shared-azure-files") {
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
    if (!(await appConfirm({
      message: `Delete storage pool "${pool.name}"? This will also delete the FSx filesystem if applicable.`,
      confirmLabel: "Delete",
      variant: "danger",
    }))) return;
    const resp = await fetch(`/api/v1/storage-pools/${pool.id}`, { method: "DELETE" });
    if (resp.ok) {
      loadData();
    } else {
      const data = await resp.json();
      setError(data.detail || "Failed to delete pool");
    }
  };

  const handleExtendNow = async (pool: StoragePool) => {
    const newSize = (poolStorageGb(pool) || 0) + pool.auto_extend_increment_gb;
    if (!(await appConfirm({
      message: `Extend storage for pool "${pool.name}"?\n\n${poolStorageGb(pool)} GB → ${newSize} GB (+${pool.auto_extend_increment_gb} GB)\n\nNote: FSx only allows one extend every 6 hours. The host may take a few minutes to see the new capacity.`,
      confirmLabel: "Extend",
    }))) return;
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
        const storage: HostStorage = await r.json();
        for (const [, info] of Object.entries(storage)) {
          const parts = info.partitions;
          if (!parts) continue;
          const shared = parts.find(p => p.mount.includes("shared"));
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
    const targetGb = Number.parseInt(extendTarget[pool.id]);
    const currentGb = poolStorageGb(pool) || 0;
    if (!targetGb || targetGb <= currentGb) {
      setError(`New size must be larger than current (${currentGb} GB)`);
      return;
    }
    const minGrow = Math.ceil(currentGb * 1.1);
    if (targetGb < minGrow) {
      setError(`FSx requires at least 10% growth (minimum ${minGrow} GB)`);
      return;
    }
    if (!(await appConfirm({
      message: `Resize pool "${pool.name}" to ${targetGb} GB? (currently ${currentGb} GB)\n\nNote: FSx only allows one resize every 6 hours.`,
      confirmLabel: "Resize",
    }))) return;
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
      setAlertMsg(`Pool storage resized: ${currentGb} GB → ${targetGb} GB`);
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

  function renderPoolEditor(pool: StoragePool) {
    return (
                    <div style={{ display: "flex", flexDirection: "column", gap: 10, marginTop: 8, maxWidth: 400 }}>
                      {(pool.mode === "shared-fsx" || pool.mode === "shared-azure-files") && (
                        <>
                          <div>
                            <LabeledField style={{ fontSize: 12, display: "block", marginBottom: 4 }} label={<>Throughput (MBps)</>}>
                            <input style={inputStyle} type="number" value={editThroughput}
                                   onChange={(e) => setEditThroughput(Number.parseInt(e.target.value) || 160)} min={pool.mode === "shared-fsx" ? 160 : 100} /></LabeledField>
                          </div>
                          <div>
                            <LabeledField style={{ fontSize: 12, display: "block", marginBottom: 4 }} label={<>Storage (GB)</>}>
                            <input style={inputStyle} type="number" value={editStorageGb}
                                   onChange={(e) => setEditStorageGb(Number.parseInt(e.target.value) || 64)}
                                   min={poolStorageGb(pool) || (pool.mode === "shared-fsx" ? 64 : 100)} /></LabeledField>
                          </div>
                        </>
                      )}
                      {pool.mode === "shared-byo" && (
                        <div>
                          <LabeledField style={{ fontSize: 12, display: "block", marginBottom: 4 }} label={<>NFS Endpoint</>}>
                          <input style={inputStyle} value={editNfsEndpoint}
                                 onChange={(e) => setEditNfsEndpoint(e.target.value)}
                                 placeholder="10.0.1.50:/exports/troshka" /></LabeledField>
                        </div>
                      )}
                      {(pool.mode === "shared-fsx" || pool.mode === "shared-azure-files") && (
                        <>
                          <div style={{ marginTop: 12, borderTop: "1px solid var(--pf-t--global--border--color--default)", paddingTop: 10 }}>
                            <span style={{ fontSize: 12, fontWeight: 600, display: "block", marginBottom: 8 }}>Auto-Extend</span>
                          </div>
                          <Switch
                            label="Auto-extend enabled"
                            isChecked={editAutoExtend}
                            onChange={(_, checked) => setEditAutoExtend(checked)}
                          />
                          <div>
                            <LabeledField style={{ fontSize: 12, display: "block", marginBottom: 4 }} label={<>Threshold (%)</>}>
                            <input style={inputStyle} type="number" value={editThresholdPct}
                                   onChange={(e) => setEditThresholdPct(Number.parseInt(e.target.value) || 80)}
                                   min={50} max={95} /></LabeledField>
                          </div>
                          <div>
                            <LabeledField style={{ fontSize: 12, display: "block", marginBottom: 4 }} label={<>Increment (GB)</>}>
                            <input style={inputStyle} type="number" value={editIncrementGb}
                                   onChange={(e) => setEditIncrementGb(Number.parseInt(e.target.value) || 64)}
                                   min={64} /></LabeledField>
                          </div>
                          <div>
                            <LabeledField style={{ fontSize: 12, display: "block", marginBottom: 4 }} label={<>Max (GB, optional)</>}>
                            <input style={inputStyle} type="number" value={editMaxGb || ""}
                                   onChange={(e) => setEditMaxGb(e.target.value ? Number.parseInt(e.target.value) : null)}
                                   placeholder="No limit" /></LabeledField>
                          </div>
                        </>
                      )}
                      <div style={{ display: "flex", gap: 8 }}>
                        <Button variant="primary" size="sm" onClick={() => saveEdit(pool)}
                                isLoading={saving} isDisabled={saving}>Save</Button>
                        <Button variant="secondary" size="sm" onClick={cancelEdit}>Cancel</Button>
                      </div>
                    </div>
    );
  }

  function renderBufferStatus(pool: StoragePool) {
    if (pool.worker_status === "connected") return (
                      <span style={{ color: "#4ade80" }}>Pattern Buffer: {pool.worker_instance_type} · {pool.worker_ip || ""}{pool.worker_private_ip ? ` (${pool.worker_private_ip})` : ""} · {pool.worker_instance_id || ""}{pool.pb_auto_sleep_minutes > 0 && pool.pb_last_activity_at && (() => {
  const idleMin = Math.floor((Date.now() - new Date(pool.pb_last_activity_at!).getTime()) / 60000);
  return (
    <span style={{ color: idleMin > pool.pb_auto_sleep_minutes * 0.8 ? "#f0ab00" : "var(--pf-t--global--text--color--subtle)", marginLeft: 8, fontSize: 10 }}>
      Idle {idleMin}m / {pool.pb_auto_sleep_minutes}m
    </span>
  );
})()}</span>
                    );
    if (pool.worker_status === "stopped") return (
                      <span style={{ opacity: 0.5 }}>Pattern Buffer: sleeping · {pool.worker_instance_type}</span>
                    );
    if (pool.worker_status === "provisioning" || pool.worker_status === "installing" || pool.worker_status === "active") return (
                      <span style={{ color: "#f0ab00" }}>Pattern Buffer: {pool.worker_status}...{pool.worker_ip ? ` · ${pool.worker_ip}` : ""}</span>
                    );
    if (pool.worker_status === "error") return (
                      <span style={{ color: "#f87171" }}>Pattern Buffer failed: {pool.worker_error || "unknown error"}</span>
                    );
    if (pool.worker_host_id) return (
                      <span style={{ color: "#f87171" }}>Pattern Buffer: {pool.worker_status || "disconnected"}{pool.worker_ip ? ` · ${pool.worker_ip}` : ""}</span>
                    );
    return (
                      <span style={{ opacity: 0.5 }}>No Pattern Buffer</span>
                    );
  }

  function finishBufferUpdate(poolId: string) {
    setPbAction(previous => { const next = { ...previous }; delete next[poolId]; return next; });
    loadData();
  }

  function renderBufferActions(pool: StoragePool) {
    return (                  <div style={{ display: "flex", gap: 6 }}>
                    {!pbAction[pool.id] && pool.worker_status === "connected" && (
                      <Button variant="secondary" size="sm"
                        isLoading={extending[`gc-${pool.id}`]}
                        isDisabled={extending[`gc-${pool.id}`]}
                        onClick={async () => {
                          setExtending((p) => ({ ...p, [`gc-${pool.id}`]: true }));
                          try {
                            const resp = await fetch(`/api/v1/hosts/${pool.worker_host_id}/gc`, { method: "POST" });
                            if (resp.ok) {
                              const r = await resp.json();
                              setAlertMsg(`Cleaned: ${r.cleanup?.cleaned || 0} orphans, ${r.cleanup?.cache_cleaned || 0} cache items`);
                            } else {
                              setAlertMsg("Clean failed");
                            }
                          } finally {
                            setExtending((p) => ({ ...p, [`gc-${pool.id}`]: false }));
                            loadData();
                          }
                        }}>Clean</Button>
                    )}
                    {(!pbAction[pool.id] || pbAction[pool.id] === "updating") && pool.worker_status === "connected" && expectedAgentVersion && pool.worker_agent_version && pool.worker_agent_version !== expectedAgentVersion && (
                      <Button variant="primary" size="sm" isLoading={pbAction[pool.id] === "updating"} isDisabled={!!pbAction[pool.id]} onClick={async () => {
                        setPbAction(prev => ({ ...prev, [pool.id]: "updating" }));
                        await fetch(`/api/v1/hosts/${pool.worker_host_id}/update-agent`, { method: "POST" });
                        setTimeout(() => finishBufferUpdate(pool.id), 5000);
                      }}>Update Pattern Buffer Agent</Button>
                    )}
                    {pool.worker_status === "connected" && (
                      <Button variant="secondary" size="sm" isLoading={pbAction[pool.id] === "sleeping"} isDisabled={!!pbAction[pool.id]} onClick={async () => {
                        if (!(await appConfirm({ message: "Stop pattern buffer? It will auto-wake when needed.", confirmLabel: "Sleep" }))) return;
                        setPbAction(prev => ({ ...prev, [pool.id]: "sleeping" }));
                        await fetch(`/api/v1/storage-pools/${pool.id}/pattern-buffer/stop`, { method: "POST" });
                        loadData();
                      }}>Sleep Pattern Buffer</Button>
                    )}
                    {pool.worker_status === "stopped" && (
                      <Button variant="secondary" size="sm" isLoading={pbAction[pool.id] === "waking"} isDisabled={!!pbAction[pool.id]} onClick={async () => {
                        setPbAction(prev => ({ ...prev, [pool.id]: "waking" }));
                        await fetch(`/api/v1/storage-pools/${pool.id}/pattern-buffer/wake`, { method: "POST" });
                        loadData();
                      }}>Wake Pattern Buffer</Button>
                    )}
                    {!pbAction[pool.id] && !["provisioning", "installing"].includes(pool.worker_status || "") && pool.status === "available" && (
                      <Button variant="secondary" size="sm" onClick={() => {
                        const prov = providers.find((p) => p.id === pool.provider_id);
                        const provType = prov?.type || "ec2";
                        const types = pbTypesByProvider[provType] || pbTypesByProvider["ec2"];
                        setPbInstanceType(types[0]?.value || "");
                        setPbPoolId(pool.id);
                      }}>
                        {pool.worker_host_id ? "Replace" : "Add"} Pattern Buffer
                      </Button>
                    )}
                    {!pbAction[pool.id] && pool.worker_host_id && (
                      <Button variant="danger" size="sm" onClick={async () => {
                        if (!(await appConfirm({
                          message: "Delete the pattern buffer instance? This will terminate the VM.",
                          confirmLabel: "Delete",
                          variant: "danger",
                        }))) return;
                        fetch(`/api/v1/storage-pools/${pool.id}/pattern-buffer`, { method: "DELETE" });
                        loadData();
                      }}>Delete Pattern Buffer</Button>
                    )}
                    {!pbAction[pool.id] && !["provisioning", "installing", "active"].includes(pool.worker_status || "") && editId !== pool.id && pool.status === "available" && (
                      <Button variant="secondary" size="sm" onClick={() => startEdit(pool)}>Edit</Button>
                    )}
                    {!pbAction[pool.id] && !["provisioning", "installing", "active"].includes(pool.worker_status || "") && (
                      <Button variant="danger" size="sm" onClick={() => handleDelete(pool)}
                              isDisabled={pool.host_count > 0 || pool.status === "creating"}>
                        Delete
                      </Button>
                    )}
                  </div>
    );
  }

  function renderPool(pool: StoragePool) {
    return (
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
renderPoolEditor(pool)
                  ) : (
                    <>
                      <div style={{ fontSize: 12, color: "var(--pf-t--global--text--color--subtle)", display: "flex", gap: 16, flexWrap: "wrap" }}>
                        {pool.az && <span>AZ: {pool.az}</span>}
                        <span>Hosts: {pool.host_count}</span>
                        {pool.fsx_filesystem_id && <span>FSx: {pool.fsx_filesystem_id}</span>}
                        {pool.azure_storage_account && <span>Account: {pool.azure_storage_account}/{pool.azure_file_share_name}</span>}
                        {(pool.fsx_throughput_mbps || pool.azure_files_throughput) && <span>Throughput: {pool.fsx_throughput_mbps || pool.azure_files_throughput} MBps</span>}
                        {poolStorageGb(pool) && <span style={poolUsage[pool.id]?.used_pct >= 80 ? { color: "#f87171", fontWeight: 600 } : undefined}>Storage: {poolUsage[pool.id] ? `${poolUsage[pool.id].used_gb} / ${poolStorageGb(pool)} GB (${poolUsage[pool.id].used_pct}%)` : `${poolStorageGb(pool)} GB`}</span>}
                        {pool.nfs_endpoint && <span>NFS: {pool.nfs_endpoint}</span>}
                      </div>
                      {pool.fsx_dns_name && (
                        <div style={{ fontSize: 11, color: "var(--pf-t--global--text--color--subtle)", marginTop: 4, fontFamily: "monospace" }}>NFS: {pool.fsx_dns_name}:/fsx</div>
                      )}
                      {pool.azure_file_share_url && (
                        <div style={{ fontSize: 11, color: "var(--pf-t--global--text--color--subtle)", marginTop: 4, fontFamily: "monospace" }}>NFS: {pool.azure_file_share_url}</div>
                      )}
                      {(pool.mode === "shared-fsx" || pool.mode === "shared-azure-files") && pool.status === "available" && (
                        <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 8, flexWrap: "wrap" }}>
                          <div style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                            <input
                              style={{ ...inputStyle, width: 80, padding: "5px 8px", fontSize: 12 }}
                              type="number"
                              value={extendTarget[pool.id] || ""}
                              onChange={(e) => setExtendTarget({ ...extendTarget, [pool.id]: e.target.value })}
                              placeholder={`${poolStorageGb(pool)} GB`}
                              min={(poolStorageGb(pool) || 0) + 1}
                            />
                            <Button
                              variant="secondary"
                              onClick={() => handleResizePool(pool)}
                              isLoading={extending[pool.id]}
                              isDisabled={extending[pool.id] || !extendTarget[pool.id] || Number.parseInt(extendTarget[pool.id]) <= (poolStorageGb(pool) || 0)}
                            >
                              Resize
                            </Button>
                          </div>
                          <span style={{ borderLeft: "1px solid var(--pf-t--global--border--color--default)", height: 24 }} />
                          <Switch
                            label={poolAutoExtendLabel(pool)}
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
                      {(pool.mode === "shared-fsx" || pool.mode === "shared-azure-files") && pool.auto_extend_enabled && expandedAutoExtend[pool.id] && (
                        <div style={{ display: "flex", gap: 16, flexWrap: "wrap", maxWidth: 600, marginTop: 8 }}>
                          <div>
                            <LabeledField style={{ fontSize: 12, display: "block", marginBottom: 4 }} label={<>Threshold (%)</>}>
                            <input
                              style={{ ...inputStyle, width: 80 }}
                              type="number"
                              value={pool.auto_extend_threshold_pct}
                              onChange={(e) => {
                                const val = Number.parseInt(e.target.value) || 80;
                                if (val >= 50 && val <= 95) updatePoolField(pool.id, "auto_extend_threshold_pct", val);
                              }}
                              min={50}
                              max={95}
                            /></LabeledField>
                          </div>
                          <div>
                            <LabeledField style={{ fontSize: 12, display: "block", marginBottom: 4 }} label={<>Increment (GB)</>}>
                            <input
                              style={{ ...inputStyle, width: 80 }}
                              type="number"
                              value={pool.auto_extend_increment_gb}
                              onChange={(e) => updatePoolField(pool.id, "auto_extend_increment_gb", Number.parseInt(e.target.value) || 64)}
                              min={64}
                            /></LabeledField>
                          </div>
                          <div>
                            <LabeledField style={{ fontSize: 12, display: "block", marginBottom: 4 }} label={<>Max (GB)</>}>
                            <input
                              style={{ ...inputStyle, width: 100 }}
                              type="number"
                              value={pool.auto_extend_max_gb || ""}
                              onChange={(e) => updatePoolField(pool.id, "auto_extend_max_gb", e.target.value ? Number.parseInt(e.target.value) : null)}
                              placeholder="No limit"
                            /></LabeledField>
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
                  <div style={{ fontSize: 11, marginBottom: 4 }}>{renderBufferStatus(pool)}</div>
                  {renderBufferActions(pool)}
                  {pool.worker_host_id && (
                    <div style={{ marginTop: 6 }}>
                      <select
                        style={{ padding: "4px 8px", borderRadius: 4, fontSize: 11, border: "1px solid var(--pf-t--global--border--color--default)", background: "var(--pf-t--global--background--color--primary--default)", color: "var(--pf-t--global--text--color--regular)" }}
                        value={pool.pb_auto_sleep_minutes}
                        onChange={async (e) => {
                          const val = Number.parseInt(e.target.value);
                          await fetch(`/api/v1/storage-pools/${pool.id}`, {
                            method: "PATCH",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({ pb_auto_sleep_minutes: val }),
                          });
                          loadData();
                        }}
                      >
                        <option value={0}>Auto-sleep: Off</option>
                        <option value={15}>Auto-sleep: 15m</option>
                        <option value={30}>Auto-sleep: 30m</option>
                        <option value={60}>Auto-sleep: 1h</option>
                        <option value={120}>Auto-sleep: 2h</option>
                        <option value={240}>Auto-sleep: 4h</option>
                      </select>
                    </div>
                  )}
                </div>
              </div>
            </CardBody>
          </Card>
    );
  }

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
                <LabeledField style={{ fontSize: 12, display: "block", marginBottom: 4 }} label={<>Mode</>}>
                <select style={inputStyle} value={newMode} onChange={(e) => handleModeChange(e.target.value)}>
                  <option value="local">Local — Pattern Buffer Only (All Providers)</option>
                  <option value="shared-fsx">FSx OpenZFS (AWS)</option>
                  <option value="shared-byo">BYO NFS (All Providers)</option>
                  <option value="shared-ceph-nfs">Ceph-NFS (OCP Virt)</option>
                  <option value="shared-azure-files">Azure Files NFS (Azure)</option>
                </select></LabeledField>
              </div>
              <div>
                <LabeledField style={{ fontSize: 12, display: "block", marginBottom: 4 }} label={<>Name</>}>
                <input style={inputStyle} value={newName} onChange={(e) => setNewName(e.target.value)}
                       placeholder="e.g. prod-east-1b" /></LabeledField>
              </div>
              {(newMode === "local" || newMode === "shared-fsx" || newMode === "shared-netapp" || newMode === "shared-azure-files") && (
                <div>
                  <LabeledField style={{ fontSize: 12, display: "block", marginBottom: 4 }} label={<>Provider</>}>
                  <select style={inputStyle} value={newProviderId} onChange={(e) => handleProviderChange(e.target.value)}>
                    <option value="">Select provider...</option>
                    {providers.filter((p) => providerSupportsMode(p, newMode)).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
                  </select></LabeledField>
                </div>
              )}
              {(newMode === "shared-fsx" || newMode === "shared-netapp" || newMode === "shared-azure-files") && (
                <div>
                  <LabeledField style={{ fontSize: 12, display: "block", marginBottom: 4 }} label={newMode === "shared-netapp" ? "Zone" : "Availability Zone"}>
                  <select style={inputStyle} value={newAz} onChange={(e) => setNewAz(e.target.value)}>
                    <option value="">{availableAzs.length ? "Select AZ..." : "Select a provider first"}</option>
                    {availableAzs.map((az) => <option key={az} value={az}>{az}</option>)}
                  </select></LabeledField>
                </div>
              )}
              {newMode === "shared-fsx" && (
                <>
                  <div>
                    <LabeledField style={{ fontSize: 12, display: "block", marginBottom: 4 }} label={<>Throughput (MBps)</>}>
                    <input style={inputStyle} type="number" value={newThroughput}
                           onChange={(e) => setNewThroughput(Number.parseInt(e.target.value) || 160)} min={160} /></LabeledField>
                  </div>
                  <div>
                    <LabeledField style={{ fontSize: 12, display: "block", marginBottom: 4 }} label={<>Storage (GB)</>}>
                    <input style={inputStyle} type="number" value={newStorageGb}
                           onChange={(e) => setNewStorageGb(Number.parseInt(e.target.value) || 64)} min={64} /></LabeledField>
                  </div>
                </>
              )}
              {newMode === "shared-netapp" && (
                <div>
                  <LabeledField style={{ fontSize: 12, display: "block", marginBottom: 4 }} label={<>Storage (GB)</>}>
                  <input style={inputStyle} type="number" value={newStorageGb}
                         onChange={(e) => setNewStorageGb(Number.parseInt(e.target.value) || 1024)} min={1024} /></LabeledField>
                </div>
              )}
              {newMode === "shared-byo" && (
                <div>
                  <LabeledField style={{ fontSize: 12, display: "block", marginBottom: 4 }} label={<>NFS Endpoint</>}>
                  <input style={inputStyle} value={newNfsEndpoint} onChange={(e) => setNewNfsEndpoint(e.target.value)}
                         placeholder="10.0.1.50:/exports/troshka" /></LabeledField>
                </div>
              )}
              {newMode === "shared-ceph-nfs" && (
                <>
                  <div>
                    <LabeledField style={{ fontSize: 12, display: "block", marginBottom: 4 }} label={<>Storage Quota (GB)</>}>
                    <input style={inputStyle} type="number" value={newStorageQuotaGb}
                           onChange={(e) => setNewStorageQuotaGb(Number.parseInt(e.target.value) || 500)} min={100} /></LabeledField>
                  </div>
                  <div>
                    <LabeledField style={{ fontSize: 12, display: "block", marginBottom: 4 }} label={<>Provider</>}>
                    <select style={inputStyle} value={newProviderId} onChange={(e) => setNewProviderId(e.target.value)}>
                      <option value="">Select provider...</option>
                      {providers.filter((p) => p.type === "ocpvirt").map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
                    </select></LabeledField>
                  </div>
                </>
              )}
              {newMode === "shared-azure-files" && (
                <>
                  <div>
                    <LabeledField style={{ fontSize: 12, display: "block", marginBottom: 4 }} label={<>Storage (GB)</>}>
                    <input style={inputStyle} type="number" value={newStorageGb}
                           onChange={(e) => setNewStorageGb(Number.parseInt(e.target.value) || 100)} min={100} /></LabeledField>
                  </div>
                  <div>
                    <LabeledField style={{ fontSize: 12, display: "block", marginBottom: 4 }} label={<>Throughput (MBps)</>}>
                    <input style={inputStyle} type="number" value={newThroughput}
                           onChange={(e) => setNewThroughput(Number.parseInt(e.target.value) || 100)} min={100} /></LabeledField>
                  </div>
                </>
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
        {pools.map(renderPool)}
      </div>
      {pbPoolId && (
        <ModalBackdrop label="Add Pattern Buffer" style={{
          position: "fixed", inset: 0, background: "rgba(0,0,0,0.6)",
          display: "flex", alignItems: "center", justifyContent: "center", zIndex: 1000,
        }} onClose={() => setPbPoolId(null)}>
          <Card style={{ width: 420 }}>
            <CardBody>
              <div style={{ fontWeight: 600, fontSize: 15, marginBottom: 12 }}>Add Pattern Buffer</div>
              <LabeledField style={{ fontSize: 12, display: "block", marginBottom: 4 }} label={<>Instance Type</>}>
              <select
                style={inputStyle}
                value={pbInstanceType}
                onChange={(e) => setPbInstanceType(e.target.value)}
              >
                {(pbTypesByProvider[providers.find((p) => p.id === pools.find((pp) => pp.id === pbPoolId)?.provider_id)?.type || "ec2"] || pbTypesByProvider["ec2"]).map((t) => (
                  <option key={t.value} value={t.value}>{t.label}</option>
                ))}
              </select></LabeledField>
              <div style={{ display: "flex", gap: 8, marginTop: 16, justifyContent: "flex-end" }}>
                <Button variant="secondary" size="sm" onClick={() => setPbPoolId(null)}>Cancel</Button>
                <Button variant="primary" size="sm" onClick={() => {
                  fetch(`/api/v1/storage-pools/${pbPoolId}/pattern-buffer`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ instance_type: pbInstanceType }),
                  });
                  setPbPoolId(null);
                  loadData();
                }}>Provision</Button>
              </div>
            </CardBody>
          </Card>
        </ModalBackdrop>
      )}
      {pbError && (
        <ModalBackdrop label="Pattern Buffer Error" style={{
          position: "fixed", inset: 0, background: "rgba(0,0,0,0.6)",
          display: "flex", alignItems: "center", justifyContent: "center", zIndex: 1000,
        }} onClose={() => setPbError(null)}>
          <Card style={{ width: 500 }}>
            <CardBody>
              <div style={{ fontWeight: 600, fontSize: 15, marginBottom: 12, color: "#f87171" }}>Pattern Buffer Error</div>
              <div style={{ fontSize: 13, marginBottom: 16 }}>{pbError}</div>
              <div style={{ display: "flex", justifyContent: "flex-end" }}>
                <Button variant="secondary" size="sm" onClick={() => { if (pbErrorPoolId) { pbErrorDismissedRef.current.add(pbErrorPoolId); } setPbError(null); }}>Close</Button>
              </div>
            </CardBody>
          </Card>
        </ModalBackdrop>
      )}
      <AlertModal message={alertMsg} onClose={() => setAlertMsg(null)} />
    </PageSection>
  );
}
