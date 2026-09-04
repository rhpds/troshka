"use client";

import React, { useState, useEffect, useRef } from "react";
import type { Node, Edge } from "@xyflow/react";
import AlertModal from "@/components/AlertModal";
import { appConfirm } from "@/lib/confirm";
import LibraryPicker from "./LibraryPicker";
import { useCanvasStore, generateNicId, generateDiskControllerId, generateMac, syncBmcNetwork, allocateBmcIp } from "@/stores/canvasStore";
import { reconcileClusterVms, applyClusterSizing, memberRole, applyClusterNetworks, applyClusterDisks, applyClusterDns, ensureSnoNodeIp, effectiveDnsNetworkId, clusterPrereqIssues, suggestClusterVips, vipCollision, vipInMemberSubnet } from "./clusterMaterialize";
import { resolveDnsRecordDisplayIp } from "@/lib/dnsRecords";
import {
  getShowroomReadiness,
  isDnsEnabledLabNetwork,
} from "@/lib/showroomValidation";
import { effectiveShowroomDnsNetwork } from "@/lib/showroomScaffold";
import { isGatewayConnectedLabNetwork } from "@/lib/gatewayValidation";
import { isShowroomManagedForward } from "@/lib/showroomPortForwards";
import {
  allowedDiskBuses,
  allowedMachineTypes,
  allowedVideoModels,
  DISK_BUS_LABELS,
  MACHINE_TYPE_LABELS,
  VIDEO_MODEL_LABELS,
} from "@/lib/kubevirtCapabilities";
import { newShowroomTab, resolveShowroomTabs, syncClusterProxyTabs, clusterConsoleHosts, clusterConsoleTabName, type ShowroomTab } from "@/lib/showroomTabs";
import {
  buildWettyCommand,
  formatCommandForInput,
  isWettyContainer,
  parseWettyCommand,
  type WettyAttrs,
} from "@/lib/wettyContainer";
import type {
  VMNodeData,
  NetworkNodeData,
  StorageNodeData,
  ContainerNodeData,
  ClusterConfig,
  DiskSpec,
} from "@/stores/canvasStore";

function HintIcon({ text }: { text: string }) {
  return (
    <span
      title={text}
      aria-label={text}
      style={{
        fontSize: 9,
        fontWeight: 600,
        color: "var(--troshka-text-dim)",
        cursor: "help",
        border: "1px solid var(--troshka-border)",
        borderRadius: "50%",
        width: 14,
        height: 14,
        display: "inline-flex",
        alignItems: "center",
        justifyContent: "center",
        lineHeight: 1,
        flexShrink: 0,
      }}
    >
      i
    </span>
  );
}

function LabelWithHint({
  label,
  hint,
  style,
}: {
  label: string;
  hint: string;
  style?: React.CSSProperties;
}) {
  return (
    <label
      className="props-label"
      style={{ display: "flex", alignItems: "center", gap: 5, ...style }}
    >
      {label}
      <HintIcon text={hint} />
    </label>
  );
}

function isDuplicateName(name: string, nodeId: string, nodeType: string): boolean {
  if (!name) return false;
  const nodes = useCanvasStore.getState().nodes;
  return nodes.some(
    (n) => n.id !== nodeId && n.type === nodeType && ((n.data as Record<string, unknown>).name || (n.data as Record<string, unknown>).label) === name
  );
}

function cidrToRange(cidr: string): [number, number] | null {
  if (!cidr) return null;
  const match = cidr.match(/^(\d+)\.(\d+)\.(\d+)\.(\d+)\/(\d+)$/);
  if (!match) return null;
  const ip = (parseInt(match[1]) << 24) + (parseInt(match[2]) << 16) + (parseInt(match[3]) << 8) + parseInt(match[4]);
  const bits = parseInt(match[5]);
  if (bits < 0 || bits > 32) return null;
  const mask = bits === 0 ? 0 : (~0 << (32 - bits)) >>> 0;
  const start = (ip & mask) >>> 0;
  const end = (start | (~mask >>> 0)) >>> 0;
  return [start, end];
}

function cidrsOverlap(a: string, b: string): boolean {
  const ra = cidrToRange(a);
  const rb = cidrToRange(b);
  if (!ra || !rb) return false;
  return ra[0] <= rb[1] && rb[0] <= ra[1];
}

function ipToNum(ip: string): number | null {
  const parts = ip.split(".");
  if (parts.length !== 4) return null;
  const nums = parts.map(Number);
  if (nums.some((n) => isNaN(n) || n < 0 || n > 255)) return null;
  return ((nums[0] << 24) + (nums[1] << 16) + (nums[2] << 8) + nums[3]) >>> 0;
}

function validateDhcpRange(cidr: string, start: string, end: string, gateway: string): string[] {
  const errors: string[] = [];
  const range = cidrToRange(cidr);
  if (!range) return [];

  const startNum = ipToNum(start);
  const endNum = ipToNum(end);
  const gwNum = ipToNum(gateway);

  if (start && !startNum) errors.push("Invalid start IP");
  if (end && !endNum) errors.push("Invalid end IP");
  if (gateway && !gwNum) errors.push("Invalid gateway IP");

  if (startNum && (startNum <= range[0] || startNum >= range[1]))
    errors.push("Start IP outside subnet");
  if (endNum && (endNum <= range[0] || endNum >= range[1]))
    errors.push("End IP outside subnet");
  if (gwNum && (gwNum <= range[0] || gwNum >= range[1]))
    errors.push("Gateway IP outside subnet");

  if (startNum && endNum && startNum >= endNum)
    errors.push("Start must be less than end");
  if (gwNum && startNum && endNum && gwNum >= startNum && gwNum <= endNum)
    errors.push("Gateway conflicts with DHCP range");

  return errors;
}

function validateDhcpRangeFull(cidr: string, start: string, end: string, gateway: string, dnsIp: string): string[] {
  const errors = validateDhcpRange(cidr, start, end, gateway);
  const startNum = ipToNum(start);
  const endNum = ipToNum(end);
  const dnsNum = ipToNum(dnsIp);
  if (dnsIp && !dnsNum) errors.push("Invalid DNS server IP");
  if (dnsNum && startNum && endNum && dnsNum >= startNum && dnsNum <= endNum)
    errors.push("DNS server IP conflicts with DHCP range");
  const range = cidrToRange(cidr);
  if (dnsNum && range && (dnsNum <= range[0] || dnsNum >= range[1]))
    errors.push("DNS server IP outside subnet");
  return errors;
}

function formatOutboundRuleLabel(entry: string): string {
  if (entry === "icmp") return "ICMP";
  if (entry.startsWith("icmp/")) return `ICMP type ${entry.slice(5)}`;
  if (entry.includes("/")) return entry;
  return `${entry} tcp/udp`;
}

const ICMP_TYPE_OPTIONS = [
  { value: "", label: "All types" },
  { value: "echo-request", label: "echo-request (8)" },
  { value: "destination-unreachable", label: "destination-unreachable (3)" },
  { value: "time-exceeded", label: "time-exceeded (11)" },
];

function OutboundRulesEditor({
  outboundPorts,
  onChange,
}: {
  outboundPorts: string;
  onChange: (value: string) => void;
}) {
  const normalizedOutboundPorts = typeof outboundPorts === "string" ? outboundPorts : "";
  const currentRules = normalizedOutboundPorts.split(",").map((p) => p.trim()).filter(Boolean);
  const [proto, setProto] = useState<"both" | "tcp" | "udp" | "icmp">("both");
  const [portInput, setPortInput] = useState("");
  const [icmpType, setIcmpType] = useState("");
  const portInputRef = useRef<HTMLInputElement>(null);

  const removeRule = (rule: string) => {
    onChange(currentRules.filter((r) => r !== rule).join(","));
  };

  const addRule = () => {
    let entry: string;
    if (proto === "icmp") {
      const type = icmpType.trim();
      entry = type ? `icmp/${type}` : "icmp";
    } else {
      const num = portInput.trim();
      const port = Number(num);
      if (!Number.isInteger(port) || port < 1 || port > 65535) return;
      entry = proto === "both" ? String(port) : `${port}/${proto}`;
    }
    if (!currentRules.includes(entry)) {
      onChange([...currentRules, entry].join(","));
    }
    setPortInput("");
    setIcmpType("");
    portInputRef.current?.focus();
  };

  const selectStyle = {
    fontSize: 11,
    padding: "3px 4px",
    borderRadius: 3,
    border: "1px solid var(--pf-t--global--border--color--default)",
    background: "var(--pf-t--global--background--color--secondary--default)",
    color: "var(--pf-t--global--text--color--regular)",
  } as const;

  return (
    <div className="props-field">
      <label className="props-label">Allowed Outbound Rules</label>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginBottom: 6 }}>
        {currentRules.map((rule) => (
          <span
            key={rule}
            style={{
              display: "inline-flex", alignItems: "center", gap: 4,
              padding: "2px 8px", borderRadius: 12, fontSize: 11,
              background: "rgba(0,102,204,0.15)", color: "#73bcf7",
              border: "1px solid rgba(0,102,204,0.3)",
            }}
          >
            {formatOutboundRuleLabel(rule)}
            <button
              type="button"
              aria-label={`Remove ${formatOutboundRuleLabel(rule)}`}
              onClick={() => removeRule(rule)}
              style={{ cursor: "pointer", opacity: 0.6, fontSize: 10, background: "none", border: 0, padding: 0 }}
            >
              ✕
            </button>
          </span>
        ))}
      </div>
      <div style={{ display: "flex", gap: 4, alignItems: "center", flexWrap: "wrap" }}>
        {proto !== "icmp" ? (
          <input
            ref={portInputRef}
            type="number"
            min={1}
            max={65535}
            step={1}
            className="props-input"
            aria-label="Outbound port"
            placeholder="Port"
            value={portInput}
            style={{ width: 70, fontSize: 11, fontFamily: "monospace" }}
            onChange={(e) => setPortInput(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") addRule(); }}
          />
        ) : (
          <select
            value={icmpType}
            onChange={(e) => setIcmpType(e.target.value)}
            style={{ ...selectStyle, minWidth: 180 }}
            aria-label="ICMP type"
          >
            {ICMP_TYPE_OPTIONS.map((opt) => (
              <option key={opt.value || "all"} value={opt.value}>{opt.label}</option>
            ))}
          </select>
        )}
        <select
          value={proto}
          onChange={(e) => setProto(e.target.value as typeof proto)}
          style={selectStyle}
          aria-label="Outbound protocol"
        >
          <option value="both">TCP+UDP</option>
          <option value="tcp">TCP</option>
          <option value="udp">UDP</option>
          <option value="icmp">ICMP</option>
        </select>
        <button
          onClick={addRule}
          style={{
            padding: "3px 8px", borderRadius: 3, fontSize: 11, cursor: "pointer",
            border: "1px solid var(--pf-t--global--border--color--default)",
            background: "transparent", color: "var(--pf-t--global--text--color--regular)",
          }}
        >Add</button>
      </div>
    </div>
  );
}

interface SshKeyOption {
  id: number;
  name: string;
  public_key: string;
}

function DiskSizeInput({ value, min, onChange }: { value: number; min: number; onChange: (v: number) => void }) {
  const [local, setLocal] = useState(String(value));
  const prevValue = useRef(value);
  useEffect(() => {
    if (value !== prevValue.current) {
      setLocal(String(value));
      prevValue.current = value;
    }
  }, [value]);
  const localNum = parseInt(local) || 0;
  const tooSmall = localNum > 0 && localNum < min;
  return (
    <>
      <input
        className="props-input"
        type="number"
        min={min}
        value={local}
        onChange={(e) => setLocal(e.target.value)}
        onBlur={() => {
          const v = Math.max(parseInt(local) || min, min);
          setLocal(String(v));
          onChange(v);
        }}
        onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }}
        style={{ borderColor: tooSmall ? "var(--troshka-red)" : undefined }}
      />
      {tooSmall && (
        <span style={{ fontSize: 11, color: "var(--troshka-red)", marginTop: 4, display: "block" }}>
          Cannot be smaller than {min} GB
        </span>
      )}
    </>
  );
}

function RegistryCredentialDropdown({
  value,
  onChange,
}: {
  value: string | null;
  onChange: (v: string | null) => void;
}) {
  const [creds, setCreds] = useState<Array<{ id: string; name: string; registry: string }>>([]);
  useEffect(() => {
    fetch("/api/v1/auth/registry-credentials")
      .then((r) => r.json())
      .then(setCreds)
      .catch(() => {});
  }, []);
  return (
    <select
      className="props-select"
      value={value || ""}
      onChange={(e) => onChange(e.target.value || null)}
    >
      <option value="">None (public)</option>
      {creds.map((c) => (
        <option key={c.id} value={c.id}>
          {c.name} ({c.registry})
        </option>
      ))}
    </select>
  );
}

// Cluster summary fields the boundary node renders as a badge — kept in sync on
// the clusterNode's own `data` whenever the cluster config changes.
const CLUSTER_SUMMARY_FIELDS = [
  "name",
  "type",
  "controlPlane",
  "workers",
  "baseDomain",
  "apiVip",
  "ingressVip",
] as const;

function clusterSummaryMirror(patch: Partial<ClusterConfig>): Record<string, unknown> {
  const mirror: Record<string, unknown> = {};
  for (const k of CLUSTER_SUMMARY_FIELDS) {
    if (k in patch) mirror[k] = (patch as Record<string, unknown>)[k];
  }
  if (patch.name !== undefined) mirror.label = patch.name;
  return mirror;
}

/**
 * Cross-cluster VIP uniqueness check. Returns an inline error message when the
 * given VIP value collides with the other VIP on the same cluster or with any
 * VIP on another cluster; null when unique or empty. Never blocks input.
 */
function vipCollisionError(
  clusters: ClusterConfig[],
  clusterId: string,
  field: "apiVip" | "ingressVip",
  value: string,
): string | null {
  const v = (value || "").trim();
  if (!v) return null;
  const self = clusters.find((c) => c.id === clusterId);
  const otherField = field === "apiVip" ? "ingressVip" : "apiVip";
  // SNO clusters legitimately share ONE VIP (single node), so api == ingress is
  // valid — only flag same-cluster duplication for multi-node clusters.
  if (self && self.type !== "sno" && (self[otherField] || "").trim() === v) {
    return `Duplicates this cluster's ${otherField === "apiVip" ? "API VIP" : "Ingress VIP"}`;
  }
  for (const c of clusters) {
    if (c.id === clusterId) continue;
    if ((c.apiVip || "").trim() === v || (c.ingressVip || "").trim() === v) {
      return `Already used by cluster "${c.name}"`;
    }
  }
  return null;
}

function ClusterNumberField({
  label,
  value,
  min = 0,
  onCommit,
  disabled = false,
  hint,
}: {
  label: string;
  value: number;
  min?: number;
  onCommit: (n: number) => void;
  disabled?: boolean;
  hint?: string;
}) {
  return (
    <div className="props-field">
      <label className="props-label" title={hint}>{label}</label>
      <input
        type="number"
        aria-label={label}
        className="props-input"
        min={min}
        value={value}
        disabled={disabled}
        title={hint}
        onChange={(e) => {
          if (disabled) return;
          const n = parseInt(e.target.value, 10);
          onCommit(Number.isNaN(n) ? min : Math.max(min, n));
        }}
        style={disabled ? { opacity: 0.6, cursor: "not-allowed" } : undefined}
      />
    </div>
  );
}

function ClusterTextField({
  label,
  value,
  placeholder,
  error,
  onCommit,
  disabled = false,
  hint,
}: {
  label: string;
  value: string;
  placeholder?: string;
  error?: string | null;
  onCommit: (v: string) => void;
  disabled?: boolean;
  hint?: React.ReactNode;
}) {
  return (
    <div className="props-field">
      <label className="props-label">{label}</label>
      <input
        aria-label={label}
        className="props-input"
        value={value}
        placeholder={placeholder}
        disabled={disabled}
        onChange={(e) => { if (!disabled) onCommit(e.target.value); }}
        style={{
          ...(error ? { borderColor: "var(--pf-t--global--color--status--warning--default)" } : {}),
          ...(disabled ? { opacity: 0.6, cursor: "not-allowed" } : {}),
        }}
      />
      {error && (
        <div style={{ color: "var(--pf-t--global--color--status--warning--default)", fontSize: 11, marginTop: 2 }}>
          {error}
        </div>
      )}
      {hint && !error && (
        <div style={{ color: "var(--troshka-text-dim, #94a3b8)", fontSize: 10, marginTop: 2 }}>
          {hint}
        </div>
      )}
    </div>
  );
}

function DiskListEditor({
  disks,
  onChange,
}: {
  disks: DiskSpec[];
  onChange: (disks: DiskSpec[]) => void;
}) {
  const handleAddDisk = () => {
    const newDisk: DiskSpec = { sizeGb: 50, bus: "virtio", bootable: false };
    onChange([...disks, newDisk]);
  };

  const handleRemoveDisk = (index: number) => {
    const remaining = disks.filter((_, i) => i !== index);
    // Keep exactly one boot disk: if we removed the boot disk (or none is
    // marked), make the first remaining disk bootable.
    if (remaining.length > 0 && !remaining.some((d) => d.bootable)) {
      remaining[0] = { ...remaining[0], bootable: true };
    }
    onChange(remaining);
  };

  const handleUpdateDisk = (index: number, patch: Partial<DiskSpec>) => {
    const updated = [...disks];
    updated[index] = { ...updated[index], ...patch };
    onChange(updated);
  };

  // Boot is exclusive (radio-style): exactly one disk per role is the boot
  // disk. Selecting a disk clears bootable on all others.
  const handleSetBoot = (index: number) => {
    onChange(disks.map((d, i) => ({ ...d, bootable: i === index })));
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      {disks.length === 0 ? (
        <span style={{ fontSize: 13, color: "var(--troshka-text-dim)" }}>
          No disks configured
        </span>
      ) : (
        disks.map((disk, idx) => (
          <div
            key={idx}
            style={{
              display: "flex",
              gap: 8,
              alignItems: "center",
              padding: "8px",
              background: "var(--troshka-surface)",
              border: "1px solid var(--troshka-border)",
              borderRadius: 4,
              fontSize: 13,
            }}
          >
            <label style={{ display: "flex", alignItems: "center", gap: 4, flex: 1 }}>
              <input
                type="checkbox"
                checked={disk.bootable ?? false}
                onChange={() => handleSetBoot(idx)}
                style={{ cursor: "pointer" }}
              />
              <span>Boot</span>
            </label>
            <input
              type="number"
              min={1}
              value={disk.sizeGb}
              onChange={(e) => handleUpdateDisk(idx, { sizeGb: parseInt(e.target.value, 10) || 1 })}
              style={{
                width: 60,
                padding: "4px 6px",
                border: "1px solid var(--troshka-border)",
                borderRadius: 4,
                fontSize: 12,
              }}
            />
            <span style={{ color: "var(--troshka-text-dim)" }}>GB</span>
            <select
              value={disk.bus ?? "virtio"}
              onChange={(e) => handleUpdateDisk(idx, { bus: e.target.value as "virtio" | "sata" | "scsi" })}
              style={{
                padding: "4px 6px",
                border: "1px solid var(--troshka-border)",
                borderRadius: 4,
                fontSize: 12,
              }}
            >
              <option value="virtio">virtio</option>
              <option value="sata">sata</option>
              <option value="scsi">scsi</option>
            </select>
            <button
              onClick={() => handleRemoveDisk(idx)}
              title="Remove disk"
              aria-label="Remove disk"
              style={{
                flex: "0 0 auto",
                width: 22,
                height: 22,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                background: "transparent",
                color: "var(--troshka-red, #ef4444)",
                border: "none",
                borderRadius: 4,
                cursor: "pointer",
                fontSize: 14,
                lineHeight: 1,
              }}
            >
              ✕
            </button>
          </div>
        ))
      )}
      <button
        onClick={handleAddDisk}
        className="props-library-btn"
        style={{ fontSize: 12, padding: "6px 8px", alignSelf: "flex-start" }}
      >
        + Add Disk
      </button>
    </div>
  );
}

// Cluster type + worker count define the cluster's node shape; changing them
// after deploy would require a full OCP redeploy, so they are locked once the
// cluster's member VMs exist.
const _CLUSTER_SHAPE_LOCK_HINT =
  "🔒 Locked while deployed — changing the cluster type or worker count requires redeploying OpenShift.";

function ClusterEditor({
  cluster,
  clusters,
  onPatch,
  onSizing,
  onTypeChange,
  onWorkersChange,
  onNetworksChange,
  onDisksChange,
  availableNetworks,
  nodes,
  ocpVersions,
}: {
  cluster: ClusterConfig;
  clusters: ClusterConfig[];
  onPatch: (patch: Partial<ClusterConfig>) => void;
  onSizing: (patch: Partial<ClusterConfig>) => void;
  onTypeChange: (type: string) => void;
  onWorkersChange: (workers: number) => void;
  onNetworksChange: (networkIds: string[]) => void;
  onDisksChange: (role: "control-plane" | "worker", disks: DiskSpec[]) => void;
  availableNetworks: Array<{ id: string; label: string; cidr?: string; dns?: boolean }>;
  nodes: Node[];
  ocpVersions: Array<{ name: string; support: string }>;
}) {
  // Bastionless (pod-install) OCP has no bastion, so the bastion-browser option
  // is hidden for pod projects.
  const ocpInstallVia = useCanvasStore((s) => s.ocpInstallVia);
  // SNO has no VIPs (OpenShift forbids them for a single node) — api/*.apps use
  // the node's own IP. Show the VIP fields as read-only N/A for SNO.
  const isSno = cluster.type === "sno";
  const apiVipError = vipCollisionError(clusters, cluster.id, "apiVip", cluster.apiVip || "");
  const ingressVipError = vipCollisionError(clusters, cluster.id, "ingressVip", cluster.ingressVip || "");

  // Auto-suggest VIPs on load
  const suggestions = suggestClusterVips(cluster, nodes);
  const apiVipSuggestion = cluster.apiVip ? null : suggestions.apiVip;
  const ingressVipSuggestion = cluster.ingressVip ? null : suggestions.ingressVip;
  // OCP DNS is api/api-int/*.apps.<name>.<baseDomain>. The base domain is a
  // SHARED parent (e.g. "local", "example.com"); uniqueness comes from the
  // cluster NAME (the subdomain), so names must be unique across clusters.
  const trimmedName = (cluster.name || "").trim().toLowerCase();
  const nameDuplicate =
    trimmedName !== "" &&
    clusters.some(
      (c) => c.id !== cluster.id && (c.name || "").trim().toLowerCase() === trimmedName,
    );

  // A cluster is "deployed" once any of its member VMs has been provisioned.
  // The base domain feeds DNS (api/api-int/*.apps) baked into every node at
  // install time, so it must not change under a live cluster — only a full
  // rebuild (all member VMs wiped) may alter it.
  const deployedVmIds = useCanvasStore.getState().deployedVmIds;
  const clusterDeployed = nodes.some(
    (n) =>
      n.type === "vmNode" &&
      (n.data as Record<string, unknown>).clusterId === cluster.id &&
      deployedVmIds.has(n.id),
  );

  // Auto-fill blank VIPs with the first available unused IP so the user does
  // not have to pick one manually (still fully editable — clearing re-fills).
  useEffect(() => {
    const patch: Partial<ClusterConfig> = {};
    if (!cluster.apiVip && suggestions.apiVip) patch.apiVip = suggestions.apiVip;
    if (!cluster.ingressVip && suggestions.ingressVip) patch.ingressVip = suggestions.ingressVip;
    if (Object.keys(patch).length > 0) onPatch(patch);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cluster.id, cluster.apiVip, cluster.ingressVip, suggestions.apiVip, suggestions.ingressVip]);

  // Mirror the cluster's api/api-int/*.apps records onto its member networks so
  // they appear in the network node's DNS list immediately (pre-deploy), and
  // update live as name/base domain/VIPs/networks change. Idempotent — the
  // helper returns the same nodes ref when nothing changes, so no loop.
  const networkIdsKey = (cluster.networkIds ?? []).join(",");
  useEffect(() => {
    const current = useCanvasStore.getState().nodes;
    // SNO: give the single node a static IP first (no DHCP), then mirror DNS off
    // it (api/*.apps → the node IP). No-op for multi-node.
    const withIp = ensureSnoNodeIp(cluster, current);
    const synced = applyClusterDns(cluster, withIp);
    if (synced !== current) useCanvasStore.setState({ nodes: synced });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    cluster.id,
    cluster.name,
    cluster.baseDomain,
    cluster.apiVip,
    cluster.ingressVip,
    cluster.dnsNetworkId,
    networkIdsKey,
  ]);

  // Default the OCP version to the latest Full Support release once the list
  // loads (falls back to the newest available if none are "Full Support").
  useEffect(() => {
    if (cluster.ocpVersion || ocpVersions.length === 0) return;
    const latest = ocpVersions.find((v) => v.support === "Full Support") || ocpVersions[0];
    if (latest) onPatch({ ocpVersion: latest.name });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cluster.ocpVersion, ocpVersions]);

  const prereqIssues = clusterPrereqIssues(cluster, nodes);

  return (
    <>
      {prereqIssues.length > 0 && (
        <div className="props-section" style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          {prereqIssues.map((issue) => (
            <div
              key={issue.message}
              style={{
                fontSize: 11,
                lineHeight: 1.35,
                color:
                  issue.level === "error"
                    ? "var(--troshka-red, #ef4444)"
                    : "var(--troshka-yellow, #f59e0b)",
              }}
            >
              {issue.level === "error" ? "⛔" : "⚠"} {issue.message}
            </div>
          ))}
        </div>
      )}
      <div className="props-section">
        <div className="props-section-title">General</div>
        <ClusterTextField
          label="Name"
          value={cluster.name || ""}
          disabled={clusterDeployed}
          hint={clusterDeployed ? "🔒 Locked while deployed — the name is part of the cluster FQDN." : undefined}
          onCommit={(v) => onPatch({ name: v })}
        />
        {nameDuplicate && (
          <div style={{ fontSize: 11, color: "var(--troshka-red, #ef4444)", marginTop: -4 }}>
            ⚠ Another cluster already uses this name — each cluster needs a unique name (it is the DNS subdomain).
          </div>
        )}
        <div className="props-field">
          <label className="props-label" title={clusterDeployed ? _CLUSTER_SHAPE_LOCK_HINT : undefined}>Cluster Type</label>
          <select
            aria-label="Cluster Type"
            className="props-select"
            value={cluster.type}
            disabled={clusterDeployed}
            title={clusterDeployed ? _CLUSTER_SHAPE_LOCK_HINT : undefined}
            style={clusterDeployed ? { opacity: 0.6, cursor: "not-allowed" } : undefined}
            onChange={(e) => { if (!clusterDeployed) onTypeChange(e.target.value); }}
          >
            <option value="sno">SNO (single node)</option>
            <option value="compact">Compact (3 control-plane)</option>
            <option value="standard">Standard</option>
          </select>
        </div>
        <div className="props-field">
          <label className="props-label">Control Plane Nodes</label>
          <span style={{ fontSize: 13, color: "var(--troshka-text-dim)" }}>
            {cluster.controlPlane} (derived from type)
          </span>
        </div>
        <ClusterNumberField
          label="Workers"
          value={cluster.workers}
          onCommit={onWorkersChange}
          disabled={clusterDeployed || cluster.type !== "standard"}
          hint={
            clusterDeployed
              ? _CLUSTER_SHAPE_LOCK_HINT
              : cluster.type !== "standard"
                ? "Only standard clusters have separate workers — SNO is a single node and compact nodes are combined control-plane + worker."
                : undefined
          }
        />
      </div>
      <div className="props-divider" />

      <div className="props-section">
        <div className="props-section-title">Control Plane Sizing</div>
        <ClusterNumberField label="Control Plane vCPUs" min={1} value={cluster.controlPlaneCpu ?? 8} onCommit={(v) => onSizing({ controlPlaneCpu: v })} />
        <ClusterNumberField label="Control Plane Memory (GB)" min={1} value={Math.round((cluster.controlPlaneMemory ?? 16384) / 1024)} onCommit={(v) => onSizing({ controlPlaneMemory: v * 1024 })} />
        <ClusterNumberField label="Control Plane Disk (GB)" min={1} value={cluster.controlPlaneDisk ?? 120} onCommit={(v) => onSizing({ controlPlaneDisk: v })} />
      </div>
      <div className="props-divider" />

      <div className="props-section">
        <div className="props-section-title">Control Plane Disks</div>
        <DiskListEditor
          disks={cluster.controlPlaneDisks ?? []}
          onChange={(disks) => onDisksChange("control-plane", disks)}
        />
      </div>
      <div className="props-divider" />

      {(cluster.workers ?? 0) > 0 && (
        <>
          <div className="props-section">
            <div className="props-section-title">Worker Sizing</div>
            <ClusterNumberField label="Worker vCPUs" min={1} value={cluster.workerCpu ?? 4} onCommit={(v) => onSizing({ workerCpu: v })} />
            <ClusterNumberField label="Worker Memory (GB)" min={1} value={Math.round((cluster.workerMemory ?? 8192) / 1024)} onCommit={(v) => onSizing({ workerMemory: v * 1024 })} />
            <ClusterNumberField label="Worker Disk (GB)" min={1} value={cluster.workerDisk ?? 100} onCommit={(v) => onSizing({ workerDisk: v })} />
          </div>
          <div className="props-divider" />

          <div className="props-section">
            <div className="props-section-title">Worker Disks</div>
            <DiskListEditor
              disks={cluster.workerDisks ?? []}
              onChange={(disks) => onDisksChange("worker", disks)}
            />
          </div>
          <div className="props-divider" />
        </>
      )}

      <div className="props-section">
        <div className="props-section-title">Networking</div>
        <div className="props-field">
          <label className="props-label">Member Networks</label>
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              gap: 6,
              padding: "8px 0",
            }}
          >
            {availableNetworks.length === 0 ? (
              <span style={{ fontSize: 13, color: "var(--troshka-text-dim)" }}>
                No networks on canvas
              </span>
            ) : (
              availableNetworks.map((net) => (
                <label
                  key={net.id}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 8,
                    fontSize: 13,
                    cursor: "pointer",
                  }}
                >
                  <input
                    type="checkbox"
                    checked={(cluster.networkIds ?? []).includes(net.id)}
                    onChange={(e) => {
                      const newIds = e.target.checked
                        ? [...(cluster.networkIds ?? []), net.id]
                        : (cluster.networkIds ?? []).filter((id) => id !== net.id);
                      onNetworksChange(newIds);
                    }}
                    style={{ cursor: "pointer" }}
                  />
                  <span>
                    {net.label}
                    {net.cidr && (
                      <span style={{ color: "var(--troshka-text-dim)", marginLeft: 4 }}>
                        ({net.cidr})
                      </span>
                    )}
                    {net.dns && (
                      <span
                        title="DNS enabled on this network"
                        style={{
                          marginLeft: 6,
                          fontSize: 9,
                          fontWeight: 700,
                          color: "var(--troshka-green, #22c55e)",
                          border: "1px solid var(--troshka-green, #22c55e)",
                          borderRadius: 3,
                          padding: "0 4px",
                        }}
                      >
                        DNS
                      </span>
                    )}
                  </span>
                </label>
              ))
            )}
          </div>
        </div>
        {(cluster.networkIds ?? []).length > 0 && (
          <div className="props-field">
            <label className="props-label">DNS Network</label>
            <select
              aria-label="DNS Network"
              className="props-select"
              value={effectiveDnsNetworkId(cluster) || ""}
              onChange={(e) => onPatch({ dnsNetworkId: e.target.value })}
            >
              {availableNetworks
                .filter((n) => (cluster.networkIds ?? []).includes(n.id))
                .map((n) => (
                  <option key={n.id} value={n.id}>
                    {n.label}
                  </option>
                ))}
            </select>
            <span style={{ fontSize: 10, color: "var(--troshka-text-dim)", marginTop: 2 }}>
              Cluster DNS records (api / api-int / *.apps) live on this network only.
            </span>
          </div>
        )}
        <ClusterTextField
          label="Base Domain (TLD)"
          value={cluster.baseDomain || ""}
          placeholder="local"
          disabled={clusterDeployed}
          hint={
            clusterDeployed ? (
              "🔒 Locked while deployed — wipe all cluster VMs to change."
            ) : (
              <>
                Shared parent domain / TLD — the cluster name is the subdomain. FQDN:{" "}
                <span style={{ fontWeight: 700, color: "var(--troshka-text, #e5e7eb)" }}>
                  api.{(cluster.name || "<name>").trim() || "<name>"}.
                  {(cluster.baseDomain || "local").trim() || "local"}
                </span>
              </>
            )
          }
          onCommit={(v) => onPatch({ baseDomain: v })}
        />
        <div className="props-field">
          <label className="props-label" htmlFor="api-vip">API VIP</label>
          <div style={{ display: "flex", gap: 6, alignItems: "flex-start", flexWrap: "wrap" }}>
            <input
              id="api-vip"
              className="props-input"
              value={isSno ? "N/A" : (cluster.apiVip || "")}
              placeholder={apiVipSuggestion || ""}
              disabled={isSno}
              title={isSno ? "SNO has no API VIP — api resolves to the single node's IP." : undefined}
              onChange={(e) => { if (!isSno) onPatch({ apiVip: e.target.value }); }}
              style={{
                flex: 1,
                minWidth: 150,
                ...(isSno ? { opacity: 0.6, cursor: "not-allowed" } : {}),
                borderColor: !isSno && vipCollision(cluster.apiVip || "", cluster, nodes) ? "var(--pf-t--global--color--status--warning--default)" : undefined,
              }}
            />
          </div>
          {!isSno && !(cluster.apiVip || "").trim() && (
            <div style={{ color: "var(--troshka-red, #ef4444)", fontSize: 11, marginTop: 2 }}>
              ⛔ API VIP is required.
            </div>
          )}
          {!isSno && apiVipError && <div style={{ color: "var(--pf-t--global--color--status--warning--default)", fontSize: 11, marginTop: 2 }}>{apiVipError}</div>}
          {!isSno && vipCollision(cluster.apiVip || "", cluster, nodes) && !apiVipError && (
            <div style={{ color: "var(--pf-t--global--color--status--warning--default)", fontSize: 11, marginTop: 2 }}>IP in use</div>
          )}
          {!isSno && !!cluster.apiVip && !vipInMemberSubnet(cluster.apiVip, cluster, nodes) && (
            <div style={{ color: "var(--troshka-red, #ef4444)", fontSize: 11, marginTop: 2 }}>
              ⚠ Not in any connected network subnet — pick an IP within a member network.
            </div>
          )}
        </div>
        <div className="props-field">
          <label className="props-label" htmlFor="ingress-vip">Ingress VIP</label>
          <div style={{ display: "flex", gap: 6, alignItems: "flex-start", flexWrap: "wrap" }}>
            <input
              id="ingress-vip"
              className="props-input"
              value={isSno ? "N/A" : (cluster.ingressVip || "")}
              placeholder={ingressVipSuggestion || ""}
              disabled={isSno}
              title={isSno ? "SNO has no Ingress VIP — *.apps resolves to the single node's IP." : undefined}
              onChange={(e) => { if (!isSno) onPatch({ ingressVip: e.target.value }); }}
              style={{
                flex: 1,
                minWidth: 150,
                ...(isSno ? { opacity: 0.6, cursor: "not-allowed" } : {}),
                borderColor: !isSno && vipCollision(cluster.ingressVip || "", cluster, nodes) ? "var(--pf-t--global--color--status--warning--default)" : undefined,
              }}
            />
          </div>
          {!isSno && !(cluster.ingressVip || "").trim() && (
            <div style={{ color: "var(--troshka-red, #ef4444)", fontSize: 11, marginTop: 2 }}>
              ⛔ Ingress VIP is required.
            </div>
          )}
          {!isSno && ingressVipError && <div style={{ color: "var(--pf-t--global--color--status--warning--default)", fontSize: 11, marginTop: 2 }}>{ingressVipError}</div>}
          {!isSno && vipCollision(cluster.ingressVip || "", cluster, nodes) && !ingressVipError && (
            <div style={{ color: "var(--pf-t--global--color--status--warning--default)", fontSize: 11, marginTop: 2 }}>IP in use</div>
          )}
          {!isSno && !!cluster.ingressVip && !vipInMemberSubnet(cluster.ingressVip, cluster, nodes) && (
            <div style={{ color: "var(--troshka-red, #ef4444)", fontSize: 11, marginTop: 2 }}>
              ⚠ Not in any connected network subnet — pick an IP within a member network.
            </div>
          )}
        </div>
        <div className="props-field">
          <label className="props-label">OCP Version</label>
          <select
            aria-label="OCP Version"
            className="props-select"
            value={cluster.ocpVersion || ""}
            onChange={(e) => onPatch({ ocpVersion: e.target.value })}
          >
            <option value="">Select version…</option>
            {/* Preserve a previously-set value even if it is no longer in the
                supported list (e.g. an already-deployed cluster). */}
            {cluster.ocpVersion &&
              !ocpVersions.some((v) => v.name === cluster.ocpVersion) && (
                <option value={cluster.ocpVersion}>{cluster.ocpVersion}</option>
              )}
            {ocpVersions.map((v) => (
              <option key={v.name} value={v.name}>
                {v.name} — {v.support}
              </option>
            ))}
          </select>
        </div>
      </div>
      <div className="props-divider" />
      <div className="props-section">
        <div className="props-section-title">OCP Options</div>
        {cluster.type === "sno" && (
          <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, cursor: "pointer" }}>
            <input
              type="checkbox"
              checked={!!cluster.recert}
              onChange={(e) => onPatch({ recert: e.target.checked })}
            />
            Recert (regenerate certificates, SNO only)
          </label>
        )}
        <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, cursor: "pointer", marginTop: 4 }}>
          <input
            type="checkbox"
            checked={!!cluster.monitorHealth}
            disabled={!!cluster.configureBastionBrowser}
            onChange={(e) => onPatch({ monitorHealth: e.target.checked })}
          />
          Monitor cluster health
        </label>
        {ocpInstallVia !== "pod" && (
          <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, cursor: "pointer", marginTop: 4 }}>
            <input
              type="checkbox"
              checked={!!cluster.configureBastionBrowser}
              onChange={async (e) => {
                if (!e.target.checked) {
                  onPatch({ configureBastionBrowser: false });
                  return;
                }
                // At most one cluster configures the bastion browser.
                const other = clusters.find(
                  (c) => c.id !== cluster.id && c.configureBastionBrowser,
                );
                if (other) {
                  if (!(await appConfirm({
                    message: `Move "Configure bastion browser" from ${other.name} to ${cluster.name}?`,
                  }))) return;
                  useCanvasStore.getState().updateCluster(other.id, { configureBastionBrowser: false });
                }
                onPatch({ configureBastionBrowser: true, monitorHealth: true });
              }}
            />
            Configure bastion browser for this cluster
          </label>
        )}
      </div>
    </>
  );
}

export default function PropertiesPanel() {
  const nodeId = useCanvasStore((s) => s.selectedNodeId);
  const nodes = useCanvasStore((s) => s.nodes);
  const edges = useCanvasStore((s) => s.edges);
  const vniMap = useCanvasStore((s) => s.vniMap);
  const updateNodeData = useCanvasStore((s) => s.updateNodeData);
  const clusters = useCanvasStore((s) => s.clusters);
  const updateCluster = useCanvasStore((s) => s.updateCluster);
  const deleteNode = useCanvasStore((s) => s.deleteNode);
  const projectState = useCanvasStore((s) => s.projectState);
  const panelLocked = ["deploying", "reconfiguring", "starting", "stopping"].includes(projectState);
  const [showLibraryPicker, setShowLibraryPicker] = useState<"iso" | "image" | null>(null);
  const [showPxeIsoPicker, setShowPxeIsoPicker] = useState(false);
  const [showPassword, setShowPassword] = useState(false);
  const [sshKeys, setSshKeys] = useState<SshKeyOption[]>([]);
  const [ocpVersions, setOcpVersions] = useState<Array<{ name: string; support: string }>>([]);
  const [consoleMenuOpen, setConsoleMenuOpen] = useState(false);
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({ boot: true, cloudinit: true, nics: true, disks: true, bmc: true, tags: true });
  const [containerLogs, setContainerLogs] = useState<{ containerId: string; logs: string; containerName: string } | null>(null);
  const [alertMsg, setAlertMsg] = useState<string | null>(null);
  const [wipeDiskModal, setWipeDiskModal] = useState<{ diskNodeId: string; connVmId: string; vmIsRunning: boolean; diskName: string } | null>(null);
  const [wipeDiskRestart, setWipeDiskRestart] = useState(true);
  const [wipeDiskLoading, setWipeDiskLoading] = useState(false);

  React.useEffect(() => {
    fetch("/api/v1/auth/ssh-keys")
      .then((r) => r.ok ? r.json() : [])
      .then((data) => setSshKeys(Array.isArray(data) ? data : []))
      .catch(() => {});
  }, []);

  React.useEffect(() => {
    fetch("/api/v1/ocp-versions")
      .then((r) => r.ok ? r.json() : { versions: [] })
      .then((data) => setOcpVersions(Array.isArray(data?.versions) ? data.versions : []))
      .catch(() => {});
  }, []);

  const node = nodes.find((n) => n.id === nodeId);

  if (!node) {
    return (
      <div className="canvas-properties">
        <div className="properties-empty">
          <div className="properties-empty-icon">{"🖱"}</div>
          <div className="properties-empty-title">No selection</div>
          <div className="properties-empty-hint">
            Click a node on the canvas to view and edit its properties, or drag
            a component from the palette to create one.
          </div>
        </div>
      </div>
    );
  }

  const data = node.data as Record<string, any>;
  const nodeType = node.type;

  const update = (field: string, value: unknown) => {
    updateNodeData(node.id, { [field]: value });
  };

  const isCollapsed = (key: string) => collapsed[key] ?? true;
  const toggleSection = (key: string) => setCollapsed((prev) => ({ ...prev, [key]: !prev[key] }));

  return (
    <div className="canvas-properties" style={panelLocked ? { pointerEvents: "none", opacity: 0.6 } : {}}>
      {/* Header */}
      <div className="props-header">
        <div
          className={`props-header-icon ${
            nodeType === "vmNode"
              ? "props-icon-vm"
              : nodeType === "containerNode"
                ? "props-icon-vm"
                : nodeType === "networkNode"
                  ? "props-icon-network"
                  : nodeType === "clusterNode"
                    ? "props-icon-network"
                    : "props-icon-storage"
          }`}
        >
          {nodeType === "vmNode"
            ? ((data as unknown as VMNodeData).icon || "🖥")
            : nodeType === "containerNode"
              ? "📦"
              : nodeType === "networkNode"
                ? (() => {
                    const st = (data as unknown as NetworkNodeData).subtype;
                    if (st === "router") return "🔀";
                    if (st === "gateway") return "🌐";
                    return (
                      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                        <rect x="4" y="2" width="16" height="16" rx="2" /><line x1="8" y1="18" x2="8" y2="22" /><line x1="12" y1="18" x2="12" y2="22" /><line x1="16" y1="18" x2="16" y2="22" />
                        <rect x="6" y="5" width="12" height="6" rx="1" /><line x1="9" y1="5" x2="9" y2="11" /><line x1="12" y1="5" x2="12" y2="11" /><line x1="15" y1="5" x2="15" y2="11" />
                      </svg>
                    );
                  })()
                : nodeType === "clusterNode"
                  ? "☸"
                  : ((data as unknown as StorageNodeData).format === "iso" ? "💿" : "🛢")}
        </div>
        <div>
          <div className="props-title">{data.name as string}</div>
          <div className="props-subtitle">
            {nodeType === "vmNode"
              ? `VM -- ${(data as unknown as VMNodeData).status === "running" ? "Running" : "Stopped"}`
              : nodeType === "containerNode"
                ? `Container · ${(data as unknown as ContainerNodeData).status === "running" ? "Running" : "Stopped"}`
                : nodeType === "networkNode"
                  ? "Network"
                  : nodeType === "clusterNode"
                    ? `OpenShift Cluster -- ${(data.type as string) || "standard"}`
                    : "Storage"}
          </div>
        </div>
      </div>

      <div className="props-divider" />

      {/* VM Properties */}
      {nodeType === "vmNode" && (
        <>
          <div className="props-section">
            <div className="props-section-title" style={{ cursor: "pointer", display: "flex", alignItems: "center", gap: 6 }} onClick={() => toggleSection("general")}>
              <span style={{ fontSize: 8, transition: "transform 0.15s", transform: isCollapsed("general") ? "rotate(-90deg)" : "rotate(0)" }}>&#9660;</span>
              General
            </div>
            {!isCollapsed("general") && (<>
              <div className="props-field">
                <label className="props-label">Name</label>
                <input
                  className="props-input"
                  value={(data.name as string) || ""}
                  onChange={(e) => update("name", e.target.value)}
                  style={isDuplicateName((data.name as string) || "", node.id, "vmNode") ? { borderColor: "var(--pf-t--global--color--status--warning--default)" } : undefined}
                />
                {isDuplicateName((data.name as string) || "", node.id, "vmNode") && (
                  <div style={{ color: "var(--pf-t--global--color--status--warning--default)", fontSize: 11, marginTop: 2 }}>Duplicate VM name</div>
                )}
              </div>
              <div className="props-field">
                <label className="props-label" style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <input
                    type="checkbox"
                    checked={(() => {
                      const entry = useCanvasStore.getState().startOrder.find((e) => e.vmId === node.id);
                      return entry ? entry.autoStart : true;
                    })()}
                    onChange={(e) => {
                      const store = useCanvasStore.getState();
                      const order = [...store.startOrder];
                      const idx = order.findIndex((o) => o.vmId === node.id);
                      if (idx >= 0) {
                        order[idx] = { ...order[idx], autoStart: e.target.checked };
                      } else {
                        order.push({ vmId: node.id, autoStart: e.target.checked, waitForVm: null, waitForService: "", waitForPort: "", delaySeconds: 0 });
                      }
                      store.setStartOrder(order);
                    }}
                  />
                  Power on at deploy
                </label>
              </div>
              <div className="props-field">
                <label className="props-label">OS Type</label>
                <select
                  className="props-select"
                  value={(data as unknown as VMNodeData).os}
                  onChange={(e) => update("os", e.target.value)}
                >
                  <optgroup label="Red Hat Enterprise Linux">
                    <option value="rhel10">RHEL 10</option>
                    <option value="rhel9">RHEL 9</option>
                    <option value="rhel8">RHEL 8</option>
                    <option value="rhel7">RHEL 7</option>
                  </optgroup>
                  <optgroup label="CentOS / Alma / Rocky">
                    <option value="centos-stream10">CentOS Stream 10</option>
                    <option value="centos-stream9">CentOS Stream 9</option>
                    <option value="almalinux9">AlmaLinux 9</option>
                    <option value="rocky9">Rocky Linux 9</option>
                  </optgroup>
                  <optgroup label="Fedora">
                    <option value="fedora42">Fedora 42</option>
                    <option value="fedora41">Fedora 41</option>
                    <option value="fedora40">Fedora 40</option>
                  </optgroup>
                  <optgroup label="Ubuntu">
                    <option value="ubuntu24.04">Ubuntu 24.04 LTS</option>
                    <option value="ubuntu22.04">Ubuntu 22.04 LTS</option>
                  </optgroup>
                  <optgroup label="Debian">
                    <option value="debian12">Debian 12</option>
                    <option value="debian11">Debian 11</option>
                  </optgroup>
                  <optgroup label="SUSE">
                    <option value="sles15">SLES 15</option>
                    <option value="opensuse15.5">openSUSE Leap 15.5</option>
                  </optgroup>
                  <optgroup label="Windows">
                    <option value="win2k25">Windows Server 2025</option>
                    <option value="win2k22">Windows Server 2022</option>
                    <option value="win2k19">Windows Server 2019</option>
                    <option value="win11">Windows 11</option>
                    <option value="win10">Windows 10</option>
                  </optgroup>
                  <optgroup label="Other">
                    <option value="rhcos">Red Hat CoreOS</option>
                    <option value="generic">Generic OS</option>
                  </optgroup>
                </select>
              </div>
            </>)}
          </div>
          <div className="props-divider" />

          <div className="props-section">
            <div className="props-section-title" style={{ cursor: "pointer", display: "flex", alignItems: "center", gap: 6 }} onClick={() => toggleSection("compute")}>
              <span style={{ fontSize: 8, transition: "transform 0.15s", transform: isCollapsed("compute") ? "rotate(-90deg)" : "rotate(0)" }}>&#9660;</span>
              Compute
            </div>
            {!isCollapsed("compute") && (<><div className="props-row">
              <div className="props-field">
                <label className="props-label">vCPUs</label>
                <input
                  className="props-input"
                  type="number"
                  min={1}
                  max={64}
                  value={(data as unknown as VMNodeData).vcpus}
                  onFocus={(e) => e.target.select()}
                  onChange={(e) =>
                    update("vcpus", parseInt(e.target.value) || 1)
                  }
                />
              </div>
              <div className="props-field">
                <label className="props-label">RAM (GB)</label>
                <input
                  className="props-input"
                  type="number"
                  min={1}
                  max={512}
                  value={(data as unknown as VMNodeData).ram}
                  onFocus={(e) => e.target.select()}
                  onChange={(e) =>
                    update("ram", parseInt(e.target.value) || 1)
                  }
                />
              </div>
            </div>
            <div className="props-field">
              <label className="props-label">Affinity Group</label>
              <input
                className="props-input"
                type="text"
                placeholder="none"
                value={(data as Record<string, any>).affinityGroup as string || ""}
                onChange={(e) => update("affinityGroup", e.target.value || undefined)}
              />
            </div>
            <div className="props-field">
              <label className="props-label">Anti-Affinity Group</label>
              <input
                className="props-input"
                type="text"
                placeholder="none"
                value={(data as Record<string, any>).separateHost as string || ""}
                onChange={(e) => update("separateHost", e.target.value || undefined)}
              />
            </div>
            {useCanvasStore.getState().providerType === "kubevirt" && (
              <div className="props-field">
                <label className="props-label" style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <input
                    type="checkbox"
                    checked={Boolean((data as Record<string, any>).nestedVirt)}
                    onChange={(e) => update("nestedVirt", e.target.checked || undefined)}
                  />
                  Nested virtualization
                  <HintIcon text="Expose the host CPU (host-passthrough) so this VM can run its own VMs. Pins the VM to compatible CPUs and disables live migration. Requires nested virt enabled on the cluster nodes." />
                </label>
              </div>
            )}
            </>)}
          </div>
          <div className="props-divider" />

          <div className="props-section">
            <div className="props-section-title" style={{ cursor: "pointer", display: "flex", alignItems: "center", gap: 6 }} onClick={() => toggleSection("boot")}>
              <span style={{ fontSize: 8, transition: "transform 0.15s", transform: isCollapsed("boot") ? "rotate(-90deg)" : "rotate(0)" }}>&#9660;</span>
              Boot
            </div>
            {!isCollapsed("boot") && <><div className="props-field">
              <label className="props-label">Firmware</label>
              <select
                className="props-select"
                value={(data as Record<string, any>).firmware as string || "bios"}
                onChange={(e) => {
                  update("firmware", e.target.value);
                  if (e.target.value === "bios") update("secureBoot", false);
                }}
              >
                <option value="bios">BIOS (SeaBIOS)</option>
                <option value="uefi">UEFI (OVMF)</option>
              </select>
            </div>
            {(data as Record<string, any>).firmware === "uefi" && (
              <div className="props-field">
                <label className="props-label" style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <input
                    type="checkbox"
                    checked={(data as Record<string, any>).secureBoot as boolean ?? false}
                    onChange={(e) => update("secureBoot", e.target.checked)}
                  />
                  Secure Boot
                </label>
              </div>
            )}
            {(() => {
              const providerType = useCanvasStore.getState().providerType;
              if (providerType !== "ocpvirt") return null;
              const clusterCapabilities = useCanvasStore.getState().clusterCapabilities;
              const machineTypes = allowedMachineTypes(clusterCapabilities, providerType);
              const currentMachineType = (data as Record<string, any>).machineType as string || "q35";
              const effectiveMachineType = machineTypes.includes(currentMachineType)
                ? currentMachineType
                : (machineTypes[0] || "q35");
              return (
              <div className="props-field">
                <label className="props-label">Machine Type</label>
                <select
                  className="props-select"
                  value={effectiveMachineType}
                  onChange={(e) => update("machineType", e.target.value)}
                >
                  {machineTypes.map((mt) => (
                    <option key={mt} value={mt}>{MACHINE_TYPE_LABELS[mt] || mt}</option>
                  ))}
                </select>
                {!machineTypes.includes(currentMachineType) && currentMachineType && (
                  <span style={{ fontSize: 10, color: "var(--troshka-red)", display: "block", marginTop: 4 }}>
                    {MACHINE_TYPE_LABELS[currentMachineType] || currentMachineType} is not supported on this cluster.
                  </span>
                )}
              </div>
              );
            })()}
            {(node.data as Record<string, any>).liveBootDevs && (
              <div style={{ background: "rgba(168,85,247,0.1)", border: "1px solid rgba(168,85,247,0.3)", borderRadius: 6, padding: "6px 8px", marginBottom: 8, fontSize: 11 }}>
                <label className="props-label" style={{ color: "rgba(168,85,247,0.9)", fontSize: 10 }}>BMC Live Boot Order</label>
                {((node.data as Record<string, any>).liveBootDevs as string[]).map((dev, i) => {
                  const labels: Record<string, string> = { hd: "Hard Disk", network: "Network (PXE)", cdrom: "CD-ROM" };
                  return <div key={i} style={{ fontFamily: "monospace" }}>{i + 1}. {labels[dev] || dev}</div>;
                })}
              </div>
            )}
            <div className="props-field">
              <label className="props-label">Boot Order</label>
              {(() => {
                // Find connected bootable disks/ISOs
                const connectedStorageIds = edges
                  .filter((e) => e.source === node.id || e.target === node.id)
                  .map((e) => e.source === node.id ? e.target : e.source)
                  .filter((nid) => nodes.some((n) => n.id === nid && n.type === "storageNode"));

                const bootableDisks = connectedStorageIds
                  .map((sid) => nodes.find((n) => n.id === sid))
                  .filter((n) => n && (n.data as Record<string, any>).bootable !== false)
                  .map((n) => ({
                    id: n!.id,
                    name: (n!.data as Record<string, any>).name as string,
                    format: (n!.data as Record<string, any>).format as string,
                    size: (n!.data as Record<string, any>).size as number,
                    type: (n!.data as Record<string, any>).format === "iso" ? "cdrom" as const : "disk" as const,
                  }));

                let bootDevices = (data as Record<string, any>).bootDevices as string[] | null;
                if (!bootDevices) {
                  bootDevices = [...bootableDisks.map((d) => d.id), "network"];
                  setTimeout(() => update("bootDevices", bootDevices!), 0);
                }

                // Build available options: connected bootable disks + network
                const options: { value: string; label: string }[] = bootableDisks.map((d) => ({
                  value: d.id,
                  label: `${d.type === "cdrom" ? "💿" : "🛢"} ${d.name} (${d.size} GB ${d.format})`,
                }));
                options.push({ value: "network", label: "🔌 Network (PXE)" });

                // Filter boot devices to only valid options
                const validDevices = bootDevices.filter((d) => options.some((o) => o.value === d));

                return (
                  <>
                    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                      {validDevices.map((dev, i) => (
                        <div key={i} style={{ display: "flex", alignItems: "center", gap: 6 }}>
                          <span style={{ fontSize: 11, color: "var(--troshka-accent)", fontWeight: 700, width: 16 }}>{i + 1}.</span>
                          <div
                            className="props-select"
                            style={{ flex: 1, fontSize: 12, display: "flex", alignItems: "center", gap: 4, padding: "4px 8px" }}
                          >
                            {dev === "network" ? (
                              <>
                                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><rect x="4" y="2" width="16" height="16" rx="2" /><line x1="8" y1="18" x2="8" y2="22" /><line x1="12" y1="18" x2="12" y2="22" /><line x1="16" y1="18" x2="16" y2="22" /><rect x="6" y="5" width="12" height="6" rx="1" /><line x1="9" y1="5" x2="9" y2="11" /><line x1="12" y1="5" x2="12" y2="11" /><line x1="15" y1="5" x2="15" y2="11" /></svg>
                                Network (PXE)
                              </>
                            ) : (
                              <>{options.find((o) => o.value === dev)?.label || dev}</>
                            )}
                          </div>
                          <button
                            style={{ background: "none", border: "none", color: "var(--troshka-text-dim)", cursor: "pointer", fontSize: 14 }}
                            title="Move up"
                            onClick={() => {
                              if (i === 0) return;
                              const updated = [...validDevices];
                              [updated[i - 1], updated[i]] = [updated[i], updated[i - 1]];
                              update("bootDevices", updated);
                            }}
                          >↑</button>
                          <button
                            style={{ background: "none", border: "none", color: "var(--troshka-text-dim)", cursor: "pointer", fontSize: 14 }}
                            title="Move down"
                            onClick={() => {
                              if (i === validDevices.length - 1) return;
                              const updated = [...validDevices];
                              [updated[i], updated[i + 1]] = [updated[i + 1], updated[i]];
                              update("bootDevices", updated);
                            }}
                          >↓</button>
                          <button
                            style={{ background: "none", border: "none", color: "var(--troshka-red)", cursor: "pointer", fontSize: 12 }}
                            title="Remove from boot order"
                            onClick={() => update("bootDevices", validDevices.filter((_, idx) => idx !== i))}
                          >✕</button>
                        </div>
                      ))}
                    </div>
                    {validDevices.length < options.length && (
                      <button
                        className="props-library-btn"
                        style={{ marginTop: 6 }}
                        onClick={() => {
                          const unused = options.filter((o) => !validDevices.includes(o.value));
                          if (unused.length > 0) update("bootDevices", [...validDevices, unused[0].value]);
                        }}
                      >
                        + Add Boot Device
                      </button>
                    )}
                    {bootableDisks.length === 0 && (
                      <span style={{ fontSize: 11, color: "var(--troshka-yellow)", marginTop: 4, display: "block" }}>
                        ⚠ No bootable disks connected. Attach a storage device.
                      </span>
                    )}
                  </>
                );
              })()}
            </div>
            {((data as Record<string, any>).bootDevices as string[] || []).includes("network") && (() => {
              const pxeMode = (data as Record<string, any>).pxeServerMode as string || "builtin";
              const pxeMethod = (data as Record<string, any>).pxeMethod as string || "legacy";
              return (
                <>
                  {pxeMode === "builtin" ? (
                    <div className="props-field">
                      <label className="props-label">Network Boot ISO</label>
                      <button
                        className="props-library-btn"
                        onClick={() => setShowPxeIsoPicker(true)}
                      >
                        📚 Select Install ISO...
                      </button>
                      {(data as Record<string, any>).pxeBootIsoName ? (
                        <div style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 4 }}>
                          <span style={{ fontSize: 12, color: "var(--troshka-green)" }}>
                            💿 {(data as Record<string, any>).pxeBootIsoName as string}
                          </span>
                          <button
                            style={{ background: "none", border: "none", color: "var(--troshka-red)", cursor: "pointer", fontSize: 11 }}
                            onClick={() => { update("pxeBootIsoId", undefined); update("pxeBootIsoName", undefined); }}
                          >✕</button>
                        </div>
                      ) : (
                        <span style={{ fontSize: 10, color: "var(--troshka-text-dim)", marginTop: 4, display: "block" }}>
                          Select an install ISO for PXE network boot. The kernel and initrd will be extracted and served automatically.
                        </span>
                      )}
                    </div>
                  ) : (
                    <>
                      <div className="props-field">
                        <label className="props-label">Boot Method</label>
                        <select
                          className="props-select"
                          value={pxeMethod}
                          onChange={(e) => update("pxeMethod", e.target.value)}
                        >
                          <option value="legacy">Legacy PXE (TFTP)</option>
                          <option value="ipxe">iPXE (HTTP)</option>
                          <option value="uefi-http">UEFI HTTP Boot</option>
                        </select>
                      </div>
                      {pxeMethod === "legacy" && (
                        <>
                          <div className="props-field">
                            <label className="props-label">Next Server (TFTP)</label>
                            <input className="props-input" value={(data as Record<string, any>).pxeNextServer as string || ""} onChange={(e) => update("pxeNextServer", e.target.value)} placeholder="TFTP server IP" style={{ fontFamily: "monospace" }} />
                          </div>
                          <div className="props-field">
                            <label className="props-label">Boot Filename</label>
                            <input className="props-input" value={(data as Record<string, any>).pxeBootFile as string || ""} onChange={(e) => update("pxeBootFile", e.target.value)} placeholder="pxelinux.0" style={{ fontFamily: "monospace" }} />
                          </div>
                        </>
                      )}
                      {pxeMethod === "ipxe" && (
                        <div className="props-field">
                          <label className="props-label">iPXE Script URL</label>
                          <input className="props-input" value={(data as Record<string, any>).ipxeScriptUrl as string || ""} onChange={(e) => update("ipxeScriptUrl", e.target.value)} placeholder="http://10.0.0.1/boot.ipxe" style={{ fontFamily: "monospace" }} />
                        </div>
                      )}
                      {pxeMethod === "uefi-http" && (
                        <div className="props-field">
                          <label className="props-label">Boot URL</label>
                          <input className="props-input" value={(data as Record<string, any>).uefiBootUrl as string || ""} onChange={(e) => update("uefiBootUrl", e.target.value)} placeholder="http://10.0.0.1/boot/grubx64.efi" style={{ fontFamily: "monospace" }} />
                        </div>
                      )}
                    </>
                  )}
                  {(data as unknown as VMNodeData).cloudInit && (() => {
                    const devs = (data as Record<string, any>).bootDevices as string[] || [];
                    const netIdx = devs.indexOf("network");
                    const diskIdx = devs.findIndex((d) => d !== "network");
                    const netFirst = netIdx >= 0 && (diskIdx < 0 || netIdx < diskIdx);
                    return netFirst ? (
                      <span style={{ fontSize: 10, color: "var(--troshka-yellow)", display: "block", marginTop: 2 }}>
                        ⚠ Network boot is before disk — the VM will PXE boot again after the installer reboots. Move a disk above network in the boot order, or use a kickstart that sets the local disk as the boot target.
                      </span>
                    ) : null;
                  })()}
                </>
              );
            })()}
            </>}
          </div>
          <div className="props-divider" />

          <div className="props-section">
            <div className="props-section-title" style={{ cursor: "pointer", display: "flex", alignItems: "center", gap: 6 }} onClick={() => toggleSection("identity")}>
              <span style={{ fontSize: 8, transition: "transform 0.15s", transform: isCollapsed("identity") ? "rotate(-90deg)" : "rotate(0)" }}>&#9660;</span>
              Identity
            </div>
            {!isCollapsed("identity") && <>
            <div className="props-field">
              <label className="props-label">SMBIOS UUID</label>
              {(() => {
                const explicit = (data as Record<string, any>).smbiosUuid as string || "";
                const deployed = (data as Record<string, any>).domainUuid as string || "";
                const display = explicit || deployed;
                return (<>
                  <div style={{ display: "flex", gap: 4 }}>
                    <input
                      className="props-input"
                      value={display}
                      onChange={(e) => update("smbiosUuid", e.target.value || undefined)}
                      placeholder="auto-generated at deploy"
                      style={{ fontFamily: "monospace", fontSize: 11, flex: 1, ...(!explicit && deployed ? { opacity: 0.6 } : {}) }}
                    />
                    {display ? (
                      <button
                        style={{ background: "none", border: "1px solid var(--troshka-border)", borderRadius: 4, color: "var(--troshka-text-dim)", cursor: "pointer", padding: "2px 6px", fontSize: 10, flexShrink: 0 }}
                        onClick={() => update("smbiosUuid", undefined)}
                        title="Clear UUID (will be auto-generated at deploy)"
                      >&#x2715;</button>
                    ) : (
                      <button
                        style={{ background: "none", border: "1px solid var(--troshka-border)", borderRadius: 4, color: "var(--troshka-accent)", cursor: "pointer", padding: "2px 6px", fontSize: 10, flexShrink: 0 }}
                        onClick={() => update("smbiosUuid", crypto.randomUUID())}
                        title="Generate UUID"
                      >Gen</button>
                    )}
                  </div>
                  <span style={{ fontSize: 10, color: "var(--troshka-text-dim)", display: "block", marginTop: 2 }}>
                    {!explicit && deployed ? "Current UUID from deploy. Edit to override." : "Hardware UUID exposed to the guest OS via SMBIOS. Leave empty for auto-generated."}
                  </span>
                </>);
              })()}
            </div>
            </>}
          </div>
          <div className="props-divider" />

          <div className="props-section">
            <div className="props-section-title" style={{ cursor: "pointer", display: "flex", alignItems: "center", gap: 6 }} onClick={() => toggleSection("io")}>
              <span style={{ fontSize: 8, transition: "transform 0.15s", transform: isCollapsed("io") ? "rotate(-90deg)" : "rotate(0)" }}>&#9660;</span>
              I/O
            </div>
            {!isCollapsed("io") && (() => {
              const providerType = useCanvasStore.getState().providerType;
              const clusterCapabilities = useCanvasStore.getState().clusterCapabilities;
              const videoModels = allowedVideoModels(clusterCapabilities, providerType);
              const currentVideo = (data as Record<string, any>).videoModel as string || "virtio";
              return (<>
            <div className="props-field">
              <label className="props-label">Video</label>
              {videoModels.length > 0 ? (
                <select
                  className="props-select"
                  value={videoModels.includes(currentVideo) ? currentVideo : videoModels[0]}
                  onChange={(e) => update("videoModel", e.target.value)}
                >
                  {videoModels.map((model) => (
                    <option key={model} value={model}>{VIDEO_MODEL_LABELS[model] || model}</option>
                  ))}
                </select>
              ) : (
                <>
                  <select className="props-select" value="default" disabled>
                    <option value="default">Default (VGA)</option>
                  </select>
                  <span style={{ fontSize: 10, color: "var(--troshka-text-dim)", display: "block", marginTop: 4 }}>
                    VideoConfig is disabled on this cluster; explicit video models are ignored.
                  </span>
                </>
              )}
            </div>
            <div className="props-field">
              <label className="props-label">Input</label>
              <select
                className="props-select"
                value={(data as Record<string, any>).inputModel as string || "virtio"}
                onChange={(e) => update("inputModel", e.target.value)}
              >
                <option value="virtio">VirtIO (recommended)</option>
                <option value="usb">USB</option>
                <option value="ps2">PS/2</option>
              </select>
            </div>
            <div className="props-field">
              <label className="props-label">Serial</label>
              <select
                className="props-select"
                value={(data as Record<string, any>).serialModel as string || "isa"}
                onChange={(e) => update("serialModel", e.target.value)}
              >
                <option value="virtio">VirtIO</option>
                <option value="isa">ISA</option>
              </select>
            </div>
            <div className="props-field">
              <label className="props-label" style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <input
                  type="checkbox"
                  checked={(data as Record<string, any>).serialConsole !== false}
                  onChange={(e) => update("serialConsole", e.target.checked)}
                />
                Serial Console
              </label>
            </div>
            <div className="props-field">
              <label className="props-label">Serial Exec CLI</label>
              <select
                className="props-select"
                value={(data as Record<string, any>).serialExecType as string || "linux"}
                onChange={(e) => update("serialExecType", e.target.value)}
              >
                <option value="linux">Linux / cloud-init</option>
                <option value="ios">Cisco IOS-XE</option>
                <option value="eos">Arista EOS</option>
                <option value="junos">Juniper Junos</option>
              </select>
            </div>
            {(providerType === "ocpvirt" || providerType === "kubevirt") && (
              <div className="props-field">
                <label className="props-label" style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <input
                    type="checkbox"
                    checked={Boolean((data as Record<string, any>).legacyRootBus)}
                    onChange={(e) => update("legacyRootBus", e.target.checked || undefined)}
                  />
                  Legacy root PCI bus (NICs)
                  <HintIcon text="Place NICs on bus 00:xx (slots 03+) instead of PCIe root ports." />
                </label>
              </div>
            )}</>);
            })()}
          </div>
          <div className="props-divider" />

          <div className="props-section">
            <div className="props-section-title" style={{ cursor: "pointer", display: "flex", alignItems: "center", gap: 6 }} onClick={() => toggleSection("cloudinit")}>
              <span style={{ fontSize: 8, transition: "transform 0.15s", transform: isCollapsed("cloudinit") ? "rotate(-90deg)" : "rotate(0)" }}>&#9660;</span>
              Cloud-Init
            </div>
            {!isCollapsed("cloudinit") && (<><div className="props-field">
              <label className="props-label" style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <input
                  type="checkbox"
                  checked={(data as unknown as VMNodeData).cloudInit ?? false}
                  onChange={(e) => update("cloudInit", e.target.checked)}
                />
                Enabled
              </label>
            </div>
            {(data as unknown as VMNodeData).cloudInit && useCanvasStore.getState().deployedVmIds.has(node.id) && (
              <span style={{ fontSize: 10, color: "var(--troshka-text-dim)", display: "block", marginTop: 4 }}>
                Cloud-init runs on first boot only. Changes here require Republish to take effect.
              </span>
            )}
            {(data as unknown as VMNodeData).cloudInit && (
              <>
                <div className="props-field">
                  <label className="props-label">Hostname</label>
                  <input className="props-input" value={(data as Record<string, any>).ciHostname as string || ""} onChange={(e) => update("ciHostname", e.target.value)} placeholder={`${(data as unknown as VMNodeData).name}`} />
                </div>
                <div className="props-field">
                  <label className="props-label">root password</label>
                  <div style={{ display: "flex", gap: 4 }}>
                    <input className="props-input" style={{ flex: 1, WebkitTextSecurity: showPassword ? "none" : "disc" } as React.CSSProperties} type="text" autoComplete="off" value={(data as Record<string, any>).ciRootPassword as string || ""} onChange={(e) => update("ciRootPassword", e.target.value)} placeholder="Leave blank for key-only auth" />
                    <button onClick={() => setShowPassword(!showPassword)} style={{ background: "none", border: "none", cursor: "pointer", fontSize: 14, padding: "0 4px" }} title={showPassword ? "Hide" : "Show"}>
                      {showPassword ? "🙈" : "👁"}
                    </button>
                  </div>
                </div>
                <div className="props-field">
                  <label className="props-label">cloud-user password</label>
                  <div style={{ display: "flex", gap: 4 }}>
                    <input className="props-input" style={{ flex: 1, WebkitTextSecurity: showPassword ? "none" : "disc" } as React.CSSProperties} type="text" autoComplete="off" value={(data as Record<string, any>).ciCloudUserPassword as string || ""} onChange={(e) => update("ciCloudUserPassword", e.target.value)} placeholder="Leave blank for key-only auth" />
                    <button onClick={() => setShowPassword(!showPassword)} style={{ background: "none", border: "none", cursor: "pointer", fontSize: 14, padding: "0 4px" }} title={showPassword ? "Hide" : "Show"}>
                      {showPassword ? "🙈" : "👁"}
                    </button>
                  </div>
                </div>
                <div className="props-field">
                  <label className="props-label" style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <input
                      type="checkbox"
                      checked={(data as Record<string, any>).ciCloudUserSudo as boolean ?? true}
                      onChange={(e) => update("ciCloudUserSudo", e.target.checked)}
                    />
                    cloud-user has sudo
                  </label>
                </div>
                <div className="props-field">
                  <label className="props-label">SSH Keys <span style={{ fontSize: 10, opacity: 0.6 }}>(injected for root + cloud-user)</span></label>
                  {sshKeys.length > 0 ? (
                    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                      {sshKeys.map((k) => {
                        const selectedIds: number[] = (data as Record<string, any>).ciSshKeyIds as number[] || [];
                        const isSelected = selectedIds.includes(k.id);
                        return (
                          <label key={k.id} style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, cursor: "pointer" }}>
                            <input type="checkbox" checked={isSelected} onChange={() => {
                              const newIds = isSelected ? selectedIds.filter((id) => id !== k.id) : [...selectedIds, k.id];
                              const newKeys = sshKeys.filter((sk) => newIds.includes(sk.id)).map((sk) => sk.public_key);
                              update("ciSshKeyIds", newIds);
                              update("ciSshKeys", newKeys);
                            }} />
                            {k.name}
                          </label>
                        );
                      })}
                    </div>
                  ) : (
                    <span style={{ fontSize: 11, color: "var(--troshka-text-dim)" }}>No SSH keys configured. Add one in Settings.</span>
                  )}
                </div>
                <div className="props-field">
                  <label className="props-label">Custom User-Data (YAML)</label>
                  <textarea className="props-input" style={{
                    minHeight: 60, fontFamily: "monospace", fontSize: 11,
                    borderColor: (() => {
                      const val = ((data as Record<string, any>).ciUserData as string || "").trim();
                      if (!val) return undefined;
                      try {
                        const jsYaml = require("js-yaml");
                        const parsed = jsYaml.load(val);
                        return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? undefined : "var(--troshka-red)";
                      } catch { return "var(--troshka-red)"; }
                    })(),
                  }} value={(data as Record<string, any>).ciUserData as string || ""} onChange={(e) => update("ciUserData", e.target.value)} placeholder="#cloud-config&#10;packages:&#10;  - vim" />
                  {(() => {
                    const val = ((data as Record<string, any>).ciUserData as string || "").trim();
                    if (!val) return null;
                    try {
                      const jsYaml = require("js-yaml");
                      const parsed = jsYaml.load(val);
                      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) return null;
                    } catch { /* fall through */ }
                    return (
                      <span style={{ fontSize: 10, color: "var(--troshka-red)" }}>Invalid YAML — must be cloud-config key-value pairs</span>
                    );
                  })()}
                </div>
              </>
            )}
            </>)}
          </div>
          <div className="props-divider" />

          <div className="props-section">
            <div className="props-section-title" style={{ cursor: "pointer", display: "flex", alignItems: "center", gap: 6 }} onClick={() => toggleSection("nics")}>
              <span style={{ fontSize: 8, transition: "transform 0.15s", transform: isCollapsed("nics") ? "rotate(-90deg)" : "rotate(0)" }}>&#9660;</span>
              Network Interfaces
            </div>
            {!isCollapsed("nics") && (() => {
              let nics = ((data as unknown as VMNodeData).nics || []) as Array<{id: string; name: string; mac: string; model: string; ip?: string}>;
              if (nics.length === 0) {
                nics = [{ id: generateNicId(), name: "eth0", mac: generateMac(), model: "virtio" }];
                update("nics", nics);
              }
              return (
                <>
                  {nics.map((nic, i) => (
                    <div key={nic.id} style={{ background: "var(--troshka-surface2)", borderRadius: 6, padding: 8, marginBottom: 6 }}>
                      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 4 }}>
                        <input
                          className="props-input"
                          value={nic.name || `eth${i}`}
                          onChange={(e) => { const updated = [...nics]; updated[i] = { ...nic, name: e.target.value }; update("nics", updated); }}
                          style={{ fontSize: 12, fontWeight: 600, background: "transparent", border: "none", padding: 0, width: 80 }}
                        />
                        {nics.length > 1 && (
                          <button
                            style={{ background: "none", border: "none", color: "var(--troshka-red)", cursor: "pointer", fontSize: 12 }}
                            onClick={() => update("nics", nics.filter((_, idx) => idx !== i))}
                          >✕</button>
                        )}
                      </div>
                      <div className="props-field" style={{ marginBottom: 4 }}>
                        <label className="props-label">Model</label>
                        <select className="props-select" value={nic.model || "virtio"} onChange={(e) => {
                          const updated = [...nics]; updated[i] = { ...nic, model: e.target.value }; update("nics", updated);
                        }}>
                          <option value="virtio">virtio</option>
                          <option value="igb">igb (SR-IOV)</option>
                          <option value="e1000e">e1000e</option>
                          <option value="e1000">e1000</option>
                          <option value="rtl8139">rtl8139</option>
                          <option value="vmxnet3">vmxnet3</option>
                        </select>
                      </div>
                      <div className="props-field">
                        <label className="props-label">MAC Address</label>
                        <input className="props-input" value={nic.mac} style={{ fontFamily: "monospace", fontSize: 11 }} onChange={(e) => {
                          const updated = [...nics]; updated[i] = { ...nic, mac: e.target.value }; update("nics", updated);
                        }} />
                      </div>
                      {(() => {
                        const nicHandleTop = `nic-${nic.id}-top`;
                        const nicHandleBottom = `nic-${nic.id}-bottom`;
                        const netEdge = edges.find((e) =>
                          (e.source === node!.id && (e.sourceHandle === nicHandleTop || e.sourceHandle === nicHandleBottom)) ||
                          (e.target === node!.id && (e.targetHandle === nicHandleTop || e.targetHandle === nicHandleBottom))
                        );
                        const netNode = netEdge ? nodes.find((n) => n.id === (netEdge.source === node!.id ? netEdge.target : netEdge.source) && n.type === "networkNode") : null;
                        const netCidr = netNode ? (netNode.data as Record<string, any>).cidr as string : "";
                        const nicIp = (nic as Record<string, any>).ip as string || "";
                        const ipInCidr = (ip: string, cidr: string) => {
                          if (!ip || !cidr) return true;
                          const [netAddr, bits] = cidr.split("/");
                          if (!netAddr || !bits) return true;
                          const ipParts = ip.split(".").map(Number);
                          const netParts = netAddr.split(".").map(Number);
                          if (ipParts.length !== 4 || ipParts.some(isNaN)) return false;
                          const mask = ~((1 << (32 - parseInt(bits))) - 1) >>> 0;
                          const ipNum = ((ipParts[0] << 24) | (ipParts[1] << 16) | (ipParts[2] << 8) | ipParts[3]) >>> 0;
                          const netNum = ((netParts[0] << 24) | (netParts[1] << 16) | (netParts[2] << 8) | netParts[3]) >>> 0;
                          return (ipNum & mask) === (netNum & mask);
                        };
                        const ipValid = !nicIp || ipInCidr(nicIp, netCidr);
                        const ipConflict = nicIp && netNode ? (() => {
                          const and = netNode.data as Record<string, any>;
                          const gwIp = (and.dhcpGateway as string) || (netCidr ? netCidr.replace(/\.\d+\/\d+$/, ".1") : "");
                          if (gwIp && gwIp === nicIp) return "gateway IP";
                          if (and.dnsServerIp === nicIp) return "DNS server IP";
                          for (const n of nodes) {
                            if (n.type !== "vmNode") continue;
                            const vmNics = ((n.data as Record<string, any>).nics || []) as Array<Record<string, unknown>>;
                            for (const otherNic of vmNics) {
                              if (n.id === node!.id && otherNic.id === nic.id) continue;
                              if (otherNic.ip === nicIp) return n.data.name as string;
                            }
                          }
                          return null;
                        })() : null;
                        const ipDuplicate = ipConflict;
                        const hasError = nicIp && (!ipValid || ipDuplicate);
                        return netNode ? (
                          <div className="props-field">
                            <label className="props-label">IP Address {netCidr ? `(${netCidr})` : ""}</label>
                            <input
                              className="props-input"
                              value={nicIp}
                              placeholder="DHCP (auto)"
                              style={{ fontFamily: "monospace", fontSize: 11, borderColor: hasError ? "var(--troshka-red)" : undefined }}
                              onChange={(e) => {
                                const updated = [...nics]; updated[i] = { ...nic, ip: e.target.value }; update("nics", updated);
                              }}
                            />
                            {nicIp && !ipValid && (
                              <span style={{ fontSize: 10, color: "var(--troshka-red)" }}>IP not in {netCidr}</span>
                            )}
                            {nicIp && ipValid && ipDuplicate && (
                              <span style={{ fontSize: 10, color: "var(--troshka-red)" }}>Already used by {ipDuplicate}</span>
                            )}
                          </div>
                        ) : null;
                      })()}
                    </div>
                  ))}
                  {nics.length < 8 && (
                    <button className="props-library-btn" onClick={() => {
                      update("nics", [...nics, { id: generateNicId(), name: `eth${nics.length}`, mac: generateMac(), model: "virtio" }]);
                    }}>+ Add NIC ({nics.length}/8)</button>
                  )}
                  {nics.length >= 8 && (
                    <span style={{ fontSize: 11, color: "var(--troshka-text-dim)" }}>Maximum 8 NICs reached</span>
                  )}
                </>
              );
            })()}
          </div>
          <div className="props-divider" />

          <div className="props-section">
            <div className="props-section-title" style={{ cursor: "pointer", display: "flex", alignItems: "center", gap: 6 }} onClick={() => toggleSection("disks")}>
              <span style={{ fontSize: 8, transition: "transform 0.15s", transform: isCollapsed("disks") ? "rotate(-90deg)" : "rotate(0)" }}>&#9660;</span>
              Disk Controllers
            </div>
            {!isCollapsed("disks") && (() => {
              const providerType = useCanvasStore.getState().providerType;
              const clusterCapabilities = useCanvasStore.getState().clusterCapabilities;
              const diskBuses = allowedDiskBuses(clusterCapabilities, providerType);
              let ports = ((data as unknown as VMNodeData).diskControllers || []) as Array<{id: string; name: string; bus: string; rotationRate?: number}>;
              if (ports.length === 0) {
                ports = [{ id: generateDiskControllerId(), name: "disk0", bus: "virtio" }];
                update("diskControllers", ports);
              }
              return (
                <>
                  {ports.map((port, i) => (
                    <div key={port.id} style={{ background: "var(--troshka-surface2)", borderRadius: 6, padding: 8, marginBottom: 6 }}>
                      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 4 }}>
                        <input
                          className="props-input"
                          value={port.name || `disk${i}`}
                          onChange={(e) => { const updated = [...ports]; updated[i] = { ...port, name: e.target.value }; update("diskControllers", updated); }}
                          style={{ fontSize: 12, fontWeight: 600, background: "transparent", border: "none", padding: 0, width: 80 }}
                        />
                        {ports.length > 1 && (
                          <button
                            style={{ background: "none", border: "none", color: "var(--troshka-red)", cursor: "pointer", fontSize: 12 }}
                            onClick={() => update("diskControllers", ports.filter((_, idx) => idx !== i))}
                          >✕</button>
                        )}
                      </div>
                      <div className="props-field">
                        <label className="props-label">Bus</label>
                        <select className="props-select" value={port.bus || "virtio"} onChange={(e) => {
                          const bus = e.target.value;
                          if (!diskBuses.includes(bus)) return;
                          const patch: Record<string, unknown> = { ...port, bus };
                          const isKubevirt = providerType === "kubevirt";
                          if (["scsi", "sata", "ide"].includes(bus) && port.rotationRate === undefined && !isKubevirt) {
                            patch.rotationRate = 1;
                          } else if (bus === "virtio" || bus === "usb") {
                            delete patch.rotationRate;
                          }
                          const updated = [...ports]; updated[i] = patch as typeof port; update("diskControllers", updated);
                        }}>
                          {diskBuses.map((bus) => (
                            <option key={bus} value={bus}>{DISK_BUS_LABELS[bus] || bus}</option>
                          ))}
                        </select>
                        {port.bus && !diskBuses.includes(port.bus) && (
                          <span style={{ fontSize: 10, color: "var(--troshka-red)", display: "block", marginTop: 4 }}>
                            {DISK_BUS_LABELS[port.bus] || port.bus} is not supported on this cluster.
                          </span>
                        )}
                      </div>
                      {["scsi", "sata", "ide"].includes(port.bus) && providerType !== "kubevirt" && (
                        <div className="props-field">
                          <label className="props-label">Rotation Rate</label>
                          <select className="props-select" value={port.rotationRate ?? 1} onChange={(e) => {
                            const val = parseInt(e.target.value);
                            const updated = [...ports]; updated[i] = { ...port, rotationRate: val }; update("diskControllers", updated);
                          }}>
                            <option value={1}>SSD (non-rotational)</option>
                            <option value={7200}>7200 RPM</option>
                            <option value={10000}>10000 RPM</option>
                            <option value={15000}>15000 RPM</option>
                          </select>
                        </div>
                      )}
                    </div>
                  ))}
                  {ports.length < 8 && (
                    <button className="props-library-btn" onClick={() => {
                      update("diskControllers", [...ports, { id: generateDiskControllerId(), name: `disk${ports.length}`, bus: "virtio" }]);
                    }}>+ Add Controller ({ports.length}/8)</button>
                  )}
                  {ports.length >= 8 && (
                    <span style={{ fontSize: 11, color: "var(--troshka-text-dim)" }}>Maximum 8 controllers reached</span>
                  )}
                </>
              );
            })()}
          </div>
          <div className="props-divider" />

          {/* ── BMC (Baseboard Management Controller) ── */}
          <div className="props-section">
            <div className="props-section-title" style={{ cursor: "pointer", display: "flex", alignItems: "center", gap: 6 }} onClick={() => toggleSection("bmc")}>
              <span style={{ fontSize: 8, transition: "transform 0.15s", transform: isCollapsed("bmc") ? "rotate(-90deg)" : "rotate(0)" }}>&#9660;</span>
              BMC
            </div>
            {!isCollapsed("bmc") && (
              <div className="props-section-body">
                <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, cursor: "pointer", marginBottom: 8 }}>
                  <input type="checkbox" checked={!!(node.data as Record<string, any>).bmcEnabled}
                    disabled={projectState === "deploying"}
                    onChange={(e) => {
                      const enabled = e.target.checked;
                      if (enabled) {
                        const ip = allocateBmcIp();
                        updateNodeData(node.id, { bmcEnabled: true, bmcIp: ip });
                      } else {
                        updateNodeData(node.id, { bmcEnabled: false, bmcIp: "" });
                      }
                      setTimeout(() => syncBmcNetwork(), 0);
                    }}
                  />
                  Enable BMC
                </label>

                {(node.data as Record<string, any>).bmcEnabled && (
                  <>
                    <div className="props-field">
                      <label className="props-label">BMC IP</label>
                      <input className="props-input" value={(node.data as Record<string, any>).bmcIp || ""} readOnly
                        style={{ fontFamily: "monospace", opacity: 0.7 }} />
                    </div>

                    {/* Show addresses when deployed */}
                    {(() => {
                      const deployedTopo = (window as any).__deployedTopology;
                      const bmcData = deployedTopo?.bmc?.vms?.[node.id];
                      if (!bmcData) return null;
                      const bmcCreds = deployedTopo?.bmc;

                      const CopyBtn = ({ value, label }: { value: string; label: string }) => (
                        <button
                          style={{ background: "none", border: "none", color: "var(--troshka-cyan)", cursor: "pointer", padding: 0, flexShrink: 0, opacity: 0.7, transition: "opacity 0.15s" }}
                          onMouseEnter={(e) => (e.currentTarget.style.opacity = "1")}
                          onMouseLeave={(e) => (e.currentTarget.style.opacity = "0.7")}
                          title={`Copy ${label}`}
                          onClick={(e) => {
                            navigator.clipboard.writeText(value);
                            const btn = e.currentTarget;
                            const orig = btn.innerHTML;
                            btn.innerHTML = `<span style="font-size:10px">Copied</span>`;
                            setTimeout(() => { btn.innerHTML = orig; }, 1000);
                          }}
                        >
                          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
                        </button>
                      );

                      return (
                        <div style={{ display: "flex", flexDirection: "column", gap: 6, marginTop: 4 }}>
                          <div className="props-field">
                            <label className="props-label">Redfish URL</label>
                            <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
                              <input className="props-input" value={bmcData.redfish_url} readOnly
                                style={{ fontFamily: "monospace", fontSize: 10, flex: 1 }} />
                              <CopyBtn value={bmcData.redfish_url} label="Redfish URL" />
                            </div>
                          </div>
                          <div className="props-field">
                            <label className="props-label">IPMI Address</label>
                            <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
                              <input className="props-input" value={bmcData.ipmi_address} readOnly
                                style={{ fontFamily: "monospace", fontSize: 11, flex: 1 }} />
                              <CopyBtn value={bmcData.ipmi_address} label="IPMI address" />
                            </div>
                          </div>
                          <div className="props-field">
                            <label className="props-label">Username</label>
                            <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
                              <input className="props-input" value={bmcCreds?.username || "admin"} readOnly
                                style={{ fontFamily: "monospace", fontSize: 11, flex: 1 }} />
                              <CopyBtn value={bmcCreds?.username || "admin"} label="username" />
                            </div>
                          </div>
                          <div className="props-field">
                            <label className="props-label">Password</label>
                            <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
                              <input className="props-input" type="password" value={bmcCreds?.password || ""} readOnly
                                style={{ fontFamily: "monospace", fontSize: 11, flex: 1 }}
                                onFocus={(e) => (e.currentTarget.type = "text")}
                                onBlur={(e) => (e.currentTarget.type = "password")} />
                              <CopyBtn value={bmcCreds?.password || ""} label="password" />
                            </div>
                          </div>
                        </div>
                      );
                    })()}
                  </>
                )}
              </div>
            )}
          </div>
          {(node.data as Record<string, any>).clusterId && (
            <>
              <div className="props-divider" />
              <div className="props-section">
                <div className="props-section-title">Cluster</div>
                <div className="props-field">
                  <label className="props-label">Cluster role</label>
                  {/* Read-only: role is managed by the cluster (its type + worker
                      count), not per-VM. */}
                  <span style={{ fontSize: 13, color: "var(--troshka-text-dim)" }}>
                    {memberRole(node) === "worker" ? "Worker" : "Control plane"}{" "}
                    <span style={{ fontSize: 11 }}>(managed by cluster)</span>
                  </span>
                </div>
              </div>
            </>
          )}
          {/* OCP control-plane options moved to the cluster editor (they are
              cluster-level now, projected onto member VMs at deploy). */}
          <div className="props-divider" />

          {/* Tags Section */}
          <div className="props-section">
            <div className="props-section-title" style={{ cursor: "pointer", display: "flex", alignItems: "center", gap: 6 }} onClick={() => toggleSection("tags")}>
              <span style={{ fontSize: 8, transition: "transform 0.15s", transform: isCollapsed("tags") ? "rotate(-90deg)" : "rotate(0)" }}>&#9660;</span>
              Tags
            </div>
            {!isCollapsed("tags") && (
              <div className="props-section-body">
                {Object.entries((data as Record<string, any>).tags || {}).map(([key, value], idx) => (
                  <div key={`${node.id}-${key}`} style={{ display: "flex", gap: 4, marginBottom: 4, alignItems: "center" }}>
                    <input
                      className="props-input"
                      defaultValue={key}
                      onBlur={(e) => {
                        const newKey = e.target.value;
                        if (newKey === key) return;
                        const tags = { ...((data as Record<string, any>).tags || {}) };
                        const val = tags[key];
                        delete tags[key];
                        tags[newKey] = val;
                        update("tags", tags);
                      }}
                      style={{ flex: 1, fontSize: 11 }}
                      placeholder="Key"
                    />
                    <input
                      className="props-input"
                      defaultValue={value as string}
                      onBlur={(e) => {
                        if (e.target.value === value) return;
                        update("tags", { ...((data as Record<string, any>).tags || {}), [key]: e.target.value });
                      }}
                      style={{ flex: 1, fontSize: 11 }}
                      placeholder="Value"
                    />
                    <button
                      style={{ background: "none", border: "none", color: "var(--troshka-red)", cursor: "pointer", fontSize: 12, padding: 4 }}
                      onClick={() => {
                        const tags = { ...((data as Record<string, any>).tags || {}) };
                        delete tags[key];
                        update("tags", tags);
                      }}
                      title="Remove tag"
                    >✕</button>
                  </div>
                ))}
                <button
                  className="props-library-btn"
                  onClick={() => {
                    const tags = { ...((data as Record<string, any>).tags || {}) };
                    let newKey = "";
                    let i = 1;
                    while (newKey in tags) { newKey = `tag${i++}`; }
                    tags[newKey] = "";
                    update("tags", tags);
                  }}
                  style={{ padding: "4px 8px", fontSize: 11 }}
                >
                  + Add tag
                </button>
              </div>
            )}
          </div>
        </>
      )}

      {/* Container Properties */}
      {nodeType === "containerNode" && (
        <>
          {(() => {
            const isPod = !!(node?.data as Record<string, unknown>)?.isPod;
            const isShowroom = !!(
              (node?.data as Record<string, unknown>)?.isShowroom ||
              (node?.data as Record<string, unknown>)?.name === "showroom"
            );
            const showroomReadiness = isShowroom
              ? getShowroomReadiness(useCanvasStore.getState().nodes, edges)
              : null;
            const updateShowroomConfig = useCanvasStore.getState().updateShowroomConfig;
            const updateShowroomTabs = useCanvasStore.getState().updateShowroomTabs;
            const showroomTabs = ((node?.data as Record<string, unknown>)?.showroomTabs ||
              []) as ShowroomTab[];
            const vmOptions = useCanvasStore
              .getState()
              .nodes.filter((n) => n.type === "vmNode")
              .map((n) => ({
                id: n.id,
                name: ((n.data as Record<string, unknown>).name as string) || n.id,
              }));
            const canvasNodes = useCanvasStore.getState().nodes;
            const networkOptions = canvasNodes
              .filter(
                (n) =>
                  isDnsEnabledLabNetwork(n) &&
                  isGatewayConnectedLabNetwork(n, canvasNodes, edges),
              )
              .map((n) => ({
                id: n.id,
                name: ((n.data as Record<string, unknown>).name as string) || n.id,
              }));
            const dnsNetworkValue = effectiveShowroomDnsNetwork(
              canvasNodes,
              String((node?.data as Record<string, unknown>).dnsNetwork || ""),
              edges,
            );
            const resolvedTabs = isShowroom
              ? resolveShowroomTabs(showroomTabs, useCanvasStore.getState().nodes, edges)
              : [];
            return (
              <>
                {isShowroom && (
                  <>
                    <div className="props-section">
                      <div className="props-section-title">Showroom</div>
                      <div className="props-field">
                        <label className="props-label">Content repo</label>
                        <input
                          className="props-input"
                          placeholder="https://github.com/org/lab-repo.git"
                          value={String((node?.data as Record<string, unknown>).contentRepo || "")}
                          onChange={(e) =>
                            updateShowroomConfig(node!.id, { content_repo: e.target.value })
                          }
                          style={{ fontFamily: "monospace", fontSize: 11 }}
                        />
                      </div>
                      <div className="props-field">
                        <label className="props-label">Content ref</label>
                        <input
                          className="props-input"
                          placeholder="main"
                          value={String((node?.data as Record<string, unknown>).contentRef || "main")}
                          onChange={(e) =>
                            updateShowroomConfig(node!.id, { content_ref: e.target.value })
                          }
                          style={{ fontFamily: "monospace", fontSize: 11 }}
                        />
                      </div>
                      <div className="props-field" style={{ display: "flex", alignItems: "center", gap: 8 }}>
                        <input
                          type="checkbox"
                          checked={(node?.data as Record<string, unknown>).buildContent !== false}
                          onChange={(e) =>
                            updateShowroomConfig(node!.id, { build_content: e.target.checked })
                          }
                        />
                        <label className="props-label" style={{ marginBottom: 0 }}>Build content at deploy</label>
                      </div>
                      <div className="props-field">
                        <LabelWithHint
                          label="DNS network"
                          hint="Lab network connected to the gateway whose dnsmasq serves showroom DNS (needs gateway outbound port 53 for external names like github.com)."
                        />
                        <select
                          className="props-select"
                          value={dnsNetworkValue}
                          onChange={(e) =>
                            updateShowroomConfig(node!.id, {
                              dns_network: e.target.value,
                            })
                          }
                        >
                          <option value="">Select network…</option>
                          {networkOptions.map((opt) => (
                            <option key={opt.id} value={opt.name}>
                              {opt.name}
                            </option>
                          ))}
                        </select>
                      </div>
                      <div className="props-field" style={{ marginTop: 12 }}>
                        <div className="props-section-title" style={{ marginBottom: 8 }}>Tabs</div>
                        {showroomTabs.length === 0 && (
                          <div style={{ fontSize: 11, color: "var(--troshka-text-dim)", marginBottom: 8 }}>
                            Add terminal or web proxy tabs linked to canvas VMs.
                          </div>
                        )}
                        {showroomTabs.map((tab, idx) => {
                          const resolved = resolvedTabs[idx];
                          // Proxy sub-mode: VM (network) app, named DNS host, or
                          // embedded OAuth app (console). Inferred from set fields.
                          const proxyMode: "vm" | "named" | "oauth" =
                            tab.proxyHosts && tab.proxyHosts.length > 0
                              ? "oauth"
                              : tab.proxyHost
                                ? "named"
                                : "vm";
                          // Cluster-managed console tab: the console/oauth HOSTS
                          // derive from the OCP cluster (read-only). The NAME is
                          // user-editable — it just defaults to "<cluster> Console".
                          const clusterManaged = !!tab.clusterId;
                          const typeLabel =
                            tab.type === "terminal"
                              ? "Terminal"
                              : tab.type === "proxy"
                                ? "Web proxy"
                                : "External";
                          return (
                            <details key={tab.id} style={{ marginBottom: 8 }}>
                              <summary
                                style={{
                                  cursor: "pointer",
                                  padding: "6px 8px",
                                  background: "var(--troshka-surface2)",
                                  borderRadius: 6,
                                  display: "flex",
                                  justifyContent: "space-between",
                                  alignItems: "center",
                                  gap: 8,
                                  border: "1px solid var(--troshka-border)",
                                }}
                              >
                                <span style={{ fontSize: 12, fontWeight: 600, flex: 1, minWidth: 0 }}>
                                  {tab.name || "Untitled tab"}
                                  <span
                                    style={{
                                      fontSize: 10,
                                      fontWeight: 400,
                                      color: "var(--troshka-text-dim)",
                                      marginLeft: 6,
                                    }}
                                  >
                                    {typeLabel}
                                  </span>
                                  {resolved?.warning && (
                                    <span style={{ color: "var(--troshka-yellow)", marginLeft: 6 }}>⚠</span>
                                  )}
                                </span>
                                <button
                                  type="button"
                                  title="Remove tab"
                                  style={{
                                    background: "none",
                                    border: "none",
                                    color: "#ef4444",
                                    cursor: "pointer",
                                    fontSize: 12,
                                    flexShrink: 0,
                                  }}
                                  onClick={(e) => {
                                    e.preventDefault();
                                    e.stopPropagation();
                                    updateShowroomTabs(
                                      node!.id,
                                      showroomTabs.filter((t) => t.id !== tab.id),
                                    );
                                  }}
                                >
                                  ✕
                                </button>
                              </summary>
                              <div
                                style={{
                                  padding: 8,
                                  border: "1px solid var(--troshka-border)",
                                  borderTop: "none",
                                  borderRadius: "0 0 6px 6px",
                                  background: "var(--troshka-surface2)",
                                }}
                              >
                              <div className="props-field" style={{ marginBottom: 6 }}>
                                <LabelWithHint
                                  label="Tab name"
                                  hint="Label shown in the showroom tab bar."
                                />
                                <input
                                  className="props-input"
                                  placeholder="e.g. control"
                                  value={tab.name}
                                  onChange={(e) => {
                                    const next = showroomTabs.map((t) =>
                                      t.id === tab.id ? { ...t, name: e.target.value } : t,
                                    );
                                    updateShowroomTabs(node!.id, next);
                                  }}
                                />
                              </div>
                              <div className="props-field" style={{ marginBottom: 6 }}>
                                <LabelWithHint
                                  label="Tab type"
                                  hint="Terminal: wetty SSH shell. Web proxy: reverse proxy to a VM app. External: link opened in a new browser tab."
                                />
                                <select
                                  className="props-input"
                                  value={tab.type}
                                  onChange={(e) => {
                                    const next = showroomTabs.map((t) =>
                                      t.id === tab.id
                                        ? { ...newShowroomTab(e.target.value as ShowroomTab["type"], t.name), id: t.id }
                                        : t,
                                    );
                                    updateShowroomTabs(node!.id, next);
                                  }}
                                >
                                  <option value="terminal">Terminal</option>
                                  <option value="proxy">Web proxy</option>
                                  <option value="external">External link</option>
                                </select>
                              </div>
                              {tab.type === "external" ? (
                                <div className="props-field">
                                  <LabelWithHint
                                    label="External URL"
                                    hint="Full URL opened when the user selects this tab (new browser tab)."
                                  />
                                  <input
                                    className="props-input"
                                    placeholder="https://..."
                                    value={tab.url || ""}
                                    onChange={(e) => {
                                      const next = showroomTabs.map((t) =>
                                        t.id === tab.id ? { ...t, url: e.target.value } : t,
                                      );
                                      updateShowroomTabs(node!.id, next);
                                    }}
                                    style={{ fontFamily: "monospace", fontSize: 11 }}
                                  />
                                </div>
                              ) : tab.type === "terminal" && tab.target === "clusters" ? (
                                <div
                                  className="props-field"
                                  style={{ fontSize: 11, opacity: 0.75, lineHeight: 1.5 }}
                                >
                                  In-showroom <code>oc</code> shell with every
                                  deployed OpenShift cluster&apos;s kubeconfig
                                  pre-installed — no bastion VM. Runs unprivileged
                                  (no sudo). Nothing to configure.
                                </div>
                              ) : (
                                <>
                                  {tab.type === "proxy" && (
                                    <div className="props-field" style={{ marginBottom: 6 }}>
                                      <LabelWithHint
                                        label="Proxy mode"
                                        hint="VM app: proxy a web app on a canvas VM (by IP). Named host: proxy a hostname via the showroom's internal DNS (Host + SNI). Embedded OAuth app: embed an OAuth-protected app like the OCP console at public routes so login works inside the iframe."
                                      />
                                      <select
                                        className="props-input"
                                        value={proxyMode}
                                        onChange={(e) => {
                                          const mode = e.target.value;
                                          const next = showroomTabs.map((t) => {
                                            if (t.id !== tab.id) return t;
                                            if (mode === "vm")
                                              return { ...t, proxyHost: undefined, proxyHosts: undefined };
                                            if (mode === "named")
                                              return {
                                                ...t,
                                                proxyHosts: undefined,
                                                vmId: undefined,
                                                networkId: undefined,
                                                network: undefined,
                                                proxyHost: t.proxyHost || "",
                                              };
                                            return {
                                              ...t,
                                              proxyHost: undefined,
                                              vmId: undefined,
                                              networkId: undefined,
                                              network: undefined,
                                              proxyHosts:
                                                t.proxyHosts && t.proxyHosts.length
                                                  ? t.proxyHosts
                                                  : ["", ""],
                                            };
                                          });
                                          updateShowroomTabs(node!.id, next);
                                        }}
                                      >
                                        <option value="vm">VM app (network)</option>
                                        <option value="named">Named host (DNS)</option>
                                        <option value="oauth">Embedded OAuth app</option>
                                      </select>
                                    </div>
                                  )}
                                  {(tab.type === "terminal" || proxyMode === "vm") && (
                                  <>
                                  <div className="props-field" style={{ marginBottom: 6 }}>
                                    <LabelWithHint
                                      label="Target VM"
                                      hint="Canvas VM this tab connects to."
                                    />
                                    <select
                                      className="props-input"
                                      value={tab.vmId || ""}
                                      onChange={(e) => {
                                        const next = showroomTabs.map((t) =>
                                          t.id === tab.id ? { ...t, vmId: e.target.value } : t,
                                        );
                                        updateShowroomTabs(node!.id, next);
                                      }}
                                    >
                                      <option value="">Select VM...</option>
                                      {vmOptions.map((vm) => (
                                        <option key={vm.id} value={vm.id}>{vm.name}</option>
                                      ))}
                                    </select>
                                  </div>
                                  <div className="props-field" style={{ marginBottom: 6 }}>
                                    <LabelWithHint
                                      label="Network"
                                      hint="NIC on that VM used to reach the workload (IP from the canvas)."
                                    />
                                    <select
                                      className="props-input"
                                      value={tab.networkId || ""}
                                      onChange={(e) => {
                                        const selected = networkOptions.find((n) => n.id === e.target.value);
                                        const next = showroomTabs.map((t) =>
                                          t.id === tab.id
                                            ? {
                                                ...t,
                                                networkId: e.target.value || undefined,
                                                network: selected?.name || undefined,
                                              }
                                            : t,
                                        );
                                        updateShowroomTabs(node!.id, next);
                                      }}
                                    >
                                      <option value="">Select network...</option>
                                      {networkOptions.map((net) => (
                                        <option key={net.id} value={net.id}>{net.name}</option>
                                      ))}
                                    </select>
                                  </div>
                                  </>
                                  )}
                                  {tab.type === "terminal" && (
                                    <div className="props-field" style={{ marginBottom: 6 }}>
                                      <LabelWithHint
                                        label="SSH credentials"
                                        hint="Optional wetty overrides; leave blank to use the VM cloud-init user and password."
                                      />
                                      <div className="props-row" style={{ gap: 8 }}>
                                        <div style={{ flex: 1 }}>
                                          <LabelWithHint
                                            label="User"
                                            hint="SSH login user (default: VM cloud-init user)."
                                            style={{ fontSize: 10 }}
                                          />
                                          <input
                                            className="props-input"
                                            placeholder="cloud-user"
                                            value={tab.sshUser || ""}
                                            onChange={(e) => {
                                              const next = showroomTabs.map((t) =>
                                                t.id === tab.id ? { ...t, sshUser: e.target.value } : t,
                                              );
                                              updateShowroomTabs(node!.id, next);
                                            }}
                                            style={{ fontSize: 11 }}
                                          />
                                        </div>
                                        <div style={{ flex: 1 }}>
                                          <LabelWithHint
                                            label="Password"
                                            hint="SSH password (default: VM cloud-init password)."
                                            style={{ fontSize: 10 }}
                                          />
                                          <input
                                            className="props-input"
                                            placeholder="optional"
                                            type="password"
                                            value={tab.sshPass || ""}
                                            onChange={(e) => {
                                              const next = showroomTabs.map((t) =>
                                                t.id === tab.id ? { ...t, sshPass: e.target.value } : t,
                                              );
                                              updateShowroomTabs(node!.id, next);
                                            }}
                                            style={{ fontSize: 11 }}
                                          />
                                        </div>
                                        <div style={{ width: 72 }}>
                                          <LabelWithHint
                                            label="Port"
                                            hint="SSH port on the target VM (default 22)."
                                            style={{ fontSize: 10 }}
                                          />
                                          <input
                                            className="props-input"
                                            type="number"
                                            placeholder="22"
                                            value={tab.sshPort ?? ""}
                                            onChange={(e) => {
                                              const raw = e.target.value;
                                              const next = showroomTabs.map((t) =>
                                                t.id === tab.id
                                                  ? {
                                                      ...t,
                                                      sshPort: raw
                                                        ? parseInt(raw, 10) || undefined
                                                        : undefined,
                                                    }
                                                  : t,
                                              );
                                              updateShowroomTabs(node!.id, next);
                                            }}
                                            style={{ fontSize: 11 }}
                                          />
                                        </div>
                                      </div>
                                    </div>
                                  )}
                                </>
                              )}
                              {tab.type === "proxy" && (
                                <div className="props-field" style={{ marginBottom: 6 }}>
                                  <LabelWithHint
                                    label="Reverse proxy"
                                    hint="Nginx path on the showroom and backend port on the target VM."
                                  />
                                  {/* App-proxy: embed an OAuth-protected app (e.g. OCP console) at public routes */}
                                  {proxyMode === "oauth" && clusterManaged && (
                                    <div style={{ marginBottom: 8 }}>
                                      {(tab.proxyHosts || []).map((h, hi) => (
                                        <div
                                          key={hi}
                                          title={h}
                                          style={{ fontFamily: "monospace", fontSize: 11, lineHeight: 1.5, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}
                                        >
                                          {h}
                                        </div>
                                      ))}
                                      <div style={{ fontSize: 10, color: "var(--troshka-accent, #3b82f6)", marginTop: 4 }}>
                                        ☸ Managed by the OpenShift cluster — rename the cluster to change these routes.
                                      </div>
                                    </div>
                                  )}
                                  {proxyMode === "oauth" && !clusterManaged && (
                                  <div style={{ marginBottom: 8 }}>
                                    {(tab.proxyHosts || []).map((h, hi) => (
                                      <div key={hi} style={{ display: "flex", gap: 4, marginBottom: 4, alignItems: "center" }}>
                                        <input
                                          className="props-input"
                                          placeholder="console-openshift-console.apps.ocp.ocp.local"
                                          value={h}
                                          onChange={(e) => {
                                            const hosts = [...(tab.proxyHosts || [])];
                                            hosts[hi] = e.target.value;
                                            const next = showroomTabs.map((t) =>
                                              t.id === tab.id ? { ...t, proxyHosts: hosts } : t,
                                            );
                                            updateShowroomTabs(node!.id, next);
                                          }}
                                          style={{ fontFamily: "monospace", fontSize: 11, flex: 1, minWidth: 0 }}
                                        />
                                        <button
                                          className="props-library-btn"
                                          style={{ fontSize: 11, flex: "0 0 auto", width: 28, padding: "0 4px" }}
                                          title="Remove host"
                                          onClick={() => {
                                            const hosts = (tab.proxyHosts || []).filter((_, i) => i !== hi);
                                            const next = showroomTabs.map((t) =>
                                              t.id === tab.id
                                                ? { ...t, proxyHosts: hosts.length ? hosts : undefined }
                                                : t,
                                            );
                                            updateShowroomTabs(node!.id, next);
                                          }}
                                        >
                                          ✕
                                        </button>
                                      </div>
                                    ))}
                                    <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                                      <button
                                        className="props-library-btn"
                                        style={{ fontSize: 11 }}
                                        onClick={() => {
                                          const hosts = [...(tab.proxyHosts || []), ""];
                                          const next = showroomTabs.map((t) =>
                                            t.id === tab.id ? { ...t, proxyHosts: hosts } : t,
                                          );
                                          updateShowroomTabs(node!.id, next);
                                        }}
                                      >
                                        + Add app host
                                      </button>
                                      {!(
                                        (tab.proxyHosts || []).some((h) =>
                                          h.startsWith("console-openshift-console."),
                                        ) &&
                                        (tab.proxyHosts || []).some((h) =>
                                          h.startsWith("oauth-openshift."),
                                        )
                                      ) && (
                                        <button
                                          className="props-library-btn"
                                          style={{ fontSize: 11 }}
                                          title="Fill in the console + oauth hosts for embedding the OpenShift web console"
                                          onClick={() => {
                                            const next = showroomTabs.map((t) =>
                                              t.id === tab.id
                                                ? {
                                                    ...t,
                                                    proxyHosts: [
                                                      "console-openshift-console.apps.ocp.ocp.local",
                                                      "oauth-openshift.apps.ocp.ocp.local",
                                                    ],
                                                    proxyTls: true,
                                                    proxyPort: 443,
                                                  }
                                                : t,
                                            );
                                            updateShowroomTabs(node!.id, next);
                                          }}
                                        >
                                          Insert OCP console hosts
                                        </button>
                                      )}
                                    </div>
                                    <div style={{ marginTop: 6 }}>
                                      <LabelWithHint
                                        label="Embedded app hosts (OAuth)"
                                        hint="For an OAuth-protected cluster app like the OCP console. List the internal .local hosts — [0] is the iframe target, the rest are login companions (oauth). Deploy publishes each at a public route and the showroom proxies + rewrites redirects so login works embedded."
                                        style={{ fontSize: 10 }}
                                      />
                                    </div>
                                  </div>
                                  )}
                                  {proxyMode === "named" && (
                                  <div style={{ marginBottom: 6 }}>
                                    <LabelWithHint
                                      label="Backend host"
                                      hint="Optional FQDN (e.g. console-openshift-console.apps.ocp.local). When set, proxy by name via internal DNS — nginx sends this as the Host header and TLS SNI so an OpenShift router routes correctly. Overrides Target VM."
                                      style={{ fontSize: 10 }}
                                    />
                                    <input
                                      className="props-input"
                                      placeholder="console-openshift-console.apps.ocp.local"
                                      value={tab.proxyHost || ""}
                                      onChange={(e) => {
                                        const next = showroomTabs.map((t) =>
                                          t.id === tab.id ? { ...t, proxyHost: e.target.value } : t,
                                        );
                                        updateShowroomTabs(node!.id, next);
                                      }}
                                      style={{ fontFamily: "monospace", fontSize: 11 }}
                                    />
                                  </div>
                                  )}
                                  {proxyMode !== "oauth" && (
                                  <div className="props-row" style={{ gap: 8, alignItems: "flex-end" }}>
                                    <div style={{ flex: 2 }}>
                                      <LabelWithHint
                                        label="Path"
                                        hint="Browser path prefix on the showroom (nginx location), e.g. /vscode/."
                                        style={{ fontSize: 10 }}
                                      />
                                      <input
                                        className="props-input"
                                        placeholder="/vscode/"
                                        value={tab.proxyPath || ""}
                                        onChange={(e) => {
                                          const next = showroomTabs.map((t) =>
                                            t.id === tab.id ? { ...t, proxyPath: e.target.value } : t,
                                          );
                                          updateShowroomTabs(node!.id, next);
                                        }}
                                        style={{ fontFamily: "monospace", fontSize: 11 }}
                                      />
                                    </div>
                                    <div style={{ width: 80 }}>
                                      <LabelWithHint
                                        label="Backend port"
                                        hint="Port of the web app on the target VM."
                                        style={{ fontSize: 10 }}
                                      />
                                      <input
                                        className="props-input"
                                        type="number"
                                        placeholder="80"
                                        value={tab.proxyPort || 80}
                                        onChange={(e) => {
                                          const next = showroomTabs.map((t) =>
                                            t.id === tab.id
                                              ? { ...t, proxyPort: parseInt(e.target.value, 10) || 80 }
                                              : t,
                                          );
                                          updateShowroomTabs(node!.id, next);
                                        }}
                                      />
                                    </div>
                                    <label
                                      style={{
                                        display: "flex",
                                        alignItems: "center",
                                        gap: 4,
                                        fontSize: 11,
                                        paddingBottom: 6,
                                      }}
                                    >
                                      <input
                                        type="checkbox"
                                        checked={!!tab.proxyTls}
                                        onChange={(e) => {
                                          const next = showroomTabs.map((t) =>
                                            t.id === tab.id ? { ...t, proxyTls: e.target.checked } : t,
                                          );
                                          updateShowroomTabs(node!.id, next);
                                        }}
                                      />
                                      TLS to VM
                                      <HintIcon text="Use HTTPS when proxying to the VM backend." />
                                    </label>
                                  </div>
                                  )}
                                </div>
                              )}
                              {resolved?.warning && (
                                <div style={{ fontSize: 10, color: "var(--troshka-yellow)", marginTop: 4 }}>
                                  ⚠ {resolved.warning}
                                </div>
                              )}
                              </div>
                            </details>
                          );
                        })}
                        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                          <button
                            className="props-library-btn"
                            style={{ fontSize: 11 }}
                            onClick={() =>
                              updateShowroomTabs(node!.id, [
                                ...showroomTabs,
                                newShowroomTab("terminal", "Terminal"),
                              ])
                            }
                          >
                            + Terminal Tab
                          </button>
                          {/* Bastionless cluster terminal: an in-showroom oc
                              shell (all deployed clusters' kubeconfigs, no
                              bastion). One per showroom (it aggregates every
                              cluster on a single wetty path). */}
                          {!showroomTabs.some(
                            (t) => t.type === "terminal" && t.target === "clusters",
                          ) && (
                            <button
                              className="props-library-btn"
                              style={{ fontSize: 11 }}
                              title="An in-showroom oc shell with every deployed cluster's kubeconfig (no bastion VM)"
                              onClick={() =>
                                updateShowroomTabs(node!.id, [
                                  ...showroomTabs,
                                  { ...newShowroomTab("terminal", "Terminal"), target: "clusters" },
                                ])
                              }
                            >
                              + OpenShift Cluster(s) Terminal Tab
                            </button>
                          )}
                          <button
                            className="props-library-btn"
                            style={{ fontSize: 11 }}
                            onClick={() =>
                              updateShowroomTabs(node!.id, [
                                ...showroomTabs,
                                newShowroomTab("proxy", "Web app"),
                              ])
                            }
                          >
                            + Web Proxy Tab
                          </button>
                          <button
                            className="props-library-btn"
                            style={{ fontSize: 11 }}
                            onClick={() =>
                              updateShowroomTabs(node!.id, [
                                ...showroomTabs,
                                newShowroomTab("external", "External"),
                              ])
                            }
                          >
                            + External Tab
                          </button>
                        </div>
                        {/* Quick-add: proxy the OpenShift console for a canvas
                            OCP cluster (console + oauth app-proxy hosts, TLS).
                            Only clusters not already proxied are offered; the
                            control is hidden when there are none. */}
                        {(() => {
                          // A cluster is already proxied if a tab is linked to it.
                          const managedIds = new Set(
                            showroomTabs.map((t) => t.clusterId).filter(Boolean),
                          );
                          const available = clusters.filter(
                            (c) => c.name && c.baseDomain && !managedIds.has(c.id),
                          );
                          if (available.length === 0) return null;
                          return (
                            <div style={{ marginTop: 6 }}>
                              <button
                                className="props-library-btn"
                                style={{ fontSize: 11 }}
                                onClick={() => setConsoleMenuOpen((o) => !o)}
                              >
                                + OpenShift Console Proxy Tab {consoleMenuOpen ? "▴" : "▾"}
                              </button>
                              {consoleMenuOpen && (
                                <div
                                  style={{
                                    display: "flex",
                                    flexDirection: "column",
                                    gap: 4,
                                    marginTop: 4,
                                    paddingLeft: 8,
                                  }}
                                >
                                  {available.map((c) => (
                                    <button
                                      key={c.id}
                                      className="props-library-btn"
                                      style={{ fontSize: 11, textAlign: "left" }}
                                      title={clusterConsoleHosts(c.name || "", c.baseDomain || "")[0]}
                                      onClick={() => {
                                        const tab: ShowroomTab = {
                                          ...newShowroomTab("proxy", clusterConsoleTabName(c.name || "")),
                                          clusterId: c.id,
                                          proxyHosts: clusterConsoleHosts(c.name || "", c.baseDomain || ""),
                                          proxyTls: true,
                                          proxyPort: 443,
                                        };
                                        updateShowroomTabs(node!.id, [...showroomTabs, tab]);
                                        setConsoleMenuOpen(false);
                                      }}
                                    >
                                      ☸ {c.name}{" "}
                                      <span style={{ color: "var(--troshka-text-dim)" }}>
                                        ({c.baseDomain})
                                      </span>
                                    </button>
                                  ))}
                                </div>
                              )}
                            </div>
                          );
                        })()}
                      </div>
                      {showroomReadiness && showroomReadiness.issues.length > 0 && (
                        <div style={{ marginTop: 8, fontSize: 11, color: "var(--troshka-yellow)" }}>
                          {showroomReadiness.issues.map((issue) => (
                            <div key={issue}>⚠ {issue}</div>
                          ))}
                        </div>
                      )}
                      {showroomReadiness && showroomReadiness.issues.length === 0 && (
                        <div style={{ marginTop: 8, fontSize: 11, color: "var(--troshka-green)" }}>
                          Ready — content configured{showroomReadiness.hasGateway ? " (gateway present)" : ""}
                        </div>
                      )}
                    </div>
                    <div className="props-divider" />
                  </>
                )}
                {!isPod && (
                  <>
          {/* Image section */}
          <div className="props-section">
            <div className="props-section-title">Image</div>
            <div className="props-field">
              <label className="props-label">Image</label>
              <input
                className="props-input"
                placeholder="registry/org/image:tag"
                value={(data as unknown as ContainerNodeData).image || ""}
                onChange={(e) => update("image", e.target.value)}
                style={{ fontFamily: "monospace", fontSize: 11 }}
              />
            </div>
            <div className="props-field">
              <label className="props-label">Registry Credential</label>
              <RegistryCredentialDropdown
                value={(data as unknown as ContainerNodeData).registryCredentialId}
                onChange={(v) => update("registryCredentialId", v)}
              />
            </div>
          </div>
          <div className="props-divider" />

          {/* Resources section */}
          <div className="props-section">
            <div className="props-section-title">Resources</div>
            <div className="props-row">
              <div className="props-field">
                <label className="props-label">CPUs</label>
                <input
                  className="props-input"
                  type="number"
                  min={1}
                  max={32}
                  value={(data as unknown as ContainerNodeData).cpus}
                  onFocus={(e) => e.target.select()}
                  onChange={(e) => update("cpus", parseInt(e.target.value) || 1)}
                />
              </div>
              <div className="props-field">
                <label className="props-label">Memory (MB)</label>
                <input
                  className="props-input"
                  type="number"
                  min={64}
                  max={524288}
                  value={(data as unknown as ContainerNodeData).memory}
                  onFocus={(e) => e.target.select()}
                  onChange={(e) => update("memory", parseInt(e.target.value) || 512)}
                />
              </div>
            </div>
          </div>
          <div className="props-divider" />
                  </>
                )}

          {/* NIC section — infra showroom uses project netns, not lab NICs */}
          {!isShowroom ? (
          <>
          <div className="props-section">
            <div className="props-section-title">Network Interfaces</div>
            {(() => {
              let nics = ((data as unknown as ContainerNodeData).nics || []) as Array<{id: string; name: string; mac: string; model: string; ip?: string}>;
              if (nics.length === 0) {
                nics = [{ id: generateNicId(), name: "eth0", mac: generateMac(), model: "virtio" }];
                update("nics", nics);
              }
              return (
                <>
                  {nics.map((nic, i) => (
                    <div key={nic.id} style={{ background: "var(--troshka-surface2)", borderRadius: 6, padding: 8, marginBottom: 6 }}>
                      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 4 }}>
                        <input
                          className="props-input"
                          value={nic.name || `eth${i}`}
                          onChange={(e) => { const updated = [...nics]; updated[i] = { ...nic, name: e.target.value }; update("nics", updated); }}
                          style={{ fontSize: 12, fontWeight: 600, background: "transparent", border: "none", padding: 0, width: 80 }}
                        />
                        {nics.length > 1 && (
                          <button
                            style={{ background: "none", border: "none", color: "var(--troshka-red)", cursor: "pointer", fontSize: 12 }}
                            onClick={() => update("nics", nics.filter((_, idx) => idx !== i))}
                          >✕</button>
                        )}
                      </div>
                      <div className="props-field" style={{ marginBottom: 4 }}>
                        <label className="props-label">Model</label>
                        <select className="props-select" value={nic.model || "virtio"} onChange={(e) => {
                          const updated = [...nics]; updated[i] = { ...nic, model: e.target.value }; update("nics", updated);
                        }}>
                          <option value="virtio">virtio</option>
                          <option value="igb">igb (SR-IOV)</option>
                          <option value="e1000e">e1000e</option>
                          <option value="e1000">e1000</option>
                          <option value="rtl8139">rtl8139</option>
                        </select>
                      </div>
                      <div className="props-field">
                        <label className="props-label">MAC Address</label>
                        <input className="props-input" value={nic.mac} style={{ fontFamily: "monospace", fontSize: 11 }} onChange={(e) => {
                          const updated = [...nics]; updated[i] = { ...nic, mac: e.target.value }; update("nics", updated);
                        }} />
                      </div>
                      {(() => {
                        const nicHandleTop = `nic-${nic.id}-top`;
                        const nicHandleBottom = `nic-${nic.id}-bottom`;
                        const netEdge = edges.find((e) =>
                          (e.source === node!.id && (e.sourceHandle === nicHandleTop || e.sourceHandle === nicHandleBottom)) ||
                          (e.target === node!.id && (e.targetHandle === nicHandleTop || e.targetHandle === nicHandleBottom))
                        );
                        const netNode = netEdge ? nodes.find((n) => n.id === (netEdge.source === node!.id ? netEdge.target : netEdge.source) && n.type === "networkNode") : null;
                        const netCidr = netNode ? (netNode.data as Record<string, any>).cidr as string : "";
                        const nicIp = (nic as Record<string, any>).ip as string || "";
                        return netNode ? (
                          <div className="props-field">
                            <label className="props-label">IP Address {netCidr ? `(${netCidr})` : ""}</label>
                            <input
                              className="props-input"
                              value={nicIp}
                              placeholder="DHCP (auto)"
                              style={{ fontFamily: "monospace", fontSize: 11 }}
                              onChange={(e) => {
                                const updated = [...nics]; updated[i] = { ...nic, ip: e.target.value }; update("nics", updated);
                              }}
                            />
                          </div>
                        ) : null;
                      })()}
                    </div>
                  ))}
                  {nics.length < 8 && (
                    <button className="props-library-btn" onClick={() => {
                      update("nics", [...nics, { id: generateNicId(), name: `eth${nics.length}`, mac: generateMac(), model: "virtio" }]);
                    }}>+ Add NIC ({nics.length}/8)</button>
                  )}
                  {nics.length >= 8 && (
                    <span style={{ fontSize: 11, color: "var(--troshka-text-dim)" }}>Maximum 8 NICs reached</span>
                  )}
                </>
              );
            })()}
          </div>
          <div className="props-divider" />
          </>
          ) : null}

                {!isPod && (
                  <>
          {/* Environment Variables section */}
          <div className="props-section">
            <div className="props-section-title">Environment Variables</div>
            {((data as unknown as ContainerNodeData).envVars || []).map((ev, i) => (
              <div key={i} style={{ display: "flex", gap: 4, marginBottom: 4, alignItems: "center" }}>
                <input
                  className="props-input"
                  placeholder="KEY"
                  value={ev.key}
                  style={{ flex: 1, fontFamily: "monospace", fontSize: 11 }}
                  onChange={(e) => {
                    const updated = [...((data as unknown as ContainerNodeData).envVars || [])];
                    updated[i] = { ...ev, key: e.target.value };
                    update("envVars", updated);
                  }}
                />
                <span style={{ color: "var(--troshka-text-dim)" }}>=</span>
                <input
                  className="props-input"
                  placeholder="value"
                  value={ev.value}
                  style={{ flex: 2, fontFamily: "monospace", fontSize: 11 }}
                  onChange={(e) => {
                    const updated = [...((data as unknown as ContainerNodeData).envVars || [])];
                    updated[i] = { ...ev, value: e.target.value };
                    update("envVars", updated);
                  }}
                />
                <button
                  style={{ background: "none", border: "none", color: "var(--troshka-red)", cursor: "pointer", fontSize: 12 }}
                  onClick={() => {
                    const updated = ((data as unknown as ContainerNodeData).envVars || []).filter((_, idx) => idx !== i);
                    update("envVars", updated);
                  }}
                >✕</button>
              </div>
            ))}
            <button
              className="props-library-btn"
              onClick={() => update("envVars", [...((data as unknown as ContainerNodeData).envVars || []), { key: "", value: "" }])}
            >+ Add Variable</button>
          </div>
          <div className="props-divider" />

          {/* Ports section */}
          <div className="props-section">
            <div className="props-section-title">Ports</div>
            {((data as unknown as ContainerNodeData).ports || []).map((p, i) => (
              <div key={i} style={{ display: "flex", gap: 4, marginBottom: 4, alignItems: "center" }}>
                <input
                  className="props-input"
                  type="number"
                  placeholder="Container"
                  value={p.containerPort || ""}
                  style={{ width: 70 }}
                  onChange={(e) => {
                    const updated = [...((data as unknown as ContainerNodeData).ports || [])];
                    updated[i] = { ...p, containerPort: parseInt(e.target.value) || 0 };
                    update("ports", updated);
                  }}
                />
                <span style={{ color: "var(--troshka-text-dim)", fontSize: 11 }}>→</span>
                <input
                  className="props-input"
                  type="number"
                  placeholder="Host (opt)"
                  value={p.hostPort || ""}
                  style={{ width: 70 }}
                  onChange={(e) => {
                    const updated = [...((data as unknown as ContainerNodeData).ports || [])];
                    updated[i] = { ...p, hostPort: parseInt(e.target.value) || undefined };
                    update("ports", updated);
                  }}
                />
                <select
                  className="props-select"
                  value={p.protocol || "tcp"}
                  style={{ width: 60 }}
                  onChange={(e) => {
                    const updated = [...((data as unknown as ContainerNodeData).ports || [])];
                    updated[i] = { ...p, protocol: e.target.value as "tcp" | "udp" };
                    update("ports", updated);
                  }}
                >
                  <option value="tcp">TCP</option>
                  <option value="udp">UDP</option>
                </select>
                <button
                  style={{ background: "none", border: "none", color: "var(--troshka-red)", cursor: "pointer", fontSize: 12 }}
                  onClick={() => {
                    const updated = ((data as unknown as ContainerNodeData).ports || []).filter((_, idx) => idx !== i);
                    update("ports", updated);
                  }}
                >✕</button>
              </div>
            ))}
            <button
              className="props-library-btn"
              onClick={() => update("ports", [...((data as unknown as ContainerNodeData).ports || []), { containerPort: 0, protocol: "tcp" }])}
            >+ Add Port</button>
          </div>
          <div className="props-divider" />

          {/* Volumes section */}
          <div className="props-section">
            <div className="props-section-title">Volumes</div>
            {(() => {
              const connectedDisks = edges
                .filter(
                  (e) =>
                    (e.source === node!.id || e.target === node!.id) &&
                    (e.sourceHandle?.startsWith("mnt-") || e.targetHandle?.startsWith("mnt-"))
                )
                .map((e) => {
                  const diskId = e.source === node!.id ? e.target : e.source;
                  return nodes.find((n) => n.id === diskId && n.type === "storageNode");
                })
                .filter(Boolean);

              if (connectedDisks.length === 0) {
                return <span style={{ fontSize: 11, color: "var(--troshka-text-dim)" }}>Connect a Disk node to add volumes</span>;
              }

              const mounts = (data as unknown as ContainerNodeData).mounts || [];
              return connectedDisks.map((diskNode) => {
                const existing = mounts.find((m) => m.diskNodeId === diskNode!.id);
                const diskData = diskNode!.data as Record<string, any>;
                return (
                  <div key={diskNode!.id} style={{ display: "flex", gap: 6, marginBottom: 4, alignItems: "center" }}>
                    <span style={{ fontSize: 12, minWidth: 60 }}>🛢 {diskData.name}</span>
                    <span style={{ color: "var(--troshka-text-dim)", fontSize: 11 }}>→</span>
                    <input
                      className="props-input"
                      placeholder="/mount/path"
                      value={existing?.mountPath || ""}
                      style={{ flex: 1, fontFamily: "monospace", fontSize: 11 }}
                      onChange={(e) => {
                        const updated = mounts.filter((m) => m.diskNodeId !== diskNode!.id);
                        updated.push({ diskNodeId: diskNode!.id, mountPath: e.target.value });
                        update("mounts", updated);
                      }}
                    />
                  </div>
                );
              });
            })()}
          </div>
          <div className="props-divider" />

          {/* Advanced section */}
          <div className="props-section">
            <div className="props-section-title">Advanced</div>
            <div className="props-field">
              <label className="props-label">Restart Policy</label>
              <select
                className="props-select"
                value={(data as unknown as ContainerNodeData).restartPolicy || "always"}
                onChange={(e) => update("restartPolicy", e.target.value)}
              >
                <option value="always">Always</option>
                <option value="on-failure">On Failure</option>
                <option value="never">Never</option>
              </select>
            </div>
            <div className="props-field">
              <label className="props-label">Command Override</label>
              <input
                className="props-input"
                placeholder="Optional entrypoint override"
                value={(data as unknown as ContainerNodeData).command || ""}
                style={{ fontFamily: "monospace", fontSize: 11 }}
                onChange={(e) => update("command", e.target.value || null)}
              />
            </div>
            <div className="props-field" style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <input
                type="checkbox"
                checked={(data as unknown as ContainerNodeData).privileged || false}
                onChange={(e) => update("privileged", e.target.checked)}
              />
              <label className="props-label" style={{ marginBottom: 0 }}>Privileged</label>
            </div>
          </div>
          <div className="props-divider" />
                  </>
                )}

                {isPod && (
                  <>
          {isShowroom && (
            <div className="props-section">
              <div
                className="props-section-title"
                style={{ cursor: "pointer", display: "flex", alignItems: "center", gap: 6 }}
                onClick={() => toggleSection("podAdvanced")}
              >
                <span
                  style={{
                    fontSize: 8,
                    transition: "transform 0.15s",
                    transform: isCollapsed("podAdvanced") ? "rotate(-90deg)" : "rotate(0)",
                  }}
                >
                  &#9660;
                </span>
                Advanced
                <HintIcon text="Init and main containers are scaffolded from content and tabs; edit only for debugging or custom sidecars." />
              </div>
            </div>
          )}
          {(!isShowroom || !isCollapsed("podAdvanced")) && (
                <>
          {/* Init Containers section */}
          <div className="props-section">
            <div className="props-section-title" style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              Init Containers
              <button className="props-library-btn" style={{ fontSize: 11, padding: "2px 8px" }} onClick={() => {
                const containers = [...((node?.data as any)?.initContainers || [])];
                containers.push({
                  name: `init-${containers.length}`,
                  image: "",
                  command: null,
                  envVars: [],
                  mounts: []
                });
                updateNodeData(node!.id, { initContainers: containers });
              }}>+ Add</button>
            </div>
            {((node?.data as any)?.initContainers || []).map((container: any, i: number) => (
              <details key={i} style={{ marginBottom: 8 }}>
                <summary style={{ cursor: "pointer", padding: "6px 8px", background: "var(--troshka-surface2)", borderRadius: 6, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <span style={{ fontSize: 12, fontWeight: 600 }}>{container.name || `init-${i}`}</span>
                  <button
                    style={{ background: "none", border: "none", color: "#ef4444", cursor: "pointer", fontSize: 12 }}
                    onClick={(e) => {
                      e.stopPropagation();
                      const containers = [...((node?.data as any)?.initContainers || [])];
                      containers.splice(i, 1);
                      updateNodeData(node!.id, { initContainers: containers });
                    }}
                  >✕</button>
                </summary>
                <div style={{ padding: "8px 0" }}>
                  <div className="props-field">
                    <label className="props-label">Name</label>
                    <input className="props-input" value={container.name || ""} onChange={(e) => {
                      const containers = [...((node?.data as any)?.initContainers || [])];
                      containers[i] = { ...container, name: e.target.value };
                      updateNodeData(node!.id, { initContainers: containers });
                    }} />
                  </div>
                  <div className="props-field">
                    <label className="props-label">Image</label>
                    <input className="props-input" value={container.image || ""} placeholder="registry/org/image:tag" style={{ fontFamily: "monospace", fontSize: 11 }} onChange={(e) => {
                      const containers = [...((node?.data as any)?.initContainers || [])];
                      containers[i] = { ...container, image: e.target.value };
                      updateNodeData(node!.id, { initContainers: containers });
                    }} />
                  </div>
                  <div className="props-field">
                    <label className="props-label">Command</label>
                    <input className="props-input" value={formatCommandForInput(container.command)} placeholder="Optional entrypoint override" style={{ fontFamily: "monospace", fontSize: 11 }} onChange={(e) => {
                      const containers = [...((node?.data as any)?.initContainers || [])];
                      containers[i] = { ...container, command: e.target.value || null };
                      updateNodeData(node!.id, { initContainers: containers });
                    }} />
                  </div>
                  <div className="props-field">
                    <label className="props-label">Environment Variables</label>
                    {(container.envVars || []).map((ev: any, evIdx: number) => (
                      <div key={evIdx} style={{ display: "flex", gap: 4, marginBottom: 4, alignItems: "center" }}>
                        <input className="props-input" placeholder="KEY" value={ev.key || ""} style={{ flex: 1, fontFamily: "monospace", fontSize: 11 }} onChange={(e) => {
                          const containers = [...((node?.data as any)?.initContainers || [])];
                          const envVars = [...(containers[i].envVars || [])];
                          envVars[evIdx] = { ...ev, key: e.target.value };
                          containers[i] = { ...container, envVars };
                          updateNodeData(node!.id, { initContainers: containers });
                        }} />
                        <span style={{ color: "var(--troshka-text-dim)" }}>=</span>
                        <input className="props-input" placeholder="value" value={ev.value || ""} style={{ flex: 2, fontFamily: "monospace", fontSize: 11 }} onChange={(e) => {
                          const containers = [...((node?.data as any)?.initContainers || [])];
                          const envVars = [...(containers[i].envVars || [])];
                          envVars[evIdx] = { ...ev, value: e.target.value };
                          containers[i] = { ...container, envVars };
                          updateNodeData(node!.id, { initContainers: containers });
                        }} />
                        <button style={{ background: "none", border: "none", color: "#ef4444", cursor: "pointer", fontSize: 12 }} onClick={() => {
                          const containers = [...((node?.data as any)?.initContainers || [])];
                          const envVars = [...(containers[i].envVars || [])];
                          envVars.splice(evIdx, 1);
                          containers[i] = { ...container, envVars };
                          updateNodeData(node!.id, { initContainers: containers });
                        }}>✕</button>
                      </div>
                    ))}
                    <button style={{ fontSize: 10, padding: "2px 6px" }} className="props-library-btn" onClick={() => {
                      const containers = [...((node?.data as any)?.initContainers || [])];
                      const envVars = [...(containers[i].envVars || [])];
                      envVars.push({ key: "", value: "" });
                      containers[i] = { ...container, envVars };
                      updateNodeData(node!.id, { initContainers: containers });
                    }}>+ Env Var</button>
                  </div>
                  <div className="props-field">
                    <label className="props-label">Mounts</label>
                    {(container.mounts || []).map((mount: any, mIdx: number) => (
                      <div key={mIdx} style={{ display: "flex", gap: 4, marginBottom: 4, alignItems: "center" }}>
                        <input className="props-input" placeholder="/mount/path" value={mount.mountPath || ""} style={{ flex: 1, fontFamily: "monospace", fontSize: 11 }} onChange={(e) => {
                          const containers = [...((node?.data as any)?.initContainers || [])];
                          const mounts = [...(containers[i].mounts || [])];
                          mounts[mIdx] = { ...mount, mountPath: e.target.value };
                          containers[i] = { ...container, mounts };
                          updateNodeData(node!.id, { initContainers: containers });
                        }} />
                        <button style={{ background: "none", border: "none", color: "#ef4444", cursor: "pointer", fontSize: 12 }} onClick={() => {
                          const containers = [...((node?.data as any)?.initContainers || [])];
                          const mounts = [...(containers[i].mounts || [])];
                          mounts.splice(mIdx, 1);
                          containers[i] = { ...container, mounts };
                          updateNodeData(node!.id, { initContainers: containers });
                        }}>✕</button>
                      </div>
                    ))}
                    <button style={{ fontSize: 10, padding: "2px 6px" }} className="props-library-btn" onClick={() => {
                      const containers = [...((node?.data as any)?.initContainers || [])];
                      const mounts = [...(containers[i].mounts || [])];
                      mounts.push({ diskNodeId: "", mountPath: "" });
                      containers[i] = { ...container, mounts };
                      updateNodeData(node!.id, { initContainers: containers });
                    }}>+ Mount</button>
                  </div>
                </div>
              </details>
            ))}
          </div>
          <div className="props-divider" />

          {/* Main Containers section */}
          <div className="props-section">
            <div className="props-section-title" style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              Main Containers
              <button className="props-library-btn" style={{ fontSize: 11, padding: "2px 8px" }} onClick={() => {
                const containers = [...((node?.data as any)?.podContainers || [])];
                containers.push({
                  name: `container-${containers.length}`,
                  image: "",
                  cpus: 1,
                  memory: 512,
                  command: null,
                  envVars: [],
                  ports: [],
                  mounts: []
                });
                updateNodeData(node!.id, { podContainers: containers });
              }}>+ Add</button>
            </div>
            {((node?.data as any)?.podContainers || []).map((container: any, i: number) => {
              if (isShowroom && isWettyContainer(container)) return null;
              const wetty = isWettyContainer(container);
              const wettyAttrs = wetty ? parseWettyCommand(container.command) : null;
              const updateWettyField = (patch: Partial<WettyAttrs>) => {
                if (!wettyAttrs) return;
                const containers = [...((node?.data as any)?.podContainers || [])];
                containers[i] = {
                  ...container,
                  command: buildWettyCommand({ ...wettyAttrs, ...patch }),
                };
                updateNodeData(node!.id, { podContainers: containers });
              };
              return (
              <details key={i} style={{ marginBottom: 8 }}>
                <summary style={{ cursor: "pointer", padding: "6px 8px", background: "var(--troshka-surface2)", borderRadius: 6, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <span style={{ fontSize: 12, fontWeight: 600 }}>{container.name || `container-${i}`}</span>
                  <button
                    style={{ background: "none", border: "none", color: "#ef4444", cursor: "pointer", fontSize: 12 }}
                    onClick={(e) => {
                      e.stopPropagation();
                      const containers = [...((node?.data as any)?.podContainers || [])];
                      containers.splice(i, 1);
                      updateNodeData(node!.id, { podContainers: containers });
                    }}
                  >✕</button>
                </summary>
                <div style={{ padding: "8px 0" }}>
                  <div className="props-field">
                    <label className="props-label">Name</label>
                    <input className="props-input" value={container.name || ""} onChange={(e) => {
                      const containers = [...((node?.data as any)?.podContainers || [])];
                      containers[i] = { ...container, name: e.target.value };
                      updateNodeData(node!.id, { podContainers: containers });
                    }} />
                  </div>
                  <div className="props-field">
                    <label className="props-label">Image</label>
                    <input className="props-input" value={container.image || ""} placeholder="registry/org/image:tag" style={{ fontFamily: "monospace", fontSize: 11 }} onChange={(e) => {
                      const containers = [...((node?.data as any)?.podContainers || [])];
                      containers[i] = { ...container, image: e.target.value };
                      updateNodeData(node!.id, { podContainers: containers });
                    }} />
                  </div>
                  <div className="props-row">
                    <div className="props-field">
                      <label className="props-label">CPUs</label>
                      <input className="props-input" type="number" min={1} max={32} value={container.cpus || 1} onChange={(e) => {
                        const containers = [...((node?.data as any)?.podContainers || [])];
                        containers[i] = { ...container, cpus: parseInt(e.target.value) || 1 };
                        updateNodeData(node!.id, { podContainers: containers });
                      }} />
                    </div>
                    <div className="props-field">
                      <label className="props-label">Memory (MB)</label>
                      <input className="props-input" type="number" min={64} step={64} value={container.memory || 512} onChange={(e) => {
                        const containers = [...((node?.data as any)?.podContainers || [])];
                        containers[i] = { ...container, memory: parseInt(e.target.value) || 512 };
                        updateNodeData(node!.id, { podContainers: containers });
                      }} />
                    </div>
                  </div>
                  {wetty && wettyAttrs ? (
                    <>
                      <div className="props-field">
                        <label className="props-label">SSH host</label>
                        <input
                          className="props-input"
                          value={wettyAttrs.sshHost}
                          onChange={(e) => updateWettyField({ sshHost: e.target.value })}
                          style={{ fontFamily: "monospace", fontSize: 11 }}
                        />
                      </div>
                      <div className="props-row">
                        <div className="props-field">
                          <label className="props-label">SSH port</label>
                          <input
                            className="props-input"
                            type="number"
                            min={1}
                            value={wettyAttrs.sshPort}
                            onChange={(e) =>
                              updateWettyField({ sshPort: parseInt(e.target.value, 10) || 22 })
                            }
                          />
                        </div>
                        <div className="props-field">
                          <label className="props-label">Wetty port</label>
                          <input
                            className="props-input"
                            type="number"
                            min={1}
                            value={wettyAttrs.port}
                            onChange={(e) =>
                              updateWettyField({ port: parseInt(e.target.value, 10) || 8001 })
                            }
                          />
                        </div>
                      </div>
                      <div className="props-row">
                        <div className="props-field">
                          <label className="props-label">SSH user</label>
                          <input
                            className="props-input"
                            value={wettyAttrs.sshUser}
                            onChange={(e) => updateWettyField({ sshUser: e.target.value })}
                          />
                        </div>
                        <div className="props-field">
                          <label className="props-label">SSH password</label>
                          <input
                            className="props-input"
                            type="password"
                            value={wettyAttrs.sshPass}
                            onChange={(e) => updateWettyField({ sshPass: e.target.value })}
                          />
                        </div>
                      </div>
                      <div className="props-field">
                        <label className="props-label">URL base path</label>
                        <input
                          className="props-input"
                          placeholder="wetty_control"
                          value={wettyAttrs.basePath}
                          onChange={(e) => updateWettyField({ basePath: e.target.value })}
                          style={{ fontFamily: "monospace", fontSize: 11 }}
                        />
                      </div>
                    </>
                  ) : (
                  <div className="props-field">
                    <label className="props-label">Command</label>
                    <input
                      className="props-input"
                      value={formatCommandForInput(container.command)}
                      placeholder="Optional entrypoint override"
                      style={{ fontFamily: "monospace", fontSize: 11 }}
                      onChange={(e) => {
                        const containers = [...((node?.data as any)?.podContainers || [])];
                        containers[i] = { ...container, command: e.target.value || null };
                        updateNodeData(node!.id, { podContainers: containers });
                      }}
                    />
                  </div>
                  )}
                  <div className="props-field">
                    <label className="props-label">Environment Variables</label>
                    {(container.envVars || []).map((ev: any, evIdx: number) => (
                      <div key={evIdx} style={{ display: "flex", gap: 4, marginBottom: 4, alignItems: "center" }}>
                        <input className="props-input" placeholder="KEY" value={ev.key || ""} style={{ flex: 1, fontFamily: "monospace", fontSize: 11 }} onChange={(e) => {
                          const containers = [...((node?.data as any)?.podContainers || [])];
                          const envVars = [...(containers[i].envVars || [])];
                          envVars[evIdx] = { ...ev, key: e.target.value };
                          containers[i] = { ...container, envVars };
                          updateNodeData(node!.id, { podContainers: containers });
                        }} />
                        <span style={{ color: "var(--troshka-text-dim)" }}>=</span>
                        <input className="props-input" placeholder="value" value={ev.value || ""} style={{ flex: 2, fontFamily: "monospace", fontSize: 11 }} onChange={(e) => {
                          const containers = [...((node?.data as any)?.podContainers || [])];
                          const envVars = [...(containers[i].envVars || [])];
                          envVars[evIdx] = { ...ev, value: e.target.value };
                          containers[i] = { ...container, envVars };
                          updateNodeData(node!.id, { podContainers: containers });
                        }} />
                        <button style={{ background: "none", border: "none", color: "#ef4444", cursor: "pointer", fontSize: 12 }} onClick={() => {
                          const containers = [...((node?.data as any)?.podContainers || [])];
                          const envVars = [...(containers[i].envVars || [])];
                          envVars.splice(evIdx, 1);
                          containers[i] = { ...container, envVars };
                          updateNodeData(node!.id, { podContainers: containers });
                        }}>✕</button>
                      </div>
                    ))}
                    <button style={{ fontSize: 10, padding: "2px 6px" }} className="props-library-btn" onClick={() => {
                      const containers = [...((node?.data as any)?.podContainers || [])];
                      const envVars = [...(containers[i].envVars || [])];
                      envVars.push({ key: "", value: "" });
                      containers[i] = { ...container, envVars };
                      updateNodeData(node!.id, { podContainers: containers });
                    }}>+ Env Var</button>
                  </div>
                  <div className="props-field">
                    <label className="props-label">Ports</label>
                    {(container.ports || []).map((port: any, pIdx: number) => (
                      <div key={pIdx} style={{ display: "flex", gap: 4, marginBottom: 4, alignItems: "center" }}>
                        <input className="props-input" type="number" placeholder="Port" value={port.containerPort || ""} style={{ width: 70 }} onChange={(e) => {
                          const containers = [...((node?.data as any)?.podContainers || [])];
                          const ports = [...(containers[i].ports || [])];
                          ports[pIdx] = { ...port, containerPort: parseInt(e.target.value) || 0 };
                          containers[i] = { ...container, ports };
                          updateNodeData(node!.id, { podContainers: containers });
                        }} />
                        <select className="props-select" value={port.protocol || "tcp"} style={{ width: 60 }} onChange={(e) => {
                          const containers = [...((node?.data as any)?.podContainers || [])];
                          const ports = [...(containers[i].ports || [])];
                          ports[pIdx] = { ...port, protocol: e.target.value as "tcp" | "udp" };
                          containers[i] = { ...container, ports };
                          updateNodeData(node!.id, { podContainers: containers });
                        }}>
                          <option value="tcp">TCP</option>
                          <option value="udp">UDP</option>
                        </select>
                        <button style={{ background: "none", border: "none", color: "#ef4444", cursor: "pointer", fontSize: 12 }} onClick={() => {
                          const containers = [...((node?.data as any)?.podContainers || [])];
                          const ports = [...(containers[i].ports || [])];
                          ports.splice(pIdx, 1);
                          containers[i] = { ...container, ports };
                          updateNodeData(node!.id, { podContainers: containers });
                        }}>✕</button>
                      </div>
                    ))}
                    <button style={{ fontSize: 10, padding: "2px 6px" }} className="props-library-btn" onClick={() => {
                      const containers = [...((node?.data as any)?.podContainers || [])];
                      const ports = [...(containers[i].ports || [])];
                      ports.push({ containerPort: 0, protocol: "tcp" });
                      containers[i] = { ...container, ports };
                      updateNodeData(node!.id, { podContainers: containers });
                    }}>+ Port</button>
                  </div>
                  <div className="props-field">
                    <label className="props-label">Mounts</label>
                    {(container.mounts || []).map((mount: any, mIdx: number) => (
                      <div key={mIdx} style={{ display: "flex", gap: 4, marginBottom: 4, alignItems: "center" }}>
                        <input className="props-input" placeholder="/mount/path" value={mount.mountPath || ""} style={{ flex: 1, fontFamily: "monospace", fontSize: 11 }} onChange={(e) => {
                          const containers = [...((node?.data as any)?.podContainers || [])];
                          const mounts = [...(containers[i].mounts || [])];
                          mounts[mIdx] = { ...mount, mountPath: e.target.value };
                          containers[i] = { ...container, mounts };
                          updateNodeData(node!.id, { podContainers: containers });
                        }} />
                        <button style={{ background: "none", border: "none", color: "#ef4444", cursor: "pointer", fontSize: 12 }} onClick={() => {
                          const containers = [...((node?.data as any)?.podContainers || [])];
                          const mounts = [...(containers[i].mounts || [])];
                          mounts.splice(mIdx, 1);
                          containers[i] = { ...container, mounts };
                          updateNodeData(node!.id, { podContainers: containers });
                        }}>✕</button>
                      </div>
                    ))}
                    <button style={{ fontSize: 10, padding: "2px 6px" }} className="props-library-btn" onClick={() => {
                      const containers = [...((node?.data as any)?.podContainers || [])];
                      const mounts = [...(containers[i].mounts || [])];
                      mounts.push({ diskNodeId: "", mountPath: "" });
                      containers[i] = { ...container, mounts };
                      updateNodeData(node!.id, { podContainers: containers });
                    }}>+ Mount</button>
                  </div>
                </div>
              </details>
              );
            })}
          </div>
                </>
          )}
          <div className="props-divider" />
                  </>
                )}

          {/* Actions are on the container node itself (start/stop/restart/logs) */}
              </>
            );
          })()}
        </>
      )}

      {/* Network Properties */}
      {nodeType === "networkNode" && (() => {
        const and = data as unknown as NetworkNodeData;
        const subtype = and.subtype || "network";
        const portForwards = (data as Record<string, any>).portForwards as Array<{extPort: string; intIp: string; intPort: string; proto: string}> || [];

        return (
          <>
            <div className="props-section">
              <div className="props-section-title">General</div>
              <div className="props-field">
                <label className="props-label">Name</label>
                <input
                  className="props-input"
                  value={(data.name as string) || ""}
                  onChange={(e) => update("name", e.target.value)}
                  style={isDuplicateName((data.name as string) || "", node.id, "networkNode") ? { borderColor: "var(--pf-t--global--color--status--warning--default)" } : undefined}
                />
                {isDuplicateName((data.name as string) || "", node.id, "networkNode") && (
                  <div style={{ color: "var(--pf-t--global--color--status--warning--default)", fontSize: 11, marginTop: 2 }}>Duplicate network name</div>
                )}
              </div>

              {/* Network: CIDR + Services */}
              {subtype === "network" && (
                <>
                  <div className="props-field">
                    <label className="props-label">CIDR</label>
                    {(() => {
                      const currentCidr = and?.cidr;
                      const conflict = nodes.some(
                        (n) => n.type === "networkNode" && n.id !== nodeId &&
                          cidrsOverlap(currentCidr, (n.data as unknown as NetworkNodeData).cidr)
                      );
                      return (
                        <>
                          <input
                            className="props-input"
                            value={currentCidr}
                            onChange={(e) => update("cidr", e.target.value)}
                            style={{ fontFamily: "monospace", borderColor: conflict ? "var(--troshka-red)" : undefined }}
                          />
                          {conflict && (
                            <span style={{ fontSize: 11, color: "var(--troshka-red)", marginTop: 2 }}>
                              ⚠ Overlaps with another network subnet
                            </span>
                          )}
                        </>
                      );
                    })()}
                  </div>
                </>
              )}
            </div>

            {/* Network services (network subtype only) */}
            {subtype === "network" && (
              <>
                <div className="props-divider" />
                <div className="props-section">
                  <div className="props-section-title">Services</div>
                  <div className="props-field">
                    <label className="props-label" style={{ display: "flex", alignItems: "center", gap: 8 }}>
                      <input type="checkbox" checked={and.dhcp ?? false} onChange={(e) => update("dhcp", e.target.checked)} />
                      DHCP
                    </label>
                  </div>
                  {and.dhcp && (
                    <>
                      <div className="props-row">
                        <div className="props-field">
                          <label className="props-label">Range Start</label>
                          <input
                            className="props-input"
                            value={(data as Record<string, any>).dhcpRangeStart as string || ""}
                            onChange={(e) => update("dhcpRangeStart", e.target.value)}
                            placeholder={and?.cidr ? and.cidr.replace(/\.\d+\/\d+$/, ".10") : "x.x.x.10"}
                            style={{ fontFamily: "monospace" }}
                          />
                        </div>
                        <div className="props-field">
                          <label className="props-label">Range End</label>
                          <input
                            className="props-input"
                            value={(data as Record<string, any>).dhcpRangeEnd as string || ""}
                            onChange={(e) => update("dhcpRangeEnd", e.target.value)}
                            placeholder={and?.cidr ? and.cidr.replace(/\.\d+\/\d+$/, ".254") : "x.x.x.254"}
                            style={{ fontFamily: "monospace" }}
                          />
                        </div>
                      </div>
                      <div className="props-field">
                        <label className="props-label">Gateway IP</label>
                        <input
                          className="props-input"
                          value={(data as Record<string, any>).dhcpGateway as string || ""}
                          onChange={(e) => update("dhcpGateway", e.target.value)}
                          placeholder={and?.cidr ? and.cidr.replace(/\.\d+\/\d+$/, ".1") : "x.x.x.1"}
                          style={{ fontFamily: "monospace" }}
                        />
                      </div>
                      {(() => {
                        const dhcpErrors = validateDhcpRangeFull(
                          and?.cidr,
                          (data as Record<string, any>).dhcpRangeStart as string || "",
                          (data as Record<string, any>).dhcpRangeEnd as string || "",
                          (data as Record<string, any>).dhcpGateway as string || "",
                          (data as Record<string, any>).dnsServerIp as string || "",
                        );
                        return dhcpErrors.length > 0 ? (
                          <div className="props-field">
                            {dhcpErrors.map((err, i) => (
                              <span key={i} style={{ fontSize: 11, color: "var(--troshka-red)", display: "block", marginBottom: 2 }}>
                                ⚠ {err}
                              </span>
                            ))}
                          </div>
                        ) : null;
                      })()}
                      <div className="props-field">
                        <label className="props-label">Lease Time</label>
                        <select
                          className="props-select"
                          value={(data as Record<string, any>).dhcpLeaseTime as string || "24h"}
                          onChange={(e) => update("dhcpLeaseTime", e.target.value)}
                        >
                          <option value="1h">1 hour</option>
                          <option value="12h">12 hours</option>
                          <option value="24h">24 hours</option>
                          <option value="7d">7 days</option>
                          <option value="infinite">Infinite</option>
                        </select>
                      </div>
                      <div className="props-divider" />
                      <div className="props-section-title">Network Boot</div>
                      <div className="props-field">
                        <label className="props-label" style={{ display: "flex", alignItems: "center", gap: 8 }}>
                          <input
                            type="checkbox"
                            checked={(data as Record<string, any>).pxeEnabled as boolean ?? false}
                            onChange={(e) => update("pxeEnabled", e.target.checked)}
                          />
                          Enable Network Boot
                        </label>
                      </div>
                      {(data as Record<string, any>).pxeEnabled && (
                        <>
                          <div className="props-field">
                            <label className="props-label">Provider</label>
                            <select className="props-select" value={(data as Record<string, any>).pxeServerMode as string || "builtin"} onChange={(e) => update("pxeServerMode", e.target.value)}>
                              <option value="builtin">Troshka managed</option>
                              <option value="custom">User provided (BYO)</option>
                            </select>
                          </div>
                          <p style={{ fontSize: 11, color: "var(--troshka-text-dim)", marginBottom: 4 }}>
                            {(data as Record<string, any>).pxeServerMode === "custom"
                              ? "Boot server details are configured per-VM in the Boot Devices section."
                              : "Troshka extracts kernel and initrd from the install ISO and serves them automatically. Select the boot ISO per-VM in the Boot Devices section."}
                          </p>
                        </>
                      )}
                    </>
                  )}
                  <div className="props-divider" />
                  <div className="props-field">
                    <label className="props-label" style={{ display: "flex", alignItems: "center", gap: 8 }}>
                      <input type="checkbox" checked={and.dns ?? false} onChange={(e) => update("dns", e.target.checked)} />
                      DNS
                    </label>
                  </div>
                  {and.dns && (
                    <>
                      <div className="props-field">
                        <label className="props-label">DNS Server IP</label>
                        <input
                          className="props-input"
                          value={(data as Record<string, any>).dnsServerIp as string || ""}
                          onChange={(e) => update("dnsServerIp", e.target.value)}
                          placeholder={and?.cidr ? and.cidr.replace(/\.\d+\/\d+$/, ".1") : "DNS server IP"}
                          style={{ fontFamily: "monospace" }}
                        />
                        <span style={{ fontSize: 10, color: "var(--troshka-text-dim)", marginTop: 2 }}>
                          Typically same as gateway. Must be outside DHCP range.
                        </span>
                      </div>
                      <div className="props-field">
                        <label className="props-label">DNS Domain</label>
                        <input className="props-input" value={and.dnsDomain || ""} onChange={(e) => update("dnsDomain", e.target.value)} placeholder="lab.local" style={{ fontFamily: "monospace" }} />
                      </div>
                      <div className="props-field">
                        <label className="props-label" style={{ display: "flex", alignItems: "center", gap: 8 }}>
                          <input
                            type="checkbox"
                            checked={(data as Record<string, any>).dnsUpstream as boolean ?? false}
                            onChange={(e) => update("dnsUpstream", e.target.checked)}
                          />
                          Forward to upstream (internet)
                        </label>
                        <span style={{ fontSize: 10, color: "var(--troshka-text-dim)", marginTop: 2 }}>
                          When off, DNS only resolves internal names.
                        </span>
                      </div>
                      {((data as Record<string, any>).dnsRecords as Array<{name: string; ip: string}> || []).length > 0 && (
                        <div className="props-field">
                          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                            <label className="props-label">DNS Records</label>
                            <button className="troshka-btn-icon" title="Add record" onClick={() => {
                              const records = [...((data as Record<string, any>).dnsRecords || [])];
                              records.push({ name: "", type: "A", ip: "" });
                              update("dnsRecords", records);
                            }}>+</button>
                          </div>
                          <div style={{ overflowX: "auto" }}>
                          {/* Editable (user-authored) records — cluster-managed
                              ones are skipped here and shown grouped below. */}
                          {((data as Record<string, any>).dnsRecords as Array<{name: string; type?: string; ip: string; target?: string; managed?: boolean; clusterId?: string}>).map((rec, i) => {
                            if (rec.managed || rec.clusterId) return null;
                            const displayIp = resolveDnsRecordDisplayIp(rec, nodes, edges, node!.id, vniMap);
                            return (
                            <div key={i} style={{ display: "flex", gap: 4, marginBottom: 3, alignItems: "center" }}>
                              <input className="props-input" style={{ flex: 3, minWidth: 0, fontSize: 10, fontFamily: "monospace" }} value={rec.name} placeholder="hostname" onChange={(e) => {
                                const records = [...((data as Record<string, any>).dnsRecords || [])];
                                records[i] = { ...records[i], name: e.target.value };
                                update("dnsRecords", records);
                              }} />
                              <select className="props-input" style={{ width: 50, fontSize: 10, fontFamily: "monospace" }} value={rec.type || "A"} onChange={(e) => {
                                const records = [...((data as Record<string, any>).dnsRecords || [])];
                                records[i] = { ...records[i], type: e.target.value };
                                update("dnsRecords", records);
                              }}>
                                <option value="A">A</option>
                                <option value="CNAME">CNAME</option>
                                <option value="TXT">TXT</option>
                                <option value="SRV">SRV</option>
                              </select>
                              <input className="props-input" style={{ flex: 2, minWidth: 0, fontSize: 10, fontFamily: "monospace" }} value={rec.ip || displayIp} placeholder={rec.type === "CNAME" ? "target" : displayIp ? displayIp : "IP"} onChange={(e) => {
                                const records = [...((data as Record<string, any>).dnsRecords || [])];
                                records[i] = { ...records[i], ip: e.target.value };
                                update("dnsRecords", records);
                              }} />
                              <button className="troshka-btn-icon-danger" title="Remove" onClick={() => {
                                const records = [...((data as Record<string, any>).dnsRecords || [])];
                                records.splice(i, 1);
                                update("dnsRecords", records);
                              }}>×</button>
                            </div>
                            );
                          })}
                          {/* Cluster-managed records — read-only, grouped by cluster. */}
                          {(() => {
                            const all = ((data as Record<string, unknown>).dnsRecords || []) as Array<{name: string; type?: string; ip: string; managed?: boolean; clusterId?: string}>;
                            const managed = all.filter((r) => r.managed || r.clusterId);
                            if (managed.length === 0) return null;
                            const groups = new Map<string, typeof managed>();
                            for (const r of managed) {
                              const key = r.clusterId || "_unknown";
                              if (!groups.has(key)) groups.set(key, []);
                              groups.get(key)!.push(r);
                            }
                            return Array.from(groups.entries()).map(([cid, recs]) => {
                              const cname = clusters.find((c) => c.id === cid)?.name;
                              return (
                                <div key={cid} style={{ marginTop: 8, borderLeft: "3px solid var(--troshka-accent, #3b82f6)", paddingLeft: 8 }}>
                                  <div style={{ fontSize: 10, fontWeight: 600, color: "var(--troshka-accent, #3b82f6)", marginBottom: 3 }}>
                                    ☸ OpenShift Cluster{cname ? ` ${cname}` : ""} DNS records
                                  </div>
                                  {recs.map((rec, j) => {
                                    const dip = resolveDnsRecordDisplayIp(rec, nodes, edges, node!.id, vniMap);
                                    return (
                                      <div
                                        key={j}
                                        title={`${rec.name} → ${rec.ip || dip}`}
                                        style={{ fontFamily: "monospace", fontSize: 10, lineHeight: 1.5, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}
                                      >
                                        {rec.name}
                                        <span style={{ color: "var(--troshka-text-dim)" }}>{" → "}{rec.ip || dip}</span>
                                      </div>
                                    );
                                  })}
                                </div>
                              );
                            });
                          })()}
                          </div>
                        </div>
                      )}
                      {((data as Record<string, any>).dnsRecords || []).length === 0 && (
                        <div className="props-field">
                          <button className="troshka-btn-icon" style={{ fontSize: 11, width: "100%", padding: "4px 8px" }} onClick={() => {
                            update("dnsRecords", [{ name: "", ip: "" }]);
                          }}>+ Add DNS Record</button>
                        </div>
                      )}
                    </>
                  )}

                  {/* BMC Network Properties */}
                  {(node.data as Record<string, any>).networkType === "bmc" && (
                    <>
                      <div className="props-divider" />
                      <div className="props-field">
                        <label className="props-label">BMC Username</label>
                        <input className="props-input" value={(node.data as Record<string, any>).bmcUsername || "admin"}
                          style={{ fontFamily: "monospace" }}
                          onChange={(e) => update("bmcUsername", e.target.value)} />
                      </div>
                      <div className="props-field">
                        <label className="props-label">BMC Password</label>
                        <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
                          <input className="props-input" type="password"
                            value={(node.data as Record<string, any>).bmcPassword || ""}
                            style={{ fontFamily: "monospace", flex: 1 }}
                            onFocus={(e) => (e.currentTarget.type = "text")}
                            onBlur={(e) => (e.currentTarget.type = "password")}
                            onChange={(e) => update("bmcPassword", e.target.value)} />
                        </div>
                      </div>

                      {/* List BMC-enabled VMs */}
                      {(() => {
                        const allNodes = useCanvasStore.getState().nodes;
                        const bmcVms = allNodes.filter((n) => n.type === "vmNode" && (n.data as Record<string, any>).bmcEnabled);
                        if (bmcVms.length === 0) return null;
                        return (
                          <div style={{ marginTop: 8 }}>
                            <label className="props-label">BMC-Enabled VMs</label>
                            <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
                              {bmcVms.map((vm) => (
                                <div key={vm.id} style={{ fontSize: 11, fontFamily: "monospace", color: "var(--troshka-text-dim)", display: "flex", justifyContent: "space-between" }}>
                                  <span>{(vm.data as Record<string, any>).name || vm.id.slice(0, 8)}</span>
                                  <span>{(vm.data as Record<string, any>).bmcIp || "—"}</span>
                                </div>
                              ))}
                            </div>
                          </div>
                        );
                      })()}
                    </>
                  )}
                </div>
              </>
            )}

            {/* Router properties */}
            {subtype === "router" && (
              <>
                <div className="props-divider" />
                <div className="props-section">
                  <div className="props-section-title">Routing</div>
                  <p style={{ fontSize: 11, color: "var(--troshka-text-dim)", marginBottom: 8 }}>
                    Connect this router to two or more networks to enable L3 forwarding between subnets. Traffic between connected networks is routed automatically.
                  </p>
                  <div className="props-field">
                    <label className="props-label">Static Routes</label>
                    <p style={{ fontSize: 10, color: "var(--troshka-text-dim)", marginBottom: 6 }}>
                      Static routes forward traffic for specific destinations to a next-hop IP. Use these for reaching networks not directly connected to this router, such as sending internet-bound traffic (0.0.0.0/0) to a gateway.
                    </p>
                    <div style={{ fontSize: 11, color: "var(--troshka-text-dim)" }}>
                      {(() => {
                        const routes = (data as Record<string, any>).staticRoutes as Array<{dest: string; nextHop: string}> || [];
                        return routes.length === 0
                          ? <span>No static routes — only connected subnets are routed.</span>
                          : routes.map((r, i) => (
                              <div key={i} style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 4 }}>
                                <input className="props-input" value={r.dest} placeholder="Destination CIDR" style={{ fontFamily: "monospace", fontSize: 11, flex: 1 }}
                                  onChange={(e) => { const updated = [...routes]; updated[i] = { ...r, dest: e.target.value }; update("staticRoutes", updated); }} />
                                <span>→</span>
                                <input className="props-input" value={r.nextHop} placeholder="Next hop IP" style={{ fontFamily: "monospace", fontSize: 11, flex: 1 }}
                                  onChange={(e) => { const updated = [...routes]; updated[i] = { ...r, nextHop: e.target.value }; update("staticRoutes", updated); }} />
                                <button style={{ background: "none", border: "none", color: "var(--troshka-red)", cursor: "pointer", fontSize: 12 }}
                                  onClick={() => update("staticRoutes", routes.filter((_, idx) => idx !== i))}>✕</button>
                              </div>
                            ));
                      })()}
                    </div>
                    <button
                      className="props-library-btn"
                      style={{ marginTop: 6 }}
                      onClick={() => {
                        const routes = (data as Record<string, any>).staticRoutes as Array<{dest: string; nextHop: string}> || [];
                        update("staticRoutes", [...routes, { dest: "", nextHop: "" }]);
                      }}
                    >
                      + Add Static Route
                    </button>
                  </div>
                </div>
              </>
            )}

            {/* Load Balancer properties */}
            {(data as Record<string, any>).networkType === "loadbalancer" && (
              <>
                <div className="props-divider" />
                <div className="props-section">
                  <div style={{ marginBottom: 8 }}>
                    <label className="props-label">Internal Load Balancer IP Address</label>
                    <input className="props-input" value={(data as Record<string, any>).lbIp as string || ""} onChange={(e) => update("lbIp", e.target.value)} placeholder="e.g. 10.0.0.2" />
                  </div>
                  <label style={{ fontSize: 12, display: "flex", alignItems: "center", gap: 8, cursor: "pointer", marginBottom: 8 }}>
                    <input
                      type="checkbox"
                      checked={(data as Record<string, any>).external ?? true}
                      onChange={(e) => update("external", e.target.checked)}
                    />
                    External access via EIP
                  </label>
                  {((data as Record<string, any>).external ?? true) && (() => {
                    const projectIps = useCanvasStore.getState().externalIps;
                    return projectIps.length > 0 ? (
                      <div style={{ marginBottom: 8 }}>
                        <label className="props-label">EIP</label>
                        <select className="props-input" value={(data as Record<string, any>).extIpId || ""} onChange={(e) => update("extIpId", e.target.value)}>
                          <option value="">Auto (first EIP)</option>
                          {projectIps.map((eip: any) => (
                            <option key={eip.id} value={eip.id}>{eip.ip || eip.label || eip.id.substring(0, 8)}</option>
                          ))}
                        </select>
                      </div>
                    ) : null;
                  })()}
                </div>
                <div className="props-divider" />
                <div className="props-section">
                  <div className="props-section-title">Frontends</div>
                  {((data as Record<string, any>).frontends || []).length === 0 && (
                    <p style={{ fontSize: 11, color: "var(--troshka-text-dim)" }}>No frontends configured</p>
                  )}
                  {((data as Record<string, any>).frontends || []).map((fe: any, i: number) => (
                    <div key={i} style={{ display: "flex", gap: 4, marginBottom: 4, alignItems: "center" }}>
                      <input
                        style={{ width: 70, fontSize: 11 }}
                        className="props-input"
                        placeholder="name"
                        value={fe.name}
                        onChange={(e) => {
                          const frontends = [...((data as Record<string, any>).frontends || [])];
                          frontends[i] = { ...frontends[i], name: e.target.value };
                          update("frontends", frontends);
                        }}
                      />
                      <input
                        style={{ width: 50, fontSize: 11 }}
                        className="props-input"
                        type="number"
                        placeholder="bind"
                        value={fe.bindPort || ""}
                        onChange={(e) => {
                          const frontends = [...((data as Record<string, any>).frontends || [])];
                          frontends[i] = { ...frontends[i], bindPort: parseInt(e.target.value) || 0 };
                          update("frontends", frontends);
                        }}
                      />
                      <span style={{ fontSize: 10, color: "var(--troshka-text-dim)" }}>-&gt;</span>
                      <input
                        style={{ width: 50, fontSize: 11 }}
                        className="props-input"
                        type="number"
                        placeholder="back"
                        value={fe.backendPort || ""}
                        onChange={(e) => {
                          const frontends = [...((data as Record<string, any>).frontends || [])];
                          frontends[i] = { ...frontends[i], backendPort: parseInt(e.target.value) || 0 };
                          update("frontends", frontends);
                        }}
                      />
                      <button
                        style={{ background: "none", border: "none", color: "var(--troshka-red)", cursor: "pointer", padding: "2px 4px", fontSize: 14, lineHeight: 1 }}
                        title="Remove"
                        onClick={() => {
                          const frontends = [...((data as Record<string, any>).frontends || [])];
                          frontends.splice(i, 1);
                          update("frontends", frontends);
                        }}
                      >&times;</button>
                    </div>
                  ))}
                  <button
                    className="props-library-btn"
                    style={{ marginTop: 4 }}
                    onClick={() => {
                      const frontends = [...((data as Record<string, any>).frontends || [])];
                      frontends.push({ name: "", bindPort: 0, mode: "tcp", backendPort: 0 });
                      update("frontends", frontends);
                    }}
                  >
                    + Add Frontend
                  </button>
                </div>

                <div className="props-divider" />
                <div className="props-section">
                  <div className="props-section-title">DNS Records</div>
                  {((data as Record<string, any>).dnsRecords || []).length === 0 && (
                    <p style={{ fontSize: 11, color: "var(--troshka-text-dim)" }}>No DNS record templates configured</p>
                  )}
                  {((data as Record<string, any>).dnsRecords || []).map((rec: any, i: number) => (
                    <div key={i} style={{ background: "var(--troshka-surface2)", borderRadius: 6, padding: 8, marginBottom: 6 }}>
                      <div className="props-field" style={{ marginBottom: 4 }}>
                        <label className="props-label">Name Template</label>
                        <input
                          className="props-input"
                          value={rec.name || ""}
                          placeholder="api.{guid}.{domain}"
                          style={{ fontFamily: "monospace", fontSize: 11 }}
                          onChange={(e) => {
                            const dnsRecords = [...((data as Record<string, any>).dnsRecords || [])];
                            dnsRecords[i] = { ...dnsRecords[i], name: e.target.value };
                            update("dnsRecords", dnsRecords);
                          }}
                        />
                      </div>
                      <div className="props-row" style={{ marginBottom: 4 }}>
                        <div className="props-field" style={{ flex: "0 0 60px" }}>
                          <label className="props-label">Type</label>
                          <input
                            className="props-input"
                            value={rec.type || "A"}
                            style={{ fontSize: 11 }}
                            onChange={(e) => {
                              const dnsRecords = [...((data as Record<string, any>).dnsRecords || [])];
                              dnsRecords[i] = { ...dnsRecords[i], type: e.target.value };
                              update("dnsRecords", dnsRecords);
                            }}
                          />
                        </div>
                        <div className="props-field" style={{ flex: 1 }}>
                          <label className="props-label">Target</label>
                          <input
                            className="props-input"
                            value={rec.target || "eip"}
                            style={{ fontFamily: "monospace", fontSize: 11 }}
                            onChange={(e) => {
                              const dnsRecords = [...((data as Record<string, any>).dnsRecords || [])];
                              dnsRecords[i] = { ...dnsRecords[i], target: e.target.value };
                              update("dnsRecords", dnsRecords);
                            }}
                          />
                        </div>
                      </div>
                      <button
                        style={{ background: "none", border: "none", color: "var(--troshka-red)", cursor: "pointer", padding: "2px 4px", fontSize: 12 }}
                        onClick={() => {
                          const dnsRecords = [...((data as Record<string, any>).dnsRecords || [])];
                          dnsRecords.splice(i, 1);
                          update("dnsRecords", dnsRecords);
                        }}
                      >Remove</button>
                    </div>
                  ))}
                  <button
                    className="props-library-btn"
                    style={{ marginTop: 4 }}
                    onClick={() => {
                      const dnsRecords = [...((data as Record<string, any>).dnsRecords || [])];
                      dnsRecords.push({ name: "", type: "A", target: "eip" });
                      update("dnsRecords", dnsRecords);
                    }}
                  >
                    + Add DNS Record
                  </button>
                  <div className="props-field" style={{ marginTop: 8 }}>
                    <label className="props-label">Default TTL</label>
                    <input
                      className="props-input"
                      type="number"
                      value={(data as Record<string, any>).dnsTtl || 30}
                      style={{ width: 80, fontSize: 11 }}
                      onChange={(e) => update("dnsTtl", parseInt(e.target.value) || 30)}
                    />
                  </div>
                </div>
              </>
            )}

            {/* Gateway properties */}
            {subtype === "gateway" && (
              <>
                <div className="props-divider" />
                <div className="props-section">
                  <div className="props-section-title">NAT / Gateway</div>
                  <div className="props-field">
                    <label className="props-label">Mode</label>
                    <select
                      className="props-select"
                      value={(data as Record<string, any>).gatewayMode as string || "nat"}
                      onChange={(e) => update("gatewayMode", e.target.value)}
                    >
                      <option value="nat">NAT (outbound only)</option>
                      <option value="nat-portforward">NAT + Port Forwarding</option>
                    </select>
                  </div>
                  {(data as Record<string, any>).gatewayMode === "nat-portforward" && (() => {
                    const projectIps = useCanvasStore.getState().externalIps;
                    const endpoints = ((data as Record<string, any>).externalEndpoints as Array<{type?: string}>) || [];
                    const hasRoutes = endpoints.some(e => e.type === "route");
                    return projectIps.length === 0 && !hasRoutes ? (
                      <div className="props-field">
                        <span style={{ fontSize: 11, color: "var(--troshka-yellow)" }}>
                          ⚠ No external IPs allocated. Use the External IPs panel in the sidebar to add some.
                        </span>
                      </div>
                    ) : null;
                  })()}
                </div>

                <div className="props-divider" />
                <div className="props-section">
                  <div className="props-section-title">Outbound Rules</div>
                  <div className="props-field">
                    <label className="props-label">Outbound Policy</label>
                    <select
                      className="props-select"
                      value={(data as Record<string, any>).outboundPolicy as string || "allow-all"}
                      onChange={(e) => update("outboundPolicy", e.target.value)}
                    >
                      <option value="allow-all">Allow all outbound</option>
                      <option value="restrict">Restrict by port</option>
                    </select>
                  </div>
                  {(data as Record<string, any>).outboundPolicy === "restrict" && (
                    <OutboundRulesEditor
                      outboundPorts={(data as Record<string, any>).outboundPorts as string || ""}
                      onChange={(value) => update("outboundPorts", value)}
                    />
                  )}
                </div>

                {(data as Record<string, any>).gatewayMode === "nat-portforward" && (
                  <>
                    <div className="props-divider" />
                    <div className="props-section">
                      <div className="props-section-title">Port Forwarding</div>
                      {portForwards.length === 0 && (
                        <p style={{ fontSize: 11, color: "var(--troshka-text-dim)" }}>No port forwards configured</p>
                      )}
                      {(() => {
                        const externalIps = useCanvasStore.getState().externalIps;
                        return portForwards.map((pf, i) => {
                          const showroomManaged = isShowroomManagedForward(pf);
                          const roFieldStyle: React.CSSProperties = {
                            fontFamily: "monospace",
                            fontSize: 11,
                            padding: "6px 8px",
                            borderRadius: 4,
                            border: "1px solid var(--troshka-border)",
                            background: "var(--troshka-surface)",
                            color: "var(--troshka-text-dim)",
                          };
                          if (showroomManaged) {
                            return (
                              <div
                                key={i}
                                style={{
                                  background: "var(--troshka-surface2)",
                                  borderRadius: 6,
                                  padding: 8,
                                  marginBottom: 6,
                                }}
                              >
                                <div
                                  style={{
                                    fontSize: 10,
                                    color: "var(--troshka-text-dim)",
                                    marginBottom: 6,
                                  }}
                                >
                                  Showroom access (auto-managed)
                                </div>
                                <div
                                  className="props-row"
                                  style={{ marginBottom: 4, alignItems: "end" }}
                                >
                                  <div className="props-field" style={{ flex: 1 }}>
                                    <label className="props-label">External IP</label>
                                    <div style={roFieldStyle}>auto</div>
                                  </div>
                                  <div className="props-field" style={{ flex: "0 0 64px" }}>
                                    <label className="props-label">Ext Port</label>
                                    <div style={roFieldStyle}>{pf.extPort}</div>
                                  </div>
                                </div>
                                <div
                                  style={{
                                    textAlign: "center",
                                    color: "var(--troshka-text-dim)",
                                    fontSize: 10,
                                    lineHeight: 1,
                                    margin: "0",
                                  }}
                                >
                                  ↓
                                </div>
                                <div className="props-row" style={{ alignItems: "end" }}>
                                  <div className="props-field" style={{ flex: 1 }}>
                                    <label className="props-label">Internal IP</label>
                                    <div style={roFieldStyle}>auto</div>
                                  </div>
                                  <div className="props-field" style={{ flex: "0 0 64px" }}>
                                    <label className="props-label">Int Port</label>
                                    <div style={roFieldStyle}>auto</div>
                                  </div>
                                </div>
                              </div>
                            );
                          }
                          const isDirty = (form: HTMLFormElement) => {
                            const fd = new FormData(form);
                            return fd.get("extPort") !== (pf.extPort || "") ||
                              fd.get("intPort") !== (pf.intPort || "") ||
                              fd.get("extIpId") !== ((pf as Record<string, string>).extIpId || "") ||
                              fd.get("intIp") !== (pf.intIp || "");
                          };
                          const applyChanges = (form: HTMLFormElement) => {
                            const fd = new FormData(form);
                            const updated = [...portForwards];
                            updated[i] = { ...pf, extPort: fd.get("extPort") as string || "", intPort: fd.get("intPort") as string || "", intIp: fd.get("intIp") as string || "" };
                            (updated[i] as Record<string, string>).extIpId = fd.get("extIpId") as string || "";
                            update("portForwards", updated);
                            const btn = form.querySelector<HTMLElement>("[data-apply]");
                            if (btn) {
                              btn.style.background = "#4ade80"; btn.textContent = "Saved";
                              setTimeout(() => { btn.style.background = ""; btn.textContent = "Apply"; btn.style.display = "none"; }, 800);
                            }
                          };
                          const formSetup = (form: HTMLFormElement | null) => {
                            if (!form || (form as any).__pfBound) return;
                            (form as any).__pfBound = true;
                            const check = () => {
                              const btn = form.querySelector<HTMLElement>("[data-apply]");
                              if (btn) btn.style.display = isDirty(form) ? "" : "none";
                            };
                            form.addEventListener("input", check);
                            form.addEventListener("change", check);
                          };
                          return (
                          <form key={i} ref={formSetup} style={{ background: "var(--troshka-surface2)", borderRadius: 6, padding: 8, marginBottom: 6 }}
                            onSubmit={(e) => { e.preventDefault(); applyChanges(e.currentTarget); }}>
                            <div className="props-row" style={{ marginBottom: 4, alignItems: "end" }}>
                              <div className="props-field" style={{ flex: 1 }}>
                                <label className="props-label">External IP</label>
                                <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
                                  <select className="props-select" name="extIpId" style={{ fontSize: 11, flex: 1 }}
                                    defaultValue={(pf as Record<string, string>).extIpId || ""}
                                    >
                                    {externalIps.length === 0 ? (
                                      <option value="">No IPs — click +</option>
                                    ) : (
                                      <>
                                        <option value="">Select IP...</option>
                                        {externalIps.map((eip) => (
                                          <option key={eip.id} value={eip.id}>{eip.name}{eip.ip ? ` (${eip.ip})` : " (auto)"}</option>
                                        ))}
                                      </>
                                    )}
                                  </select>
                                  {externalIps.length === 0 ? (
                                    <button type="button"
                                      style={{ background: "none", border: "1px solid var(--troshka-cyan)", color: "var(--troshka-cyan)", cursor: "pointer", padding: "2px 8px", flexShrink: 0, borderRadius: 4, fontSize: 11, fontWeight: 600 }}
                                      title="Create an external IP"
                                      onClick={() => {
                                        const newId = `eip-${Date.now()}-${Math.random().toString(36).slice(2, 5)}`;
                                        useCanvasStore.getState().setExternalIps([...externalIps, { id: newId, name: "IP-1", ip: "" }]);
                                        const updated = [...portForwards];
                                        (updated[i] as Record<string, string>).extIpId = newId;
                                        update("portForwards", updated);
                                      }}
                                    >+</button>
                                  ) : (() => {
                                    const selEip = externalIps.find((e) => e.id === (pf as Record<string, string>).extIpId);
                                    return selEip?.ip ? (
                                      <button type="button"
                                        style={{ background: "none", border: "none", color: "var(--troshka-cyan)", cursor: "pointer", padding: 0, flexShrink: 0, opacity: 0.7, transition: "opacity 0.15s" }}
                                        onMouseEnter={(e) => (e.currentTarget.style.opacity = "1")}
                                        onMouseLeave={(e) => (e.currentTarget.style.opacity = "0.7")}
                                        title={`Copy ${selEip.ip}`}
                                        onClick={(e) => { navigator.clipboard.writeText(selEip.ip); const btn = e.currentTarget; const orig = btn.innerHTML; btn.innerHTML = '<span style="font-size:10px">Copied IP</span>'; setTimeout(() => { btn.innerHTML = orig; }, 1000); }}
                                      ><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg></button>
                                    ) : null;
                                  })()}
                                </div>
                              </div>
                              <div className="props-field" style={{ flex: "0 0 64px" }}>
                                <label className="props-label">Ext Port</label>
                                <input className="props-input" name="extPort" defaultValue={pf.extPort} placeholder="80" style={{ fontFamily: "monospace" }}
                                  onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); const f = e.currentTarget.closest("form"); if (f) applyChanges(f); } }}
                                />
                              </div>
                            </div>
                            <div style={{ textAlign: "center", color: "var(--troshka-text-dim)", fontSize: 10, lineHeight: 1, margin: "0" }}>↓</div>
                            <div className="props-row" style={{ alignItems: "end" }}>
                              <div className="props-field" style={{ flex: 1 }}>
                                <label className="props-label">Internal IP</label>
                                {(() => {
                                  const vmIps: { ip: string; vmName: string }[] = [];
                                  for (const n of nodes) {
                                    if (n.type !== "vmNode") continue;
                                    const vmData = n.data as Record<string, any>;
                                    for (const nic of (vmData.nics || []) as Array<Record<string, any>>) {
                                      if (nic.ip) vmIps.push({ ip: nic.ip, vmName: vmData.name || vmData.label || "" });
                                    }
                                  }
                                  const isCustom = (pf.intIp && !vmIps.some((v) => v.ip === pf.intIp));
                                  return isCustom ? (
                                    <div style={{ display: "flex", gap: 4 }}>
                                      <input className="props-input" name="intIp" defaultValue={pf.intIp.trim()} placeholder="e.g. 192.168.1.10" style={{ fontFamily: "monospace", flex: 1 }}
                                        autoFocus
                                        onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); const f = e.currentTarget.closest("form"); if (f) applyChanges(f); } }}
                                      />
                                      {vmIps.length > 0 && (
                                        <button type="button" style={{ background: "none", border: "none", color: "var(--troshka-text-dim)", cursor: "pointer", padding: "0 2px", fontSize: 10, flexShrink: 0 }}
                                          title="Switch to VM picker"
                                          onClick={() => { const updated = [...portForwards]; updated[i] = { ...pf, intIp: "" }; update("portForwards", updated); }}
                                        >▾</button>
                                      )}
                                    </div>
                                  ) : (
                                    <select className="props-select" name="intIp" style={{ fontFamily: "monospace", fontSize: 11 }}
                                      defaultValue={pf.intIp}
                                      onChange={(e) => {
                                        if (e.target.value === "__other__") {
                                          const updated = [...portForwards]; updated[i] = { ...pf, intIp: " " }; update("portForwards", updated);
                                        } else {
                                          /* dirty check handled by native listener */
                                        }
                                      }}>
                                      <option value="">Select VM...</option>
                                      {vmIps.map((v) => (
                                        <option key={v.ip} value={v.ip}>{v.ip} ({v.vmName})</option>
                                      ))}
                                      <option disabled style={{ fontSize: 9 }}>──────────</option>
                                      <option value="__other__">Other...</option>
                                    </select>
                                  );
                                })()}
                              </div>
                              <div className="props-field" style={{ flex: "0 0 64px" }}>
                                <label className="props-label">Int Port</label>
                                <input className="props-input" name="intPort" defaultValue={pf.intPort} placeholder="80" style={{ fontFamily: "monospace" }}
                                  onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); const f = e.currentTarget.closest("form"); if (f) applyChanges(f); } }}
                                />
                              </div>
                            </div>
                            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: 4 }}>
                              <button type="button"
                                style={{ background: "none", border: "none", color: "var(--troshka-red)", cursor: "pointer", padding: "4px", fontSize: 12 }}
                                onClick={() => { update("portForwards", portForwards.filter((_, idx) => idx !== i)); }}
                              >✕ Remove</button>
                              <button type="submit" data-apply style={{ display: "none", background: "var(--troshka-cyan)", color: "#000", border: "none", cursor: "pointer", padding: "3px 12px", borderRadius: 4, fontSize: 11, fontWeight: 600, transition: "background 0.2s" }}
                              >Apply</button>
                            </div>
                            {(() => {
                              const errors: string[] = [];
                              if (!(pf as Record<string, string>).extIpId) errors.push("External IP required");
                              if (!pf.extPort) errors.push("External port required");
                              if (!pf.intIp) errors.push("Internal IP required");
                              if (!pf.intPort) errors.push("Internal port required");
                              if (pf.extPort && !/^\d+$/.test(pf.extPort)) errors.push("External port must be a number");
                              if (pf.extPort === "22" && useCanvasStore.getState().providerType === "ocpvirt") errors.push("Port 22 is blocked on OCP Virt — use another port");
                              if (pf.intPort && !/^\d+$/.test(pf.intPort)) errors.push("Internal port must be a number");
                              if (pf.intIp && !/^\d+\.\d+\.\d+\.\d+$/.test(pf.intIp)) errors.push("Invalid internal IP format");
                              return errors.length > 0 ? (
                                <div style={{ gridColumn: "1 / -1", marginTop: 4 }}>
                                  {errors.map((err, ei) => (
                                    <span key={ei} style={{ fontSize: 10, color: "var(--troshka-red)", display: "block" }}>⚠ {err}</span>
                                  ))}
                                </div>
                              ) : null;
                            })()}
                          </form>
                        );
                        });
                      })()}
                      <button
                        className="props-library-btn"
                        style={{ marginTop: 4 }}
                        onClick={() => {
                          const firstIp = useCanvasStore.getState().externalIps[0];
                          update("portForwards", [...portForwards, { extPort: "", intIp: "", intPort: "", proto: "tcp", extIpId: firstIp?.id || "" }]);
                        }}
                      >
                        + Add Port Forward
                      </button>
                    </div>
                  </>
                )}
              </>
            )}
          </>
        );
      })()}

      {/* Storage Properties */}
      {nodeType === "storageNode" && (() => {
        const sd = data as unknown as StorageNodeData;
        const isIso = sd.format === "iso";
        const connVmEdge = edges.find((e) => e.source === node.id || e.target === node.id);
        const connVmId = connVmEdge ? (connVmEdge.source === node.id ? connVmEdge.target : connVmEdge.source) : null;
        const diskIsDeployed = connVmId ? useCanvasStore.getState().deployedVmIds.has(connVmId) : false;
        return (
          <>
            <div className="props-section">
              <div className="props-section-title">General</div>
              <div className="props-field">
                <label className="props-label">Name</label>
                <input
                  className="props-input"
                  value={(data.name as string) || ""}
                  onChange={(e) => update("name", e.target.value)}
                  style={isDuplicateName((data.name as string) || "", node.id, "storageNode") ? { borderColor: "var(--pf-t--global--color--status--warning--default)" } : undefined}
                />
                {isDuplicateName((data.name as string) || "", node.id, "storageNode") && (
                  <div style={{ color: "var(--pf-t--global--color--status--warning--default)", fontSize: 11, marginTop: 2 }}>Duplicate disk name</div>
                )}
              </div>
              <div className="props-field">
                <label className="props-label">Type</label>
                {isIso ? (
                  <span style={{ fontSize: 13 }}>ISO Image</span>
                ) : (
                  <select
                    className="props-select"
                    value={sd.format}
                    onChange={(e) => update("format", e.target.value)}
                  >
                    <option value="qcow2">QCOW2</option>
                    <option value="raw">Raw</option>
                  </select>
                )}
              </div>
            </div>
            <div className="props-divider" />

            {isIso ? (
              <div className="props-section">
                <div className="props-section-title">ISO Image</div>
                <div className="props-field">
                  <label className="props-label">Source</label>
                  <button
                    className="props-library-btn"
                    onClick={() => setShowLibraryPicker("iso")}
                  >
                    📚 Select from Library...
                  </button>
                  {(data as Record<string, any>).libraryItemName ? (
                    <span style={{ fontSize: 12, marginTop: 4, display: "block", color: "var(--troshka-green)" }}>
                      💿 {(data as Record<string, any>).libraryItemName as string}
                    </span>
                  ) : (
                    <span style={{ fontSize: 11, color: "var(--troshka-text-dim)", marginTop: 4, display: "block" }}>
                      No ISO selected
                    </span>
                  )}
                </div>
                {(data as Record<string, any>).libraryItemSize && (
                  <div className="props-field">
                    <label className="props-label">Size</label>
                    <span style={{ fontSize: 13, color: "var(--troshka-text-dim)" }}>
                      {(data as Record<string, any>).libraryItemSize as number} GB (read-only)
                    </span>
                  </div>
                )}
              </div>
            ) : (
              <div className="props-section">
                <div className="props-section-title">Disk</div>
                <div className="props-field">
                  <label className="props-label">Source</label>
                  <select
                    className="props-select"
                    value={(data as Record<string, any>).source as string || "blank"}
                    onChange={(e) => {
                      update("source", e.target.value);
                      if (e.target.value === "blank") {
                        update("libraryItemId", undefined);
                        update("libraryItemName", undefined);
                        update("libraryItemSize", undefined);
                      }
                    }}
                  >
                    <option value="blank">Blank disk</option>
                    <option value="library">From library image...</option>
                  </select>
                </div>
                {(data as Record<string, any>).source === "library" && (
                  <div className="props-field">
                    <button
                      className="props-library-btn"
                      onClick={() => setShowLibraryPicker("image")}
                    >
                      📚 Select from Library...
                    </button>
                    {(data as Record<string, any>).libraryItemName ? (
                      <span style={{ fontSize: 12, marginTop: 4, display: "block", color: "var(--troshka-green)" }}>
                        🛢 {(data as Record<string, any>).libraryItemName as string}
                      </span>
                    ) : (
                      <span style={{ fontSize: 11, color: "var(--troshka-text-dim)", marginTop: 4, display: "block" }}>
                        No image selected
                      </span>
                    )}
                  </div>
                )}
                <div className="props-row">
                  <div className="props-field">
                    {(() => {
                      const isFromLibrary = (data as Record<string, any>).source === "library";
                      const sourceImageSize = (data as Record<string, any>).libraryItemSize as number || 0;
                      const currentSize = sd.size;
                      const baseMin = isFromLibrary && sourceImageSize > 0 ? sourceImageSize : 1;
                      const deployedSize = (useCanvasStore.getState().deployedDiskSizes as Record<string, number>)[node.id] || 0;
                      const minSize = Math.max(baseMin, deployedSize);
                      const tooSmall = currentSize < minSize;

                      return (
                        <>
                          <label className="props-label">Size (GB)</label>
                          <DiskSizeInput value={currentSize} min={minSize} onChange={(v) => update("size", v)} />
                          {minSize > 1 && (
                            <span style={{ fontSize: 10, color: "var(--troshka-text-dim)", marginTop: 2 }}>
                              Min {minSize} GB{deployedSize > 0 ? " (deployed)" : " (source image)"}
                            </span>
                          )}
                        </>
                      );
                    })()}
                  </div>
                  <div className="props-field">
                    <label className="props-label">Format</label>
                    <select
                      className="props-select"
                      value={sd.format}
                      onChange={(e) => update("format", e.target.value)}
                    >
                      <option value="qcow2">qcow2</option>
                      <option value="raw">raw</option>
                    </select>
                  </div>
                </div>
                <div className="props-field">
                  <label className="props-label" style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <input
                      type="checkbox"
                      checked={(data as Record<string, any>).bootable as boolean ?? true}
                      onChange={(e) => update("bootable", e.target.checked)}
                    />
                    Bootable
                  </label>
                  <span style={{ fontSize: 10, color: "var(--troshka-text-dim)", marginTop: 2 }}>
                    Bootable disks appear in the VM boot device order.
                  </span>
                </div>
              </div>
            )}
            {!isIso && diskIsDeployed && (
              <>
                <div className="props-divider" />
                <div className="props-section">
                  <button
                    className="props-library-btn"
                    style={{ color: "#ef4444", borderColor: "#ef4444" }}
                    onClick={() => {
                      const vmNode = connVmId ? nodes.find((n) => n.id === connVmId) : null;
                      const vmIsRunning = vmNode ? (vmNode.data as unknown as VMNodeData).status === "running" : false;
                      setWipeDiskRestart(vmIsRunning);
                      setWipeDiskModal({ diskNodeId: node.id, connVmId: connVmId || "", vmIsRunning, diskName: (sd.name || "disk") });
                    }}
                  >
                    Wipe Disk
                  </button>
                </div>
              </>
            )}
          </>
        );
      })()}

      {nodeType === "clusterNode" && (() => {
        const clusterId = (data.clusterId as string) || node.id.replace(/^cluster-/, "");
        const cluster = clusters.find((c) => c.id === clusterId);
        if (!cluster) return null;
        const editCluster = (patch: Partial<ClusterConfig>) => {
          updateCluster(clusterId, patch);
          const mirror = clusterSummaryMirror(patch);
          if (Object.keys(mirror).length) updateNodeData(node.id, mirror);
          // Cluster-managed console proxy tabs derive their name + hosts from the
          // cluster, so a rename (or base-domain change) re-syncs them.
          if (patch.name !== undefined || patch.baseDomain !== undefined) {
            const updated = { ...cluster, ...patch };
            const store = useCanvasStore.getState();
            for (const n of store.nodes) {
              if (n.type !== "containerNode") continue;
              const tabs = (n.data as Record<string, unknown>).showroomTabs as ShowroomTab[] | undefined;
              if (!tabs) continue;
              const synced = syncClusterProxyTabs(tabs, updated);
              if (synced) store.updateShowroomTabs(n.id, synced);
            }
          }
        };
        // Count/type edits materialize member VMs. reconcile appends any new
        // members after the (already-present) boundary node, so React Flow's
        // parent-before-child ordering is preserved. reconcile itself only
        // setState's nodes; topologyDirty is recomputed by the preceding
        // editCluster -> updateCluster call, so no explicit dirty call here.
        const reconcile = (updated: ClusterConfig) => {
          const { nodes: nextNodes, edges: nextEdges } = reconcileClusterVms(
            updated,
            useCanvasStore.getState().nodes,
          );
          useCanvasStore.getState().pushHistory();
          const currentEdges = useCanvasStore.getState().edges;
          // Merge new edges: add/replace by id, dedupe
          const edgeMap = new Map(currentEdges.map((e) => [e.id, e]));
          nextEdges.forEach((e) => edgeMap.set(e.id, e));
          useCanvasStore.setState({ nodes: nextNodes, edges: Array.from(edgeMap.values()) });
        };
        // Per-role sizing edits must reach EXISTING generated member VMs, not
        // just clusters[] — otherwise the sizing controls are no-ops for
        // already-materialized members.
        const handleSizing = (patch: Partial<ClusterConfig>) => {
          editCluster(patch);
          const sized = applyClusterSizing({ ...cluster, ...patch }, useCanvasStore.getState().nodes);
          if (sized !== useCanvasStore.getState().nodes) {
            useCanvasStore.getState().pushHistory();
            useCanvasStore.setState({ nodes: sized });
          }
        };
        const handleTypeChange = (type: string) => {
          const controlPlane = type === "sno" ? 1 : 3;
          // Only standard clusters have separate workers; SNO/compact force 0.
          const workers = type === "standard" ? cluster.workers : 0;
          editCluster({ type, controlPlane, workers });
          reconcile({ ...cluster, type, controlPlane, workers });
        };
        const handleWorkersChange = (workers: number) => {
          editCluster({ workers });
          reconcile({ ...cluster, workers });
        };
        const handleNetworksChange = (networkIds: string[]) => {
          editCluster({ networkIds });
          const updated = { ...cluster, networkIds };
          const { nodes: nextNodes, edges: nextEdges } = applyClusterNetworks(
            updated,
            useCanvasStore.getState().nodes,
            useCanvasStore.getState().edges,
          );
          // Sync the visible box↔network anchor edges to networkIds: drop this
          // cluster's existing anchor edges, then add one per selected network
          // (checking a network draws the line; unchecking removes it). Matches
          // the box-handle onConnect anchor edge (Task 11).
          const anchorEdges = networkIds.map(
            (netId) =>
              ({
                id: `edge-clusternet-${netId}-to-${cluster.nodeId}`,
                source: netId,
                target: cluster.nodeId,
                sourceHandle: "bottom",
                targetHandle: "cluster-net-top",
                type: "smoothstep",
                animated: true,
                style: { stroke: "rgba(34,211,238,0.7)", strokeWidth: 2 },
              }) as Edge,
          );
          const withoutOldAnchors = nextEdges.filter(
            (e) =>
              !(
                e.target === cluster.nodeId &&
                typeof e.targetHandle === "string" &&
                e.targetHandle.startsWith("cluster-net")
              ),
          );
          // An OCP cluster needs DNS on exactly ONE member network (its DNS
          // network) to resolve api/api-int/apps — enable DNS there so the
          // requirement is satisfied on select (backend writes to one node too).
          const dnsNetId = effectiveDnsNetworkId({ ...cluster, networkIds });
          const nodesWithDns = dnsNetId
            ? nextNodes.map((n) =>
                n.id === dnsNetId && n.type === "networkNode"
                  ? { ...n, data: { ...(n.data as Record<string, unknown>), dns: true } }
                  : n,
              )
            : nextNodes;
          useCanvasStore.getState().pushHistory();
          useCanvasStore.setState({
            nodes: nodesWithDns,
            edges: [...withoutOldAnchors, ...anchorEdges],
          });
        };
        const handleDisksChange = (role: "control-plane" | "worker", disks: DiskSpec[]) => {
          const patch = role === "control-plane" ? { controlPlaneDisks: disks } : { workerDisks: disks };
          editCluster(patch);
          const updated = { ...cluster, ...patch };
          const { nodes: nextNodes, edges: nextEdges } = applyClusterDisks(
            updated,
            useCanvasStore.getState().nodes,
            useCanvasStore.getState().edges,
          );
          useCanvasStore.getState().pushHistory();
          useCanvasStore.setState({ nodes: nextNodes, edges: nextEdges });
        };
        // Get available networks from canvas (networkNodes with subtype === "network", excluding bmc)
        const availableNetworks = nodes
          .filter((n) => {
            const d = n.data as Record<string, unknown>;
            const isBmc = d?.networkType === "bmc";
            return n.type === "networkNode" && d?.subtype === "network" && !isBmc;
          })
          .map((n) => {
            const d = n.data as Record<string, unknown>;
            return {
              id: n.id,
              label: (d?.label || d?.name || n.id) as string,
              cidr: (d?.cidr || d?.subnet) as string | undefined,
              dns: Boolean(d?.dns),
            };
          });
        return (
          <ClusterEditor
            cluster={cluster}
            clusters={clusters}
            onPatch={editCluster}
            onSizing={handleSizing}
            onTypeChange={handleTypeChange}
            onWorkersChange={handleWorkersChange}
            onNetworksChange={handleNetworksChange}
            onDisksChange={handleDisksChange}
            availableNetworks={availableNetworks}
            nodes={nodes}
            ocpVersions={ocpVersions}
          />
        );
      })()}

      {/* Redeploy VM button */}
      {nodeType === "vmNode" && useCanvasStore.getState().deployedVmIds.has(node.id) && (
        <>
          <div className="props-divider" />
          <div className="props-section">
            <button
              className="props-library-btn"
              style={{ color: "#ef4444", borderColor: "#ef4444" }}
              onClick={async () => {
                const vmName = (data as unknown as VMNodeData).name;
                if (!(await appConfirm({
                  title: "Redeploy VM",
                  message: `Redeploy ${vmName}? This will destroy and recreate this VM (disk data will be lost).`,
                  confirmLabel: "Redeploy",
                  variant: "danger",
                }))) return;
                const projectId = useCanvasStore.getState().currentProjectId;
                updateNodeData(node.id, { status: "redeploying" });
                const resp = await fetch(`/api/v1/projects/${projectId}/vms/${node.id}/redeploy`, { method: "POST" });
                const result = await resp.json();
                if (result.status === "redeploying") {
                  updateNodeData(node.id, { status: "redeploying" });
                } else {
                  updateNodeData(node.id, { status: "stopped" });
                  setAlertMsg(`Redeploy failed: ${result.output || result.error || "unknown error"}`);
                }
              }}
            >
              🔄 Redeploy This VM
            </button>
          </div>
        </>
      )}

      {/* Redeploy container/showroom button */}
      {nodeType === "containerNode" && projectState === "active" && (
        <>
          <div className="props-divider" />
          <div className="props-section">
            <button
              className="props-library-btn"
              onClick={async () => {
                const label = (node.data as Record<string, unknown>)?.isShowroom
                  ? "showroom"
                  : ((node.data as Record<string, unknown>)?.name as string) || "container";
                if (!(await appConfirm({
                  title: "Redeploy Container",
                  message: `Redeploy the ${label}? It is destroyed and recreated, rebuilding content from the repo. VMs are not affected.`,
                  confirmLabel: "Redeploy",
                }))) return;
                const projectId = useCanvasStore.getState().currentProjectId;
                if (!projectId) return;
                const resp = await fetch(`/api/v1/projects/${projectId}/containers/${node.id}/redeploy`, { method: "POST" });
                if (!resp.ok) {
                  const err = await resp.json().catch(() => ({ detail: "Redeploy failed" }));
                  setAlertMsg(err.detail || "Redeploy failed");
                }
              }}
            >
              ♻ Redeploy This Container
            </button>
          </div>
        </>
      )}

      {/* Delete button — disabled for cluster-member VMs (managed by the OCP box) */}
      <div className="props-divider" />
      <div className="props-section">
        {(() => {
          const isMemberVm =
            nodeType === "vmNode" && !!(data as Record<string, unknown>).clusterId;
          return (
            <button
              className="props-delete-btn"
              disabled={isMemberVm}
              title={
                isMemberVm
                  ? "Managed by the OpenShift cluster — change the cluster's node counts to add or remove members."
                  : undefined
              }
              style={isMemberVm ? { opacity: 0.5, cursor: "not-allowed" } : undefined}
              onClick={() => {
                if (!isMemberVm) deleteNode(node.id);
              }}
            >
              Delete {nodeType === "vmNode" ? "VM" : nodeType === "clusterNode" ? "Cluster" : nodeType === "networkNode" ? (
                (data as unknown as NetworkNodeData).subtype === "router" ? "Router" :
                (data as unknown as NetworkNodeData).subtype === "gateway" ? "Gateway" : "Network"
              ) : "Storage"}
            </button>
          );
        })()}
      </div>
      {containerLogs && (
        <div
          style={{
            position: "fixed",
            top: 0,
            left: 0,
            right: 0,
            bottom: 0,
            background: "rgba(0, 0, 0, 0.7)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            zIndex: 10000,
          }}
          onClick={() => setContainerLogs(null)}
        >
          <div
            style={{
              background: "var(--troshka-surface1)",
              border: "1px solid var(--troshka-border)",
              borderRadius: 8,
              maxWidth: "90vw",
              maxHeight: "80vh",
              display: "flex",
              flexDirection: "column",
              overflow: "hidden",
            }}
            onClick={(e) => e.stopPropagation()}
          >
            <div
              style={{
                padding: 16,
                borderBottom: "1px solid var(--troshka-border)",
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                background: "var(--troshka-surface)",
                borderRadius: "12px 12px 0 0",
              }}
            >
              <div>
                <div style={{ fontSize: 16, fontWeight: 600 }}>Container Logs</div>
                <div style={{ fontSize: 11, color: "var(--troshka-text-dim)", marginTop: 4 }}>
                  {containerLogs.containerName}
                </div>
              </div>
              <div style={{ display: "flex", gap: 8 }}>
                <button
                  className="props-library-btn"
                  onClick={async () => {
                    const projectId = useCanvasStore.getState().currentProjectId;
                    if (!projectId) return;
                    try {
                      const resp = await fetch(`/api/v1/projects/${projectId}/containers/${containerLogs.containerId}/logs?tail=500`);
                      if (!resp.ok) {
                        setAlertMsg(`Failed to refresh logs: ${resp.statusText}`);
                        return;
                      }
                      const data = await resp.json();
                      setContainerLogs({ ...containerLogs, logs: data.logs });
                    } catch (err) {
                      console.error("Failed to refresh logs:", err);
                      setAlertMsg(`Error refreshing logs: ${err}`);
                    }
                  }}
                  style={{ marginBottom: 0 }}
                >
                  🔄 Refresh
                </button>
                <button
                  style={{
                    background: "none",
                    border: "none",
                    color: "var(--troshka-text)",
                    cursor: "pointer",
                    fontSize: 18,
                    padding: 0,
                  }}
                  onClick={() => setContainerLogs(null)}
                >
                  ✕
                </button>
              </div>
            </div>
            <div
              style={{
                padding: 16,
                overflow: "auto",
                fontFamily: "monospace",
                fontSize: 11,
                whiteSpace: "pre-wrap",
                background: "var(--troshka-surface2)",
                minWidth: "60vw",
                minHeight: "40vh",
              }}
            >
              {containerLogs.logs || "(no logs)"}
            </div>
          </div>
        </div>
      )}
      {showPxeIsoPicker && node && (
        <LibraryPicker
          type="iso"
          onSelect={(item) => {
            updateNodeData(node.id, {
              pxeBootIsoId: item.id,
              pxeBootIsoName: item.name,
            });
          }}
          onClose={() => setShowPxeIsoPicker(false)}
        />
      )}
      {showLibraryPicker && node && (
        <LibraryPicker
          type={showLibraryPicker}
          onSelect={(item) => {
            updateNodeData(node.id, {
              libraryItemId: item.id,
              libraryItemName: item.name,
              libraryItemSize: item.size_gb,
              source: "library",
              size: Math.max(item.size_gb, (data as Record<string, any>).size as number || 0),
              format: item.format === "iso" ? "iso" : item.format,
            });
          }}
          onClose={() => setShowLibraryPicker(null)}
        />
      )}
      {wipeDiskModal && (
        <div className="start-order-overlay" onClick={() => { if (!wipeDiskLoading) setWipeDiskModal(null); }}>
          <div className="start-order-modal" style={{ maxWidth: 460 }} onClick={(e) => e.stopPropagation()}>
            <div className="start-order-header">
              <span>Wipe Disk</span>
              <button onClick={() => { if (!wipeDiskLoading) setWipeDiskModal(null); }}>&#x2715;</button>
            </div>
            <div className="start-order-body" style={{ padding: 16 }}>
              {!wipeDiskLoading ? (
                <>
                  <div style={{
                    padding: "8px 12px", marginBottom: 16, borderRadius: 6,
                    background: "rgba(239,68,68,0.12)", border: "1px solid rgba(239,68,68,0.3)",
                    color: "#f87171", fontSize: 13,
                  }}>
                    This will erase the boot sector and partition table on this disk. All data will be lost.
                  </div>
                  <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13, cursor: "pointer" }}>
                    <input
                      type="checkbox"
                      checked={wipeDiskRestart}
                      onChange={(e) => setWipeDiskRestart(e.target.checked)}
                    />
                    Restart VM after wipe
                  </label>
                </>
              ) : (
                <div style={{ textAlign: "center", padding: "20px 0" }}>
                  <div style={{ fontSize: 14, color: "#ccc", marginBottom: 8 }}>Wiping disk &quot;{wipeDiskModal.diskName}&quot;...</div>
                  <div style={{ fontSize: 12, color: "#888" }}>Starting VM, wiping boot sector, then stopping.</div>
                  <div style={{ fontSize: 12, color: "#888", marginTop: 4 }}>This may take up to 2 minutes.</div>
                </div>
              )}
            </div>
            <div className="start-order-footer">
              {!wipeDiskLoading ? (
                <>
                  <button className="start-order-btn cancel" onClick={() => setWipeDiskModal(null)}>Cancel</button>
                  <button
                    className="start-order-btn save"
                    style={{ background: "rgba(239,68,68,0.15)", borderColor: "#ef4444", color: "#ef4444" }}
                    onClick={async () => {
                      setWipeDiskLoading(true);
                      try {
                        const projectId = useCanvasStore.getState().currentProjectId;
                        const resp = await fetch(`/api/v1/projects/${projectId}/vms/${wipeDiskModal.connVmId}/disks/${wipeDiskModal.diskNodeId}/wipe?restart=${wipeDiskRestart}`, { method: "POST" });
                        if (!resp.ok) {
                          const err = await resp.json().catch(() => ({ detail: "Unknown error" }));
                          setAlertMsg(`Wipe failed: ${err.detail || err.error || resp.statusText}`);
                          setWipeDiskLoading(false);
                          setWipeDiskModal(null);
                          return;
                        }
                      } catch {
                        setAlertMsg("Failed to connect to server");
                        setWipeDiskLoading(false);
                        setWipeDiskModal(null);
                        return;
                      }
                      const pollWipe = async () => {
                        const projectId2 = useCanvasStore.getState().currentProjectId;
                        try {
                          const r = await fetch(`/api/v1/projects/${projectId2}/vms/${wipeDiskModal.connVmId}/disks/${wipeDiskModal.diskNodeId}/wipe-status`);
                          if (r.ok) {
                            const d = await r.json();
                            if (d.status === "done" || d.status === "error") {
                              setWipeDiskLoading(false);
                              setWipeDiskModal(null);
                              setAlertMsg(d.status === "done" ? `Disk "${wipeDiskModal.diskName}" wiped successfully.` : `Wipe failed: ${d.detail || "unknown error"}`);
                              return;
                            }
                          }
                        } catch { /* ignore */ }
                        setTimeout(pollWipe, 3000);
                      };
                      setTimeout(pollWipe, 3000);
                    }}
                  >
                    Wipe
                  </button>
                </>
              ) : null}
            </div>
          </div>
        </div>
      )}
      <AlertModal message={alertMsg} onClose={() => setAlertMsg(null)} />
    </div>
  );
}
