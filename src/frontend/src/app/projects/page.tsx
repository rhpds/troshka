"use client";

import LabeledField from "@/components/LabeledField";
import ModalBackdrop from "@/components/ModalBackdrop";

import React, { useEffect, useState } from "react";
import AlertModal from "@/components/AlertModal";
import ConfirmModal from "@/components/ConfirmModal";
import { appConfirm } from "@/lib/confirm";
import { formatApiDetail } from "@/lib/apiError";
import { generateImportPassword } from "@/lib/importPassword";
import { TEMPLATE_YAML_EXAMPLE } from "@/lib/templateImport";
import { summarizeDeployProgress } from "@/lib/deployProgress";
import {
  formatTemplateVersionLabel,
  pickDefaultOcpVersion,
} from "@/lib/ocpVersion";
import TagEditor from "@/components/TagEditor";
import OwnershipFilter, {
  DEFAULT_OWNERSHIP_FILTERS,
  matchesOwnershipFilter,
  type OwnershipCategory,
} from "@/components/OwnershipFilter";
import {
  Button,
  Card,
  CardBody,
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
import UserIcon from "@patternfly/react-icons/dist/esm/icons/user-icon";
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
  deploy_progress?: { step?: string; detail?: string; queue_position?: number; queue_length?: number } | null;
  deploy_started_at?: string | null;
  deploy_error?: string | null;
  owner_id?: string | null;
  owner_email?: string | null;
}

interface LiveSpend {
  total_usd: number;
  running_seconds?: number;
  running_since?: string | null;
  running_until?: string | null;
}

const API_BASE = "";

function formatRunningElapsed(totalSeconds: number): string {
  const total = Math.max(0, Math.floor(totalSeconds));
  const d = Math.floor(total / 86400);
  const h = Math.floor((total % 86400) / 3600);
  const m = Math.floor((total % 3600) / 60);
  const parts: string[] = [];
  if (d > 0) parts.push(`${d}d`);
  if (h > 0) parts.push(`${h}h`);
  parts.push(`${m}m`);
  return parts.join(" ");
}

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
  distribution?: string;
  requires_pull_secret?: boolean;
  requires_kubevirt?: boolean;
  versions?: string[];
}

type DeployHostOption = {
  id: string;
  ip_address: string;
  instance_id: string;
  provider_type: string;
  host_type?: string;
  used_vcpus: number;
  total_vcpus: number;
  used_ram_mb: number;
  total_ram_mb: number;
};

interface CreationRequirements {
  mode: string; yamlContent: string; selectedPattern: string | null; selectedTemplate: string | null;
  template?: TemplateSummary; requiresHost: boolean; hasHost: boolean;
  commonPassword: string; hasPullSecret: boolean; loadingVersions: boolean;
  installVia: string; bastionImageId: string; bastionIsoId: string; bmcIpError: string;
}

function isOcpCreationBlocked(options: CreationRequirements): boolean {
  const { template, commonPassword, hasPullSecret, loadingVersions, installVia, bastionImageId, bastionIsoId, bmcIpError } = options;
  if (template?.category !== "openshift") return false;
  if (!commonPassword || (template.requires_pull_secret !== false && !hasPullSecret)) return true;
  if (loadingVersions && !template.versions?.length) return true;
  return installVia === "bastion" && (!bastionImageId || !bastionIsoId || !!bmcIpError);
}

function isProjectCreationBlocked(options: CreationRequirements): boolean {
  if (options.mode === "yaml") return !options.yamlContent;
  if (options.mode === "pattern") return !options.selectedPattern;
  if (options.mode !== "template") return false;
  if (!options.selectedTemplate) return true;
  if (options.requiresHost && options.template?.requires_kubevirt && !options.hasHost) return true;
  return isOcpCreationBlocked(options);
}

function runningLabel(state: string, paused: boolean): string {
  if (state === "error") return "Paused";
  return paused ? "Stopped" : "Running";
}

function ocpBadgeStyle(status: string): React.CSSProperties {
  if (status === "ready") return { background: "rgba(74,222,128,0.15)", color: "#4ade80" };
  if (status === "error") return { background: "rgba(248,113,113,0.15)", color: "#f87171" };
  return { background: "rgba(251,191,36,0.15)", color: "#fbbf24" };
}

function isDefaultRhelImage(name: string): boolean {
  const match = /rhel\s+\d/i.exec(name);
  if (!match) return false;
  return name.slice(match.index + match[0].length).toLowerCase().includes("image");
}

function patternAvailability(count: number): string {
  if (count === 0) return "No patterns yet";
  return count + " pattern" + (count > 1 ? "s" : "") + " available";
}

function projectCreateLabel(mode: string): string {
  const labels: Record<string, string> = { yaml: "Import & Create", pattern: "Create from Pattern", template: "Create" };
  return labels[mode] || "Create Project";
}

function sortedProjects(data: unknown): Project[] {
  if (!Array.isArray(data)) return [];
  return [...data].sort((left: Project, right: Project) => left.name.localeCompare(right.name));
}

function projectsSettled(projects: Project[]): boolean {
  const settled = new Set(["draft", "active", "stopped", "error"]);
  return projects.every((project) => settled.has(project.state));
}

function canAcceptProject(host: { state: string; agent_status: string; host_type?: string; accepting_work?: boolean }): boolean {
  return host.state === "active" && host.agent_status === "connected" && host.host_type !== "pattern_buffer" && host.accepting_work !== false;
}

class ProjectRequestError extends Error {}

async function readProjectResponse(response: Response, fallback: string) {
  if (response.ok) return response.json();
  const error = await response.json().catch(() => ({}));
  throw new ProjectRequestError(formatApiDetail(error.detail, fallback));
}

async function postProjectJson(url: string, body: Record<string, unknown>, fallback: string) {
  const response = await fetch(url, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  return readProjectResponse(response, fallback);
}

function addProvidedValues(body: Record<string, unknown>, values: Record<string, unknown>) {
  for (const [key, value] of Object.entries(values)) {
    if (value) body[key] = value;
  }
}

async function parseProjectYaml(content: string): Promise<Record<string, unknown>> {
  let parsed: unknown;
  try {
    const yaml = await import("js-yaml");
    parsed = yaml.load(content);
  } catch {
    throw new ProjectRequestError("Invalid YAML syntax in template file");
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new ProjectRequestError("Template must be a YAML mapping");
  }
  return parsed as Record<string, unknown>;
}

export function NewProjectModal({ onClose, onCreated, userRole, availableHosts, setAlertMsg }: Readonly<{ onClose: () => void; onCreated: (id: string) => void; userRole: string; availableHosts: DeployHostOption[]; setAlertMsg: (msg: string | null) => void }>) {
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
  const versionedName = (tName: string, ver: string) => {
    if (!ver) return tName;
    if (/^OKD/i.test(tName)) return tName.replace(/^(OKD)/i, `$1 ${ver}`);
    return tName.replace(/^(OpenShift)/, `$1 ${ver}`);
  };
  const [bastionImageId, setBastionImageId] = useState("");
  const [bastionIsoId, setBastionIsoId] = useState("");
  const [bastionSshKeyId, setBastionSshKeyId] = useState("");
  const [commonPassword, setCommonPassword] = useState(generateImportPassword);
  const [bastionBmcIp, setBastionBmcIp] = useState("192.168.100.50");
  const [bmcIpError, setBmcIpError] = useState("");
  const [autoDeploy, setAutoDeploy] = useState(true);
  const [autoStart, setAutoStart] = useState(true);
  // Cluster name / base domain are now set on the canvas cluster boundary after
  // creation. We still send sensible defaults so the backend's legacy/quickstart
  // from-template path can seed the single cluster (matches its own defaults).
  const clusterName = "ocp";
  const baseDomain = "ocp.local";
  const [ocpVersion, setOcpVersion] = useState("");
  const [ocpVersions, setOcpVersions] = useState<{minor: string; latest: string}[]>([]);
  const [autoInstallOcp, setAutoInstallOcp] = useState(true);
  const [installVia, setInstallVia] = useState<"pod" | "bastion">("pod");
  const [blockOutbound, setBlockOutbound] = useState(true);
  const [deployHostId, setDeployHostId] = useState("");
  const [customVersion, setCustomVersion] = useState(false);
  const [customVersionText, setCustomVersionText] = useState("");
  const [loadingVersions, setLoadingVersions] = useState(true);
  const [hasPullSecret, setHasPullSecret] = useState(false);
  const [libraryImages, setLibraryImages] = useState<Array<{id: string; name: string; size_gb: number; format: string}>>([]);
  const [libraryIsos, setLibraryIsos] = useState<Array<{id: string; name: string; size_gb: number}>>([]);
  const [sshKeys, setSshKeys] = useState<Array<{id: string; name: string}>>([]);
  const [createError, setCreateError] = useState<string | null>(null);

  const requiresKubevirt =
    mode === "template" &&
    !!templates.find((t) => t.id === selectedTemplate)?.requires_kubevirt;
  const deployHosts = requiresKubevirt
    ? availableHosts.filter((h) => h.host_type === "kubevirt-cluster")
    : availableHosts;

  useEffect(() => {
    if (deployHostId && !deployHosts.some((h) => h.id === deployHostId)) {
      setDeployHostId("");
    }
  }, [deployHostId, deployHosts]);

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
          const ver = pickDefaultOcpVersion(data.map((v: { minor: string }) => v.minor));
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
          || images.find((i: any) => isDefaultRhelImage(i.name));
        if (defaultImg) {
          setBastionImageId(defaultImg.id);
          const defaultIso = isos.find((i: any) => i.tags?.ocp_default_iso);
          if (defaultIso) {
            setBastionIsoId(defaultIso.id);
          } else {
            const verMatch = /(?<!\d)(\d+\.\d+)/.exec(defaultImg.name);
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

  const deployCreatedProject = async (id: string) => {
    if (!autoDeploy) return;
    const params = new URLSearchParams();
    if (deployHostId) params.set("host_id", deployHostId);
    const query = params.size ? `?${params}` : "";
    const response = await fetch(`/api/v1/projects/${id}/deploy${query}`, { method: "POST" });
    if (!response.ok) await readProjectResponse(response, "Deploy failed — project was created as draft");
  };

  const getTemplateBody = () => {
    const body: Record<string, unknown> = {
      template_id: selectedTemplate, name,
      auto_install_ocp: autoInstallOcp, block_outbound: blockOutbound,
    };
    addProvidedValues(body, {
      bastion_image_id: bastionImageId, bastion_iso_id: bastionIsoId,
      bastion_ssh_key_id: bastionSshKeyId, common_password: commonPassword,
      bastion_bmc_ip: bastionBmcIp, cluster_name: clusterName,
      base_domain: baseDomain, ocp_version: ocpVersion,
    });
    if (templates.find((t) => t.id === selectedTemplate)?.category === "openshift") {
      body.install_via = installVia;
      body.external_access = true;
    }
    return body;
  };

  const createFromTemplate = async () => {
    const data = await postProjectJson("/api/v1/projects/from-template", getTemplateBody(), "Failed to create project");
    if (data?.warnings?.length) setAlertMsg("Created with warnings: " + data.warnings.join("; "));
    // Load the server-generated topology without a client-side rewrite.
    const { useCanvasStore } = await import("@/stores/canvasStore");
    await useCanvasStore.getState().loadProject(data.id);
    await deployCreatedProject(data.id);
    onCreated(data.id);
  };

  const createFromYaml = async () => {
    const body: Record<string, unknown> = { name, template_yaml: await parseProjectYaml(yamlContent) };
    addProvidedValues(body, {
      common_password: commonPassword, bastion_ssh_key_id: bastionSshKeyId,
      bastion_image_id: bastionImageId, bastion_iso_id: bastionIsoId,
    });
    const data = await postProjectJson("/api/v1/projects/from-template", body, "Template import failed");
    if (data?.warnings?.length) setAlertMsg("Imported with warnings: " + data.warnings.join("; "));
    await deployCreatedProject(data.id);
    onCreated(data.id);
  };

  const createBasicProject = async () => {
        const pattern = mode === "pattern" && selectedPattern;
        const url = pattern ? `/api/v1/patterns/${selectedPattern}/deploy` : "/api/v1/projects/";
        const body = pattern ? { name, auto_deploy: autoDeploy, auto_start: autoStart } : { name };
        const data = await postProjectJson(url, body, "Failed to create project");
        onCreated(data.id);
  };

  const handleCreate = async () => {
    if (!name.trim()) return;
    if (userRole === "admin" && mode === "template" && requiresKubevirt && deployHosts.length === 0) {
      setCreateError("This template requires a KubeVirt cluster host — none are currently available.");
      return;
    }
    setCreating(true);
    setCreateError(null);
    try {
      if (mode === "template" && selectedTemplate) await createFromTemplate();
      else if (mode === "yaml" && yamlContent) await createFromYaml();
      else {
        await createBasicProject();
      }
    } catch (error) {
      setCreateError(error instanceof ProjectRequestError ? error.message : "Request failed");
    } finally {
      setCreating(false);
    }
  };

  const selectedTemplateInfo = templates.find((template) => template.id === selectedTemplate);
  const createDisabled = creating || !name.trim() || isProjectCreationBlocked({
    mode, yamlContent, selectedPattern, selectedTemplate,
    template: selectedTemplateInfo, requiresHost: userRole === "admin",
    hasHost: deployHosts.length > 0, commonPassword, hasPullSecret,
    loadingVersions, installVia, bastionImageId, bastionIsoId, bmcIpError,
  });

  const optionStyle = (active: boolean) => ({
    flex: 1,
    padding: "16px",
    borderRadius: 8,
    border: `2px solid ${active ? "#4ade80" : "var(--pf-t--global--border--color--default)"}`,
    background: active ? "rgba(74,222,128,0.08)" : "transparent",
    cursor: "pointer" as const,
    textAlign: "center" as const,
  });

  const renderYamlFields = () => mode === "yaml" ? (

              <div>
                <LabeledField style={{ fontSize: 12, display: "block", marginBottom: 4 }} label={<>Template YAML</>}>
                <textarea
                  value={yamlContent}
                  onChange={(e) => { setYamlContent(e.target.value); setYamlFileName(""); }}
                  placeholder={TEMPLATE_YAML_EXAMPLE}
                  style={{
                    width: "100%", height: 200, fontFamily: "monospace", fontSize: 12,
                    padding: 12, borderRadius: 8, resize: "vertical",
                    background: "var(--pf-t--global--background--color--secondary--default)",
                    color: "var(--pf-t--global--text--color--regular)",
                    border: "1px solid var(--pf-t--global--border--color--default)",
                  }}
                /></LabeledField>
                {yamlFileName && (
                  <div style={{ fontSize: 11, color: "var(--pf-t--global--text--color--subtle)", marginTop: 4 }}>
                    Loaded from {yamlFileName}
                  </div>
                )}
                <div style={{ borderTop: "1px solid var(--pf-t--global--border--color--default)", paddingTop: 12, marginTop: 12 }}>
                  <div style={{ fontSize: 11, color: "var(--pf-t--global--text--color--subtle)", marginBottom: 8 }}>
                    Cloud-init credentials (applied when the template enables cloud-init and omits them)
                  </div>
                  <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                    <div>
                      <LabeledField style={{ fontSize: 12, display: "block", marginBottom: 4 }} label={<>SSH Key</>}>
                      <select style={inputStyle} value={bastionSshKeyId} onChange={(e) => setBastionSshKeyId(e.target.value)}>
                        <option value="">None</option>
                        {sshKeys.map((k) => (
                          <option key={k.id} value={k.id}>{k.name}</option>
                        ))}
                      </select></LabeledField>
                    </div>
                    <div>
                      <LabeledField style={{ fontSize: 12, display: "block", marginBottom: 4 }} label={<>
                        Password <span style={{ color: "var(--pf-t--global--text--color--subtle)" }}>(cloud-user console)</span>
                      </>} controlWrapper={<div style={{ display: "flex", gap: 4 }} />} afterControl={
                        <button
                          type="button"
                          style={{ ...inputStyle, width: "auto", flex: "0 0 auto", cursor: "pointer", padding: "4px 10px", fontSize: 12 }}
                          onClick={() => { navigator.clipboard.writeText(commonPassword); }}
                          title="Copy password"
                        >Copy</button>
                      }>
                        <input
                          style={{ ...inputStyle, width: "auto", flex: "1 1 0", minWidth: 0 }}
                          value={commonPassword}
                          onChange={(e) => setCommonPassword(e.target.value)}
                          placeholder="Used for console login"
                          onKeyDown={(e) => { if (e.key === "Enter") handleCreate(); }}
                        />
                      </LabeledField>
                      {!commonPassword.trim() && (
                        <div style={{ fontSize: 11, color: "#f59e0b", marginTop: 4 }}>
                          No password set — cloud images may have no console login until you set one on the VM.
                        </div>
                      )}
                    </div>
                  </div>
                </div>
                <div style={{ borderTop: "1px solid var(--pf-t--global--border--color--default)", paddingTop: 12, marginTop: 12 }}>
                  {userRole === "admin" && deployHosts.length > 0 && autoDeploy && (
                    <div style={{ marginBottom: 8 }}>
                      <LabeledField style={{ fontSize: 12, display: "block", marginBottom: 4 }} label={<>Host</>}>
                      <select style={inputStyle} value={deployHostId} onChange={(e) => setDeployHostId(e.target.value)}>
                        <option value="">Auto (best host)</option>
                        {deployHosts.map((h) => (
                          <option key={h.id} value={h.id}>
                            {h.id.slice(0, 8)} — {h.ip_address} ({h.provider_type}), {h.total_vcpus - h.used_vcpus} vCPUs / {Math.round((h.total_ram_mb - h.used_ram_mb) / 1024)}G free
                          </option>
                        ))}
                      </select></LabeledField>
                    </div>
                  )}
                  <label style={{ fontSize: 12, display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
                    <input type="checkbox" checked={autoDeploy} onChange={(e) => setAutoDeploy(e.target.checked)} />
                    {"Deploy immediately after import"}
                  </label>
                </div>
              </div>
  ) : null;

  const renderPatternFields = () => mode === "pattern" ? (

              <div style={{ position: "relative" }}>
                <LabeledField style={{ fontSize: 12, display: "block", marginBottom: 4 }} label={<>Pattern</>}>
                <input
                  style={{
                    ...inputStyle,
                    borderColor: selectedPattern ? "#4ade80" : undefined,
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
                /></LabeledField>
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
                        <button type="button" className="plain-button"
                          key={p.id}
                          onClick={() => {
                            setSelectedPattern(p.id);
                            setPatternSearch(p.name);
                            setPatternDropdownOpen(false);
                            if (!name) setName(p.name);
                          }}
                          style={{
                            padding: "8px 12px", cursor: "pointer", width: "100%", textAlign: "left",
                            background: selectedPattern === p.id ? "rgba(74,222,128,0.08)" : "transparent",
                          }}
                          onMouseEnter={(e) => { (e.target as HTMLElement).style.background = "rgba(255,255,255,0.05)"; }}
                          onMouseLeave={(e) => { (e.target as HTMLElement).style.background = selectedPattern === p.id ? "rgba(74,222,128,0.08)" : "transparent"; }}
                        >
                          <div style={{ fontWeight: 500, fontSize: 13 }}>{p.name}</div>
                          {p.description && <div style={{ fontSize: 11, opacity: 0.6 }}>{p.description}</div>}
                        </button>
                      ))}
                    {patterns.filter((p) => !patternSearch || p.name.toLowerCase().includes(patternSearch.toLowerCase())).length === 0 && (
                      <div style={{ padding: "8px 12px", fontSize: 13, opacity: 0.5 }}>No patterns found</div>
                    )}
                  </div>
                )}
              </div>
  ) : null;

  return (
    <ModalBackdrop label="Project settings" style={{
      position: "fixed", inset: 0, zIndex: 10000,
      display: "flex", alignItems: "center", justifyContent: "center",
      background: "rgba(0,0,0,0.6)",
    }} onClose={() => { onClose(); }}>
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
              <button type="button" className="plain-button" style={optionStyle(false)} onClick={() => setMode("blank")}>
                <div style={{ fontSize: 28, marginBottom: 4 }}>📄</div>
                <div style={{ fontWeight: 600, fontSize: 14 }}>Blank Project</div>
                <div style={{ fontSize: 12, opacity: 0.6, marginTop: 4 }}>Start from scratch</div>
              </button>
              <button type="button" className="plain-button" style={optionStyle(false)} onClick={() => setMode("yaml")}>
                <div style={{ fontSize: 28, marginBottom: 4 }}>📋</div>
                <div style={{ fontWeight: 600, fontSize: 14 }}>From Template</div>
                <div style={{ fontSize: 12, opacity: 0.6, marginTop: 4 }}>Import YAML</div>
              </button>
            </div>
            <div style={{ display: "flex", gap: 12 }}>
              <button type="button" className="plain-button" disabled={patterns.length === 0}
                style={{ ...optionStyle(false), opacity: patterns.length === 0 ? 0.4 : 1, pointerEvents: patterns.length === 0 ? "none" : "auto" }}
                onClick={() => setMode("pattern")}
              >
                <div style={{ fontSize: 28, marginBottom: 4 }}>🧩</div>
                <div style={{ fontWeight: 600, fontSize: 14 }}>From Pattern</div>
                <div style={{ fontSize: 12, opacity: 0.6, marginTop: 4 }}>
                  {patternAvailability(patterns.length)}
                </div>
              </button>
              {templates.length > 0 && (
                <button type="button" className="plain-button"
                  style={{ ...optionStyle(false), flex: 1 }}
                  onClick={() => setMode("template-picker" as any)}
                >
                  <div style={{ fontSize: 28, marginBottom: 4 }}>🚀</div>
                  <div style={{ fontWeight: 600, fontSize: 14 }}>Quick Starts</div>
                  <div style={{ fontSize: 12, opacity: 0.6, marginTop: 4 }}>Guided deploy</div>
                </button>
              )}
            </div>
            <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 4 }}>
              <button type="button" onClick={onClose} style={{ ...inputStyle, width: "auto", cursor: "pointer", padding: "6px 16px" }}>
                Cancel
              </button>
            </div>
          </div>
        ) : null}
        {mode === "template-picker" ? (
          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
              <button type="button" onClick={() => setMode("choose")} style={{ background: "none", border: "none", color: "var(--pf-t--global--text--color--regular)", cursor: "pointer", fontSize: 16, padding: 0 }}>
                ←
              </button>
              <span style={{ fontWeight: 600, fontSize: 14 }}>Choose a Template</span>
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(180px, 1fr))", gap: 12 }}>
              {templates.map((t) => (
                <button type="button"
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
                    const tmplVers = t.versions?.length
                      ? t.versions
                      : ocpVersions.map((v) => v.minor);
                    const ver = tmplVers.length
                      ? pickDefaultOcpVersion(tmplVers)
                      : ocpVersion;
                    if (t.versions?.length) {
                      setOcpVersion(ver);
                    }
                    setName(versionedName(t.name, ver));
                    setNameAutoSet(true);
                    setMode("template");
                    if (t.bastion_image_name) {
                      const match = libraryImages.find((i) => i.name === t.bastion_image_name);
                      if (match) {
                        setBastionImageId(match.id);
                        const verMatch = /(?<!\d)(\d+\.\d+)/.exec(match.name);
                        if (verMatch) {
                          const iso = libraryIsos.find((i) => i.name.includes(verMatch[1]) && /dvd|binary/i.test(i.name));
                          if (iso) setBastionIsoId(iso.id);
                        }
                      }
                    }
                  }} className="plain-button"
                >
                  <div style={{ marginBottom: 8, display: "flex", justifyContent: "center" }}>
                    {t.distribution === "okd-scos" ? (
                      // Official OKD "O" mark (okd.io docs/img/okd_logo.svg) — ring, not the OpenShift swirl
                      <svg width="32" height="32" viewBox="47 35 79 80" xmlns="http://www.w3.org/2000/svg" aria-label="OKD">
                        <path fill="#F22B29" d="M86.4,35.3c10.8,0,20.1,3.8,27.7,11.3c7.7,7.5,11.5,16.7,11.5,27.5c0,10.8-3.9,20.2-11.6,28.1 c-7.7,7.9-17,11.8-27.7,11.8c-10.5,0-19.6-3.9-27.3-11.8c-7.7-7.8-11.5-17.1-11.5-27.8c0-10.8,3.8-20,11.3-27.7 C66.4,39.1,75.6,35.3,86.4,35.3z M61.4,74.7c0,7,2.5,13.1,7.5,18.3c5,5.2,10.9,7.9,17.6,7.9c6.9,0,12.8-2.6,17.8-7.7 c5-5.1,7.5-11.4,7.5-18.6c0-7.2-2.5-13.4-7.5-18.7c-5-5.3-10.9-8-17.8-8c-6.5,0-12.4,2.6-17.5,8C64,61.2,61.4,67.4,61.4,74.7z"/>
                      </svg>
                    ) : null}
                    {t.distribution !== "okd-scos" && t.category === "openshift" && (
                      <svg width="32" height="32" viewBox="0 0 24 24" fill="#EE0000" xmlns="http://www.w3.org/2000/svg" aria-label="OpenShift">
                        <path d="M21.665,11.812c-0.11-1.377-0.476-2.724-1.08-3.966L24,6.599c-0.268-0.556-0.585-1.092-0.943-1.595 l-1.601,0.583c-3.534-4.95-10.412-6.098-15.363-2.565c-3.144,2.244-4.883,5.972-4.582,9.823l1.604-0.584 c0.051,0.615,0.153,1.224,0.305,1.822L0,15.335c0.338,1.339,0.922,2.604,1.721,3.731l1.812-0.659 c3.526,4.95,10.398,6.106,15.349,2.58c1.555-1.107,2.796-2.6,3.599-4.332c0.802-1.715,1.144-3.61,0.991-5.497L21.665,11.812z M16.925,9.177c0.687,1.227,0.998,2.629,0.895,4.032l1.809-0.657c-0.063,0.856-0.282,1.694-0.646,2.471 c-1.67,3.584-5.928,5.138-9.514,3.472c-0.782-0.365-1.491-0.87-2.092-1.49l-1.813,0.66c-0.979-1.01-1.64-2.285-1.903-3.667 l3.426-1.242c-0.121-0.624-0.159-1.262-0.111-1.896H6.97l-1.604,0.583c0.294-3.932,3.72-6.881,7.652-6.587 c0.868,0.065,1.716,0.288,2.504,0.658V5.508c0.778,0.364,1.483,0.867,2.082,1.483l1.599-0.582c0.002,0.002,0.004,0.003,0.006,0.005 c0.441,0.454,0.82,0.965,1.128,1.518L16.925,9.177z"/>
                      </svg>
                    )}
                    {t.distribution !== "okd-scos" && t.category !== "openshift" && (
                      <img src="/images/troshka-logo-32.png" alt="" width={32} height={32} />
                    )}
                  </div>
                  <div style={{ fontWeight: 600, fontSize: 13, textAlign: "center" }}>{t.name}</div>
                  <div style={{ fontSize: 11, opacity: 0.6, marginTop: 4, textAlign: "center", lineHeight: 1.3 }}>{t.description}</div>
                  {t.deploy_time && <div style={{ fontSize: 11, opacity: 0.5, marginTop: 6 }}>⏱ {t.deploy_time}</div>}
                </button>
              ))}
            </div>
            <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 4 }}>
              <button type="button" onClick={() => setMode("choose")} style={{ ...inputStyle, width: "auto", cursor: "pointer", padding: "6px 16px" }}>
                Back
              </button>
            </div>
          </div>
        ) : null}
        {mode !== "choose" && mode !== "template-picker" && (
          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            <div>
              <LabeledField style={{ fontSize: 12, display: "block", marginBottom: 4 }} label={<>Project Name</>}>
              <input
                style={inputStyle}
                value={name}
                onChange={(e) => { setName(e.target.value); setNameAutoSet(false); }}
                placeholder="My Project"
                autoFocus={mode === "blank"}
                onKeyDown={(e) => { if (e.key === "Enter") handleCreate(); }}
              /></LabeledField>
            </div>
            {renderYamlFields()}
            {mode === "template" && selectedTemplate && (() => {
              const _selTmpl = templates.find((t) => t.id === selectedTemplate);
              const _isOcp = _selTmpl?.category === "openshift";
              const _needsPullSecret = _isOcp && _selTmpl?.requires_pull_secret !== false;
              const _isOkd = _selTmpl?.distribution === "okd-scos";
              const _versionChoices = _selTmpl?.versions?.length
                ? _selTmpl.versions.map((v) => ({ minor: v, latest: v }))
                : ocpVersions;
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
                  <div style={{ fontSize: 11, color: "var(--pf-t--global--text--color--subtle)", marginBottom: 8 }}>{_isOkd ? "OKD Version" : "OpenShift Version"}</div>
                  <div>
                    <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>{_isOkd ? "OKD Version" : "OCP Version"}</label>
                    {loadingVersions && !_selTmpl?.versions?.length ? (
                      <div style={{ ...inputStyle, display: "flex", alignItems: "center", justifyContent: "center" }}>
                        <span className="project-btn-spinner" style={{ width: 14, height: 14, aspectRatio: "1 / 1" }} />
                      </div>
                    ) : null}
                    {(!loadingVersions || !!_selTmpl?.versions?.length) && customVersion && (
                      <input style={inputStyle} autoFocus value={customVersionText} placeholder="e.g. 4.18" onChange={(e) => {
                        const v = e.target.value.replaceAll(/[^\d.]/g, ""); setCustomVersionText(v);
                        if (/^\d+\.\d+$/.test(v)) { setOcpVersion(v); if (nameAutoSet && selectedTemplate) { const t = templates.find((t) => t.id === selectedTemplate); if (t) setName(versionedName(t.name, v)); } }
                      }} onBlur={() => { if (!/^\d+\.\d+$/.test(customVersionText)) { setCustomVersion(false); setOcpVersion(pickDefaultOcpVersion(_versionChoices.map((v) => v.minor)) || "4.20"); } }} />
                    )}
                    {(!loadingVersions || !!_selTmpl?.versions?.length) && !customVersion && (
                      <select style={inputStyle} value={ocpVersion} onChange={(e) => {
                        if (e.target.value === "__other__") { setCustomVersion(true); setCustomVersionText(""); }
                        else {
                          setOcpVersion(e.target.value);
                          if (nameAutoSet && selectedTemplate) { const t = templates.find((t) => t.id === selectedTemplate); if (t) setName(versionedName(t.name, e.target.value)); }
                        }
                      }}>
                        {_versionChoices.map((v) => (
                          <option key={v.minor} value={v.minor}>{formatTemplateVersionLabel(v.minor, !!_isOkd, v.latest)}</option>
                        ))}
                        <option value="__other__">Other...</option>
                      </select>
                    )}
                  </div>
                  <div style={{ fontSize: 10, color: "var(--pf-t--global--text--color--subtle)", marginTop: 6 }}>
                    Set the cluster name, base domain, and VIPs on the canvas after creation.
                  </div>
                </div>}
                {_isOcp && <div style={{ borderTop: "1px solid var(--pf-t--global--border--color--default)", paddingTop: 12, marginTop: 4 }}>
                  <LabeledField style={{ fontSize: 12, display: "block", marginBottom: 4 }} label={<>Install method</>}>
                  <select aria-label="Install method" style={inputStyle} value={installVia} onChange={(e) => setInstallVia(e.target.value as "pod" | "bastion")}>
                    <option value="pod">Pod (default)</option>
                    <option value="bastion">Bastion</option>
                  </select></LabeledField>
                  <div style={{ fontSize: 10, color: "var(--pf-t--global--text--color--subtle)", marginTop: 6 }}>
                    {installVia === "bastion" ? "Installs from a dedicated bastion VM." : "Installs from an ephemeral ops pod (no bastion VM)."}
                  </div>
                </div>}
                <div style={{ borderTop: "1px solid var(--pf-t--global--border--color--default)", paddingTop: 12, marginTop: 4 }}>
                  <div style={{ fontSize: 11, color: "var(--pf-t--global--text--color--subtle)", marginBottom: 8 }}>{_isOcp && installVia === "bastion" ? "Bastion Configuration" : "Configuration"}</div>
                  <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                    {_isOcp && installVia === "bastion" && <div>
                      <label htmlFor="bastion-kvm-image" style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Red Hat Enterprise Linux KVM Guest Image <span style={{ color: "#f87171" }}>*</span></label>
                      {libraryImages.length > 0 ? (
                        <select id="bastion-kvm-image" style={inputStyle} value={bastionImageId} onChange={(e) => {
                          setBastionImageId(e.target.value);
                          const img = libraryImages.find((i) => i.id === e.target.value);
                          if (img) {
                            const ver = /(?<!\d)(\d+\.\d+)/.exec(img.name);
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
                    </div>}
                    {_isOcp && installVia === "bastion" && <div>
                      <LabeledField style={{ fontSize: 12, display: "block", marginBottom: 4 }} label={<>Red Hat Enterprise Linux Binary DVD ISO (For local package repos) <span style={{ color: "#f87171" }}>*</span></>}>
                      <select style={inputStyle} value={bastionIsoId} onChange={(e) => setBastionIsoId(e.target.value)}>
                        <option value="">Select an ISO...</option>
                        {libraryIsos.map((img) => (
                          <option key={img.id} value={img.id}>{img.name}</option>
                        ))}
                      </select></LabeledField>
                    </div>}
                    {_needsPullSecret && <div style={{ fontSize: 12, marginBottom: 8, display: "flex", alignItems: "center", gap: 6 }}>
                      <span>Pull Secret <span style={{ color: "#f87171" }}>*</span>:</span>
                      {hasPullSecret ? (
                        <span style={{ color: "#4ade80" }}>configured ✓</span>
                      ) : (
                        <span style={{ color: "#f87171" }}>not set — <a href="/settings" style={{ color: "#3b82f6" }}>configure in Settings</a></span>
                      )}
                    </div>}
                    {_isOkd && !_needsPullSecret && <div style={{ fontSize: 12, marginBottom: 8, opacity: 0.7 }}>
                      No Red Hat pull secret required (OKD/SCOS).
                    </div>}
                    <div>
                      <LabeledField style={{ fontSize: 12, display: "block", marginBottom: 4 }} label={<>SSH Key</>}>
                      <select style={inputStyle} value={bastionSshKeyId} onChange={(e) => setBastionSshKeyId(e.target.value)}>
                        <option value="">None</option>
                        {sshKeys.map((k) => (
                          <option key={k.id} value={k.id}>{k.name}</option>
                        ))}
                      </select></LabeledField>
                    </div>
                    <div>
                      <LabeledField style={{ fontSize: 12, display: "block", marginBottom: 4 }} label={<>Password <span style={{ color: "#f87171" }}>*</span> <span style={{ color: "var(--pf-t--global--text--color--subtle)" }}>(cloud-user + BMC)</span></>} controlWrapper={<div style={{ display: "flex", gap: 4 }} />} afterControl={
                        <button
                          type="button"
                          style={{ ...inputStyle, width: "auto", flex: "0 0 auto", cursor: "pointer", padding: "4px 10px", fontSize: 12 }}
                          onClick={() => { navigator.clipboard.writeText(commonPassword); }}
                          title="Copy password"
                        >Copy</button>
                      }>
                        <input style={{ ...inputStyle, width: "auto", flex: "1 1 0", minWidth: 0 }} value={commonPassword} onChange={(e) => setCommonPassword(e.target.value)} placeholder="Used for console access and BMC auth" onKeyDown={(e) => { if (e.key === "Enter") handleCreate(); }} />
                      </LabeledField>
                    </div>
                    {_isOcp && installVia === "bastion" && <div>
                      <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 4 }}>
                        <label htmlFor="bastion-bmc-ip" style={{ fontSize: 12 }}>Bastion BMC IP</label>
                        <div style={{ position: "relative", display: "inline-block" }}>
                          <button type="button" className="plain-button" aria-label="BMC network information"
                            style={{ cursor: "help", fontSize: 12, width: 16, height: 16, borderRadius: "50%", background: "rgba(59,130,246,0.2)", color: "#3b82f6", display: "inline-flex", alignItems: "center", justifyContent: "center", fontWeight: 600 }}
                            title="BMC Network Info"
                            onClick={(e) => { e.stopPropagation(); const el = e.currentTarget.nextElementSibling as HTMLElement; el.style.display = el.style.display === "none" ? "block" : "none"; }}
                          >i</button>
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
                        id="bastion-bmc-ip" value={bastionBmcIp}
                        onChange={(e) => {
                          setBastionBmcIp(e.target.value);
                          const ip = e.target.value.trim();
                          if (!ip) { setBmcIpError("Required"); return; }
                          const match = /^(\d+\.\d+\.\d+)\.(\d+)$/.exec(ip);
                          if (!match) { setBmcIpError("Invalid IP"); return; }
                          if (match[1] !== "192.168.100") { setBmcIpError("Must be in 192.168.100.0/24"); return; }
                          const octet = Number.parseInt(match[2]);
                          if (octet < 2 || octet > 254 || octet <= 13) { setBmcIpError("Must be > .13 (reserved for BMC endpoints)"); return; }
                          setBmcIpError("");
                        }}
                        placeholder="192.168.100.50"
                      />
                      {bmcIpError && <div style={{ fontSize: 11, color: "#f87171", marginTop: 2 }}>{bmcIpError}</div>}
                    </div>}
                  </div>
                  <div style={{ borderTop: "1px solid var(--pf-t--global--border--color--default)", paddingTop: 8, marginTop: 4 }}>
                    {userRole === "admin" && deployHosts.length > 0 && (
                      <div style={{ marginBottom: 6 }}>
                        <LabeledField style={{ fontSize: 12, display: "block", marginBottom: 2 }} label={<>
                          Host{requiresKubevirt ? " (KubeVirt only)" : ""}
                        </>}>
                        <select style={{
                          padding: "4px 8px", borderRadius: 6, fontSize: 12, width: "100%",
                          border: "1px solid var(--pf-t--global--border--color--default)",
                          background: "var(--pf-t--global--background--color--primary--default)",
                          color: "var(--pf-t--global--text--color--regular)",
                        }} value={deployHostId} onChange={(e) => setDeployHostId(e.target.value)}>
                          <option value="">Auto (best host)</option>
                          {deployHosts.map((h) => (
                            <option key={h.id} value={h.id}>
                              {h.id.slice(0, 8)} — {h.ip_address} ({h.provider_type}), {h.total_vcpus - h.used_vcpus} vCPUs / {Math.round((h.total_ram_mb - h.used_ram_mb) / 1024)}G free
                            </option>
                          ))}
                        </select></LabeledField>
                      </div>
                    )}
                    {userRole === "admin" && requiresKubevirt && deployHosts.length === 0 && (
                      <div style={{ fontSize: 11, color: "#f59e0b", marginBottom: 6 }}>
                        This template requires a KubeVirt cluster host — none are currently available.
                      </div>
                    )}
                    <label style={{ fontSize: 12, display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
                      <input type="checkbox" checked={autoDeploy} onChange={(e) => setAutoDeploy(e.target.checked)} />
                      {"Deploy immediately after creation"}
                    </label>
                    {_isOcp && <label style={{ fontSize: 12, display: "flex", alignItems: "center", gap: 8, cursor: "pointer", marginTop: 4 }}>
                      <input type="checkbox" checked={blockOutbound} onChange={(e) => setBlockOutbound(e.target.checked)} />
                      {"Restrict outbound ports to install OCP only"}
                    </label>}
                    {_isOcp && <div style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 4 }}>
                      <label style={{ fontSize: 12, display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
                        <input type="checkbox" checked={autoInstallOcp} onChange={(e) => setAutoInstallOcp(e.target.checked)} />
                        {"Install OpenShift on deploy"}
                      </label>
                      <div style={{ position: "relative", display: "inline-block" }}>
                        <button type="button" className="plain-button" aria-label="OCP installer information"
                          style={{ cursor: "help", fontSize: 12, width: 16, height: 16, borderRadius: "50%", background: "rgba(59,130,246,0.2)", color: "#3b82f6", display: "inline-flex", alignItems: "center", justifyContent: "center", fontWeight: 600 }}
                          onClick={(e) => { e.stopPropagation(); const el = e.currentTarget.nextElementSibling as HTMLElement; el.style.display = el.style.display === "none" ? "block" : "none"; }}
                        >i</button>
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
            {renderPatternFields()}
            {mode === "pattern" && selectedPattern && (
              <div style={{ display: "flex", flexDirection: "column", gap: 4, marginTop: 8 }}>
                <label style={{ fontSize: 12, display: "flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
                  <input type="checkbox" checked={autoDeploy} onChange={(e) => { setAutoDeploy(e.target.checked); if (!e.target.checked) setAutoStart(false); }} />
                  {"Deploy immediately"}
                </label>
                {autoDeploy && (
                  <>
                    <label style={{ fontSize: 12, display: "flex", alignItems: "center", gap: 6, cursor: "pointer", marginLeft: 20 }}>
                      <input type="checkbox" checked={autoStart} onChange={(e) => setAutoStart(e.target.checked)} />
                      {"Start VMs after deploy"}
                    </label>
                    {userRole === "admin" && deployHosts.length > 0 && (
                      <div style={{ marginTop: 4 }}>
                        <LabeledField style={{ fontSize: 12, display: "block", marginBottom: 2 }} label={<>Host</>}>
                        <select style={{
                          padding: "4px 8px", borderRadius: 6, fontSize: 12, width: "100%",
                          border: "1px solid var(--pf-t--global--border--color--default)",
                          background: "var(--pf-t--global--background--color--primary--default)",
                          color: "var(--pf-t--global--text--color--regular)",
                        }} value={deployHostId} onChange={(e) => setDeployHostId(e.target.value)}>
                          <option value="">Auto (best host)</option>
                          {deployHosts.map((h) => (
                            <option key={h.id} value={h.id}>
                              {h.id.slice(0, 8)} — {h.ip_address} ({h.provider_type}), {h.total_vcpus - h.used_vcpus} vCPUs / {Math.round((h.total_ram_mb - h.used_ram_mb) / 1024)}G free
                            </option>
                          ))}
                        </select></LabeledField>
                      </div>
                    )}
                  </>
                )}
              </div>
            )}
            {createError && (
              <div style={{ fontSize: 12, color: "#f87171", marginTop: 8, lineHeight: 1.4, whiteSpace: "pre-wrap" }}>
                {createError}
              </div>
            )}
            <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginTop: 4 }}>
              <button type="button" onClick={() => { setMode("choose"); setSelectedPattern(null); setSelectedTemplate(null); setYamlContent(""); setYamlFileName(""); setCreateError(null); }} style={{ ...inputStyle, width: "auto", cursor: "pointer", padding: "6px 16px" }}>
                Back
              </button>
              {mode === "yaml" && (
                <label style={{ ...inputStyle, width: "auto", cursor: "pointer", padding: "6px 16px", display: "inline-block", textAlign: "center" }}>
                  {"Upload File"}
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
              <button type="button"
                onClick={handleCreate}
                disabled={createDisabled}
                style={{
                  ...inputStyle, width: "auto", padding: "6px 16px",
                  cursor: creating ? "wait" : "pointer",
                  background: "rgba(74,222,128,0.15)", borderColor: "#4ade80", color: "#4ade80",
                  opacity: createDisabled ? 0.4 : 1,
                }}
              >
                {creating ? "Creating..." : projectCreateLabel(mode)}
              </button>
            </div>
          </div>
        )}
      </div>
    </ModalBackdrop>
  );
}

function NoProjectsIcon() {
  return <EmptyStateIcon icon={CubesIcon} />;
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
  const [userId, setUserId] = useState("");
  const [ownershipFilters, setOwnershipFilters] = useState(DEFAULT_OWNERSHIP_FILTERS);
  const [pools, setPools] = useState<{id: string; name: string; mode: string; status: string}[]>([]);
  const [deployPoolId, setDeployPoolId] = useState("");
  const [availableHosts, setAvailableHosts] = useState<{id: string; ip_address: string; instance_id: string; provider_type: string; host_type?: string; used_vcpus: number; total_vcpus: number; used_ram_mb: number; total_ram_mb: number}[]>([]);
  const [deployHostId, setDeployHostId] = useState("");
  const [alertMsg, setAlertMsg] = useState<string | null>(null);
  const [republishTarget, setRepublishTarget] = useState<Project | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Project | null>(null);
  const [bulkDeletePending, setBulkDeletePending] = useState(false);
  const [spendById, setSpendById] = useState<Record<string, LiveSpend>>({});
  const [spendNowMs, setSpendNowMs] = useState(() => Date.now());
  const [spendFetchedAt, setSpendFetchedAt] = useState(() => Date.now());
  const meterProjectKey = projects
    .filter((p) => p.state !== "draft" && p.state !== "deleting")
    .map((p) => p.id)
    .sort((left, right) => left.localeCompare(right))
    .join("|");

  const pollUntilSettled = () => {
    const poll = setInterval(() => {
      fetch(`${API_BASE}/api/v1/projects/`).then((r) => r.ok ? r.json() : []).then((data) => {
        const list = sortedProjects(data);
        setProjects(list);
        if (projectsSettled(list)) clearInterval(poll);
      }).catch(() => {});
    }, 2000);
  };

  const removeProject = (id: string) => {
    setProjects((projects) => projects.filter((project) => project.id !== id));
  };

  const updateProjectState = (id: string, state: string) => {
    setProjects((projects) => projects.map((project) => project.id === id ? { ...project, state } : project));
  };

  const fetchProjects = () => {
    fetch(`${API_BASE}/api/v1/projects/`)
      .then((r) => {
        if (!r.ok) throw new Error("Failed to fetch projects");
        return r.json();
      })
      .then((data) => {
        const sorted = Array.isArray(data) ? data.toSorted((a: Project, b: Project) => a.name.localeCompare(b.name)) : [];
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
    fetch("/api/v1/auth/me").then(r => r.ok ? r.json() : {}).then((d: { id?: string; role?: string; email?: string }) => {
      setUserRole(d.role || "");
      setUserId(d.id || "");
      if (d.role === "admin") {
        fetch("/api/v1/hosts/").then(r => r.ok ? r.json() : []).then(hosts => {
          setAvailableHosts(hosts.filter(canAcceptProject));
        });
      }
    });
    fetch("/api/v1/storage-pools/").then(r => r.ok ? r.json() : []).then(d => setPools(d.filter((p: any) => p.status === "available")));
    const interval = setInterval(fetchProjects, 10000);
    return () => clearInterval(interval);
  }, []);

  useEffect(() => {
    if (!meterProjectKey) {
      setSpendById({});
      return;
    }
    const ids = meterProjectKey.split("|");
    let cancelled = false;
    const load = () => {
      Promise.all(
        ids.map(async (id) => {
          try {
            const r = await fetch(`/api/v1/projects/${id}/metering`);
            if (!r.ok) return null;
            return [id, (await r.json()) as LiveSpend] as const;
          } catch {
            return null;
          }
        })
      ).then((pairs) => {
        if (cancelled) return;
        const next: Record<string, LiveSpend> = {};
        for (const pair of pairs) {
          if (pair) next[pair[0]] = pair[1];
        }
        setSpendById(next);
        setSpendFetchedAt(Date.now());
      });
    };
    load();
    const interval = setInterval(load, 30000);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [meterProjectKey]);

  useEffect(() => {
    const anyLive = Object.values(spendById).some(
      (s) => s.running_since && !s.running_until
    );
    if (!anyLive) return;
    const id = setInterval(() => setSpendNowMs(Date.now()), 1000);
    return () => clearInterval(id);
  }, [spendById]);

          const q = search.toLowerCase();
          const isAdmin = userRole === "admin";
          const filteredProjects = projects.filter((p) => {
            if (userId) {
              const cat: OwnershipCategory = p.owner_id === userId ? "mine" : "others";
              if (!matchesOwnershipFilter(cat, ownershipFilters, isAdmin)) return false;
            }
            if (!q) return true;
            return p.name.toLowerCase().includes(q) || (p.description || "").toLowerCase().includes(q);
          });

  if (loading) {
    return <PageSection><Title headingLevel="h1">Loading...</Title></PageSection>;
  }

  if (projects.length === 0) {
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
            {(userRole === "admin" || userId) && (
              <ToolbarItem>
                <OwnershipFilter
                  value={ownershipFilters}
                  onChange={setOwnershipFilters}
                  isAdmin={userRole === "admin"}
                  showShared={false}
                />
              </ToolbarItem>
            )}
            <ToolbarItem align={{ default: "alignEnd" }}>
              <Button variant="primary" icon={<PlusCircleIcon />} onClick={() => setShowNewModal(true)}>
                New Project
              </Button>
            </ToolbarItem>
          </ToolbarContent>
        </Toolbar>
      </PageSection>
      <PageSection>

        {filteredProjects.length > 0 && (() => {
          const selected = filteredProjects.filter((p) => selectedProjects.has(p.id));
          const allSelected = selected.length === filteredProjects.length;
          const someSelected = selected.length > 0;
          const allActive = someSelected && selected.every((p) => p.state === "active");
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
                    <Button variant="secondary" size="sm" onClick={async () => {
                      if (!(await appConfirm({ message: `Stop ${selected.length} project(s)?`, confirmLabel: "Stop" }))) return;
                      for (const p of selected) { fetch(`${API_BASE}/api/v1/projects/${p.id}/stop`, { method: "POST" }); }
                      setSelectedProjects(new Set());
                      fetchProjects();
                      pollUntilSettled();
                    }}>Stop ({selected.length})</Button>
                  )}
                  {allStoppedOrError && (
                    <Button variant="secondary" size="sm" onClick={async () => {
                      if (!(await appConfirm({ message: `Start ${selected.length} project(s)?`, confirmLabel: "Start" }))) return;
                      for (const p of selected) { fetch(`${API_BASE}/api/v1/projects/${p.id}/start`, { method: "POST" }); }
                      setSelectedProjects(new Set());
                      fetchProjects();
                      pollUntilSettled();
                    }}>Start ({selected.length})</Button>
                  )}
                  {allDraft && (
                    <Button variant="secondary" size="sm" onClick={async () => {
                      if (!(await appConfirm({ title: "Deploy", message: `Deploy ${selected.length} project(s)?`, confirmLabel: "Deploy" }))) return;
                      for (const p of selected) { fetch(`${API_BASE}/api/v1/projects/${p.id}/deploy`, { method: "POST" }); }
                      setSelectedProjects(new Set());
                      fetchProjects();
                      pollUntilSettled();
                    }}>Deploy ({selected.length})</Button>
                  )}
                  {allDeployed && (
                    <Button variant="secondary" size="sm" onClick={async () => {
                      const ocpSelected = selected.filter((p) => p.ocp_status && p.ocp_status !== "none");
                      const msg = ocpSelected.length
                        ? `Republish ${selected.length} project(s)? ${ocpSelected.length} have OpenShift clusters — they will be rebuilt from scratch (disks wiped). Re-cert is only available from the project page when all clusters are ready.`
                        : `Republish ${selected.length} project(s)? All VMs will be destroyed and recreated.`;
                      if (!(await appConfirm({
                        title: "Republish",
                        message: msg,
                        confirmLabel: "Rebuild",
                        variant: "danger",
                      }))) return;
                      for (const p of selected) {
                        const hasOcp = !!(p.ocp_status && p.ocp_status !== "none");
                        fetch(`${API_BASE}/api/v1/projects/${p.id}/redeploy`, {
                          method: "POST",
                          headers: hasOcp ? { "Content-Type": "application/json" } : undefined,
                          body: hasOcp ? JSON.stringify({ ocp_mode: "rebuild" }) : undefined,
                        });
                      }
                      setSelectedProjects(new Set());
                      fetchProjects();
                      pollUntilSettled();
                    }}>Republish ({selected.length})</Button>
                  )}
                  <Button variant="danger" size="sm" onClick={() => setBulkDeletePending(true)}>Delete ({selected.length})</Button>
                </div>
              )}
            </div>
          );
        })()}
        <div>
          {filteredProjects.length === 0 && (
            <p style={{ opacity: 0.6 }}>
              {search
                ? `No projects match "${search}"`
                : "No projects match the selected ownership filters."}
            </p>
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
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 8, flexWrap: "wrap" }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 200, flex: 1 }}>
                      <strong style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{p.name}</strong>
                      {p.guid && (
                        <span style={{ fontSize: 10, color: "var(--pf-t--global--text--color--subtle)", fontFamily: "monospace", flexShrink: 0 }}>
                          {p.guid}
                        </span>
                      )}
                      <span
                        title={p.owner_email ? `Owned by ${p.owner_email}` : "System"}
                        style={{
                          fontSize: 11,
                          padding: "1px 6px",
                          borderRadius: 4,
                          background: "rgba(96,165,250,0.18)",
                          color: "#60a5fa",
                          display: "inline-flex",
                          alignItems: "center",
                          gap: 4,
                          fontWeight: 600,
                          flexShrink: 0,
                        }}
                      >
                        <UserIcon style={{ width: 11, height: 11 }} />
                        {p.owner_email ? p.owner_email.split("@")[0] : "system"}
                      </span>
                    </div>
                    <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap", justifyContent: "flex-end", maxWidth: "100%", minWidth: 0 }}>
                    <span style={{
                      fontSize: 11, padding: "1px 6px", borderRadius: 4,
                      background: `${stateColors[p.state] || "#94a3b8"}22`,
                      color: stateColors[p.state] || "#94a3b8",
                      textTransform: "uppercase",
                      fontWeight: 500,
                    }}>
                      {p.state === "stopped" && p.auto_stopped ? "stopped (auto)" : p.state}
                    </span>
                    {(() => {
                      const spend = spendById[p.id];
                      if (spend?.running_since == null) return null;
                      const paused =
                        p.state === "stopped" ||
                        p.state === "stopping" ||
                        p.state === "error" ||
                        Boolean(spend.running_until);
                      const elapsed =
                        (spend.running_seconds || 0) +
                        (paused
                          ? 0
                          : Math.max(0, (spendNowMs - spendFetchedAt) / 1000));
                      const label =
                        runningLabel(p.state, paused);
                      return (
                        <span
                          title={`$${String(Number(spend.total_usd.toFixed(8)))}`}
                          style={{
                            fontSize: 11,
                            padding: "1px 6px",
                            borderRadius: 4,
                            background: paused
                              ? "rgba(148,163,184,0.12)"
                              : "rgba(34,197,94,0.12)",
                            color: paused ? "#94a3b8" : "#86efac",
                            whiteSpace: "nowrap",
                          }}
                        >
                          {label}: {formatRunningElapsed(elapsed)} · ${spend.total_usd.toFixed(2)}
                        </span>
                      );
                    })()}
                    {p.state === "deploying" && p.deploy_progress?.step === "queued" && (
                      <span style={{
                        fontSize: 11, padding: "1px 6px", borderRadius: 4,
                        background: "rgba(99,102,241,0.15)", color: "#818cf8",
                      }}>
                        queued {p.deploy_progress.detail}
                      </span>
                    )}
                    {p.state === "deploying" && p.deploy_progress?.detail && p.deploy_progress?.step !== "queued" && (
                      <span title={p.deploy_progress.detail} style={{
                        fontSize: 11, padding: "1px 6px", borderRadius: 4,
                        background: "rgba(251,191,36,0.15)", color: "#fbbf24",
                        maxWidth: "100%", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                      }}>
                        {summarizeDeployProgress(p.deploy_progress)}
                      </span>
                    )}
                    {(p.state === "deploying" || p.state === "stopping" || p.state === "starting" || p.state === "deleting") && (
                      <span className="project-btn-spinner" style={{ width: 14, height: 14 }} />
                    )}
                    {p.state !== "deleting" && p.ocp_status && p.ocp_status !== "none" && (
                      <span style={{
                        fontSize: 11, padding: "1px 6px", borderRadius: 4,
                        ...ocpBadgeStyle(p.ocp_status),
                      }}>
                        {p.ocp_status_detail || `OCP ${p.ocp_status}`}
                      </span>
                    )}
                    </div>
                  </div>
                  {p.state === "error" && p.deploy_error && (
                    <p style={{ fontSize: 12, color: "#f87171", margin: "4px 0 0" }}>{p.deploy_error}</p>
                  )}
                  <p style={{ fontSize: 13, opacity: 0.7, margin: "4px 0 0" }}>{p.description || "No description"}</p>
                  <p style={{ fontSize: 11, opacity: 0.5, margin: "4px 0 0" }}>
                    {p.host_type} &middot; created {new Date(p.created_at).toLocaleString()}
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
                      body: JSON.stringify({ tags: { ...(p.tags), user_tags: [...cur, tag] } }),
                    });
                    fetchProjects();
                  }}
                  onRemove={async (tag) => {
                    const cur = (p.tags?.user_tags as string[]) || [];
                    await fetch(`/api/v1/projects/${p.id}`, {
                      method: "PATCH",
                      headers: { "Content-Type": "application/json" },
                      body: JSON.stringify({ tags: { ...(p.tags), user_tags: cur.filter((t: string) => t !== tag) } }),
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
                    <Button variant="primary" onClick={async () => {
                      if (!(await appConfirm({
                        title: "Deploy",
                        message: `Deploy project "${p.name}"? This will provision networking and start all VMs.`,
                        confirmLabel: "Deploy",
                      }))) return;
                      updateProjectState(p.id, "deploying");
                      const params = new URLSearchParams();
                      if (deployPoolId) params.set("storage_pool_id", deployPoolId);
                      if (deployHostId) params.set("host_id", deployHostId);
                      const qs = params.toString() ? `?${params.toString()}` : "";
                      fetch(`${API_BASE}/api/v1/projects/${p.id}/deploy${qs}`, { method: "POST" }).then(r => r.json()).then(d => {
                        if (d.status === "deploying") { pollUntilSettled(); }
                        else { setAlertMsg(d.detail || "Deploy failed"); updateProjectState(p.id, "draft"); }
                      });
                    }}>Deploy</Button>
                  </>
                )}
                {p.state === "active" && (
                  <Button variant="secondary" onClick={async () => {
                    if (!(await appConfirm({ message: `Stop project "${p.name}"? All VMs will be shut down.`, confirmLabel: "Stop" }))) return;
                    updateProjectState(p.id, "stopping");
                    fetch(`${API_BASE}/api/v1/projects/${p.id}/stop`, { method: "POST" }).then(() => pollUntilSettled());
                  }}>Stop</Button>
                )}
                {(p.state === "stopped" || p.state === "error") && (
                  <Button variant="secondary" onClick={async () => {
                    if (!(await appConfirm({ message: `Start project "${p.name}"? All VMs will be started.`, confirmLabel: "Start" }))) return;
                    updateProjectState(p.id, "starting");
                    fetch(`${API_BASE}/api/v1/projects/${p.id}/start`, { method: "POST" }).then(() => pollUntilSettled());
                  }}>Start</Button>
                )}
                {(p.state === "error" || p.state === "active" || p.state === "stopped") && (
                  <Button variant="secondary" onClick={() => setRepublishTarget(p)}>Republish</Button>
                )}
                {p.state !== "deleting" && <Button variant="danger" isDisabled={deletingProjects.has(p.id)} onClick={(e) => {
                  e.stopPropagation();
                  setDeleteTarget(p);
                }}>{deletingProjects.has(p.id) ? <><span className="project-btn-spinner" style={{ width: 12, height: 12 }} /> Deleting...</> : "Delete"}</Button>}
                </>)}
              </CardBody>
            </Card>
          ))}
        </div>
      </PageSection>
      {showNewModal && (
        <NewProjectModal onClose={() => setShowNewModal(false)} onCreated={(id) => router.push(`/projects/${id}`)} userRole={userRole} availableHosts={availableHosts} setAlertMsg={setAlertMsg} />
      )}
      <AlertModal message={alertMsg} onClose={() => setAlertMsg(null)} />
      {republishTarget && (
        <ConfirmModal
          title="Republish Project"
          message={
            republishTarget.ocp_status && republishTarget.ocp_status !== "none"
              ? `Republish "${republishTarget.name}"? OpenShift clusters will be rebuilt from scratch (all disks wiped). For re-cert when clusters are ready, open the project page.`
              : `Republish project "${republishTarget.name}"? This will destroy and recreate all VMs.`
          }
          confirmLabel={
            republishTarget.ocp_status && republishTarget.ocp_status !== "none"
              ? "Rebuild"
              : "Republish"
          }
          variant="danger"
          onCancel={() => setRepublishTarget(null)}
          onConfirm={() => {
            const p = republishTarget;
            setRepublishTarget(null);
            updateProjectState(p.id, "deploying");
            const hasOcp = !!(p.ocp_status && p.ocp_status !== "none");
            fetch(`${API_BASE}/api/v1/projects/${p.id}/redeploy`, {
              method: "POST",
              headers: hasOcp ? { "Content-Type": "application/json" } : undefined,
              body: hasOcp ? JSON.stringify({ ocp_mode: "rebuild" }) : undefined,
            }).then(r => r.json()).then(d => {
              if (d.status === "deploying") { pollUntilSettled(); }
              else { setAlertMsg(d.detail || "Republish failed"); updateProjectState(p.id, "error"); }
            });
          }}
        />
      )}
      {deleteTarget && (
        <ConfirmModal
          title="Delete Project"
          message={`Delete project "${deleteTarget.name}"? This cannot be undone.`}
          confirmLabel="Delete"
          variant="danger"
          onCancel={() => setDeleteTarget(null)}
          onConfirm={() => {
            const p = deleteTarget;
            setDeleteTarget(null);
            setDeletingProjects(prev => new Set(prev).add(p.id));
            fetch(`${API_BASE}/api/v1/projects/${p.id}`, { method: "DELETE" }).then((r) => {
              if (r.ok) {
                r.json().then((data) => {
                  if (data.status === "deleting") {
                    updateProjectState(p.id, "deleting");
                  } else {
                    removeProject(p.id);
                    localStorage.removeItem(`troshka-canvas-${p.id}`);
                  }
                }).catch(() => {
                  removeProject(p.id);
                });
              }
              setDeletingProjects(prev => { const s = new Set(prev); s.delete(p.id); return s; });
            });
          }}
        />
      )}
      {bulkDeletePending && selectedProjects.size > 0 && (
        <ConfirmModal
          title="Delete Projects"
          message={`Delete ${selectedProjects.size} project(s)? This cannot be undone.`}
          confirmLabel="Delete"
          variant="danger"
          onCancel={() => setBulkDeletePending(false)}
          onConfirm={() => {
            const toDelete = projects.filter((pr) => selectedProjects.has(pr.id));
            setBulkDeletePending(false);
            for (const p of toDelete) {
              fetch(`${API_BASE}/api/v1/projects/${p.id}`, { method: "DELETE" });
            }
            setSelectedProjects(new Set());
            setTimeout(fetchProjects, 1000);
          }}
        />
      )}
    </>
  );
}
