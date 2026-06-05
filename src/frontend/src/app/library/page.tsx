"use client";

import React, { useEffect, useState, useRef } from "react";
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

interface LibraryItem {
  id: string;
  name: string;
  description: string;
  type: string;
  format: string;
  size_bytes: number;
  os_variant: string;
  state: string;
  tags: string[] | null;
  created_at: string;
}

export default function LibraryPage() {
  const [items, setItems] = useState<LibraryItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [filter, setFilter] = useState("");
  const [typeFilter, setTypeFilter] = useState("");
  const [uploading, setUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);

  const [showUpload, setShowUpload] = useState(false);
  const [newName, setNewName] = useState("");
  const [newType, setNewType] = useState("iso");
  const [newFormat, setNewFormat] = useState("iso");
  const [newOs, setNewOs] = useState("");
  const [newDesc, setNewDesc] = useState("");

  const loadItems = () => {
    let url = "/api/v1/library/";
    const params = new URLSearchParams();
    if (typeFilter) params.set("type", typeFilter);
    if (filter) params.set("q", filter);
    if (params.toString()) url += `?${params.toString()}`;

    fetch(url)
      .then((r) => r.ok ? r.json() : [])
      .then((data) => { setItems(Array.isArray(data) ? data : []); setLoading(false); })
      .catch(() => setLoading(false));
  };

  useEffect(() => { loadItems(); }, [typeFilter, filter]);

  const handleUpload = async () => {
    const file = fileRef.current?.files?.[0];
    if (!file || !newName.trim()) {
      setError("Name and file are required");
      return;
    }

    setUploading(true);
    setUploadProgress("Creating item...");
    setError("");

    try {
      // Step 1: Create metadata
      const createResp = await fetch("/api/v1/library/", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: newName,
          description: newDesc,
          type: newType,
          format: newFormat,
          os_variant: newOs,
        }),
      });
      if (!createResp.ok) {
        setError("Failed to create item");
        setUploading(false);
        return;
      }
      const { id } = await createResp.json();

      // Step 2: Upload file
      setUploadProgress(`Uploading ${file.name} (${formatSize(file.size)})...`);
      const formData = new FormData();
      formData.append("file", file);

      const uploadResp = await fetch(`/api/v1/library/${id}/upload`, {
        method: "POST",
        body: formData,
      });

      if (uploadResp.ok) {
        setUploadProgress("");
        setShowUpload(false);
        setNewName("");
        setNewDesc("");
        loadItems();
      } else {
        setError("Upload failed");
      }
    } catch {
      setError("Failed to connect to server");
    }
    setUploading(false);
  };

  const deleteItem = async (id: string) => {
    if (!window.confirm("Delete this library item? The file will be removed from S3.")) return;
    await fetch(`/api/v1/library/${id}`, { method: "DELETE" });
    loadItems();
  };

  const formatSize = (bytes: number) => {
    if (bytes === 0) return "0 B";
    if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
    if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
    return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
  };

  const inputStyle = { width: "100%", padding: "6px 10px", borderRadius: 6, border: "1px solid var(--pf-t--global--border--color--default)", background: "var(--pf-t--global--background--color--primary--default)", color: "var(--pf-t--global--text--color--regular)", fontSize: 13 };
  const stateColors: Record<string, string> = { ready: "#4ade80", uploading: "#fbbf24", pending: "#94a3b8", error: "#f87171" };

  if (loading) return <PageSection><Title headingLevel="h1">Loading...</Title></PageSection>;

  return (
    <>
      <PageSection>
        <Toolbar>
          <ToolbarContent>
            <ToolbarItem><Title headingLevel="h1">Library</Title></ToolbarItem>
            <ToolbarItem>
              <select style={inputStyle} value={typeFilter} onChange={(e) => setTypeFilter(e.target.value)}>
                <option value="">All Types</option>
                <option value="iso">ISOs</option>
                <option value="image">Disk Images</option>
              </select>
            </ToolbarItem>
            <ToolbarItem>
              <input style={inputStyle} placeholder="Search..." value={filter} onChange={(e) => setFilter(e.target.value)} />
            </ToolbarItem>
            <ToolbarItem align={{ default: "alignEnd" }}>
              <Button variant="primary" onClick={() => setShowUpload(!showUpload)}>
                {showUpload ? "Cancel" : "+ Upload"}
              </Button>
            </ToolbarItem>
          </ToolbarContent>
        </Toolbar>
      </PageSection>

      {error && <PageSection><Alert variant="danger" title={error} /></PageSection>}

      {showUpload && (
        <PageSection>
          <Card>
            <CardBody>
              <Title headingLevel="h3" size="md" style={{ marginBottom: 12 }}>Upload to Library</Title>
              <div style={{ display: "flex", flexDirection: "column", gap: 10, maxWidth: 500 }}>
                <div>
                  <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Name</label>
                  <input style={inputStyle} value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="e.g. RHEL 10.2 Install DVD" />
                </div>
                <div style={{ display: "flex", gap: 10 }}>
                  <div style={{ flex: 1 }}>
                    <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Type</label>
                    <select style={inputStyle} value={newType} onChange={(e) => {
                      setNewType(e.target.value);
                      setNewFormat(e.target.value === "iso" ? "iso" : "qcow2");
                    }}>
                      <option value="iso">ISO</option>
                      <option value="image">Disk Image</option>
                    </select>
                  </div>
                  <div style={{ flex: 1 }}>
                    <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Format</label>
                    <select style={inputStyle} value={newFormat} onChange={(e) => setNewFormat(e.target.value)}>
                      {newType === "iso" ? (
                        <option value="iso">ISO</option>
                      ) : (
                        <>
                          <option value="qcow2">QCOW2</option>
                          <option value="raw">Raw</option>
                        </>
                      )}
                    </select>
                  </div>
                </div>
                <div>
                  <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>OS Variant (optional)</label>
                  <input style={inputStyle} value={newOs} onChange={(e) => setNewOs(e.target.value)} placeholder="e.g. rhel10, win11" />
                </div>
                <div>
                  <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Description (optional)</label>
                  <input style={inputStyle} value={newDesc} onChange={(e) => setNewDesc(e.target.value)} placeholder="Notes about this image" />
                </div>
                <div>
                  <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>File</label>
                  <input type="file" ref={fileRef} accept=".iso,.qcow2,.raw,.img" style={{ fontSize: 13 }} />
                </div>
                <Button variant="primary" onClick={handleUpload} isLoading={uploading} isDisabled={uploading} style={{ alignSelf: "flex-start" }}>
                  {uploading ? uploadProgress : "Upload"}
                </Button>
              </div>
            </CardBody>
          </Card>
        </PageSection>
      )}

      <PageSection>
        {items.length === 0 && !showUpload && (
          <p style={{ opacity: 0.6 }}>No items in library. Click &quot;+ Upload&quot; to add ISOs or disk images.</p>
        )}
        {items.map((item) => (
          <Card key={item.id} style={{ marginBottom: 8 }}>
            <CardBody style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <div style={{ flex: 1 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <span style={{ fontSize: 18 }}>{item.format === "iso" ? "💿" : "🛢"}</span>
                  <strong>{item.name}</strong>
                  <span style={{ fontSize: 11, padding: "1px 6px", borderRadius: 4, background: `${stateColors[item.state] || "#94a3b8"}22`, color: stateColors[item.state] || "#94a3b8" }}>
                    {item.state}
                  </span>
                  <span style={{ fontSize: 11, padding: "1px 6px", borderRadius: 4, background: "rgba(148,163,184,0.15)", color: "#94a3b8" }}>
                    {item.type}
                  </span>
                  <span style={{ fontSize: 11, padding: "1px 6px", borderRadius: 4, background: "rgba(148,163,184,0.15)", color: "#94a3b8" }}>
                    {item.format}
                  </span>
                </div>
                <div style={{ fontSize: 12, opacity: 0.6, marginTop: 4 }}>
                  {formatSize(item.size_bytes)}
                  {item.os_variant && ` · ${item.os_variant}`}
                  {item.description && ` · ${item.description}`}
                  {" · "}{new Date(item.created_at).toLocaleDateString()}
                </div>
              </div>
              <Button variant="danger" onClick={() => deleteItem(item.id)} isDisabled={item.state === "uploading"}>
                Delete
              </Button>
            </CardBody>
          </Card>
        ))}
      </PageSection>
    </>
  );
}
