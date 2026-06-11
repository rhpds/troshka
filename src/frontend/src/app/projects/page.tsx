"use client";

import React, { useEffect, useState } from "react";
import {
  Button,
  Card,
  CardBody,
  CardTitle,
  EmptyState,
  EmptyStateBody,
  EmptyStateVariant,
  PageSection,
  Title,
  Toolbar,
  ToolbarContent,
  ToolbarItem,
} from "@patternfly/react-core";
import { EmptyStateHeader } from "@patternfly/react-core/dist/esm/components/EmptyState/EmptyStateHeader";
import { EmptyStateIcon } from "@patternfly/react-core/dist/esm/components/EmptyState/EmptyStateIcon";
import PlusCircleIcon from "@patternfly/react-icons/dist/esm/icons/plus-circle-icon";
import CubesIcon from "@patternfly/react-icons/dist/esm/icons/cubes-icon";
import { useRouter } from "next/navigation";

interface Project {
  id: string;
  name: string;
  description: string | null;
  state: string;
  host_type: string;
  poweroff_mode: string;
  created_at: string;
}

const API_BASE = "";

const stateColors: Record<string, string> = {
  draft: "#94a3b8",
  deploying: "#fbbf24",
  active: "#4ade80",
  stopping: "#fbbf24",
  starting: "#fbbf24",
  stopped: "#f87171",
  error: "#ef4444",
};

interface PatternSummary {
  id: string;
  name: string;
  description: string | null;
  created_at: string;
}

interface TemplateSummary {
  id: string;
  name: string;
  description: string;
  category: string;
}

function NewProjectModal({ onClose, onCreated }: { onClose: () => void; onCreated: (id: string) => void }) {
  const [mode, setMode] = useState<"choose" | "blank" | "pattern" | "template">("choose");
  const [name, setName] = useState("");
  const [patterns, setPatterns] = useState<PatternSummary[]>([]);
  const [templates, setTemplates] = useState<TemplateSummary[]>([]);
  const [selectedPattern, setSelectedPattern] = useState<string | null>(null);
  const [selectedTemplate, setSelectedTemplate] = useState<string | null>(null);
  const [patternSearch, setPatternSearch] = useState("");
  const [patternDropdownOpen, setPatternDropdownOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [bastionImageId, setBastionImageId] = useState("");
  const [bastionIsoId, setBastionIsoId] = useState("");
  const [bastionSshKeyId, setBastionSshKeyId] = useState("");
  const [bastionPassword, setBastionPassword] = useState("");
  const [libraryImages, setLibraryImages] = useState<Array<{id: string; name: string; size_gb: number; format: string}>>([]);
  const [libraryIsos, setLibraryIsos] = useState<Array<{id: string; name: string; size_gb: number}>>([]);
  const [sshKeys, setSshKeys] = useState<Array<{id: string; name: string}>>([]);

  useEffect(() => {
    fetch(`${API_BASE}/api/v1/patterns/`)
      .then((r) => r.ok ? r.json() : [])
      .then((data) => setPatterns(Array.isArray(data) ? data : []))
      .catch(() => {});
    fetch(`${API_BASE}/api/v1/projects/templates`)
      .then((r) => r.ok ? r.json() : [])
      .then((data) => setTemplates(Array.isArray(data) ? data : []))
      .catch(() => {});
    fetch(`${API_BASE}/api/v1/library/`)
      .then((r) => r.ok ? r.json() : [])
      .then((data) => {
        const items = Array.isArray(data) ? data : [];
        setLibraryImages(items.filter((i: any) => i.format === "qcow2" && i.state === "ready"));
        setLibraryIsos(items.filter((i: any) => i.format === "iso" && i.state === "ready"));
      })
      .catch(() => {});
    fetch(`${API_BASE}/api/v1/auth/ssh-keys`)
      .then((r) => r.ok ? r.json() : [])
      .then((data) => setSshKeys(Array.isArray(data) ? data : []))
      .catch(() => {});
  }, []);

  const inputStyle = {
    width: "100%",
    padding: "6px 10px",
    borderRadius: 6,
    border: "1px solid var(--pf-t--global--border--color--default)",
    background: "var(--pf-t--global--background--color--primary--default)",
    color: "var(--pf-t--global--text--color--regular)",
    fontSize: 13,
  };

  const handleCreate = async () => {
    if (!name.trim()) return;
    setCreating(true);
    try {
      if (mode === "pattern" && selectedPattern) {
        const resp = await fetch(`${API_BASE}/api/v1/patterns/${selectedPattern}/deploy`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name }),
        });
        if (resp.ok) {
          const data = await resp.json();
          onCreated(data.id);
        }
      } else if (mode === "template" && selectedTemplate) {
        const templateBody: Record<string, any> = { template_id: selectedTemplate, name };
        if (bastionImageId) templateBody.bastion_image_id = bastionImageId;
        if (bastionIsoId) templateBody.bastion_iso_id = bastionIsoId;
        if (bastionSshKeyId) templateBody.bastion_ssh_key_id = bastionSshKeyId;
        if (bastionPassword) templateBody.bastion_password = bastionPassword;
        const resp = await fetch(`${API_BASE}/api/v1/projects/from-template`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(templateBody),
        });
        if (resp.ok) {
          const data = await resp.json();
          onCreated(data.id);
        }
      } else {
        const resp = await fetch(`${API_BASE}/api/v1/projects/`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name }),
        });
        if (resp.ok) {
          const data = await resp.json();
          onCreated(data.id);
        }
      }
    } catch {
      /* ignore */
    }
    setCreating(false);
  };

  const optionStyle = (active: boolean) => ({
    flex: 1,
    padding: "16px",
    borderRadius: 8,
    border: `2px solid ${active ? "#4ade80" : "var(--pf-t--global--border--color--default)"}`,
    background: active ? "rgba(74,222,128,0.08)" : "transparent",
    cursor: "pointer" as const,
    textAlign: "center" as const,
  });

  return (
    <div style={{
      position: "fixed", inset: 0, zIndex: 10000,
      display: "flex", alignItems: "center", justifyContent: "center",
      background: "rgba(0,0,0,0.6)",
    }} onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div style={{
        background: "var(--pf-t--global--background--color--primary--default)",
        borderRadius: 12, padding: 24, width: 500, maxWidth: "90vw",
        boxShadow: "0 8px 32px rgba(0,0,0,0.5)",
        border: "1px solid var(--pf-t--global--border--color--default)",
      }}>
        <h2 style={{ marginTop: 0, marginBottom: 16 }}>New Project</h2>

        {mode === "choose" ? (
          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            <div style={{ display: "flex", gap: 12 }}>
              <div style={optionStyle(false)} onClick={() => setMode("blank")}>
                <div style={{ fontSize: 28, marginBottom: 4 }}>📄</div>
                <div style={{ fontWeight: 600, fontSize: 14 }}>Blank Project</div>
                <div style={{ fontSize: 12, opacity: 0.6, marginTop: 4 }}>Start from scratch</div>
              </div>
              <div
                style={{ ...optionStyle(false), opacity: patterns.length === 0 ? 0.4 : 1, pointerEvents: patterns.length === 0 ? "none" : "auto" }}
                onClick={() => setMode("pattern")}
              >
                <div style={{ fontSize: 28, marginBottom: 4 }}>🧩</div>
                <div style={{ fontWeight: 600, fontSize: 14 }}>From Pattern</div>
                <div style={{ fontSize: 12, opacity: 0.6, marginTop: 4 }}>
                  {patterns.length > 0 ? `${patterns.length} pattern${patterns.length > 1 ? "s" : ""} available` : "No patterns yet"}
                </div>
              </div>
            </div>
            {templates.length > 0 && (
              <div style={{ display: "flex", gap: 12 }}>
                {templates.map((t) => (
                  <div key={t.id} style={optionStyle(false)} onClick={() => { setSelectedTemplate(t.id); setName(t.name); setMode("template"); }}>
                    <div style={{ marginBottom: 4, display: "flex", justifyContent: "center" }}>
                      <svg width="28" height="28" viewBox="0 0 24 24" fill="#EE0000" xmlns="http://www.w3.org/2000/svg">
                        <path d="M21.665,11.812c-0.11-1.377-0.476-2.724-1.08-3.966L24,6.599c-0.268-0.556-0.585-1.092-0.943-1.595 l-1.601,0.583c-3.534-4.95-10.412-6.098-15.363-2.565c-3.144,2.244-4.883,5.972-4.582,9.823l1.604-0.584 c0.051,0.615,0.153,1.224,0.305,1.822L0,15.335c0.338,1.339,0.922,2.604,1.721,3.731l1.812-0.659 c3.526,4.95,10.398,6.106,15.349,2.58c1.555-1.107,2.796-2.6,3.599-4.332c0.802-1.715,1.144-3.61,0.991-5.497L21.665,11.812z M16.925,9.177c0.687,1.227,0.998,2.629,0.895,4.032l1.809-0.657c-0.063,0.856-0.282,1.694-0.646,2.471 c-1.67,3.584-5.928,5.138-9.514,3.472c-0.782-0.365-1.491-0.87-2.092-1.49l-1.813,0.66c-0.979-1.01-1.64-2.285-1.903-3.667 l3.426-1.242c-0.121-0.624-0.159-1.262-0.111-1.896H6.97l-1.604,0.583c0.294-3.932,3.72-6.881,7.652-6.587 c0.868,0.065,1.716,0.288,2.504,0.658V5.508c0.778,0.364,1.483,0.867,2.082,1.483l1.599-0.582c0.002,0.002,0.004,0.003,0.006,0.005 c0.441,0.454,0.82,0.965,1.128,1.518L16.925,9.177z"/>
                      </svg>
                    </div>
                    <div style={{ fontWeight: 600, fontSize: 14 }}>{t.name}</div>
                    <div style={{ fontSize: 12, opacity: 0.6, marginTop: 4 }}>{t.description}</div>
                  </div>
                ))}
              </div>
            )}
            <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 4 }}>
              <button onClick={onClose} style={{ ...inputStyle, width: "auto", cursor: "pointer", padding: "6px 16px" }}>
                Cancel
              </button>
            </div>
          </div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            <div>
              <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Project Name</label>
              <input
                style={inputStyle}
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="My Project"
                autoFocus={mode === "blank"}
                onKeyDown={(e) => { if (e.key === "Enter") handleCreate(); }}
              />
            </div>
            {mode === "template" && selectedTemplate && (
              <>
                <div style={{
                  padding: "8px 12px", borderRadius: 6,
                  background: "rgba(59,130,246,0.08)", border: "1px solid rgba(59,130,246,0.3)",
                  fontSize: 12,
                }}>
                  Template: <strong>{templates.find((t) => t.id === selectedTemplate)?.name}</strong>
                  <div style={{ opacity: 0.6, marginTop: 2 }}>{templates.find((t) => t.id === selectedTemplate)?.description}</div>
                </div>
                <div style={{ borderTop: "1px solid var(--pf-t--global--border--color--default)", paddingTop: 12, marginTop: 4 }}>
                  <div style={{ fontSize: 11, color: "var(--pf-t--global--text--color--subtle)", marginBottom: 8 }}>Bastion Configuration</div>
                  <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                    <div>
                      <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Bastion Image</label>
                      {libraryImages.length > 0 ? (
                        <select style={inputStyle} value={bastionImageId} onChange={(e) => setBastionImageId(e.target.value)}>
                          <option value="">Select an image...</option>
                          {libraryImages.map((img) => (
                            <option key={img.id} value={img.id}>{img.name} ({img.size_gb} GB)</option>
                          ))}
                        </select>
                      ) : (
                        <div style={{ fontSize: 12, color: "#f87171", padding: "6px 10px", borderRadius: 6, background: "rgba(248,113,113,0.08)", border: "1px solid rgba(248,113,113,0.3)" }}>
                          No qcow2 images in library. Upload the RHEL KVM Guest Image from the Red Hat Download site.
                        </div>
                      )}
                    </div>
                    <div>
                      <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>RHEL DVD ISO (for yum repo)</label>
                      <select style={inputStyle} value={bastionIsoId} onChange={(e) => setBastionIsoId(e.target.value)}>
                        <option value="">None</option>
                        {libraryIsos.map((img) => (
                          <option key={img.id} value={img.id}>{img.name}</option>
                        ))}
                      </select>
                    </div>
                    <div>
                      <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>SSH Key</label>
                      <select style={inputStyle} value={bastionSshKeyId} onChange={(e) => setBastionSshKeyId(e.target.value)}>
                        <option value="">None</option>
                        {sshKeys.map((k) => (
                          <option key={k.id} value={k.id}>{k.name}</option>
                        ))}
                      </select>
                    </div>
                    <div>
                      <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Password <span style={{ color: "var(--pf-t--global--text--color--subtle)" }}>(cloud-user)</span></label>
                      <input style={inputStyle} type="password" value={bastionPassword} onChange={(e) => setBastionPassword(e.target.value)} placeholder="Required for console access" />
                    </div>
                  </div>
                </div>
              </>
            )}
            {mode === "pattern" && (
              <div style={{ position: "relative" }}>
                <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Pattern</label>
                <input
                  style={{
                    ...inputStyle,
                    borderColor: selectedPattern ? "#4ade80" : inputStyle.border ? undefined : undefined,
                    border: selectedPattern ? "1px solid #4ade80" : inputStyle.border,
                  }}
                  value={patternSearch}
                  onChange={(e) => {
                    setPatternSearch(e.target.value);
                    setSelectedPattern(null);
                    setPatternDropdownOpen(true);
                  }}
                  onFocus={() => setPatternDropdownOpen(true)}
                  placeholder="Search patterns..."
                  autoFocus
                />
                {patternDropdownOpen && (
                  <div style={{
                    position: "absolute", top: "100%", left: 0, right: 0, zIndex: 10,
                    maxHeight: 180, overflowY: "auto",
                    background: "var(--pf-t--global--background--color--primary--default)",
                    border: "1px solid var(--pf-t--global--border--color--default)",
                    borderTop: "none", borderRadius: "0 0 6px 6px",
                    boxShadow: "0 4px 12px rgba(0,0,0,0.3)",
                  }}>
                    {patterns
                      .filter((p) => !patternSearch || p.name.toLowerCase().includes(patternSearch.toLowerCase()))
                      .map((p) => (
                        <div
                          key={p.id}
                          onClick={() => {
                            setSelectedPattern(p.id);
                            setPatternSearch(p.name);
                            setPatternDropdownOpen(false);
                            if (!name) setName(p.name);
                          }}
                          style={{
                            padding: "8px 12px", cursor: "pointer",
                            background: selectedPattern === p.id ? "rgba(74,222,128,0.08)" : "transparent",
                          }}
                          onMouseEnter={(e) => { (e.target as HTMLElement).style.background = "rgba(255,255,255,0.05)"; }}
                          onMouseLeave={(e) => { (e.target as HTMLElement).style.background = selectedPattern === p.id ? "rgba(74,222,128,0.08)" : "transparent"; }}
                        >
                          <div style={{ fontWeight: 500, fontSize: 13 }}>{p.name}</div>
                          {p.description && <div style={{ fontSize: 11, opacity: 0.6 }}>{p.description}</div>}
                        </div>
                      ))}
                    {patterns.filter((p) => !patternSearch || p.name.toLowerCase().includes(patternSearch.toLowerCase())).length === 0 && (
                      <div style={{ padding: "8px 12px", fontSize: 13, opacity: 0.5 }}>No patterns found</div>
                    )}
                  </div>
                )}
              </div>
            )}
            <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginTop: 4 }}>
              <button onClick={() => { setMode("choose"); setSelectedPattern(null); setSelectedTemplate(null); }} style={{ ...inputStyle, width: "auto", cursor: "pointer", padding: "6px 16px" }}>
                Back
              </button>
              <button
                onClick={handleCreate}
                disabled={creating || !name.trim() || (mode === "pattern" && !selectedPattern) || (mode === "template" && (!selectedTemplate || !bastionPassword || !bastionImageId))}
                style={{
                  ...inputStyle, width: "auto", padding: "6px 16px",
                  cursor: creating ? "wait" : "pointer",
                  background: "rgba(74,222,128,0.15)", borderColor: "#4ade80", color: "#4ade80",
                  opacity: creating || !name.trim() || (mode === "pattern" && !selectedPattern) || (mode === "template" && (!selectedTemplate || !bastionPassword || !bastionImageId)) ? 0.4 : 1,
                }}
              >
                {creating ? "Creating..." : mode === "pattern" ? "Create from Pattern" : mode === "template" ? "Create from Template" : "Create Project"}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

export default function ProjectsPage() {
  const router = useRouter();
  const [projects, setProjects] = useState<Project[]>([]);
  const [loading, setLoading] = useState(true);
  const [showNewModal, setShowNewModal] = useState(false);
  const [selectedProjects, setSelectedProjects] = useState<Set<string>>(new Set());
  const [search, setSearch] = useState("");
  const [userRole, setUserRole] = useState("");
  const [pools, setPools] = useState<{id: string; name: string; mode: string; status: string}[]>([]);
  const [deployPoolId, setDeployPoolId] = useState("");

  const pollUntilSettled = () => {
    const settled = ["draft", "active", "stopped", "error"];
    const poll = setInterval(() => {
      fetch(`${API_BASE}/api/v1/projects/`).then((r) => r.ok ? r.json() : []).then((data) => {
        const list = Array.isArray(data) ? data.sort((a: Project, b: Project) => a.name.localeCompare(b.name)) : [];
        setProjects(list);
        if (list.every((p: Project) => settled.includes(p.state))) clearInterval(poll);
      }).catch(() => {});
    }, 2000);
  };

  const fetchProjects = () => {
    fetch(`${API_BASE}/api/v1/projects/`)
      .then((r) => {
        if (!r.ok) throw new Error("Failed to fetch projects");
        return r.json();
      })
      .then((data) => {
        const sorted = Array.isArray(data) ? data.sort((a: Project, b: Project) => a.name.localeCompare(b.name)) : [];
        setProjects(sorted);
        setLoading(false);
      })
      .catch(() => {
        setProjects([]);
        setLoading(false);
      });
  };

  useEffect(() => {
    fetchProjects();
    fetch("/api/v1/auth/me").then(r => r.ok ? r.json() : {}).then(d => setUserRole(d.role || ""));
    fetch("/api/v1/storage-pools/").then(r => r.ok ? r.json() : []).then(d => setPools(d.filter((p: any) => p.status === "available")));
    const interval = setInterval(fetchProjects, 10000);
    return () => clearInterval(interval);
  }, []);

  if (loading) {
    return <PageSection><Title headingLevel="h1">Loading...</Title></PageSection>;
  }

  if (projects.length === 0) {
    const NoProjectsIcon = () => <EmptyStateIcon icon={CubesIcon} />;

    return (
      <PageSection>
        <EmptyState variant={EmptyStateVariant.full}>
          <EmptyStateHeader
            titleText="No projects yet"
            icon={NoProjectsIcon}
            headingLevel="h1"
          />
          <EmptyStateBody>
            Create your first VM environment to get started.
          </EmptyStateBody>
          <Button variant="primary" icon={<PlusCircleIcon />} onClick={() => setShowNewModal(true)}>
            New Project
          </Button>
        </EmptyState>
        {showNewModal && (
          <NewProjectModal onClose={() => setShowNewModal(false)} onCreated={(id) => router.push(`/projects/${id}`)} />
        )}
      </PageSection>
    );
  }

  return (
    <>
      <PageSection>
        <Toolbar>
          <ToolbarContent>
            <ToolbarItem>
              <Title headingLevel="h1">Projects</Title>
            </ToolbarItem>
            <ToolbarItem>
              <input
                style={{ padding: "6px 10px", borderRadius: 6, border: "1px solid var(--pf-t--global--border--color--default)", background: "var(--pf-t--global--background--color--primary--default)", color: "var(--pf-t--global--text--color--regular)", fontSize: 13, width: 200 }}
                placeholder="Search projects..."
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            </ToolbarItem>
            <ToolbarItem align={{ default: "alignEnd" }}>
              <Button variant="primary" icon={<PlusCircleIcon />} onClick={() => setShowNewModal(true)}>
                New Project
              </Button>
            </ToolbarItem>
          </ToolbarContent>
        </Toolbar>
      </PageSection>
      <PageSection>
        {(() => {
          const q = search.toLowerCase();
          const filteredProjects = q ? projects.filter((p) => p.name.toLowerCase().includes(q) || (p.description || "").toLowerCase().includes(q)) : projects;
          return (<>
        {filteredProjects.length > 0 && (() => {
          const selected = filteredProjects.filter((p) => selectedProjects.has(p.id));
          const allSelected = selected.length === filteredProjects.length;
          const someSelected = selected.length > 0;
          const allActive = someSelected && selected.every((p) => p.state === "active");
          const allStopped = someSelected && selected.every((p) => p.state === "stopped");
          const allStoppedOrError = someSelected && selected.every((p) => p.state === "stopped" || p.state === "error");
          const allDeployed = someSelected && selected.every((p) => ["active", "stopped", "error"].includes(p.state));
          const allDraft = someSelected && selected.every((p) => p.state === "draft");
          return (
            <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 12, flexWrap: "wrap" }}>
              <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 13, cursor: "pointer" }}>
                <input
                  type="checkbox"
                  checked={allSelected}
                  onChange={() => {
                    if (allSelected) setSelectedProjects(new Set());
                    else setSelectedProjects(new Set(filteredProjects.map((p) => p.id)));
                  }}
                />
                {someSelected ? `${selected.length} of ${filteredProjects.length} selected` : "Select all"}
              </label>
              {someSelected && (
                <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                  {allActive && (
                    <Button variant="secondary" size="sm" onClick={() => {
                      if (!window.confirm(`Stop ${selected.length} project(s)?`)) return;
                      for (const p of selected) { fetch(`${API_BASE}/api/v1/projects/${p.id}/stop`, { method: "POST" }); }
                      setSelectedProjects(new Set());
                      fetchProjects();
                      pollUntilSettled();
                    }}>Stop ({selected.length})</Button>
                  )}
                  {allStoppedOrError && (
                    <Button variant="secondary" size="sm" onClick={() => {
                      if (!window.confirm(`Start ${selected.length} project(s)?`)) return;
                      for (const p of selected) { fetch(`${API_BASE}/api/v1/projects/${p.id}/start`, { method: "POST" }); }
                      setSelectedProjects(new Set());
                      fetchProjects();
                      pollUntilSettled();
                    }}>Start ({selected.length})</Button>
                  )}
                  {allDraft && (
                    <Button variant="secondary" size="sm" onClick={() => {
                      if (!window.confirm(`Deploy ${selected.length} project(s)?`)) return;
                      for (const p of selected) { fetch(`${API_BASE}/api/v1/projects/${p.id}/deploy`, { method: "POST" }); }
                      setSelectedProjects(new Set());
                      fetchProjects();
                      pollUntilSettled();
                    }}>Deploy ({selected.length})</Button>
                  )}
                  {allDeployed && (
                    <Button variant="secondary" size="sm" onClick={() => {
                      if (!window.confirm(`Republish ${selected.length} project(s)? All VMs will be destroyed and recreated.`)) return;
                      for (const p of selected) { fetch(`${API_BASE}/api/v1/projects/${p.id}/redeploy`, { method: "POST" }); }
                      setSelectedProjects(new Set());
                      fetchProjects();
                      pollUntilSettled();
                    }}>Republish ({selected.length})</Button>
                  )}
                  <Button variant="danger" size="sm" onClick={() => {
                    if (!window.confirm(`Delete ${selected.length} project(s)? This cannot be undone.`)) return;
                    for (const p of selected) { fetch(`${API_BASE}/api/v1/projects/${p.id}`, { method: "DELETE" }); }
                    setSelectedProjects(new Set());
                    setTimeout(fetchProjects, 1000);
                  }}>Delete ({selected.length})</Button>
                </div>
              )}
            </div>
          );
        })()}
        <div>
          {filteredProjects.length === 0 && (
            <p style={{ opacity: 0.6 }}>No projects match &quot;{search}&quot;</p>
          )}
          {filteredProjects.map((p) => (
            <Card
              key={p.id}
              style={{ marginBottom: 8, cursor: "pointer" }}
            >
              {/* Row 1: Info */}
              <CardBody style={{ display: "flex", alignItems: "flex-start", gap: 8 }} onClick={() => router.push(`/projects/${p.id}`)}>
                <input
                  type="checkbox"
                  checked={selectedProjects.has(p.id)}
                  onChange={(e) => {
                    e.stopPropagation();
                    setSelectedProjects((prev) => {
                      const next = new Set(prev);
                      if (next.has(p.id)) next.delete(p.id); else next.add(p.id);
                      return next;
                    });
                  }}
                  onClick={(e) => e.stopPropagation()}
                  style={{ width: 18, height: 18, minWidth: 18, cursor: "pointer", marginTop: 2 }}
                />
                <div style={{ flex: 1 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <strong>{p.name}</strong>
                    <span style={{
                      fontSize: 11, padding: "1px 6px", borderRadius: 4,
                      background: `${stateColors[p.state] || "#94a3b8"}22`,
                      color: stateColors[p.state] || "#94a3b8",
                    }}>
                      {p.state}
                    </span>
                    {(p.state === "stopping" || p.state === "starting" || p.state === "deploying") && (
                      <span className="project-btn-spinner" style={{ width: 14, height: 14 }} />
                    )}
                  </div>
                  <p style={{ fontSize: 13, opacity: 0.7, margin: "4px 0 0" }}>{p.description || "No description"}</p>
                  <p style={{ fontSize: 11, opacity: 0.5, margin: "4px 0 0" }}>
                    {p.host_type} &middot; {new Date(p.created_at).toLocaleDateString()}
                  </p>
                </div>
              </CardBody>
              {/* Row 2: Buttons */}
              <CardBody style={{ borderTop: "1px solid var(--pf-t--global--border--color--default)", display: "flex", gap: 8, flexWrap: "wrap", paddingTop: 8, paddingBottom: 8 }} onClick={(e) => e.stopPropagation()}>
                {p.state === "draft" && (
                  <>
                    {userRole === "admin" && pools.length > 1 && (
                      <select style={{
                        padding: "4px 8px", borderRadius: 6, fontSize: 12,
                        border: "1px solid var(--pf-t--global--border--color--default)",
                        background: "var(--pf-t--global--background--color--primary--default)",
                        color: "var(--pf-t--global--text--color--regular)",
                      }} value={deployPoolId} onChange={(e) => setDeployPoolId(e.target.value)}>
                        <option value="">Auto (best pool)</option>
                        {pools.map((pl) => <option key={pl.id} value={pl.id}>{pl.name} ({pl.mode})</option>)}
                      </select>
                    )}
                    <Button variant="primary" onClick={() => {
                      if (!window.confirm(`Deploy project "${p.name}"? This will provision networking and start all VMs.`)) return;
                      setProjects(prev => prev.map(pr => pr.id === p.id ? { ...pr, state: "deploying" } : pr));
                      const poolParam = deployPoolId ? `?storage_pool_id=${deployPoolId}` : "";
                      fetch(`${API_BASE}/api/v1/projects/${p.id}/deploy${poolParam}`, { method: "POST" }).then(r => r.json()).then(d => {
                        if (d.status === "deploying") { pollUntilSettled(); }
                        else { alert(d.detail || "Deploy failed"); setProjects(prev => prev.map(pr => pr.id === p.id ? { ...pr, state: "draft" } : pr)); }
                      });
                    }}>Deploy</Button>
                  </>
                )}
                {p.state === "active" && (
                  <Button variant="secondary" onClick={() => {
                    if (!window.confirm(`Stop project "${p.name}"? All VMs will be shut down.`)) return;
                    setProjects(prev => prev.map(pr => pr.id === p.id ? { ...pr, state: "stopping" } : pr));
                    fetch(`${API_BASE}/api/v1/projects/${p.id}/stop`, { method: "POST" }).then(() => pollUntilSettled());
                  }}>Stop</Button>
                )}
                {(p.state === "stopped" || p.state === "error") && (
                  <Button variant="secondary" onClick={() => {
                    if (!window.confirm(`Start project "${p.name}"? All VMs will be started.`)) return;
                    setProjects(prev => prev.map(pr => pr.id === p.id ? { ...pr, state: "starting" } : pr));
                    fetch(`${API_BASE}/api/v1/projects/${p.id}/start`, { method: "POST" }).then(() => pollUntilSettled());
                  }}>Start</Button>
                )}
                {(p.state === "error" || p.state === "active" || p.state === "stopped") && (
                  <Button variant="secondary" onClick={() => {
                    if (!window.confirm(`Republish project "${p.name}"? This will destroy and recreate all VMs.`)) return;
                    setProjects(prev => prev.map(pr => pr.id === p.id ? { ...pr, state: "deploying" } : pr));
                    fetch(`${API_BASE}/api/v1/projects/${p.id}/redeploy`, { method: "POST" }).then(r => r.json()).then(d => {
                      if (d.status === "deploying") { pollUntilSettled(); }
                      else { alert(d.detail || "Republish failed"); setProjects(prev => prev.map(pr => pr.id === p.id ? { ...pr, state: "error" } : pr)); }
                    });
                  }}>Republish</Button>
                )}
                <Button variant="danger" onClick={() => {
                  if (!window.confirm(`Delete project "${p.name}"? This cannot be undone.`)) return;
                  fetch(`${API_BASE}/api/v1/projects/${p.id}`, { method: "DELETE" }).then((r) => {
                    if (r.ok) { setProjects(projects.filter((pr) => pr.id !== p.id)); localStorage.removeItem(`troshka-canvas-${p.id}`); }
                  });
                }}>Delete</Button>
              </CardBody>
            </Card>
          ))}
        </div>
          </>);
        })()}
      </PageSection>
      {showNewModal && (
        <NewProjectModal onClose={() => setShowNewModal(false)} onCreated={(id) => router.push(`/projects/${id}`)} />
      )}
    </>
  );
}
