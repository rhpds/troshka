"use client";

import React, { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import TagEditor from "@/components/TagEditor";
import {
  Button,
  Card,
  CardBody,
  CardTitle,
  EmptyState,
  EmptyStateBody,
  Label,
  PageSection,
  Title,
  Toolbar,
  ToolbarContent,
  ToolbarItem,
  Tooltip,
} from "@patternfly/react-core";
import BulkDeployModal from "@/components/canvas/BulkDeployModal";
import PatternPreviewModal from "@/components/canvas/PatternPreviewModal";

interface PatternDisk {
  id: string;
  name: string;
  size_gb: number;
}

interface Pattern {
  id: string;
  name: string;
  description: string;
  visibility: string;
  state: string;
  disk_count: number;
  total_size_gb: number;
  total_size_bytes: number;
  capture_progress?: { step?: string; detail?: string; vms?: string[] };
  tags: Record<string, any> | null;
  disks: PatternDisk[];
  created_at: string;
  owner_id: string;
  total_vcpus?: number;
  total_ram_gb?: number;
  total_disk_gb?: number;
  vm_count?: number;
  is_ocp?: boolean;
}

function DeployNameModal({ patternName, deploying, onDeploy, onClose }: {
  patternName: string; deploying: boolean;
  onDeploy: (name: string, guid?: string, domain?: string, dnsProviderId?: string, autoDeploy?: boolean, autoStart?: boolean, hostId?: string) => void;
  onClose: () => void;
}) {
  const [name, setName] = useState(patternName);
  const [guid, setGuid] = useState("");
  const [domain, setDomain] = useState("");
  const [dnsProviderId, setDnsProviderId] = useState("");
  const [dnsProviders, setDnsProviders] = useState<Array<{id: string; name: string}>>([]);
  const [autoDeploy, setAutoDeploy] = useState(true);
  const [autoStart, setAutoStart] = useState(true);
  const [userRole, setUserRole] = useState("");
  const [availableHosts, setAvailableHosts] = useState<Array<{id: string; ip_address: string; instance_id: string; provider_type: string; used_vcpus: number; total_vcpus: number; used_ram_mb: number; total_ram_mb: number}>>([]);
  const [deployHostId, setDeployHostId] = useState("");

  useEffect(() => {
    fetch("/api/v1/dns-providers")
      .then(r => r.ok ? r.json() : [])
      .then(data => setDnsProviders(Array.isArray(data) ? data : []))
      .catch(() => {});
    fetch("/api/v1/auth/me").then(r => r.ok ? r.json() : {}).then(d => {
      setUserRole(d.role || "");
      if (d.role === "admin") {
        fetch("/api/v1/hosts/").then(r => r.ok ? r.json() : []).then(hosts => {
          setAvailableHosts(hosts.filter((h: any) => h.state === "active" && h.agent_status === "connected" && h.host_type !== "pattern_buffer"));
        });
      }
    });
  }, []);

  const inputStyle = {
    width: "100%", padding: "6px 10px", borderRadius: 6,
    border: "1px solid var(--pf-t--global--border--color--default)",
    background: "var(--pf-t--global--background--color--primary--default)",
    color: "var(--pf-t--global--text--color--regular)", fontSize: 13,
  };
  return (
    <div style={{
      position: "fixed", inset: 0, zIndex: 10000,
      display: "flex", alignItems: "center", justifyContent: "center",
      background: "rgba(0,0,0,0.6)",
    }}>
      <div style={{
        background: "var(--pf-t--global--background--color--primary--default)",
        borderRadius: 12, padding: 24, width: 420, maxWidth: "90vw",
        boxShadow: "0 8px 32px rgba(0,0,0,0.5)",
        border: "1px solid var(--pf-t--global--border--color--default)",
      }}>
        <h2 style={{ marginTop: 0, marginBottom: 16 }}>Create Project from Pattern</h2>
        <div style={{ marginBottom: 16 }}>
          <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Project Name</label>
          <input
            style={inputStyle}
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Project name"
            autoFocus
            onKeyDown={(e) => { if (e.key === "Enter" && name.trim()) onDeploy(name, guid || undefined, domain || undefined, dnsProviderId || undefined, autoDeploy, autoStart, deployHostId || undefined); }}
          />
        </div>
        <div style={{ borderTop: "1px solid var(--pf-t--global--border--color--default)", paddingTop: 12, marginTop: 4 }}>
          <div style={{ fontSize: 11, color: "var(--pf-t--global--text--color--subtle)", marginBottom: 8 }}>DNS Integration (optional)</div>
          <div style={{ display: "flex", gap: 8, marginBottom: 8 }}>
            <div style={{ flex: 1 }}>
              <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>GUID</label>
              <input style={inputStyle} value={guid} onChange={(e) => setGuid(e.target.value)} placeholder="abc123" />
            </div>
            <div style={{ flex: 2 }}>
              <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Domain</label>
              <input style={inputStyle} value={domain} onChange={(e) => setDomain(e.target.value)} placeholder="sandbox.example.com" />
            </div>
          </div>
          {dnsProviders.length > 0 && (
            <div>
              <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>DNS Provider</label>
              <select style={inputStyle} value={dnsProviderId} onChange={(e) => setDnsProviderId(e.target.value)}>
                <option value="">None</option>
                {dnsProviders.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
              </select>
            </div>
          )}
        </div>
        <div style={{ borderTop: "1px solid var(--pf-t--global--border--color--default)", paddingTop: 8, marginTop: 8, display: "flex", flexDirection: "column", gap: 4 }}>
          <label style={{ fontSize: 12, display: "flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
            <input type="checkbox" checked={autoDeploy} onChange={(e) => { setAutoDeploy(e.target.checked); if (!e.target.checked) setAutoStart(false); }} />
            Deploy immediately
          </label>
          {autoDeploy && (
            <>
              <label style={{ fontSize: 12, display: "flex", alignItems: "center", gap: 6, cursor: "pointer", marginLeft: 20 }}>
                <input type="checkbox" checked={autoStart} onChange={(e) => setAutoStart(e.target.checked)} />
                Start VMs after deploy
              </label>
              {userRole === "admin" && availableHosts.length > 0 && (
                <div style={{ marginTop: 4 }}>
                  <label style={{ fontSize: 12, display: "block", marginBottom: 2 }}>Host</label>
                  <select style={inputStyle} value={deployHostId} onChange={(e) => setDeployHostId(e.target.value)}>
                    <option value="">Auto (best host)</option>
                    {availableHosts.map((h) => (
                      <option key={h.id} value={h.id}>
                        {h.id.slice(0, 8)} — {h.ip_address} ({h.provider_type}), {h.total_vcpus - h.used_vcpus} vCPUs / {Math.round((h.total_ram_mb - h.used_ram_mb) / 1024)}G free
                      </option>
                    ))}
                  </select>
                </div>
              )}
            </>
          )}
        </div>
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginTop: 16 }}>
          <button onClick={onClose} disabled={deploying}
            style={{ ...inputStyle, width: "auto", cursor: deploying ? "not-allowed" : "pointer", padding: "6px 16px", opacity: deploying ? 0.4 : 1 }}>
            Cancel
          </button>
          <button onClick={() => onDeploy(name, guid || undefined, domain || undefined, dnsProviderId || undefined, autoDeploy, autoStart, deployHostId || undefined)} disabled={!name.trim() || deploying}
            style={{
              ...inputStyle, width: "auto", cursor: deploying ? "wait" : "pointer",
              padding: "6px 16px", background: "rgba(74,222,128,0.15)",
              borderColor: "#4ade80", color: "#4ade80",
              opacity: !name.trim() || deploying ? 0.4 : 1,
            }}>
            {deploying ? "Creating..." : "Create Project"}
          </button>
        </div>
      </div>
    </div>
  );
}

export default function PatternsPage() {
  const router = useRouter();
  const [patterns, setPatterns] = useState<Pattern[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [bulkPatternId, setBulkPatternId] = useState<string | null>(null);
  const [previewPattern, setPreviewPattern] = useState<{ id: string; name: string } | null>(null);
  const [deployPattern, setDeployPattern] = useState<{ id: string; name: string } | null>(null);
  const [deploying, setDeploying] = useState<string | null>(null);
  const [selectedPatterns, setSelectedPatterns] = useState<Set<string>>(new Set());
  const [editingName, setEditingName] = useState<string | null>(null);
  const [editNameValue, setEditNameValue] = useState("");

  const loadPatterns = () => {
    fetch("/api/v1/patterns/")
      .then((r) => r.ok ? r.json() : [])
      .then((data) => { setPatterns(Array.isArray(data) ? data : []); setLoading(false); })
      .catch(() => setLoading(false));
  };

  useEffect(() => {
    loadPatterns();
    const interval = setInterval(loadPatterns, 10000);
    const onVisible = () => { if (document.visibilityState === "visible") loadPatterns(); };
    document.addEventListener("visibilitychange", onVisible);
    return () => { clearInterval(interval); document.removeEventListener("visibilitychange", onVisible); };
  }, []);

  // Poll faster while any pattern is still saving
  useEffect(() => {
    const hasPending = patterns.some((p) => p.state === "creating" || p.state === "capturing");
    if (!hasPending) return;
    const timer = setInterval(loadPatterns, 3000);
    return () => clearInterval(timer);
  }, [patterns]);

  const filtered = patterns.filter((p) => {
    if (!search) return true;
    const q = search.toLowerCase();
    return p.name.toLowerCase().includes(q) || p.description.toLowerCase().includes(q);
  });

  const handleDeploy = async (patternId: string, projectName: string, guid?: string, domain?: string, dnsProviderId?: string, autoDeploy?: boolean, autoStart?: boolean, hostId?: string) => {
    setDeploying(patternId);
    try {
      const body: Record<string, any> = { name: projectName, auto_deploy: autoDeploy ?? true, auto_start: autoStart ?? true };
      if (guid) body.guid = guid;
      if (domain) body.domain = domain;
      if (dnsProviderId) body.dns_provider_id = dnsProviderId;
      if (hostId) body.host_id = hostId;
      const resp = await fetch(`/api/v1/patterns/${patternId}/deploy`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (resp.ok) {
        const data = await resp.json();
        router.push(`/projects/${data.id}`);
      } else {
        const err = await resp.json().catch(() => ({ detail: "Deploy failed" }));
        alert(err.detail || "Deploy failed");
      }
    } catch {
      alert("Failed to connect to server");
    }
    setDeploying(null);
  };

  const visibilityColor = (v: string) => {
    switch (v) {
      case "public": return "green";
      case "shared": return "blue";
      default: return "grey";
    }
  };

  const formatSize = (bytes: number) => {
    if (!bytes) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    const i = Math.floor(Math.log(bytes) / Math.log(1024));
    return `${(bytes / Math.pow(1024, i)).toFixed(1)} ${units[i]}`;
  };

  if (loading) return <PageSection><Title headingLevel="h1">Loading...</Title></PageSection>;

  return (
    <>
      <PageSection>
        <Toolbar>
          <ToolbarContent>
            <ToolbarItem><Title headingLevel="h1">Patterns</Title></ToolbarItem>
            <ToolbarItem>
              <input
                style={{ padding: "6px 10px", borderRadius: 6, border: "1px solid var(--pf-t--global--border--color--default)", background: "var(--pf-t--global--background--color--primary--default)", color: "var(--pf-t--global--text--color--regular)", fontSize: 13, width: 200 }}
                placeholder="Search patterns..."
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            </ToolbarItem>
          </ToolbarContent>
        </Toolbar>
      </PageSection>

      <PageSection>
        {filtered.length === 0 ? (
          <EmptyState>
            <EmptyStateBody>
              {search
                ? "No patterns match your search."
                : "No patterns yet. Save a project as a pattern to create reusable templates."}
            </EmptyStateBody>
          </EmptyState>
        ) : (
          <>
          {(() => {
            const selected = filtered.filter((p) => selectedPatterns.has(p.id));
            const allSelected = selected.length === filtered.length && filtered.length > 0;
            const someSelected = selected.length > 0;
            return (
              <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 12, flexWrap: "wrap" }}>
                <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 13, cursor: "pointer" }}>
                  <input type="checkbox" checked={allSelected} onChange={() => {
                    if (allSelected) setSelectedPatterns(new Set());
                    else setSelectedPatterns(new Set(filtered.map((p) => p.id)));
                  }} />
                  {someSelected ? `${selected.length} of ${filtered.length} selected` : "Select all"}
                </label>
                {someSelected && (
                  <Button variant="danger" size="sm" onClick={() => {
                    if (!window.confirm(`Delete ${selected.length} pattern(s)? This cannot be undone.`)) return;
                    for (const p of selected) { fetch(`/api/v1/patterns/${p.id}`, { method: "DELETE" }); }
                    setSelectedPatterns(new Set());
                    setTimeout(loadPatterns, 1000);
                  }}>Delete ({selected.length})</Button>
                )}
              </div>
            );
          })()}
          <div>
            {filtered.map((pattern) => {
              const saving = pattern.state === "creating" || pattern.state === "capturing";
              const ageMonths = (Date.now() - new Date(pattern.created_at).getTime()) / (1000 * 60 * 60 * 24 * 30);
              const certWarning = pattern.is_ocp && ageMonths >= 6;
              return (
              <Card key={pattern.id} isCompact style={{ cursor: saving ? "default" : "pointer", opacity: saving ? 0.7 : 1, marginBottom: 8 }} onClick={() => { if (!saving) setPreviewPattern({ id: pattern.id, name: pattern.name }); }}>
                <CardTitle>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
                    <div style={{ display: "flex", alignItems: "flex-start", gap: 8 }}>
                      <input type="checkbox" checked={selectedPatterns.has(pattern.id)} onChange={(e) => {
                        e.stopPropagation();
                        setSelectedPatterns((prev) => {
                          const next = new Set(prev);
                          if (next.has(pattern.id)) next.delete(pattern.id); else next.add(pattern.id);
                          return next;
                        });
                      }} onClick={(e) => e.stopPropagation()} style={{ width: 18, height: 18, minWidth: 18, cursor: "pointer", marginTop: 2 }} />
                      {editingName === pattern.id ? (
                        <input
                          autoFocus
                          value={editNameValue}
                          onChange={(e) => setEditNameValue(e.target.value)}
                          onBlur={() => {
                            const trimmed = editNameValue.trim();
                            if (trimmed && trimmed !== pattern.name) {
                              fetch(`/api/v1/patterns/${pattern.id}`, {
                                method: "PATCH",
                                headers: { "Content-Type": "application/json" },
                                body: JSON.stringify({ name: trimmed }),
                              }).then((r) => { if (r.ok) loadPatterns(); });
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
                            borderRadius: 4, padding: "2px 6px", width: "100%",
                          }}
                        />
                      ) : (
                        <strong
                          onClick={(e) => {
                            if (!saving) {
                              e.stopPropagation();
                              setEditingName(pattern.id);
                              setEditNameValue(pattern.name);
                            }
                          }}
                          title="Click to rename"
                          style={{ cursor: saving ? "default" : "text" }}
                        >{pattern.name}</strong>
                      )}
                    </div>
                    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                      {certWarning && (
                        <Tooltip content={`This OCP pattern is ${Math.floor(ageMonths)} months old. OpenShift certificates expire after ~1 year. CSRs will be auto-approved at deploy time.`}>
                          <Label color="gold">cert age: {Math.floor(ageMonths)}mo</Label>
                        </Tooltip>
                      )}
                      {saving ? (
                        <Label color="orange">saving…</Label>
                      ) : pattern.state === "error" ? (
                        <Label color="red">error</Label>
                      ) : (
                        <Label color={visibilityColor(pattern.visibility)}>{pattern.visibility}</Label>
                      )}
                    </div>
                  </div>
                </CardTitle>
                <CardBody>
                  {pattern.description && (
                    <p style={{ fontSize: 13, opacity: 0.7, marginBottom: 8 }}>{pattern.description}</p>
                  )}
                  {saving && pattern.capture_progress?.vms ? (
                    <div style={{ fontSize: 12, opacity: 0.8, whiteSpace: "pre-line", lineHeight: 1.6 }}>
                      {pattern.capture_progress.detail}
                      {"\n"}{pattern.capture_progress.vms.join("\n")}
                    </div>
                  ) : (
                    <div style={{ fontSize: 12, opacity: 0.6 }}>
                      {pattern.vm_count || 0} VM{(pattern.vm_count || 0) !== 1 ? "s" : ""}
                      {" · "}{pattern.total_vcpus || 0} vCPU
                      {" · "}{pattern.total_ram_gb || 0} GB RAM
                      {" · "}{pattern.total_disk_gb || 0} GB disk
                      {" · "}{formatSize(pattern.total_size_bytes)} compressed
                      {" · "}{new Date(pattern.created_at).toLocaleDateString()}
                    </div>
                  )}
                </CardBody>
                <CardBody style={{ borderTop: "1px solid var(--pf-t--global--border--color--default)", display: "flex", gap: 8, flexWrap: "wrap", paddingTop: 8, paddingBottom: 8, alignItems: "center" }} onClick={(e) => e.stopPropagation()}>
                  <TagEditor
                    tags={(pattern.tags?.user_tags as string[]) || []}
                    onAdd={async (tag) => {
                      const cur = (pattern.tags?.user_tags as string[]) || [];
                      await fetch(`/api/v1/patterns/${pattern.id}`, {
                        method: "PATCH",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ tags: { ...(pattern.tags || {}), user_tags: [...cur, tag] } }),
                      });
                      loadPatterns();
                    }}
                    onRemove={async (tag) => {
                      const cur = (pattern.tags?.user_tags as string[]) || [];
                      await fetch(`/api/v1/patterns/${pattern.id}`, {
                        method: "PATCH",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ tags: { ...(pattern.tags || {}), user_tags: cur.filter((t: string) => t !== tag) } }),
                      });
                      loadPatterns();
                    }}
                  />
                  <Button variant="primary" size="sm" isDisabled={saving} onClick={() => setDeployPattern({ id: pattern.id, name: pattern.name })}>
                    Create Project
                  </Button>
                  <Button variant="secondary" size="sm" isDisabled={saving} onClick={() => setBulkPatternId(pattern.id)}>
                    Bulk Deploy
                  </Button>
                  {saving && (
                    <Button variant="warning" size="sm" onClick={() => {
                      if (!window.confirm("Cancel pattern capture? This will stop the capture and clean up.")) return;
                      fetch(`/api/v1/patterns/${pattern.id}`, { method: "DELETE" }).then((r) => {
                        if (r.ok) setPatterns(patterns.filter((p) => p.id !== pattern.id));
                      });
                    }}>Cancel</Button>
                  )}
                  {!saving && (
                    <Button variant="danger" size="sm" onClick={() => {
                      if (!window.confirm(`Delete pattern "${pattern.name}"? This cannot be undone.`)) return;
                      fetch(`/api/v1/patterns/${pattern.id}`, { method: "DELETE" }).then((r) => {
                        if (r.ok) setPatterns(patterns.filter((p) => p.id !== pattern.id));
                      });
                    }}>Delete</Button>
                  )}
                </CardBody>
              </Card>
              );
            })}
          </div>
          </>
        )}
      </PageSection>

      {previewPattern && (
        <PatternPreviewModal
          patternId={previewPattern.id}
          patternName={previewPattern.name}
          onClose={() => setPreviewPattern(null)}
        />
      )}

      {bulkPatternId && (
        <BulkDeployModal
          patternId={bulkPatternId}
          onClose={() => setBulkPatternId(null)}
          onDeployed={(count) => {
            setBulkPatternId(null);
            alert(`Successfully created ${count} project(s). Check the Projects page.`);
          }}
        />
      )}

      {deployPattern && <DeployNameModal
        patternName={deployPattern.name}
        deploying={deploying === deployPattern.id}
        onDeploy={(name, guid, domain, dnsProviderId, ad, as_, hostId) => handleDeploy(deployPattern.id, name, guid, domain, dnsProviderId, ad, as_, hostId)}
        onClose={() => { if (!deploying) setDeployPattern(null); }}
      />}
    </>
  );
}
