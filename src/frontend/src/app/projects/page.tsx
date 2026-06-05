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

export default function ProjectsPage() {
  const router = useRouter();
  const [projects, setProjects] = useState<Project[]>([]);
  const [loading, setLoading] = useState(true);

  const createProject = async () => {
    const name = window.prompt("Project name:");
    if (!name) return;
    try {
      const resp = await fetch(`${API_BASE}/api/v1/projects/`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      if (!resp.ok) {
        const err = await resp.json();
        alert(err.detail || "Failed to create project");
        return;
      }
      const project = await resp.json();
      router.push(`/projects/${project.id}`);
    } catch {
      alert("Failed to connect to server");
    }
  };

  useEffect(() => {
    fetch(`${API_BASE}/api/v1/projects/`)
      .then((r) => {
        if (!r.ok) throw new Error("Failed to fetch projects");
        return r.json();
      })
      .then((data) => {
        setProjects(Array.isArray(data) ? data : []);
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
          <Button variant="primary" icon={<PlusCircleIcon />} onClick={createProject}>
            New Project
          </Button>
        </EmptyState>
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
              <Button variant="primary" icon={<PlusCircleIcon />}>
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
            >
              <CardTitle style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                {p.name}
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
              </CardTitle>
              <CardBody>
                <p>{p.description || "No description"}</p>
                <p style={{ marginTop: 8, fontSize: 12, opacity: 0.7 }}>
                  State: {p.state} &middot; {p.host_type}
                </p>
              </CardBody>
            </Card>
          ))}
        </Gallery>
      </PageSection>
    </>
  );
}
