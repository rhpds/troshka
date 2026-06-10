"use client";

import { useEffect, useState } from "react";
import {
  PageSection,
  Title,
  Button,
  Card,
  CardBody,
  Alert,
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

  const [editId, setEditId] = useState<string | null>(null);
  const [editThroughput, setEditThroughput] = useState(160);
  const [editNfsEndpoint, setEditNfsEndpoint] = useState("");
  const [saving, setSaving] = useState(false);

  const loadData = () => {
    Promise.all([
      fetch("/api/v1/storage-pools").then((r) => (r.ok ? r.json() : [])),
      fetch("/api/v1/providers/").then((r) => (r.ok ? r.json() : [])),
    ]).then(([p, prov]) => {
      setPools(p);
      setProviders(prov);
    });
  };

  useEffect(() => {
    loadData();
    const interval = setInterval(loadData, 10000);
    return () => clearInterval(interval);
  }, []);

  const handleCreate = async () => {
    setError("");
    if (!newName.trim()) { setError("Name is required"); return; }
    if (newMode === "shared-fsx" && !newProviderId) { setError("Provider is required for FSx pools"); return; }
    if (newMode === "shared-fsx" && !newAz) { setError("AZ is required for FSx pools"); return; }
    if (newMode === "shared-byo" && !newNfsEndpoint) { setError("NFS endpoint is required"); return; }
    if (newMode === "shared-byo" && !newAz) { setError("AZ is required for BYO NFS pools"); return; }

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
    } else {
      const data = await resp.json();
      setError(data.detail || "Failed to create pool");
    }
  };

  const startEdit = (pool: StoragePool) => {
    setEditId(pool.id);
    setEditThroughput(pool.fsx_throughput_mbps || 160);
    setEditNfsEndpoint(pool.nfs_endpoint || "");
  };

  const cancelEdit = () => {
    setEditId(null);
  };

  const saveEdit = async (pool: StoragePool) => {
    setSaving(true);
    setError("");
    const body: Record<string, unknown> = {};
    if (pool.mode === "shared-fsx") {
      body.fsx_throughput_mbps = editThroughput;
    }
    if (pool.mode === "shared-byo") {
      body.nfs_endpoint = editNfsEndpoint;
    }
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
                <select style={inputStyle} value={newMode} onChange={(e) => setNewMode(e.target.value)}>
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
                  <select style={inputStyle} value={newProviderId} onChange={(e) => setNewProviderId(e.target.value)}>
                    <option value="">Select provider...</option>
                    {providers.filter((p) => p.type === "ec2").map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
                  </select>
                </div>
              )}
              {(newMode === "shared-fsx" || newMode === "shared-byo") && (
                <div>
                  <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Availability Zone</label>
                  <input style={inputStyle} value={newAz} onChange={(e) => setNewAz(e.target.value)}
                         placeholder="e.g. us-east-1b" />
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
                        <div>
                          <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Throughput (MBps)</label>
                          <input style={inputStyle} type="number" value={editThroughput}
                                 onChange={(e) => setEditThroughput(parseInt(e.target.value) || 160)} min={160} />
                        </div>
                      )}
                      {pool.mode === "shared-byo" && (
                        <div>
                          <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>NFS Endpoint</label>
                          <input style={inputStyle} value={editNfsEndpoint}
                                 onChange={(e) => setEditNfsEndpoint(e.target.value)}
                                 placeholder="10.0.1.50:/exports/troshka" />
                        </div>
                      )}
                      <div style={{ display: "flex", gap: 8 }}>
                        <Button variant="primary" size="sm" onClick={() => saveEdit(pool)}
                                isLoading={saving} isDisabled={saving}>Save</Button>
                        <Button variant="secondary" size="sm" onClick={cancelEdit}>Cancel</Button>
                      </div>
                    </div>
                  ) : (
                    <div style={{ fontSize: 12, color: "var(--pf-t--global--text--color--subtle)", display: "flex", gap: 16 }}>
                      {pool.az && <span>AZ: {pool.az}</span>}
                      <span>Hosts: {pool.host_count}</span>
                      {pool.fsx_filesystem_id && <span>FSx: {pool.fsx_filesystem_id}</span>}
                      {pool.fsx_throughput_mbps && <span>Throughput: {pool.fsx_throughput_mbps} MBps</span>}
                      {pool.fsx_storage_gb && <span>Storage: {pool.fsx_storage_gb} GB</span>}
                      {pool.nfs_endpoint && <span>NFS: {pool.nfs_endpoint}</span>}
                    </div>
                  )}
                </div>
                <div style={{ display: "flex", gap: 6 }}>
                  {editId !== pool.id && pool.status === "available" && (
                    <Button variant="secondary" size="sm" onClick={() => startEdit(pool)}>Edit</Button>
                  )}
                  <Button variant="danger" size="sm" onClick={() => handleDelete(pool)}
                          isDisabled={pool.host_count > 0 || pool.status === "creating"}>
                    Delete
                  </Button>
                </div>
              </div>
            </CardBody>
          </Card>
        ))}
      </div>
    </PageSection>
  );
}
