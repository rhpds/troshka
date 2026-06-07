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

export default function PatternsPage() {
  const router = useRouter();
  const [patterns, setPatterns] = useState<Pattern[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [bulkPatternId, setBulkPatternId] = useState<string | null>(null);
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

  const handleDeploy = async (patternId: string) => {
    setDeploying(patternId);
    try {
      const resp = await fetch(`/api/v1/patterns/${patternId}/deploy`, { method: "POST" });
      if (resp.ok) {
        const data = await resp.json();
        router.push(`/projects/${data.project_id}`);
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

  const formatSize = (gb: number) => {
    if (gb < 1) return `${Math.round(gb * 1024)} MB`;
    return `${gb.toFixed(1)} GB`;
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
              <Card key={pattern.id} isCompact>
                <CardTitle>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                    <strong>{pattern.name}</strong>
                    <Label color={visibilityColor(pattern.visibility)}>{pattern.visibility}</Label>
                  </div>
                </CardTitle>
                <CardBody>
                  {pattern.description && (
                    <p style={{ fontSize: 13, opacity: 0.7, marginBottom: 8 }}>{pattern.description}</p>
                  )}
                  <div style={{ fontSize: 12, opacity: 0.6, marginBottom: 12 }}>
                    {pattern.disk_count} disk{pattern.disk_count !== 1 ? "s" : ""}
                    {" · "}{formatSize(pattern.total_size_gb)}
                    {" · "}{new Date(pattern.created_at).toLocaleDateString()}
                  </div>
                  <div style={{ display: "flex", gap: 8 }}>
                    <Button
                      variant="primary"
                      size="sm"
                      onClick={() => handleDeploy(pattern.id)}
                      isLoading={deploying === pattern.id}
                      isDisabled={deploying === pattern.id}
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
    </>
  );
}
