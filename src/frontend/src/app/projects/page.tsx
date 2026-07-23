"use client";

import React, { useEffect, useState } from "react";
import AlertModal from "@/components/AlertModal";
import TagEditor from "@/components/TagEditor";
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
  tags: Record<string, any> | null;
  guid: string | null;
  created_at: string;
  host_instance_id: string | null;
  host_ip: string | null;
  host_provider_name: string | null;
  host_provider_type: string | null;
  auto_stopped?: boolean;
  ocp_status?: string | null;
  ocp_status_detail?: string | null;
  ocp_install_elapsed?: number | null;
  deploy_progress?: { step?: string; detail?: string } | null;
  deploy_started_at?: string | null;
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
  deleting: "#fb923c",
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
  deploy_time?: string;
  bastion_image_name?: string;
}

function NewProjectModal({ onClose, onCreated, userRole, availableHosts, setAlertMsg }: { onClose: () => void; onCreated: (id: string) => void; userRole: string; availableHosts: {id: string; ip_address: string; instance_id: string; provider_type: string; used_vcpus: number; total_vcpus: number; used_ram_mb: number; total_ram_mb: number}[]; setAlertMsg: (msg: string | null) => void }) {
  const [mode, setMode] = useState<"choose" | "blank" | "yaml" | "pattern" | "template" | "template-picker">("choose");
  const [yamlContent, setYamlContent] = useState("");
  const [yamlFileName, setYamlFileName] = useState("");
  const [name, setName] = useState("");
  const [patterns, setPatterns] = useState<PatternSummary[]>([]);
  const [templates, setTemplates] = useState<TemplateSummary[]>([]);
  const [selectedPattern, setSelectedPattern] = useState<string | null>(null);
  const [selectedTemplate, setSelectedTemplate] = useState<string | null>(null);
  const [patternSearch, setPatternSearch] = useState("");
  const [patternDropdownOpen, setPatternDropdownOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [nameAutoSet, setNameAutoSet] = useState(true);
  const versionedName = (tName: string, ver: string) => ver ? tName.replace(/^(OpenShift)/, `$1 ${ver}`) : tName;
  const [bastionImageId, setBastionImageId] = useState("");
  const [bastionIsoId, setBastionIsoId] = useState("");
  const [bastionSshKeyId, setBastionSshKeyId] = useState("");
  const [commonPassword, setCommonPassword] = useState(() => {
    const chars = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789";
    return Array.from({ length: 12 }, () => chars[Math.floor(Math.random() * chars.length)]).join("");
  });
  const [bastionBmcIp, setBastionBmcIp] = useState("192.168.100.50");
  const [bmcIpError, setBmcIpError] = useState("");
  const [autoDeploy, setAutoDeploy] = useState(true);
  const [autoStart, setAutoStart] = useState(true);
  const [clusterName, setClusterName] = useState("ocp");
  const [baseDomain, setBaseDomain] = useState("ocp.local");
  const [ocpVersion, setOcpVersion] = useState("");
  const [ocpVersions, setOcpVersions] = useState<{minor: string; latest: string}[]>([]);
  const [autoInstallOcp, setAutoInstallOcp] = useState(true);
  const [externalAccess, setExternalAccess] = useState(false);
  const [blockOutbound, setBlockOutbound] = useState(true);
  const [deployHostId, setDeployHostId] = useState("");
  const [customVersion, setCustomVersion] = useState(false);
  const [customVersionText, setCustomVersionText] = useState("");
  const [loadingVersions, setLoadingVersions] = useState(true);
  const [hasPullSecret, setHasPullSecret] = useState(false);
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
    fetch(`${API_BASE}/api/v1/ocp/versions`)
      .then((r) => r.ok ? r.json() : [])
      .then((data) => {
        setOcpVersions(Array.isArray(data) ? data : []);
        if (data.length) {
          const ver = data[data.length - 1].minor;
          setOcpVersion(ver);
          setName((prev) => prev ? prev.replace(/^(OpenShift)(\s+\d+\.\d+)?/, `$1 ${ver}`) : prev);
        }
        setLoadingVersions(false);
      })
      .catch(() => { setLoadingVersions(false); });
    fetch(`${API_BASE}/api/v1/library/`)
      .then((r) => r.ok ? r.json() : [])
      .then((data) => {
        const items = Array.isArray(data) ? data : [];
        const addSize = (i: any) => ({ ...i, size_gb: Math.round((i.size_bytes || 0) / (1024 ** 3)) });
        const images = items.filter((i: any) => i.format === "qcow2" && i.state === "ready").map(addSize);
        const isos = items.filter((i: any) => i.format === "iso" && i.state === "ready").map(addSize);
        setLibraryImages(images);
        setLibraryIsos(isos);
        const defaultImg = images.find((i: any) => i.tags?.ocp_default_image)
          || images.find((i: any) => /rhel\s+\d+(\.\d+)?.*image/i.test(i.name));
        if (defaultImg) {
          setBastionImageId(defaultImg.id);
          const defaultIso = isos.find((i: any) => i.tags?.ocp_default_iso);
          if (defaultIso) {
            setBastionIsoId(defaultIso.id);
          } else {
            const verMatch = defaultImg.name.match(/(\d+\.\d+)/);
            if (verMatch) {
              const matchingIso = isos.find((i: any) => i.name.includes(verMatch[1]) && /dvd|binary/i.test(i.name));
              if (matchingIso) setBastionIsoId(matchingIso.id);
            }
          }
        }
      })
      .catch(() => {});
    fetch(`${API_BASE}/api/v1/auth/ssh-keys`)
      .then((r) => r.ok ? r.json() : [])
      .then((data) => setSshKeys(Array.isArray(data) ? data : []))
      .catch(() => {});
    fetch(`${API_BASE}/api/v1/auth/ocp-pull-secret`)
      .then((r) => r.ok ? r.json() : {})
      .then((data: { has_secret?: boolean }) => setHasPullSecret(data.has_secret || false))
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
          body: JSON.stringify({ name, auto_deploy: autoDeploy, auto_start: autoStart }),
        });
        if (resp.ok) {
          const data = await resp.json();
          onCreated(data.id);
        } else {
          const err = await resp.json().catch(() => ({ detail: "Failed to create project" }));
          setAlertMsg(err.detail || "Failed to create project");
        }
      } else if (mode === "template" && selectedTemplate) {
        const templateBody: Record<string, any> = { template_id: selectedTemplate, name };
        if (bastionImageId) templateBody.bastion_image_id = bastionImageId;
        if (bastionIsoId) templateBody.bastion_iso_id = bastionIsoId;
        if (bastionSshKeyId) templateBody.bastion_ssh_key_id = bastionSshKeyId;
        if (commonPassword) templateBody.common_password = commonPassword;
        if (bastionBmcIp) templateBody.bastion_bmc_ip = bastionBmcIp;
        if (clusterName) templateBody.cluster_name = clusterName;
        if (baseDomain) templateBody.base_domain = baseDomain;
        if (ocpVersion) templateBody.ocp_version = ocpVersion;
        templateBody.auto_install_ocp = autoInstallOcp;
        templateBody.external_access = externalAccess;
        templateBody.block_outbound = blockOutbound;
        const resp = await fetch(`${API_BASE}/api/v1/projects/from-template`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(templateBody),
        });
        if (!resp.ok) {
          const err = await resp.json().catch(() => ({ detail: "Failed to create project" }));
          setAlertMsg(err.detail || "Failed to create project");
        } else {
          const data = await resp.json();
          // Load topology into canvas store, auto-arrange, save back before deploying
          const projResp = await fetch(`${API_BASE}/api/v1/projects/${data.id}`);
          if (projResp.ok) {
            const proj = await projResp.json();
            const t = proj.topology || {};
            if ((t.nodes || []).length > 0) {
              const { useCanvasStore } = await import("@/stores/canvasStore");
              useCanvasStore.setState({
                currentProjectId: data.id,
                nodes: t.nodes || [],
                edges: t.edges || [],
                hiddenNodeIds: t.hiddenNodeIds || [],
                startOrder: t.startOrder || [],
                externalIps: t.externalIps || [],
              });
              useCanvasStore.getState().autoLayout();
              const s = useCanvasStore.getState();
              await fetch(`${API_BASE}/api/v1/projects/${data.id}`, {
                method: "PATCH",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ topology: { nodes: s.nodes, edges: s.edges, hiddenNodeIds: s.hiddenNodeIds, startOrder: s.startOrder, externalIps: s.externalIps } }),
              });
            }
          }
          if (autoDeploy) {
            const deployParams = new URLSearchParams();
            if (deployHostId) deployParams.set("host_id", deployHostId);
            const deployQs = deployParams.toString() ? `?${deployParams.toString()}` : "";
            const deployResp = await fetch(`${API_BASE}/api/v1/projects/${data.id}/deploy${deployQs}`, { method: "POST" });
            if (!deployResp.ok) {
              const err = await deployResp.json().catch(() => ({ detail: "Deploy failed" }));
              setAlertMsg(err.detail || "Deploy failed");
            }
          }
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
          if (mode === "yaml" && yamlContent) {
            try {
              const jsYaml = await import("js-yaml");
              const parsed = jsYaml.load(yamlContent) as Record<string, unknown>;
              const importResp = await fetch(`${API_BASE}/api/v1/projects/${data.id}/import-template`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ template_yaml: parsed }),
              });
              if (!importResp.ok) {
                const err = await importResp.json().catch(() => ({ detail: "Import failed" }));
                setAlertMsg(err.detail || "Template import failed");
              }
            } catch {
              setAlertMsg("Invalid YAML syntax in template file");
            }
          }
          onCreated(data.id);
        } else {
          const err = await resp.json().catch(() => ({ detail: "Failed to create project" }));
          setAlertMsg(err.detail || "Failed to create project");
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
        borderRadius: 12, padding: 24, width: mode === "template-picker" ? 640 : 500, maxWidth: "90vw",
        transition: "width 0.2s ease",
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
              <div style={optionStyle(false)} onClick={() => setMode("yaml")}>
                <div style={{ fontSize: 28, marginBottom: 4 }}>📋</div>
                <div style={{ fontWeight: 600, fontSize: 14 }}>From Template</div>
                <div style={{ fontSize: 12, opacity: 0.6, marginTop: 4 }}>Import YAML</div>
              </div>
            </div>
            <div style={{ display: "flex", gap: 12 }}>
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
              {templates.length > 0 && (
                <div
                  style={{ ...optionStyle(false), flex: 1 }}
                  onClick={() => setMode("template-picker" as any)}
                >
                  <div style={{ fontSize: 28, marginBottom: 4 }}>🚀</div>
                  <div style={{ fontWeight: 600, fontSize: 14 }}>Quick Starts</div>
                  <div style={{ fontSize: 12, opacity: 0.6, marginTop: 4 }}>Guided deploy</div>
                </div>
              )}
            </div>
            <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 4 }}>
              <button onClick={onClose} style={{ ...inputStyle, width: "auto", cursor: "pointer", padding: "6px 16px" }}>
                Cancel
              </button>
            </div>
          </div>
        ) : mode === "template-picker" ? (
          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
              <button onClick={() => setMode("choose")} style={{ background: "none", border: "none", color: "var(--pf-t--global--text--color--regular)", cursor: "pointer", fontSize: 16, padding: 0 }}>
                ←
              </button>
              <span style={{ fontWeight: 600, fontSize: 14 }}>Choose a Template</span>
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(180px, 1fr))", gap: 12 }}>
              {templates.map((t) => (
                <div
                  key={t.id}
                  style={{
                    ...optionStyle(false),
                    flex: "none",
                    display: "flex",
                    flexDirection: "column",
                    alignItems: "center",
                    padding: "16px 12px",
                  }}
                  onClick={() => {
                    setSelectedTemplate(t.id);
                    setName(versionedName(t.name, ocpVersion));
                    setNameAutoSet(true);
                    setMode("template");
                    if (t.bastion_image_name) {
                      const match = libraryImages.find((i) => i.name === t.bastion_image_name);
                      if (match) {
                        setBastionImageId(match.id);
                        const ver = match.name.match(/(\d+\.\d+)/);
                        if (ver) {
                          const iso = libraryIsos.find((i) => i.name.includes(ver[1]) && /dvd|binary/i.test(i.name));
                          if (iso) setBastionIsoId(iso.id);
                        }
                      }
                    }
                  }}
                >
                  <div style={{ marginBottom: 8, display: "flex", justifyContent: "center" }}>
                    {t.category === "openshift" ? (
                      <svg width="32" height="32" viewBox="0 0 24 24" fill="#EE0000" xmlns="http://www.w3.org/2000/svg">
                        <path d="M21.665,11.812c-0.11-1.377-0.476-2.724-1.08-3.966L24,6.599c-0.268-0.556-0.585-1.092-0.943-1.595 l-1.601,0.583c-3.534-4.95-10.412-6.098-15.363-2.565c-3.144,2.244-4.883,5.972-4.582,9.823l1.604-0.584 c0.051,0.615,0.153,1.224,0.305,1.822L0,15.335c0.338,1.339,0.922,2.604,1.721,3.731l1.812-0.659 c3.526,4.95,10.398,6.106,15.349,2.58c1.555-1.107,2.796-2.6,3.599-4.332c0.802-1.715,1.144-3.61,0.991-5.497L21.665,11.812z M16.925,9.177c0.687,1.227,0.998,2.629,0.895,4.032l1.809-0.657c-0.063,0.856-0.282,1.694-0.646,2.471 c-1.67,3.584-5.928,5.138-9.514,3.472c-0.782-0.365-1.491-0.87-2.092-1.49l-1.813,0.66c-0.979-1.01-1.64-2.285-1.903-3.667 l3.426-1.242c-0.121-0.624-0.159-1.262-0.111-1.896H6.97l-1.604,0.583c0.294-3.932,3.72-6.881,7.652-6.587 c0.868,0.065,1.716,0.288,2.504,0.658V5.508c0.778,0.364,1.483,0.867,2.082,1.483l1.599-0.582c0.002,0.002,0.004,0.003,0.006,0.005 c0.441,0.454,0.82,0.965,1.128,1.518L16.925,9.177z"/>
                      </svg>
                    ) : (
                      <img src="/images/troshka-logo-32.png" alt="" width={32} height={32} />
                    )}
                  </div>
                  <div style={{ fontWeight: 600, fontSize: 13, textAlign: "center" }}>{t.name}</div>
                  <div style={{ fontSize: 11, opacity: 0.6, marginTop: 4, textAlign: "center", lineHeight: 1.3 }}>{t.description}</div>
                  {t.deploy_time && <div style={{ fontSize: 11, opacity: 0.5, marginTop: 6 }}>⏱ {t.deploy_time}</div>}
                </div>
              ))}
            </div>
            <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 4 }}>
              <button onClick={() => setMode("choose")} style={{ ...inputStyle, width: "auto", cursor: "pointer", padding: "6px 16px" }}>
                Back
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
                onChange={(e) => { setName(e.target.value); setNameAutoSet(false); }}
                placeholder="My Project"
                autoFocus={mode === "blank"}
                onKeyDown={(e) => { if (e.key === "Enter") handleCreate(); }}
              />
            </div>
            {mode === "yaml" && (
              <div>
                <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Template YAML</label>
                <textarea
                  value={yamlContent}
                  onChange={(e) => { setYamlContent(e.target.value); setYamlFileName(""); }}
                  placeholder={"networks:\n  network-00:\n    cidr: 10.0.0.0/24\n    dhcp: true\nvms:\n  vm-00:\n    vcpus: 2\n    ram_gb: 4\n    ..."}
                  style={{
                    width: "100%", height: 200, fontFamily: "monospace", fontSize: 12,
                    padding: 12, borderRadius: 8, resize: "vertical",
                    background: "var(--pf-t--global--background--color--secondary--default)",
                    color: "var(--pf-t--global--text--color--regular)",
                    border: "1px solid var(--pf-t--global--border--color--default)",
                  }}
                />
                {yamlFileName && (
                  <div style={{ fontSize: 11, color: "var(--pf-t--global--text--color--subtle)", marginTop: 4 }}>
                    Loaded from {yamlFileName}
                  </div>
                )}
              </div>
            )}
            {mode === "template" && selectedTemplate && (() => {
              const _selTmpl = templates.find((t) => t.id === selectedTemplate);
              const _isOcp = _selTmpl?.category === "openshift";
              return (
              <>
                <div style={{
                  padding: "8px 12px", borderRadius: 6,
                  background: "rgba(59,130,246,0.08)", border: "1px solid rgba(59,130,246,0.3)",
                  fontSize: 12,
                }}>
                  Template: <strong>{_selTmpl?.name}</strong>
                  <div style={{ opacity: 0.6, marginTop: 2 }}>{_selTmpl?.description}</div>
                </div>
                {_isOcp && <div style={{ borderTop: "1px solid var(--pf-t--global--border--color--default)", paddingTop: 12, marginTop: 4 }}>
                  <div style={{ fontSize: 11, color: "var(--pf-t--global--text--color--subtle)", marginBottom: 8 }}>Cluster DNS</div>
                  <div style={{ display: "flex", gap: 8, marginBottom: 8 }}>
                    <div style={{ flex: 1 }}>
                      <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Cluster Name</label>
                      <input style={inputStyle} value={clusterName} onChange={(e) => setClusterName(e.target.value)} placeholder="ocp" />
                    </div>
                    <div style={{ flex: 2 }}>
                      <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Base Domain</label>
                      <input style={inputStyle} value={baseDomain} onChange={(e) => setBaseDomain(e.target.value)} placeholder="ocp.local" />
                    </div>
                    <div style={{ flex: 1 }}>
                      <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>OCP Version</label>
                      {loadingVersions ? (
                        <div style={{ ...inputStyle, display: "flex", alignItems: "center", justifyContent: "center" }}>
                          <span className="project-btn-spinner" style={{ width: 14, height: 14, aspectRatio: "1 / 1" }} />
                        </div>
                      ) : customVersion ? (
                        <input style={inputStyle} autoFocus value={customVersionText} placeholder="e.g. 4.18" onChange={(e) => {
                          const v = e.target.value.replace(/[^\d.]/g, ""); setCustomVersionText(v);
                          if (/^\d+\.\d+$/.test(v)) { setOcpVersion(v); if (nameAutoSet && selectedTemplate) { const t = templates.find((t) => t.id === selectedTemplate); if (t) setName(versionedName(t.name, v)); } }
                        }} onBlur={() => { if (!/^\d+\.\d+$/.test(customVersionText)) { setCustomVersion(false); setOcpVersion(ocpVersions.length ? ocpVersions[ocpVersions.length - 1].minor : "4.20"); } }} />
                      ) : (
                        <select style={inputStyle} value={ocpVersion} onChange={(e) => {
                          if (e.target.value === "__other__") { setCustomVersion(true); setCustomVersionText(""); }
                          else {
                            setOcpVersion(e.target.value);
                            if (nameAutoSet && selectedTemplate) { const t = templates.find((t) => t.id === selectedTemplate); if (t) setName(versionedName(t.name, e.target.value)); }
                          }
                        }}>
                          {ocpVersions.map((v) => (
                            <option key={v.minor} value={v.minor}>{v.minor} (latest: {v.latest})</option>
                          ))}
                          <option value="__other__">Other...</option>
                        </select>
                      )}
                    </div>
                  </div>
                  <div style={{ fontSize: 10, color: "var(--pf-t--global--text--color--subtle)", marginBottom: 4, fontFamily: "monospace" }}>
                    api.{clusterName}.{baseDomain} → 10.0.0.2 (LB)
                  </div>
                </div>}
                <div style={{ borderTop: "1px solid var(--pf-t--global--border--color--default)", paddingTop: 12, marginTop: 4 }}>
                  <div style={{ fontSize: 11, color: "var(--pf-t--global--text--color--subtle)", marginBottom: 8 }}>{_isOcp ? "Bastion Configuration" : "Configuration"}</div>
                  <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                    <div>
                      <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Red Hat Enterprise Linux KVM Guest Image <span style={{ color: "#f87171" }}>*</span></label>
                      {libraryImages.length > 0 ? (
                        <select style={inputStyle} value={bastionImageId} onChange={(e) => {
                          setBastionImageId(e.target.value);
                          const img = libraryImages.find((i) => i.id === e.target.value);
                          if (img) {
                            const ver = img.name.match(/(\d+\.\d+)/);
                            if (ver) {
                              const match = libraryIsos.find((i) => i.name.includes(ver[1]) && /dvd|binary/i.test(i.name));
                              if (match) setBastionIsoId(match.id);
                              else setBastionIsoId("");
                            }
                          }
                        }}>
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
                      <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Red Hat Enterprise Linux Binary DVD ISO (For local package repos) <span style={{ color: "#f87171" }}>*</span></label>
                      <select style={inputStyle} value={bastionIsoId} onChange={(e) => setBastionIsoId(e.target.value)}>
                        <option value="">Select an ISO...</option>
                        {libraryIsos.map((img) => (
                          <option key={img.id} value={img.id}>{img.name}</option>
                        ))}
                      </select>
                    </div>
                    {_isOcp && <div style={{ fontSize: 12, marginBottom: 8, display: "flex", alignItems: "center", gap: 6 }}>
                      <span>Pull Secret <span style={{ color: "#f87171" }}>*</span>:</span>
                      {hasPullSecret ? (
                        <span style={{ color: "#4ade80" }}>configured ✓</span>
                      ) : (
                        <span style={{ color: "#f87171" }}>not set — <a href="/settings" style={{ color: "#3b82f6" }}>configure in Settings</a></span>
                      )}
                    </div>}
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
                      <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Password <span style={{ color: "#f87171" }}>*</span> <span style={{ color: "var(--pf-t--global--text--color--subtle)" }}>(cloud-user + BMC)</span></label>
                      <div style={{ display: "flex", gap: 4 }}>
                        <input style={{ ...inputStyle, width: "auto", flex: "1 1 0", minWidth: 0 }} value={commonPassword} onChange={(e) => setCommonPassword(e.target.value)} placeholder="Used for console access and BMC auth" onKeyDown={(e) => { if (e.key === "Enter") handleCreate(); }} />
                        <button
                          type="button"
                          style={{ ...inputStyle, width: "auto", flex: "0 0 auto", cursor: "pointer", padding: "4px 10px", fontSize: 12 }}
                          onClick={() => { navigator.clipboard.writeText(commonPassword); }}
                          title="Copy password"
                        >Copy</button>
                      </div>
                    </div>
                    {_isOcp && <div>
                      <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 4 }}>
                        <label style={{ fontSize: 12 }}>Bastion BMC IP</label>
                        <div style={{ position: "relative", display: "inline-block" }}>
                          <span
                            style={{ cursor: "help", fontSize: 12, width: 16, height: 16, borderRadius: "50%", background: "rgba(59,130,246,0.2)", color: "#3b82f6", display: "inline-flex", alignItems: "center", justifyContent: "center", fontWeight: 600 }}
                            title="BMC Network Info"
                            onClick={(e) => { e.stopPropagation(); const el = e.currentTarget.nextElementSibling as HTMLElement; el.style.display = el.style.display === "none" ? "block" : "none"; }}
                          >i</span>
                          <div style={{
                            display: "none", position: "absolute", left: 24, top: -8, zIndex: 100,
                            background: "var(--pf-t--global--background--color--primary--default)",
                            border: "1px solid var(--pf-t--global--border--color--default)",
                            borderRadius: 8, padding: "10px 14px", width: 260,
                            boxShadow: "0 4px 16px rgba(0,0,0,0.4)", fontSize: 11, lineHeight: 1.6,
                          }}>
                            <div style={{ fontWeight: 600, marginBottom: 4 }}>BMC Network (192.168.100.0/24)</div>
                            <div style={{ fontFamily: "monospace", fontSize: 10 }}>
                              <div>cp-0:      192.168.100.10:8000</div>
                              <div>cp-1:      192.168.100.11:8000</div>
                              <div>cp-2:      192.168.100.12:8000</div>
                              <div>bootstrap: 192.168.100.13:8000</div>
                              <div style={{ borderTop: "1px solid var(--pf-t--global--border--color--default)", marginTop: 4, paddingTop: 4 }}>
                                IPMI: port 623 (same IPs)
                              </div>
                              <div>BMC user: admin</div>
                              <div>BMC pass: (same as password above)</div>
                            </div>
                          </div>
                        </div>
                      </div>
                      <input
                        style={{ ...inputStyle, borderColor: bmcIpError ? "#f87171" : undefined }}
                        value={bastionBmcIp}
                        onChange={(e) => {
                          setBastionBmcIp(e.target.value);
                          const ip = e.target.value.trim();
                          if (!ip) { setBmcIpError("Required"); return; }
                          const match = ip.match(/^(\d+\.\d+\.\d+)\.(\d+)$/);
                          if (!match) { setBmcIpError("Invalid IP"); return; }
                          if (match[1] !== "192.168.100") { setBmcIpError("Must be in 192.168.100.0/24"); return; }
                          const octet = parseInt(match[2]);
                          if (octet < 2 || octet > 254 || octet <= 13) { setBmcIpError("Must be > .13 (reserved for BMC endpoints)"); return; }
                          setBmcIpError("");
                        }}
                        placeholder="192.168.100.50"
                      />
                      {bmcIpError && <div style={{ fontSize: 11, color: "#f87171", marginTop: 2 }}>{bmcIpError}</div>}
                    </div>}
                  </div>
                  <div style={{ borderTop: "1px solid var(--pf-t--global--border--color--default)", paddingTop: 8, marginTop: 4 }}>
                    {userRole === "admin" && availableHosts.length > 0 && (
                      <div style={{ marginBottom: 6 }}>
                        <label style={{ fontSize: 12, display: "block", marginBottom: 2 }}>Host</label>
                        <select style={{
                          padding: "4px 8px", borderRadius: 6, fontSize: 12, width: "100%",
                          border: "1px solid var(--pf-t--global--border--color--default)",
                          background: "var(--pf-t--global--background--color--primary--default)",
                          color: "var(--pf-t--global--text--color--regular)",
                        }} value={deployHostId} onChange={(e) => setDeployHostId(e.target.value)}>
                          <option value="">Auto (best host)</option>
                          {availableHosts.map((h) => (
                            <option key={h.id} value={h.id}>
                              {h.id.slice(0, 8)} — {h.ip_address} ({h.provider_type}), {h.total_vcpus - h.used_vcpus} vCPUs / {Math.round((h.total_ram_mb - h.used_ram_mb) / 1024)}G free
                            </option>
                          ))}
                        </select>
                      </div>
                    )}
                    <label style={{ fontSize: 12, display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
                      <input type="checkbox" checked={autoDeploy} onChange={(e) => setAutoDeploy(e.target.checked)} />
                      Deploy immediately after creation
                    </label>
                    <label style={{ fontSize: 12, display: "flex", alignItems: "center", gap: 8, cursor: "pointer", marginTop: 4 }}>
                      <input type="checkbox" checked={externalAccess} onChange={(e) => setExternalAccess(e.target.checked)} />
                      External access (allocate EIP)
                    </label>
                    {_isOcp && <label style={{ fontSize: 12, display: "flex", alignItems: "center", gap: 8, cursor: "pointer", marginTop: 4 }}>
                      <input type="checkbox" checked={blockOutbound} onChange={(e) => setBlockOutbound(e.target.checked)} />
                      Restrict outbound ports to install OCP only
                    </label>}
                    {_isOcp && <div style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 4 }}>
                      <label style={{ fontSize: 12, display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
                        <input type="checkbox" checked={autoInstallOcp} onChange={(e) => setAutoInstallOcp(e.target.checked)} />
                        Auto-run OCP installer on bastion
                      </label>
                      <div style={{ position: "relative", display: "inline-block" }}>
                        <span
                          style={{ cursor: "help", fontSize: 12, width: 16, height: 16, borderRadius: "50%", background: "rgba(59,130,246,0.2)", color: "#3b82f6", display: "inline-flex", alignItems: "center", justifyContent: "center", fontWeight: 600 }}
                          onClick={(e) => { e.stopPropagation(); const el = e.currentTarget.nextElementSibling as HTMLElement; el.style.display = el.style.display === "none" ? "block" : "none"; }}
                        >i</span>
                        <div style={{
                          display: "none", position: "absolute", left: 24, top: -8, zIndex: 100,
                          background: "var(--pf-t--global--background--color--primary--default)",
                          border: "1px solid var(--pf-t--global--border--color--default)",
                          borderRadius: 8, padding: "10px 14px", width: 280,
                          boxShadow: "0 4px 16px rgba(0,0,0,0.4)", fontSize: 11, lineHeight: 1.6,
                        }}>
                          <div style={{ fontWeight: 600, marginBottom: 4 }}>OCP Installer</div>
                          <div style={{ fontFamily: "monospace", fontSize: 10 }}>
                            <div>Watch progress:</div>
                            <div style={{ paddingLeft: 8 }}>tmux attach -t setup</div>
                            <div style={{ marginTop: 4 }}>Log file:</div>
                            <div style={{ paddingLeft: 8 }}>~/install.log</div>
                            <div style={{ marginTop: 4 }}>Kubeconfig (after install):</div>
                            <div style={{ paddingLeft: 8 }}>~/ocp-install/auth/kubeconfig</div>
                            <div style={{ marginTop: 4 }}>Kubeadmin password:</div>
                            <div style={{ paddingLeft: 8 }}>~/ocp-install/auth/kubeadmin-password</div>
                          </div>
                        </div>
                      </div>
                    </div>}
                  </div>
                </div>
              </>
              );
            })()}
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
            {mode === "pattern" && selectedPattern && (
              <div style={{ display: "flex", flexDirection: "column", gap: 4, marginTop: 8 }}>
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
                        <select style={{
                          padding: "4px 8px", borderRadius: 6, fontSize: 12, width: "100%",
                          border: "1px solid var(--pf-t--global--border--color--default)",
                          background: "var(--pf-t--global--background--color--primary--default)",
                          color: "var(--pf-t--global--text--color--regular)",
                        }} value={deployHostId} onChange={(e) => setDeployHostId(e.target.value)}>
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
            )}
            <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginTop: 4 }}>
              <button onClick={() => { setMode("choose"); setSelectedPattern(null); setSelectedTemplate(null); setYamlContent(""); setYamlFileName(""); }} style={{ ...inputStyle, width: "auto", cursor: "pointer", padding: "6px 16px" }}>
                Back
              </button>
              {mode === "yaml" && (
                <label style={{ ...inputStyle, width: "auto", cursor: "pointer", padding: "6px 16px", display: "inline-block", textAlign: "center" }}>
                  Upload File
                  <input type="file" accept=".yaml,.yml" style={{ display: "none" }} onChange={(e) => {
                    const file = e.target.files?.[0];
                    if (file) {
                      setYamlFileName(file.name);
                      file.text().then((text) => {
                        setYamlContent(text);
                        if (nameAutoSet || !name.trim()) {
                          try {
                            const jsYaml = require("js-yaml");
                            const parsed = jsYaml.load(text);
                            if (parsed?.name) { setName(parsed.name); setNameAutoSet(true); }
                          } catch { /* ignore */ }
                        }
                      });
                    }
                    e.target.value = "";
                  }} />
                </label>
              )}
              <button
                onClick={handleCreate}
                disabled={creating || !name.trim() || (mode === "yaml" && !yamlContent) || (mode === "pattern" && !selectedPattern) || (mode === "template" && (!selectedTemplate || (templates.find((t) => t.id === selectedTemplate)?.category === "openshift" && (!commonPassword || !bastionImageId || !bastionIsoId || !!bmcIpError || !hasPullSecret || loadingVersions))))}
                style={{
                  ...inputStyle, width: "auto", padding: "6px 16px",
                  cursor: creating ? "wait" : "pointer",
                  background: "rgba(74,222,128,0.15)", borderColor: "#4ade80", color: "#4ade80",
                  opacity: creating || !name.trim() || (mode === "yaml" && !yamlContent) || (mode === "pattern" && !selectedPattern) || (mode === "template" && (!selectedTemplate || (templates.find((t) => t.id === selectedTemplate)?.category === "openshift" && (!commonPassword || !bastionImageId || !bastionIsoId || !!bmcIpError || !hasPullSecret || loadingVersions)))) ? 0.4 : 1,
                }}
              >
                {creating ? "Creating..." : mode === "yaml" ? "Import & Create" : mode === "pattern" ? "Create from Pattern" : mode === "template" ? "Create" : "Create Project"}
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
  const [deletingProjects, setDeletingProjects] = useState<Set<string>>(() => {
    try {
      const stored = JSON.parse(localStorage.getItem("troshka-deleting-projects") || "[]");
      return new Set(stored);
    } catch { return new Set(); }
  });
  const [search, setSearch] = useState("");
  const [userRole, setUserRole] = useState("");
  const [pools, setPools] = useState<{id: string; name: string; mode: string; status: string}[]>([]);
  const [deployPoolId, setDeployPoolId] = useState("");
  const [availableHosts, setAvailableHosts] = useState<{id: string; ip_address: string; instance_id: string; provider_type: string; used_vcpus: number; total_vcpus: number; used_ram_mb: number; total_ram_mb: number}[]>([]);
  const [deployHostId, setDeployHostId] = useState("");
  const [alertMsg, setAlertMsg] = useState<string | null>(null);

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
        // Clean up deleting IDs only when project disappears from the list
        const projectIds = new Set(sorted.map((p: Project) => p.id));
        setDeletingProjects(prev => {
          const updated = new Set(prev);
          for (const id of prev) { if (!projectIds.has(id)) updated.delete(id); }
          if (updated.size !== prev.size) localStorage.setItem("troshka-deleting-projects", JSON.stringify([...updated]));
          return updated;
        });
      })
      .catch(() => {
        setProjects([]);
        setLoading(false);
      });
  };

  useEffect(() => {
    fetchProjects();
    fetch("/api/v1/auth/me").then(r => r.ok ? r.json() : {}).then((d: { role?: string }) => {
      setUserRole(d.role || "");
      if (d.role === "admin") {
        fetch("/api/v1/hosts/").then(r => r.ok ? r.json() : []).then(hosts => {
          setAvailableHosts(hosts.filter((h: any) => h.state === "active" && h.agent_status === "connected" && h.host_type !== "pattern_buffer"));
        });
      }
    });
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
          <NewProjectModal onClose={() => setShowNewModal(false)} onCreated={(id) => router.push(`/projects/${id}`)} userRole={userRole} availableHosts={availableHosts} setAlertMsg={setAlertMsg} />
        )}
        <AlertModal message={alertMsg} onClose={() => setAlertMsg(null)} />
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
              <CardBody component="a" href={`/projects/${p.id}`} style={{ display: "flex", alignItems: "flex-start", gap: 8, opacity: deletingProjects.has(p.id) ? 0.4 : 1, pointerEvents: deletingProjects.has(p.id) ? "none" : "auto", textDecoration: "none", color: "inherit" }} onClick={(e: React.MouseEvent) => { e.preventDefault(); if (!deletingProjects.has(p.id)) router.push(`/projects/${p.id}`); }}>
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
                    {p.guid && (
                      <span style={{ fontSize: 10, color: "var(--pf-t--global--text--color--subtle)", fontFamily: "monospace" }}>
                        {p.guid}
                      </span>
                    )}
                    <span style={{
                      fontSize: 11, padding: "1px 6px", borderRadius: 4,
                      background: `${stateColors[p.state] || "#94a3b8"}22`,
                      color: stateColors[p.state] || "#94a3b8",
                    }}>
                      {p.state === "stopped" && p.auto_stopped ? "stopped (auto)" : p.state}
                    </span>
                    {p.state === "deploying" && p.deploy_progress?.detail && (
                      <span style={{
                        fontSize: 11, padding: "1px 6px", borderRadius: 4,
                        background: "rgba(251,191,36,0.15)", color: "#fbbf24",
                      }}>
                        {p.deploy_progress.step ? `${p.deploy_progress.step}: ${p.deploy_progress.detail}` : p.deploy_progress.detail}
                      </span>
                    )}
                    {(p.state === "deploying" || p.state === "stopping" || p.state === "starting" || p.state === "deleting") && (
                      <span className="project-btn-spinner" style={{ width: 14, height: 14, marginLeft: "auto" }} />
                    )}
                    {p.ocp_status && p.ocp_status !== "none" && (
                      <span style={{
                        fontSize: 11, padding: "1px 6px", borderRadius: 4,
                        background: p.ocp_status === "ready" ? "rgba(74,222,128,0.15)" : p.ocp_status === "error" ? "rgba(248,113,113,0.15)" : "rgba(251,191,36,0.15)",
                        color: p.ocp_status === "ready" ? "#4ade80" : p.ocp_status === "error" ? "#f87171" : "#fbbf24",
                      }}>
                        {p.ocp_status_detail || `OCP ${p.ocp_status}`}
                      </span>
                    )}
                  </div>
                  <p style={{ fontSize: 13, opacity: 0.7, margin: "4px 0 0" }}>{p.description || "No description"}</p>
                  <p style={{ fontSize: 11, opacity: 0.5, margin: "4px 0 0" }}>
                    {p.host_type} &middot; {new Date(p.created_at).toLocaleDateString()}
                    {p.deploy_started_at && <> &middot; deployed {new Date(p.deploy_started_at).toLocaleString()}</>}
                    {p.host_instance_id && <> &middot; {p.host_instance_id}{p.host_ip ? ` · ${p.host_ip}` : ""}{p.host_provider_name ? ` · ${p.host_provider_name} (${p.host_provider_type})` : ""}</>}
                  </p>
                </div>
              </CardBody>
              {/* Row 2: Tags + Buttons */}
              <CardBody style={{ borderTop: "1px solid var(--pf-t--global--border--color--default)", display: "flex", gap: 8, flexWrap: "wrap", paddingTop: 8, paddingBottom: 8, alignItems: "center" }} onClick={(e) => e.stopPropagation()}>
                <TagEditor
                  tags={(p.tags?.user_tags as string[]) || []}
                  onAdd={async (tag) => {
                    const cur = (p.tags?.user_tags as string[]) || [];
                    await fetch(`/api/v1/projects/${p.id}`, {
                      method: "PATCH",
                      headers: { "Content-Type": "application/json" },
                      body: JSON.stringify({ tags: { ...(p.tags || {}), user_tags: [...cur, tag] } }),
                    });
                    fetchProjects();
                  }}
                  onRemove={async (tag) => {
                    const cur = (p.tags?.user_tags as string[]) || [];
                    await fetch(`/api/v1/projects/${p.id}`, {
                      method: "PATCH",
                      headers: { "Content-Type": "application/json" },
                      body: JSON.stringify({ tags: { ...(p.tags || {}), user_tags: cur.filter((t: string) => t !== tag) } }),
                    });
                    fetchProjects();
                  }}
                />
                {deletingProjects.has(p.id) ? (
                  <Button variant="plain" isDisabled><span className="project-btn-spinner" style={{ width: 12, height: 12 }} /> Deleting...</Button>
                ) : (<>
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
                    {userRole === "admin" && availableHosts.length > 0 && (
                      <select style={{
                        padding: "4px 8px", borderRadius: 6, fontSize: 12,
                        border: "1px solid var(--pf-t--global--border--color--default)",
                        background: "var(--pf-t--global--background--color--primary--default)",
                        color: "var(--pf-t--global--text--color--regular)",
                      }} value={deployHostId} onChange={(e) => setDeployHostId(e.target.value)}>
                        <option value="">Auto (best host)</option>
                        {availableHosts.map((h) => <option key={h.id} value={h.id}>{h.id.slice(0, 8)} — {h.ip_address} ({h.provider_type}), {h.total_vcpus - h.used_vcpus} vCPUs / {Math.round((h.total_ram_mb - h.used_ram_mb) / 1024)}G free</option>)}
                      </select>
                    )}
                    <Button variant="primary" onClick={() => {
                      if (!window.confirm(`Deploy project "${p.name}"? This will provision networking and start all VMs.`)) return;
                      setProjects(prev => prev.map(pr => pr.id === p.id ? { ...pr, state: "deploying" } : pr));
                      const params = new URLSearchParams();
                      if (deployPoolId) params.set("storage_pool_id", deployPoolId);
                      if (deployHostId) params.set("host_id", deployHostId);
                      const qs = params.toString() ? `?${params.toString()}` : "";
                      fetch(`${API_BASE}/api/v1/projects/${p.id}/deploy${qs}`, { method: "POST" }).then(r => r.json()).then(d => {
                        if (d.status === "deploying") { pollUntilSettled(); }
                        else { setAlertMsg(d.detail || "Deploy failed"); setProjects(prev => prev.map(pr => pr.id === p.id ? { ...pr, state: "draft" } : pr)); }
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
                      else { setAlertMsg(d.detail || "Republish failed"); setProjects(prev => prev.map(pr => pr.id === p.id ? { ...pr, state: "error" } : pr)); }
                    });
                  }}>Republish</Button>
                )}
                {p.state !== "deleting" && <Button variant="danger" isDisabled={deletingProjects.has(p.id)} onClick={() => {
                  if (!window.confirm(`Delete project "${p.name}"? This cannot be undone.`)) return;
                  setDeletingProjects(prev => new Set(prev).add(p.id));
                  fetch(`${API_BASE}/api/v1/projects/${p.id}`, { method: "DELETE" }).then((r) => {
                    if (r.ok) {
                      r.json().then((data) => {
                        if (data.status === "deleting") {
                          setProjects(prev => prev.map(pr => pr.id === p.id ? { ...pr, state: "deleting" } : pr));
                        } else {
                          setProjects(prev => prev.filter(pr => pr.id !== p.id));
                          localStorage.removeItem(`troshka-canvas-${p.id}`);
                        }
                      }).catch(() => {
                        setProjects(prev => prev.filter(pr => pr.id !== p.id));
                      });
                    }
                    setDeletingProjects(prev => { const s = new Set(prev); s.delete(p.id); return s; });
                  });
                }}>{deletingProjects.has(p.id) ? <><span className="project-btn-spinner" style={{ width: 12, height: 12 }} /> Deleting...</> : "Delete"}</Button>}
                </>)}
              </CardBody>
            </Card>
          ))}
        </div>
          </>);
        })()}
      </PageSection>
      {showNewModal && (
        <NewProjectModal onClose={() => setShowNewModal(false)} onCreated={(id) => router.push(`/projects/${id}`)} userRole={userRole} availableHosts={availableHosts} setAlertMsg={setAlertMsg} />
      )}
      <AlertModal message={alertMsg} onClose={() => setAlertMsg(null)} />
    </>
  );
}
