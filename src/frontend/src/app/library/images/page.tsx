"use client";

import React, { useEffect, useState } from "react";
import TagEditor from "@/components/TagEditor";
import {
  Button,
  Card,
  CardBody,
  FileUpload,
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
  tags: {
    ocp_default_iso?: boolean;
    ocp_default_image?: boolean;
    user_tags?: string[];
  } | null;
  created_at: string;
  source_url?: string | null;
  source?: string;
  readonly?: boolean;
}

export default function ImagesPage() {
  const [items, setItems] = useState<LibraryItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [filter, setFilter] = useState("");
  const [typeFilter, setTypeFilter] = useState("");
  const [uploading, setUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState("");
  const [toast, setToast] = useState<string | null>(null);
  const showToast = (msg: string) => { setToast(msg); setTimeout(() => setToast(null), 4000); };
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [selectedFileName, setSelectedFileName] = useState("");

  const [showUpload, setShowUpload] = useState(false);
  const [selectedItems, setSelectedItems] = useState<Set<string>>(new Set());
  const [sourceMode, setSourceMode] = useState<"file" | "url">("file");
  const [importUrl, setImportUrl] = useState("");
  const [newName, setNewName] = useState("");
  const [editItem, setEditItem] = useState<LibraryItem | null>(null);
  const [editName, setEditName] = useState("");
  const [editUrl, setEditUrl] = useState("");
  const [editingName, setEditingName] = useState<string | null>(null);
  const [editNameValue, setEditNameValue] = useState("");
  const [editOcpDefault, setEditOcpDefault] = useState(false);
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

  // Auto-refresh when any item is importing
  useEffect(() => {
    if (items.some((i) => ["importing", "uploading", "downloading", "uploading_s3"].includes(i.state))) {
      const interval = setInterval(loadItems, 3000);
      return () => clearInterval(interval);
    }
  }, [items]);

  const handleUpload = async () => {
    if (!newName.trim()) { setError("Name is required"); return; }
    if (sourceMode === "file" && !selectedFile) { setError("Select a file"); return; }
    if (sourceMode === "url" && !importUrl.trim()) { setError("Enter a URL"); return; }
    const file = selectedFile;

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

      if (sourceMode === "url") {
        // Import from URL — server-side download
        setUploadProgress("Importing from URL...");
        const importResp = await fetch(`/api/v1/library/${id}/import-url`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ url: importUrl }),
        });
        if (importResp.ok) {
          setUploadProgress("");
          setShowUpload(false);
          setNewName(""); setNewDesc(""); setImportUrl("");
          showToast("Import started — download in progress on server");
          loadItems();
        } else {
          setError("Failed to start import");
        }
        setUploading(false);
        return;
      }

      // Step 2: Start multipart upload
      setUploadProgress("Preparing upload...");
      const startResp = await fetch(`/api/v1/library/${id}/upload-start`, { method: "POST" });
      if (!startResp.ok) { setError("Failed to start upload"); setUploading(false); return; }
      const { upload_id } = await startResp.json();

      // Step 3: Upload parts (500 MB chunks)
      const CHUNK_SIZE = 500 * 1024 * 1024;
      const totalParts = Math.ceil(file!.size / CHUNK_SIZE);
      const parts: Array<{ part_number: number; etag: string }> = [];
      let uploaded = 0;

      for (let i = 0; i < totalParts; i++) {
        const partNumber = i + 1;
        const start = i * CHUNK_SIZE;
        const end = Math.min(start + CHUNK_SIZE, file!.size);
        const chunk = file!.slice(start, end);

        // Get presigned URL for this part
        if (partNumber === 1) setUploadProgress(`Reading file (${formatSize(file!.size)})...`);
        const partResp = await fetch(`/api/v1/library/${id}/upload-part-url?upload_id=${upload_id}&part_number=${partNumber}`, { method: "POST" });
        if (!partResp.ok) throw new Error("Failed to get part URL");
        const { url } = await partResp.json();

        // Upload the chunk
        const xhr = new XMLHttpRequest();
        xhr.open("PUT", url);

        const etag = await new Promise<string>((resolve, reject) => {
          xhr.upload.onprogress = (e) => {
            if (e.lengthComputable) {
              const totalUploaded = uploaded + e.loaded;
              const pct = Math.round((totalUploaded / file!.size) * 100);
              setUploadProgress(`Uploading... ${pct}% (${formatSize(totalUploaded)} / ${formatSize(file!.size)})`);
            }
          };
          xhr.onload = () => {
            if (xhr.status >= 200 && xhr.status < 300) {
              resolve(xhr.getResponseHeader("ETag") || "");
            } else {
              reject(new Error(`Part ${partNumber} failed (HTTP ${xhr.status})`));
            }
          };
          xhr.onerror = () => reject(new Error(`Part ${partNumber} network error`));
          xhr.send(chunk);
        });

        parts.push({ part_number: partNumber, etag });
        uploaded = end;
      }

      // Step 4: Complete multipart upload
      setUploadProgress("Finalizing...");
      const completeResp = await fetch(`/api/v1/library/${id}/upload-complete`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ upload_id, parts }),
      });
      if (completeResp.ok) {
        setUploadProgress("");
        setShowUpload(false);
        setNewName("");
        setNewDesc("");
        setSelectedFile(null);
        setSelectedFileName("");
        loadItems();
      } else {
        setError("Upload finalization failed");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to connect to server");
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
  const stateColors: Record<string, string> = { ready: "#4ade80", uploading: "#fbbf24", importing: "#fbbf24", downloading: "#fbbf24", uploading_s3: "#22d3ee", pending: "#94a3b8", error: "#f87171" };

  if (loading) return <PageSection><Title headingLevel="h1">Loading...</Title></PageSection>;

  return (
    <>
      <PageSection>
        <Toolbar>
          <ToolbarContent>
            <ToolbarItem><Title headingLevel="h1">Images</Title></ToolbarItem>
            <ToolbarItem>
              <select style={inputStyle} value={typeFilter} onChange={(e) => setTypeFilter(e.target.value)}>
                <option value="">All Types</option>
                <option value="iso">ISOs</option>
                <option value="image">Disk Images</option>
              </select>
            </ToolbarItem>
            <ToolbarItem>
              <input style={{ ...inputStyle, width: 200 }} placeholder="Search images..." value={filter} onChange={(e) => setFilter(e.target.value)} />
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
                  {newType !== "iso" && (
                    <div style={{ flex: 1 }}>
                      <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Format</label>
                      <select style={inputStyle} value={newFormat} onChange={(e) => setNewFormat(e.target.value)}>
                        <option value="qcow2">QCOW2</option>
                        <option value="raw">Raw</option>
                      </select>
                    </div>
                  )}
                </div>
                <div>
                  <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Description (optional)</label>
                  <input style={inputStyle} value={newDesc} onChange={(e) => setNewDesc(e.target.value)} placeholder="Notes about this image" />
                </div>
                <div>
                  <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Source</label>
                  <div style={{ display: "flex", gap: 8, marginBottom: 8 }}>
                    <button onClick={() => setSourceMode("file")} style={{ ...inputStyle, textAlign: "center" as const, cursor: "pointer", background: sourceMode === "file" ? "rgba(74,222,128,0.15)" : undefined, borderColor: sourceMode === "file" ? "#4ade80" : undefined }}>
                      Upload File
                    </button>
                    <button onClick={() => setSourceMode("url")} style={{ ...inputStyle, textAlign: "center" as const, cursor: "pointer", background: sourceMode === "url" ? "rgba(74,222,128,0.15)" : undefined, borderColor: sourceMode === "url" ? "#4ade80" : undefined }}>
                      Import from URL
                    </button>
                  </div>
                  {sourceMode === "file" ? (
                    <FileUpload
                      id="library-file-upload"
                      value={selectedFile ?? undefined}
                      filename={selectedFileName}
                      onFileInputChange={(_e, file) => {
                        setSelectedFile(file);
                        setSelectedFileName(file.name);
                        if (!newName.trim()) setNewName(file.name.replace(/\.[^.]+$/, ""));
                      }}
                      onClearClick={() => {
                        setSelectedFile(null);
                        setSelectedFileName("");
                      }}
                      browseButtonText="Browse"
                      hideDefaultPreview
                      dropzoneProps={{
                        accept: newType === "iso"
                          ? { "application/x-iso9660-image": [".iso"] }
                          : { "application/octet-stream": [".qcow2", ".raw", ".img"] },
                      }}
                    />
                  ) : (
                    <input style={inputStyle} value={importUrl} onChange={(e) => setImportUrl(e.target.value)} placeholder="https://example.com/rhel-9.4-x86_64-dvd.iso" />
                  )}
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
        {items.length > 0 && (() => {
          const selected = items.filter((i) => selectedItems.has(i.id));
          const allSelected = selected.length === items.length;
          const someSelected = selected.length > 0;
          return (
            <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 8, flexWrap: "wrap" }}>
              <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 13, cursor: "pointer" }}>
                <input type="checkbox" checked={allSelected} onChange={() => {
                  if (allSelected) setSelectedItems(new Set());
                  else setSelectedItems(new Set(items.map((i) => i.id)));
                }} />
                {someSelected ? `${selected.length} of ${items.length} selected` : "Select all"}
              </label>
              {someSelected && (
                <Button variant="danger" size="sm" onClick={() => {
                  if (!window.confirm(`Delete ${selected.length} library item(s)? Files will be removed from S3.`)) return;
                  for (const i of selected) { fetch(`/api/v1/library/${i.id}`, { method: "DELETE" }); }
                  setSelectedItems(new Set());
                  setTimeout(loadItems, 1000);
                }}>Delete ({selected.length})</Button>
              )}
            </div>
          );
        })()}
        {items.length === 0 && !showUpload && (
          <p style={{ opacity: 0.6 }}>No items in library. Click &quot;+ Upload&quot; to add ISOs or disk images.</p>
        )}
        {items.map((item) => (
          <Card key={item.id} style={{ marginBottom: 8 }}>
            <CardBody style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
              <div style={{ flex: 1 }}>
                <div style={{ display: "flex", alignItems: "flex-start", gap: 8 }}>
                  <input type="checkbox" checked={selectedItems.has(item.id)} onChange={() => setSelectedItems((prev) => {
                    const next = new Set(prev);
                    if (next.has(item.id)) next.delete(item.id); else next.add(item.id);
                    return next;
                  })} style={{ width: 18, height: 18, minWidth: 18, cursor: "pointer", marginTop: 2 }} />
                  <span style={{ fontSize: 18 }}>{item.format === "iso" ? "💿" : "🛢"}</span>
                  {editingName === item.id ? (
                    <input
                      autoFocus
                      value={editNameValue}
                      onChange={(e) => setEditNameValue(e.target.value)}
                      onBlur={() => {
                        const trimmed = editNameValue.trim();
                        if (trimmed && trimmed !== item.name) {
                          fetch(`/api/v1/library/${item.id}`, {
                            method: "PATCH",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({ name: trimmed }),
                          }).then((r) => { if (r.ok) loadItems(); });
                        }
                        setEditingName(null);
                      }}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") (e.target as HTMLInputElement).blur();
                        if (e.key === "Escape") setEditingName(null);
                      }}
                      onClick={(e) => e.stopPropagation()}
                      style={{
                        fontSize: "inherit", fontWeight: "bold", fontFamily: "inherit",
                        background: "var(--pf-t--global--background--color--primary--default)",
                        color: "var(--pf-t--global--text--color--regular)",
                        border: "1px solid var(--pf-t--global--border--color--default)",
                        borderRadius: 4, padding: "2px 6px", minWidth: 200,
                      }}
                    />
                  ) : (
                    <strong
                      onClick={item.readonly ? undefined : () => { setEditingName(item.id); setEditNameValue(item.name); }}
                      title={item.readonly ? undefined : "Click to rename"}
                      style={{ cursor: item.readonly ? "default" : "text" }}
                    >{item.name}</strong>
                  )}
                  <span style={{ fontSize: 11, padding: "1px 6px", borderRadius: 4, background: `${stateColors[item.state] || "#94a3b8"}22`, color: stateColors[item.state] || "#94a3b8" }}>
                    {item.state === "downloading" ? (item.size_bytes > 0 ? `downloading from URL · ${formatSize(item.size_bytes)}` : "starting download...")
                      : item.state === "uploading_s3" ? (() => {
                          const tags = item.tags as Record<string, number> | null;
                          const tp = tags?.total_parts || 0;
                          const up = tags?.uploaded_parts || 0;
                          return tp ? `uploading to library · ${Math.round((up / tp) * 100)}%` : `uploading to library · ${formatSize(item.size_bytes)}`;
                        })()
                      : item.state === "importing" ? (item.size_bytes > 0 ? `importing · ${formatSize(item.size_bytes)}` : "starting download...")
                      : item.state}
                  </span>
                  <span style={{ fontSize: 11, padding: "1px 6px", borderRadius: 4, background: "rgba(148,163,184,0.15)", color: "#94a3b8" }}>
                    {item.format === "iso" ? "ISO" : item.format}
                  </span>
                  {item.source === "central" && (
                    <span style={{ fontSize: 11, padding: "1px 6px", borderRadius: 4, background: "rgba(59,130,246,0.15)", color: "#3b82f6" }}>
                      Central
                    </span>
                  )}
                </div>
                <div style={{ fontSize: 12, opacity: 0.6, marginTop: 4 }}>
                  {item.state !== "importing" && formatSize(item.size_bytes)}
                  {item.description && `${item.state !== "importing" ? " · " : ""}${item.description}`}
                  {" · "}{new Date(item.created_at).toLocaleDateString()}
                </div>
              </div>
            </CardBody>
            <CardBody style={{ borderTop: "1px solid var(--pf-t--global--border--color--default)", display: "flex", gap: 8, flexWrap: "wrap", paddingTop: 8, paddingBottom: 8, alignItems: "center" }}>
              {item.state === "ready" && !item.readonly && (
                <label style={{ fontSize: 12, display: "flex", alignItems: "center", gap: 6, cursor: "pointer", marginRight: 8, opacity: 0.7 }}>
                  <input type="checkbox" checked={item.format === "iso" ? (item.tags?.ocp_default_iso || false) : (item.tags?.ocp_default_image || false)} onChange={async (e) => {
                    const tagKey = item.format === "iso" ? "ocp_default_iso" : "ocp_default_image";
                    await fetch(`/api/v1/library/${item.id}`, {
                      method: "PATCH",
                      headers: { "Content-Type": "application/json" },
                      body: JSON.stringify({ tags: { ...(item.tags || {}), [tagKey]: e.target.checked || undefined } }),
                    });
                    loadItems();
                  }} />
                  OCP Default
                </label>
              )}
              <TagEditor
                tags={(item.tags?.user_tags as string[]) || []}
                onAdd={async (tag) => {
                  const cur = (item.tags?.user_tags as string[]) || [];
                  await fetch(`/api/v1/library/${item.id}`, {
                    method: "PATCH",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ tags: { ...(item.tags || {}), user_tags: [...cur, tag] } }),
                  });
                  loadItems();
                }}
                onRemove={async (tag) => {
                  const cur = (item.tags?.user_tags as string[]) || [];
                  await fetch(`/api/v1/library/${item.id}`, {
                    method: "PATCH",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ tags: { ...(item.tags || {}), user_tags: cur.filter((t: string) => t !== tag) } }),
                  });
                  loadItems();
                }}
              />
              {["error", "pending"].includes(item.state) && (
                <Button variant="secondary" onClick={() => {
                  setEditItem(item);
                  setEditName(item.name);
                  setEditUrl(item.source_url || "");
                }}>
                  Edit URL
                </Button>
              )}
              {["importing", "uploading", "downloading", "uploading_s3"].includes(item.state) && (
                <Button variant="secondary" onClick={async () => {
                  if (!window.confirm("Cancel this transfer?")) return;
                  await fetch(`/api/v1/library/${item.id}/cancel`, { method: "POST" });
                  loadItems();
                }}>
                  Cancel
                </Button>
              )}
              <Button variant="danger" onClick={() => deleteItem(item.id)} isDisabled={item.readonly || ["uploading", "importing", "downloading", "uploading_s3"].includes(item.state)}>
                Delete
              </Button>
            </CardBody>
          </Card>
        ))}
      </PageSection>
      {editItem && (
        <div style={{
          position: "fixed", inset: 0, zIndex: 10000,
          display: "flex", alignItems: "center", justifyContent: "center",
          background: "rgba(0,0,0,0.6)",
        }} onClick={() => setEditItem(null)}>
          <div style={{
            background: "var(--pf-t--global--background--color--primary--default)",
            borderRadius: 12, padding: 24, width: 450,
            boxShadow: "0 8px 32px rgba(0,0,0,0.5)",
            border: "1px solid var(--pf-t--global--border--color--default)",
          }} onClick={(e) => e.stopPropagation()}>
            <h3 style={{ margin: "0 0 16px" }}>Edit Source URL</h3>
            <div style={{ marginBottom: 16 }}>
              <label style={{ fontSize: 12, color: "var(--pf-t--global--text--color--subtle)", display: "block", marginBottom: 4 }}>Source URL</label>
              <input
                autoFocus
                value={editUrl}
                onChange={(e) => setEditUrl(e.target.value)}
                placeholder="https://..."
                style={{
                  width: "100%", padding: "6px 10px", borderRadius: 4, fontSize: 13,
                  border: "1px solid var(--pf-t--global--border--color--default)",
                  background: "var(--pf-t--global--background--color--secondary--default)",
                  color: "var(--pf-t--global--text--color--regular)",
                  fontFamily: "monospace",
                }}
              />
            </div>
            <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
              <Button variant="secondary" onClick={() => setEditItem(null)}>Cancel</Button>
              <Button variant="primary" onClick={async () => {
                if (editUrl !== (editItem.source_url || "")) {
                  await fetch(`/api/v1/library/${editItem.id}`, {
                    method: "PATCH",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ source_url: editUrl }),
                  });
                }
                setEditItem(null);
                loadItems();
              }}>Save</Button>
              {editUrl && (
                <Button variant="primary" style={{ background: "#3e8635" }} onClick={async () => {
                  if (editUrl !== (editItem.source_url || "")) {
                    await fetch(`/api/v1/library/${editItem.id}`, {
                      method: "PATCH",
                      headers: { "Content-Type": "application/json" },
                      body: JSON.stringify({ source_url: editUrl }),
                    });
                  }
                  await fetch(`/api/v1/library/${editItem.id}/import-url`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ url: editUrl }),
                  });
                  setEditItem(null);
                  loadItems();
                }}>Save & Retry</Button>
              )}
            </div>
          </div>
        </div>
      )}
      {toast && (
        <div style={{
          position: "fixed", bottom: 24, left: "50%", transform: "translateX(-50%)",
          padding: "8px 20px", borderRadius: 8,
          background: "rgba(30,30,50,0.95)", color: "#4ade80",
          fontSize: 13, boxShadow: "0 4px 20px rgba(0,0,0,0.4)",
          border: "1px solid rgba(74,222,128,0.3)", zIndex: 1000,
        }}>
          {toast}
        </div>
      )}
    </>
  );
}
