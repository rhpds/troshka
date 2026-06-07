"use client";

import React, { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import {
  Button,
  Card,
  CardBody,
  CardTitle,
  EmptyState,
  EmptyStateBody,
  Label,
  PageSection,
  SearchInput,
  Title,
  Toolbar,
  ToolbarContent,
  ToolbarItem,
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
  disk_count: number;
  total_size_gb: number;
  disks: PatternDisk[];
  created_at: string;
  owner_id: string;
}

function DeployNameModal({ patternName, deploying, onDeploy, onClose }: {
  patternName: string; deploying: boolean; onDeploy: (name: string) => void; onClose: () => void;
}) {
  const [name, setName] = useState(patternName);
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
    }} onClick={(e) => { if (e.target === e.currentTarget && !deploying) onClose(); }}>
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
            onKeyDown={(e) => { if (e.key === "Enter" && name.trim()) onDeploy(name); }}
          />
        </div>
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
          <button onClick={onClose} disabled={deploying}
            style={{ ...inputStyle, width: "auto", cursor: deploying ? "not-allowed" : "pointer", padding: "6px 16px", opacity: deploying ? 0.4 : 1 }}>
            Cancel
          </button>
          <button onClick={() => onDeploy(name)} disabled={!name.trim() || deploying}
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

  const loadPatterns = () => {
    fetch("/api/v1/patterns/")
      .then((r) => r.ok ? r.json() : [])
      .then((data) => { setPatterns(Array.isArray(data) ? data : []); setLoading(false); })
      .catch(() => setLoading(false));
  };

  useEffect(() => { loadPatterns(); }, []);

  const filtered = patterns.filter((p) => {
    if (!search) return true;
    const q = search.toLowerCase();
    return p.name.toLowerCase().includes(q) || p.description.toLowerCase().includes(q);
  });

  const handleDeploy = async (patternId: string, projectName: string) => {
    setDeploying(patternId);
    try {
      const resp = await fetch(`/api/v1/patterns/${patternId}/deploy`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: projectName }),
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
              <SearchInput
                placeholder="Search patterns..."
                value={search}
                onChange={(_e, val) => setSearch(val)}
                onClear={() => setSearch("")}
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
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(340px, 1fr))", gap: 16 }}>
            {filtered.map((pattern) => (
              <Card key={pattern.id} isCompact style={{ cursor: "pointer" }} onClick={() => setPreviewPattern({ id: pattern.id, name: pattern.name })}>
                <CardTitle>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                    <strong>{pattern.name}</strong>
                    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                      <Label color={visibilityColor(pattern.visibility)}>{pattern.visibility}</Label>
                      <span
                        style={{ color: "var(--pf-t--global--color--status--danger--default)", cursor: "pointer", padding: "0 4px", fontSize: 14 }}
                        onClick={(e) => {
                          e.stopPropagation();
                          if (!window.confirm(`Delete pattern "${pattern.name}"? This cannot be undone.`)) return;
                          fetch(`/api/v1/patterns/${pattern.id}`, { method: "DELETE" })
                            .then((r) => {
                              if (r.ok) setPatterns(patterns.filter((p) => p.id !== pattern.id));
                            });
                        }}
                      >✕</span>
                    </div>
                  </div>
                </CardTitle>
                <CardBody>
                  {pattern.description && (
                    <p style={{ fontSize: 13, opacity: 0.7, marginBottom: 8 }}>{pattern.description}</p>
                  )}
                  <div style={{ fontSize: 12, opacity: 0.6, marginBottom: 12 }}>
                    {pattern.disk_count} disk{pattern.disk_count !== 1 ? "s" : ""}
                    {" · "}{formatSize(pattern.total_size_bytes)}
                    {" · "}{new Date(pattern.created_at).toLocaleDateString()}
                  </div>
                  <div style={{ display: "flex", gap: 8 }} onClick={(e) => e.stopPropagation()}>
                    <Button
                      variant="primary"
                      size="sm"
                      onClick={() => setDeployPattern({ id: pattern.id, name: pattern.name })}
                    >
                      Create Project
                    </Button>
                    <Button
                      variant="secondary"
                      size="sm"
                      onClick={() => setBulkPatternId(pattern.id)}
                    >
                      Bulk Deploy
                    </Button>
                  </div>
                </CardBody>
              </Card>
            ))}
          </div>
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
        onDeploy={(name) => handleDeploy(deployPattern.id, name)}
        onClose={() => { if (!deploying) setDeployPattern(null); }}
      />}
    </>
  );
}
