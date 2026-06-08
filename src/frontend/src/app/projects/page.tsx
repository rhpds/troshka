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
  Gallery,
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

function NewProjectModal({ onClose, onCreated }: { onClose: () => void; onCreated: (id: string) => void }) {
  const [mode, setMode] = useState<"choose" | "blank" | "pattern">("choose");
  const [name, setName] = useState("");
  const [patterns, setPatterns] = useState<PatternSummary[]>([]);
  const [selectedPattern, setSelectedPattern] = useState<string | null>(null);
  const [patternSearch, setPatternSearch] = useState("");
  const [patternDropdownOpen, setPatternDropdownOpen] = useState(false);
  const [creating, setCreating] = useState(false);

  useEffect(() => {
    fetch(`${API_BASE}/api/v1/patterns/`)
      .then((r) => r.ok ? r.json() : [])
      .then((data) => setPatterns(Array.isArray(data) ? data : []))
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
              <button onClick={() => { setMode("choose"); setSelectedPattern(null); }} style={{ ...inputStyle, width: "auto", cursor: "pointer", padding: "6px 16px" }}>
                Back
              </button>
              <button
                onClick={handleCreate}
                disabled={creating || !name.trim() || (mode === "pattern" && !selectedPattern)}
                style={{
                  ...inputStyle, width: "auto", padding: "6px 16px",
                  cursor: creating ? "wait" : "pointer",
                  background: "rgba(74,222,128,0.15)", borderColor: "#4ade80", color: "#4ade80",
                  opacity: creating || !name.trim() || (mode === "pattern" && !selectedPattern) ? 0.4 : 1,
                }}
              >
                {creating ? "Creating..." : mode === "pattern" ? "Create from Pattern" : "Create Project"}
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

  useEffect(() => {
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
            <ToolbarItem align={{ default: "alignEnd" }}>
              <Button variant="primary" icon={<PlusCircleIcon />} onClick={() => setShowNewModal(true)}>
                New Project
              </Button>
            </ToolbarItem>
          </ToolbarContent>
        </Toolbar>
      </PageSection>
      <PageSection>
        <Gallery hasGutter minWidths={{ default: "300px" }}>
          {projects.map((p) => (
            <Card
              key={p.id}
              isClickable
              isSelectable
              onClick={() => router.push(`/projects/${p.id}`)}
              style={{ border: "1px solid var(--pf-t--global--border--color--default)", borderRadius: 8 }}
            >
              <CardTitle style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <strong>{p.name}</strong>
                  <span style={{
                    fontSize: 11, padding: "1px 6px", borderRadius: 4,
                    background: `${stateColors[p.state] || "#94a3b8"}22`,
                    color: stateColors[p.state] || "#94a3b8",
                  }}>
                    {p.state}
                  </span>
                </div>
                <div style={{ display: "flex", gap: 4, alignItems: "center" }}>
                  {p.state === "active" && (
                    <Button
                      variant="secondary"
                      isDanger
                      style={{ fontSize: 11, padding: "2px 8px" }}
                      onClick={(e) => {
                        e.stopPropagation();
                        if (!window.confirm(`Stop project "${p.name}"? All VMs will be shut down.`)) return;
                        setProjects(prev => prev.map(pr => pr.id === p.id ? { ...pr, state: "stopping" } : pr));
                        fetch(`${API_BASE}/api/v1/projects/${p.id}/stop`, { method: "POST" });
                        const poll = setInterval(() => {
                          fetch(`${API_BASE}/api/v1/projects/${p.id}`).then(r => r.json()).then(d => {
                            if (d.state === "stopped" || d.state === "error") {
                              clearInterval(poll);
                              window.location.reload();
                            }
                          });
                        }, 2000);
                      }}
                    >Stop</Button>
                  )}
                  {p.state === "stopped" && (
                    <Button
                      variant="secondary"
                      style={{ fontSize: 11, padding: "2px 8px" }}
                      onClick={(e) => {
                        e.stopPropagation();
                        if (!window.confirm(`Start project "${p.name}"? All VMs will be started in the configured start order.`)) return;
                        setProjects(prev => prev.map(pr => pr.id === p.id ? { ...pr, state: "starting" } : pr));
                        fetch(`${API_BASE}/api/v1/projects/${p.id}/start`, { method: "POST" });
                        const poll = setInterval(() => {
                          fetch(`${API_BASE}/api/v1/projects/${p.id}`).then(r => r.json()).then(d => {
                            if (d.state === "active" || d.state === "error") {
                              clearInterval(poll);
                              window.location.reload();
                            }
                          });
                        }, 2000);
                      }}
                    >Start</Button>
                  )}
                  {(p.state === "stopping" || p.state === "starting") && (
                    <span className="project-btn-spinner" style={{ width: 14, height: 14 }} />
                  )}
                  <Button
                    variant="plain"
                    style={{ color: "var(--pf-t--global--color--status--danger--default)", padding: 4 }}
                    onClick={(e) => {
                      e.stopPropagation();
                      if (!window.confirm(`Delete project "${p.name}"? This cannot be undone.`)) return;
                      fetch(`${API_BASE}/api/v1/projects/${p.id}`, { method: "DELETE" })
                        .then((r) => {
                          if (r.ok) {
                            setProjects(projects.filter((pr) => pr.id !== p.id));
                            localStorage.removeItem(`troshka-canvas-${p.id}`);
                          }
                        });
                    }}
                  >✕</Button>
                </div>
              </CardTitle>
              <CardBody>
                <p style={{ fontSize: 13, opacity: 0.7 }}>{p.description || "No description"}</p>
                <p style={{ marginTop: 8, fontSize: 11, opacity: 0.5 }}>
                  {p.host_type} &middot; {new Date(p.created_at).toLocaleDateString()}
                </p>
              </CardBody>
            </Card>
          ))}
        </Gallery>
      </PageSection>
      {showNewModal && (
        <NewProjectModal onClose={() => setShowNewModal(false)} onCreated={(id) => router.push(`/projects/${id}`)} />
      )}
    </>
  );
}
