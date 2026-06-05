"use client";

import React, { useEffect, useState } from "react";
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

interface Host {
  id: string;
  instance_id: string | null;
  instance_type: string | null;
  region: string | null;
  state: string;
  host_type: string;
  total_vcpus: number;
  total_ram_mb: number;
  used_vcpus: number;
  used_ram_mb: number;
  ip_address: string | null;
  agent_status: string;
  created_at: string;
}

interface RegionSummary {
  region: string;
  total_hosts: number;
  active_hosts: number;
  total_vcpus: number;
  used_vcpus: number;
  total_ram_mb: number;
  used_ram_mb: number;
}

export default function AdminHostsPage() {
  const [hosts, setHosts] = useState<Host[]>([]);
  const [summary, setSummary] = useState<RegionSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [provisioning, setProvisioning] = useState(false);
  const [error, setError] = useState("");
  const [filterRegion, setFilterRegion] = useState("");

  const loadData = () => {
    Promise.all([
      fetch("/api/v1/hosts/").then((r) => r.ok ? r.json() : []),
      fetch("/api/v1/hosts/summary").then((r) => r.ok ? r.json() : []),
    ]).then(([h, s]) => {
      setHosts(Array.isArray(h) ? h : []);
      setSummary(Array.isArray(s) ? s : []);
      setLoading(false);
    }).catch(() => setLoading(false));
  };

  useEffect(() => { loadData(); }, []);

  const addHost = async () => {
    const instanceType = window.prompt("Instance type (e.g., m8i.xlarge):", "m8i.xlarge");
    if (!instanceType) return;
    const region = window.prompt("Region:", "us-east-1");
    if (!region) return;

    setProvisioning(true);
    setError("");
    try {
      const resp = await fetch("/api/v1/hosts/", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ instance_type: instanceType, region }),
      });
      if (!resp.ok) {
        const data = await resp.json();
        setError(data.detail || "Failed to provision host");
      } else {
        loadData();
      }
    } catch {
      setError("Failed to connect to server");
    }
    setProvisioning(false);
  };

  const removeHost = async (hostId: string, instanceId: string | null) => {
    if (!window.confirm(`Remove host ${instanceId || hostId}? This will terminate the EC2 instance.`)) return;
    const resp = await fetch(`/api/v1/hosts/${hostId}`, { method: "DELETE" });
    if (resp.ok) {
      loadData();
    } else {
      const data = await resp.json();
      alert(data.detail || "Failed to remove host");
    }
  };

  const filteredHosts = filterRegion ? hosts.filter((h) => (h.region || "unknown") === filterRegion) : hosts;

  const stateColors: Record<string, string> = {
    active: "#4ade80",
    provisioning: "#fbbf24",
    draining: "#fbbf24",
    terminated: "#94a3b8",
  };

  const agentColors: Record<string, string> = {
    connected: "#4ade80",
    disconnected: "#f87171",
  };

  if (loading) {
    return <PageSection><Title headingLevel="h1">Loading...</Title></PageSection>;
  }

  return (
    <>
      <PageSection>
        <Toolbar>
          <ToolbarContent>
            <ToolbarItem>
              <Title headingLevel="h1">Host Pool</Title>
            </ToolbarItem>
            <ToolbarItem align={{ default: "alignEnd" }}>
              <Button variant="primary" onClick={addHost} isLoading={provisioning} isDisabled={provisioning}>
                {provisioning ? "Provisioning..." : "+ Add Host"}
              </Button>
            </ToolbarItem>
          </ToolbarContent>
        </Toolbar>
      </PageSection>

      {error && (
        <PageSection>
          <Alert variant="danger" title={error} />
        </PageSection>
      )}

      {/* Region Summary Cards */}
      <PageSection>
        <div style={{ display: "flex", gap: 16, flexWrap: "wrap", marginBottom: 16 }}>
          <Card
            isClickable isSelectable
            onClick={() => setFilterRegion("")}
            style={{ minWidth: 200, borderLeft: !filterRegion ? "3px solid var(--pf-t--global--color--brand--default)" : undefined }}
          >
            <CardBody>
              <div style={{ fontSize: 13, fontWeight: 600 }}>All Regions</div>
              <div style={{ fontSize: 24, fontWeight: 700 }}>{hosts.length}</div>
              <div style={{ fontSize: 11, opacity: 0.6 }}>hosts</div>
            </CardBody>
          </Card>
          {summary.map((s) => (
            <Card
              key={s.region}
              isClickable isSelectable
              onClick={() => setFilterRegion(s.region === filterRegion ? "" : s.region)}
              style={{ minWidth: 200, borderLeft: filterRegion === s.region ? "3px solid var(--pf-t--global--color--brand--default)" : undefined }}
            >
              <CardBody>
                <div style={{ fontSize: 13, fontWeight: 600 }}>{s.region}</div>
                <div style={{ fontSize: 24, fontWeight: 700 }}>{s.active_hosts}<span style={{ fontSize: 14, opacity: 0.5 }}>/{s.total_hosts}</span></div>
                <div style={{ fontSize: 11, opacity: 0.6 }}>active hosts</div>
                <div style={{ fontSize: 11, marginTop: 8 }}>
                  <span>vCPU: {s.used_vcpus}/{s.total_vcpus}</span>
                  <span style={{ marginLeft: 12 }}>RAM: {Math.round(s.used_ram_mb / 1024)}/{Math.round(s.total_ram_mb / 1024)} GB</span>
                </div>
                <div style={{ height: 4, background: "rgba(255,255,255,0.1)", borderRadius: 2, marginTop: 4 }}>
                  <div style={{
                    height: 4,
                    borderRadius: 2,
                    width: `${s.total_vcpus ? (s.used_vcpus / s.total_vcpus) * 100 : 0}%`,
                    background: (s.used_vcpus / Math.max(s.total_vcpus, 1)) > 0.8 ? "#f87171" : "#4ade80",
                  }} />
                </div>
              </CardBody>
            </Card>
          ))}
        </div>
      </PageSection>

      {/* Host List */}
      <PageSection>
        {filteredHosts.length === 0 && (
          <p style={{ opacity: 0.6 }}>No hosts{filterRegion ? ` in ${filterRegion}` : ""}. Click &quot;+ Add Host&quot; to provision one.</p>
        )}
        {filteredHosts.map((h) => (
          <Card key={h.id} style={{ marginBottom: 8 }}>
            <CardBody style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <div style={{ flex: 1 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <strong>{h.instance_id || h.id.slice(0, 8)}</strong>
                  <span style={{ fontSize: 11, padding: "1px 6px", borderRadius: 4, background: `${stateColors[h.state] || "#94a3b8"}22`, color: stateColors[h.state] || "#94a3b8" }}>
                    {h.state}
                  </span>
                  <span style={{ fontSize: 11, padding: "1px 6px", borderRadius: 4, background: `${agentColors[h.agent_status] || "#94a3b8"}22`, color: agentColors[h.agent_status] || "#94a3b8" }}>
                    agent: {h.agent_status}
                  </span>
                </div>
                <div style={{ fontSize: 12, opacity: 0.6, marginTop: 4 }}>
                  {h.instance_type} · {h.region} · {h.ip_address || "no IP"} · {h.host_type}
                </div>
              </div>
              <div style={{ textAlign: "right", marginRight: 16 }}>
                <div style={{ fontSize: 13 }}>
                  vCPU: <strong>{h.used_vcpus}</strong>/{h.total_vcpus}
                </div>
                <div style={{ fontSize: 13 }}>
                  RAM: <strong>{Math.round(h.used_ram_mb / 1024)}</strong>/{Math.round(h.total_ram_mb / 1024)} GB
                </div>
              </div>
              <Button variant="danger" onClick={() => removeHost(h.id, h.instance_id)} isDisabled={h.used_vcpus > 0}>
                {h.used_vcpus > 0 ? "In Use" : "Remove"}
              </Button>
            </CardBody>
          </Card>
        ))}
      </PageSection>
    </>
  );
}
