"use client";

import React, { useState, useEffect, useRef } from "react";
import LibraryPicker from "./LibraryPicker";
import { useCanvasStore, generateNicId, generateDiskControllerId, generateMac, syncBmcNetwork, allocateBmcIp } from "@/stores/canvasStore";
import type {
  VMNodeData,
  NetworkNodeData,
  StorageNodeData,
  ContainerNodeData,
} from "@/stores/canvasStore";

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

export default function PropertiesPanel() {
  const selectedNodeId = useCanvasStore((s) => s.selectedNodeId);
  const nodes = useCanvasStore((s) => s.nodes);
  const edges = useCanvasStore((s) => s.edges);
  const updateNodeData = useCanvasStore((s) => s.updateNodeData);
  const deleteNode = useCanvasStore((s) => s.deleteNode);
  const projectState = useCanvasStore((s) => s.projectState);
  const panelLocked = ["deploying", "reconfiguring", "starting", "stopping"].includes(projectState);
  const [showLibraryPicker, setShowLibraryPicker] = useState<"iso" | "image" | null>(null);
  const [showPxeIsoPicker, setShowPxeIsoPicker] = useState(false);
  const [showPassword, setShowPassword] = useState(false);
  const [sshKeys, setSshKeys] = useState<SshKeyOption[]>([]);
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({ boot: true, cloudinit: true, nics: true, disks: true, bmc: true, tags: true });
  const [containerLogs, setContainerLogs] = useState<{ containerId: string; logs: string; containerName: string } | null>(null);

  React.useEffect(() => {
    fetch("/api/v1/auth/ssh-keys")
      .then((r) => r.ok ? r.json() : [])
      .then((data) => setSshKeys(Array.isArray(data) ? data : []))
      .catch(() => {});
  }, []);

  const node = nodes.find((n) => n.id === selectedNodeId);

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
            <div className="props-section-title" style={{ cursor: "pointer", display: "flex", alignItems: "center", gap: 6 }} onClick={() => toggleSection("io")}>
              <span style={{ fontSize: 8, transition: "transform 0.15s", transform: isCollapsed("io") ? "rotate(-90deg)" : "rotate(0)" }}>&#9660;</span>
              I/O
            </div>
            {!isCollapsed("io") && <><div className="props-field">
              <label className="props-label">Video</label>
              <select
                className="props-select"
                value={(data as Record<string, any>).videoModel as string || "virtio"}
                onChange={(e) => update("videoModel", e.target.value)}
              >
                <option value="virtio">VirtIO (recommended)</option>
                <option value="vga">VGA</option>
                <option value="qxl">QXL</option>
              </select>
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
                value={(data as Record<string, any>).serialModel as string || "virtio"}
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
            </div></>}
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
              let ports = ((data as unknown as VMNodeData).diskControllers || []) as Array<{id: string; name: string; bus: string}>;
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
                          const updated = [...ports]; updated[i] = { ...port, bus: e.target.value }; update("diskControllers", updated);
                        }}>
                          <option value="virtio">virtio-blk</option>
                          <option value="scsi">virtio-scsi</option>
                          <option value="sata">SATA (AHCI)</option>
                          <option value="ide">IDE</option>
                          <option value="usb">USB</option>
                        </select>
                      </div>
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
          <div className="props-divider" />

          {/* Tags Section */}
          <div className="props-section">
            <div className="props-section-title" style={{ cursor: "pointer", display: "flex", alignItems: "center", gap: 6 }} onClick={() => toggleSection("tags")}>
              <span style={{ fontSize: 8, transition: "transform 0.15s", transform: isCollapsed("tags") ? "rotate(-90deg)" : "rotate(0)" }}>&#9660;</span>
              Tags
            </div>
            {!isCollapsed("tags") && (
              <div className="props-section-body">
                {Object.entries((data as Record<string, any>).tags || {}).map(([key, value]) => (
                  <div key={key} style={{ display: "flex", gap: 4, marginBottom: 4, alignItems: "center" }}>
                    <input
                      className="props-input"
                      value={key}
                      onChange={(e) => {
                        const newKey = e.target.value;
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
                      value={value as string}
                      onChange={(e) => {
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
                    let newKey = "NewTag";
                    let i = 1;
                    while (newKey in tags) { newKey = `NewTag${i++}`; }
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
            const isPod = !!(selectedNode?.data as Record<string, unknown>)?.isPod;
            return (
              <>
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

          {/* NIC section */}
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
          {/* Init Containers section */}
          <div className="props-section">
            <div className="props-section-title" style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              Init Containers
              <button className="props-library-btn" style={{ fontSize: 11, padding: "2px 8px" }} onClick={() => {
                const containers = [...((selectedNode?.data as any)?.initContainers || [])];
                containers.push({
                  name: `init-${containers.length}`,
                  image: "",
                  command: null,
                  envVars: [],
                  mounts: []
                });
                updateNodeData(selectedNode!.id, { initContainers: containers });
              }}>+ Add</button>
            </div>
            {((selectedNode?.data as any)?.initContainers || []).map((container: any, i: number) => (
              <details key={i} style={{ marginBottom: 8 }}>
                <summary style={{ cursor: "pointer", padding: "6px 8px", background: "var(--troshka-surface2)", borderRadius: 6, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <span style={{ fontSize: 12, fontWeight: 600 }}>{container.name || `init-${i}`}</span>
                  <button
                    style={{ background: "none", border: "none", color: "#ef4444", cursor: "pointer", fontSize: 12 }}
                    onClick={(e) => {
                      e.stopPropagation();
                      const containers = [...((selectedNode?.data as any)?.initContainers || [])];
                      containers.splice(i, 1);
                      updateNodeData(selectedNode!.id, { initContainers: containers });
                    }}
                  >✕</button>
                </summary>
                <div style={{ padding: "8px 0" }}>
                  <div className="props-field">
                    <label className="props-label">Name</label>
                    <input className="props-input" value={container.name || ""} onChange={(e) => {
                      const containers = [...((selectedNode?.data as any)?.initContainers || [])];
                      containers[i] = { ...container, name: e.target.value };
                      updateNodeData(selectedNode!.id, { initContainers: containers });
                    }} />
                  </div>
                  <div className="props-field">
                    <label className="props-label">Image</label>
                    <input className="props-input" value={container.image || ""} placeholder="registry/org/image:tag" style={{ fontFamily: "monospace", fontSize: 11 }} onChange={(e) => {
                      const containers = [...((selectedNode?.data as any)?.initContainers || [])];
                      containers[i] = { ...container, image: e.target.value };
                      updateNodeData(selectedNode!.id, { initContainers: containers });
                    }} />
                  </div>
                  <div className="props-field">
                    <label className="props-label">Command</label>
                    <input className="props-input" value={container.command || ""} placeholder="Optional entrypoint override" style={{ fontFamily: "monospace", fontSize: 11 }} onChange={(e) => {
                      const containers = [...((selectedNode?.data as any)?.initContainers || [])];
                      containers[i] = { ...container, command: e.target.value || null };
                      updateNodeData(selectedNode!.id, { initContainers: containers });
                    }} />
                  </div>
                  <div className="props-field">
                    <label className="props-label">Environment Variables</label>
                    {(container.envVars || []).map((ev: any, evIdx: number) => (
                      <div key={evIdx} style={{ display: "flex", gap: 4, marginBottom: 4, alignItems: "center" }}>
                        <input className="props-input" placeholder="KEY" value={ev.key || ""} style={{ flex: 1, fontFamily: "monospace", fontSize: 11 }} onChange={(e) => {
                          const containers = [...((selectedNode?.data as any)?.initContainers || [])];
                          const envVars = [...(containers[i].envVars || [])];
                          envVars[evIdx] = { ...ev, key: e.target.value };
                          containers[i] = { ...container, envVars };
                          updateNodeData(selectedNode!.id, { initContainers: containers });
                        }} />
                        <span style={{ color: "var(--troshka-text-dim)" }}>=</span>
                        <input className="props-input" placeholder="value" value={ev.value || ""} style={{ flex: 2, fontFamily: "monospace", fontSize: 11 }} onChange={(e) => {
                          const containers = [...((selectedNode?.data as any)?.initContainers || [])];
                          const envVars = [...(containers[i].envVars || [])];
                          envVars[evIdx] = { ...ev, value: e.target.value };
                          containers[i] = { ...container, envVars };
                          updateNodeData(selectedNode!.id, { initContainers: containers });
                        }} />
                        <button style={{ background: "none", border: "none", color: "#ef4444", cursor: "pointer", fontSize: 12 }} onClick={() => {
                          const containers = [...((selectedNode?.data as any)?.initContainers || [])];
                          const envVars = [...(containers[i].envVars || [])];
                          envVars.splice(evIdx, 1);
                          containers[i] = { ...container, envVars };
                          updateNodeData(selectedNode!.id, { initContainers: containers });
                        }}>✕</button>
                      </div>
                    ))}
                    <button style={{ fontSize: 10, padding: "2px 6px" }} className="props-library-btn" onClick={() => {
                      const containers = [...((selectedNode?.data as any)?.initContainers || [])];
                      const envVars = [...(containers[i].envVars || [])];
                      envVars.push({ key: "", value: "" });
                      containers[i] = { ...container, envVars };
                      updateNodeData(selectedNode!.id, { initContainers: containers });
                    }}>+ Env Var</button>
                  </div>
                  <div className="props-field">
                    <label className="props-label">Mounts</label>
                    {(container.mounts || []).map((mount: any, mIdx: number) => (
                      <div key={mIdx} style={{ display: "flex", gap: 4, marginBottom: 4, alignItems: "center" }}>
                        <input className="props-input" placeholder="/mount/path" value={mount.mountPath || ""} style={{ flex: 1, fontFamily: "monospace", fontSize: 11 }} onChange={(e) => {
                          const containers = [...((selectedNode?.data as any)?.initContainers || [])];
                          const mounts = [...(containers[i].mounts || [])];
                          mounts[mIdx] = { ...mount, mountPath: e.target.value };
                          containers[i] = { ...container, mounts };
                          updateNodeData(selectedNode!.id, { initContainers: containers });
                        }} />
                        <button style={{ background: "none", border: "none", color: "#ef4444", cursor: "pointer", fontSize: 12 }} onClick={() => {
                          const containers = [...((selectedNode?.data as any)?.initContainers || [])];
                          const mounts = [...(containers[i].mounts || [])];
                          mounts.splice(mIdx, 1);
                          containers[i] = { ...container, mounts };
                          updateNodeData(selectedNode!.id, { initContainers: containers });
                        }}>✕</button>
                      </div>
                    ))}
                    <button style={{ fontSize: 10, padding: "2px 6px" }} className="props-library-btn" onClick={() => {
                      const containers = [...((selectedNode?.data as any)?.initContainers || [])];
                      const mounts = [...(containers[i].mounts || [])];
                      mounts.push({ diskNodeId: "", mountPath: "" });
                      containers[i] = { ...container, mounts };
                      updateNodeData(selectedNode!.id, { initContainers: containers });
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
                const containers = [...((selectedNode?.data as any)?.podContainers || [])];
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
                updateNodeData(selectedNode!.id, { podContainers: containers });
              }}>+ Add</button>
            </div>
            {((selectedNode?.data as any)?.podContainers || []).map((container: any, i: number) => (
              <details key={i} open style={{ marginBottom: 8 }}>
                <summary style={{ cursor: "pointer", padding: "6px 8px", background: "var(--troshka-surface2)", borderRadius: 6, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <span style={{ fontSize: 12, fontWeight: 600 }}>{container.name || `container-${i}`}</span>
                  <button
                    style={{ background: "none", border: "none", color: "#ef4444", cursor: "pointer", fontSize: 12 }}
                    onClick={(e) => {
                      e.stopPropagation();
                      const containers = [...((selectedNode?.data as any)?.podContainers || [])];
                      containers.splice(i, 1);
                      updateNodeData(selectedNode!.id, { podContainers: containers });
                    }}
                  >✕</button>
                </summary>
                <div style={{ padding: "8px 0" }}>
                  <div className="props-field">
                    <label className="props-label">Name</label>
                    <input className="props-input" value={container.name || ""} onChange={(e) => {
                      const containers = [...((selectedNode?.data as any)?.podContainers || [])];
                      containers[i] = { ...container, name: e.target.value };
                      updateNodeData(selectedNode!.id, { podContainers: containers });
                    }} />
                  </div>
                  <div className="props-field">
                    <label className="props-label">Image</label>
                    <input className="props-input" value={container.image || ""} placeholder="registry/org/image:tag" style={{ fontFamily: "monospace", fontSize: 11 }} onChange={(e) => {
                      const containers = [...((selectedNode?.data as any)?.podContainers || [])];
                      containers[i] = { ...container, image: e.target.value };
                      updateNodeData(selectedNode!.id, { podContainers: containers });
                    }} />
                  </div>
                  <div className="props-row">
                    <div className="props-field">
                      <label className="props-label">CPUs</label>
                      <input className="props-input" type="number" min={1} max={32} value={container.cpus || 1} onChange={(e) => {
                        const containers = [...((selectedNode?.data as any)?.podContainers || [])];
                        containers[i] = { ...container, cpus: parseInt(e.target.value) || 1 };
                        updateNodeData(selectedNode!.id, { podContainers: containers });
                      }} />
                    </div>
                    <div className="props-field">
                      <label className="props-label">Memory (MB)</label>
                      <input className="props-input" type="number" min={64} step={64} value={container.memory || 512} onChange={(e) => {
                        const containers = [...((selectedNode?.data as any)?.podContainers || [])];
                        containers[i] = { ...container, memory: parseInt(e.target.value) || 512 };
                        updateNodeData(selectedNode!.id, { podContainers: containers });
                      }} />
                    </div>
                  </div>
                  <div className="props-field">
                    <label className="props-label">Command</label>
                    <input className="props-input" value={container.command || ""} placeholder="Optional entrypoint override" style={{ fontFamily: "monospace", fontSize: 11 }} onChange={(e) => {
                      const containers = [...((selectedNode?.data as any)?.podContainers || [])];
                      containers[i] = { ...container, command: e.target.value || null };
                      updateNodeData(selectedNode!.id, { podContainers: containers });
                    }} />
                  </div>
                  <div className="props-field">
                    <label className="props-label">Environment Variables</label>
                    {(container.envVars || []).map((ev: any, evIdx: number) => (
                      <div key={evIdx} style={{ display: "flex", gap: 4, marginBottom: 4, alignItems: "center" }}>
                        <input className="props-input" placeholder="KEY" value={ev.key || ""} style={{ flex: 1, fontFamily: "monospace", fontSize: 11 }} onChange={(e) => {
                          const containers = [...((selectedNode?.data as any)?.podContainers || [])];
                          const envVars = [...(containers[i].envVars || [])];
                          envVars[evIdx] = { ...ev, key: e.target.value };
                          containers[i] = { ...container, envVars };
                          updateNodeData(selectedNode!.id, { podContainers: containers });
                        }} />
                        <span style={{ color: "var(--troshka-text-dim)" }}>=</span>
                        <input className="props-input" placeholder="value" value={ev.value || ""} style={{ flex: 2, fontFamily: "monospace", fontSize: 11 }} onChange={(e) => {
                          const containers = [...((selectedNode?.data as any)?.podContainers || [])];
                          const envVars = [...(containers[i].envVars || [])];
                          envVars[evIdx] = { ...ev, value: e.target.value };
                          containers[i] = { ...container, envVars };
                          updateNodeData(selectedNode!.id, { podContainers: containers });
                        }} />
                        <button style={{ background: "none", border: "none", color: "#ef4444", cursor: "pointer", fontSize: 12 }} onClick={() => {
                          const containers = [...((selectedNode?.data as any)?.podContainers || [])];
                          const envVars = [...(containers[i].envVars || [])];
                          envVars.splice(evIdx, 1);
                          containers[i] = { ...container, envVars };
                          updateNodeData(selectedNode!.id, { podContainers: containers });
                        }}>✕</button>
                      </div>
                    ))}
                    <button style={{ fontSize: 10, padding: "2px 6px" }} className="props-library-btn" onClick={() => {
                      const containers = [...((selectedNode?.data as any)?.podContainers || [])];
                      const envVars = [...(containers[i].envVars || [])];
                      envVars.push({ key: "", value: "" });
                      containers[i] = { ...container, envVars };
                      updateNodeData(selectedNode!.id, { podContainers: containers });
                    }}>+ Env Var</button>
                  </div>
                  <div className="props-field">
                    <label className="props-label">Ports</label>
                    {(container.ports || []).map((port: any, pIdx: number) => (
                      <div key={pIdx} style={{ display: "flex", gap: 4, marginBottom: 4, alignItems: "center" }}>
                        <input className="props-input" type="number" placeholder="Port" value={port.containerPort || ""} style={{ width: 70 }} onChange={(e) => {
                          const containers = [...((selectedNode?.data as any)?.podContainers || [])];
                          const ports = [...(containers[i].ports || [])];
                          ports[pIdx] = { ...port, containerPort: parseInt(e.target.value) || 0 };
                          containers[i] = { ...container, ports };
                          updateNodeData(selectedNode!.id, { podContainers: containers });
                        }} />
                        <select className="props-select" value={port.protocol || "tcp"} style={{ width: 60 }} onChange={(e) => {
                          const containers = [...((selectedNode?.data as any)?.podContainers || [])];
                          const ports = [...(containers[i].ports || [])];
                          ports[pIdx] = { ...port, protocol: e.target.value as "tcp" | "udp" };
                          containers[i] = { ...container, ports };
                          updateNodeData(selectedNode!.id, { podContainers: containers });
                        }}>
                          <option value="tcp">TCP</option>
                          <option value="udp">UDP</option>
                        </select>
                        <button style={{ background: "none", border: "none", color: "#ef4444", cursor: "pointer", fontSize: 12 }} onClick={() => {
                          const containers = [...((selectedNode?.data as any)?.podContainers || [])];
                          const ports = [...(containers[i].ports || [])];
                          ports.splice(pIdx, 1);
                          containers[i] = { ...container, ports };
                          updateNodeData(selectedNode!.id, { podContainers: containers });
                        }}>✕</button>
                      </div>
                    ))}
                    <button style={{ fontSize: 10, padding: "2px 6px" }} className="props-library-btn" onClick={() => {
                      const containers = [...((selectedNode?.data as any)?.podContainers || [])];
                      const ports = [...(containers[i].ports || [])];
                      ports.push({ containerPort: 0, protocol: "tcp" });
                      containers[i] = { ...container, ports };
                      updateNodeData(selectedNode!.id, { podContainers: containers });
                    }}>+ Port</button>
                  </div>
                  <div className="props-field">
                    <label className="props-label">Mounts</label>
                    {(container.mounts || []).map((mount: any, mIdx: number) => (
                      <div key={mIdx} style={{ display: "flex", gap: 4, marginBottom: 4, alignItems: "center" }}>
                        <input className="props-input" placeholder="/mount/path" value={mount.mountPath || ""} style={{ flex: 1, fontFamily: "monospace", fontSize: 11 }} onChange={(e) => {
                          const containers = [...((selectedNode?.data as any)?.podContainers || [])];
                          const mounts = [...(containers[i].mounts || [])];
                          mounts[mIdx] = { ...mount, mountPath: e.target.value };
                          containers[i] = { ...container, mounts };
                          updateNodeData(selectedNode!.id, { podContainers: containers });
                        }} />
                        <button style={{ background: "none", border: "none", color: "#ef4444", cursor: "pointer", fontSize: 12 }} onClick={() => {
                          const containers = [...((selectedNode?.data as any)?.podContainers || [])];
                          const mounts = [...(containers[i].mounts || [])];
                          mounts.splice(mIdx, 1);
                          containers[i] = { ...container, mounts };
                          updateNodeData(selectedNode!.id, { podContainers: containers });
                        }}>✕</button>
                      </div>
                    ))}
                    <button style={{ fontSize: 10, padding: "2px 6px" }} className="props-library-btn" onClick={() => {
                      const containers = [...((selectedNode?.data as any)?.podContainers || [])];
                      const mounts = [...(containers[i].mounts || [])];
                      mounts.push({ diskNodeId: "", mountPath: "" });
                      containers[i] = { ...container, mounts };
                      updateNodeData(selectedNode!.id, { podContainers: containers });
                    }}>+ Mount</button>
                  </div>
                </div>
              </details>
            ))}
          </div>
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
                        (n) => n.type === "networkNode" && n.id !== selectedNodeId &&
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
                          {((data as Record<string, any>).dnsRecords as Array<{name: string; type?: string; ip: string}>).map((rec, i) => (
                            <div key={i} style={{ display: "flex", gap: 4, marginBottom: 3, alignItems: "center", minWidth: 320 }}>
                              <input className="props-input" style={{ flex: 3, fontSize: 10, fontFamily: "monospace" }} value={rec.name} placeholder="hostname" onChange={(e) => {
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
                              <input className="props-input" style={{ flex: 2, fontSize: 10, fontFamily: "monospace" }} value={rec.ip} placeholder={rec.type === "CNAME" ? "target" : "IP"} onChange={(e) => {
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
                          ))}
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
                    return projectIps.length === 0 ? (
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
                  {(data as Record<string, any>).outboundPolicy === "restrict" && (() => {
                    const currentPorts = ((data as Record<string, any>).outboundPorts as string || "").split(",").map((p: string) => p.trim()).filter(Boolean);
                    const removePort = (port: string) => {
                      update("outboundPorts", currentPorts.filter((p: string) => p !== port).join(","));
                    };
                    let _portInputEl: HTMLInputElement | null = null;
                    const addPortProtoRef = { current: "both" };
                    const addPort = () => {
                      const num = (_portInputEl?.value || "").trim();
                      if (!num || isNaN(Number(num))) return;
                      const proto = addPortProtoRef.current;
                      const entry = proto === "both" ? num : `${num}/${proto}`;
                      if (!currentPorts.includes(entry)) {
                        update("outboundPorts", [...currentPorts, entry].join(","));
                      }
                      if (_portInputEl) _portInputEl.value = "";
                    };
                    return (
                      <div className="props-field">
                        <label className="props-label">Allowed Outbound Ports</label>
                        <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginBottom: 6 }}>
                          {currentPorts.map((port: string) => {
                            const label = port.includes("/") ? port : `${port} tcp/udp`;
                            return (
                              <span key={port} style={{
                                display: "inline-flex", alignItems: "center", gap: 4,
                                padding: "2px 8px", borderRadius: 12, fontSize: 11,
                                background: "rgba(0,102,204,0.15)", color: "#73bcf7",
                                border: "1px solid rgba(0,102,204,0.3)",
                              }}>
                                {label}
                                <span onClick={() => removePort(port)} style={{ cursor: "pointer", opacity: 0.6, fontSize: 10 }}>✕</span>
                              </span>
                            );
                          })}
                        </div>
                        <div style={{ display: "flex", gap: 4, alignItems: "center" }}>
                          <input
                            type="number"
                            className="props-input"
                            placeholder="Port"
                            style={{ width: 70, fontSize: 11, fontFamily: "monospace" }}
                            ref={(el) => { _portInputEl = el; }}
                            onKeyDown={(e) => { if (e.key === "Enter") addPort(); }}
                          />
                          <select
                            defaultValue="both"
                            onChange={(e) => { addPortProtoRef.current = e.target.value; }}
                            style={{
                              fontSize: 11, padding: "3px 4px", borderRadius: 3,
                              border: "1px solid var(--pf-t--global--border--color--default)",
                              background: "var(--pf-t--global--background--color--secondary--default)",
                              color: "var(--pf-t--global--text--color--regular)",
                            }}
                          >
                            <option value="both">TCP+UDP</option>
                            <option value="tcp">TCP</option>
                            <option value="udp">UDP</option>
                            <option value="icmp">ICMP</option>
                          </select>
                          <button
                            onClick={() => { addPort(); }}
                            style={{
                              padding: "3px 8px", borderRadius: 3, fontSize: 11, cursor: "pointer",
                              border: "1px solid var(--pf-t--global--border--color--default)",
                              background: "transparent", color: "var(--pf-t--global--text--color--regular)",
                            }}
                          >Add</button>
                        </div>
                      </div>
                    );
                  })()}
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
                        return portForwards.map((pf, i) => (
                          <div key={i} style={{ background: "var(--troshka-surface2)", borderRadius: 6, padding: 8, marginBottom: 6 }}>
                            <div className="props-row" style={{ marginBottom: 4, alignItems: "end" }}>
                              <div className="props-field" style={{ flex: 1 }}>
                                {<label className="props-label">External IP</label>}
                                <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
                                  <select className="props-select" style={{ fontSize: 11, flex: 1 }}
                                    value={(pf as Record<string, string>).extIpId || ""}
                                    onChange={(e) => {
                                      const updated = [...portForwards];
                                      (updated[i] as Record<string, string>).extIpId = e.target.value;
                                      update("portForwards", updated);
                                    }}>
                                    <option value="">Select IP...</option>
                                    {externalIps.map((eip) => (
                                      <option key={eip.id} value={eip.id}>{eip.name}{eip.ip ? ` (${eip.ip})` : " (auto)"}</option>
                                    ))}
                                  </select>
                                  {(() => {
                                    const selEip = externalIps.find((e) => e.id === (pf as Record<string, string>).extIpId);
                                    return selEip?.ip ? (
                                      <button
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
                              <div className="props-field" style={{ flex: "0 0 50px" }}>
                                {<label className="props-label">Ext Port</label>}
                                <input className="props-input" value={pf.extPort} placeholder="80" style={{ fontFamily: "monospace" }}
                                  onChange={(e) => {
                                    const updated = [...portForwards];
                                    updated[i] = { ...pf, extPort: e.target.value };
                                    update("portForwards", updated);
                                  }}
                                />
                              </div>
                            </div>
                            <div style={{ textAlign: "center", color: "var(--troshka-text-dim)", fontSize: 10, lineHeight: 1, margin: "0" }}>↓</div>
                            <div className="props-row" style={{ alignItems: "end" }}>
                              <div className="props-field" style={{ flex: 1 }}>
                                {<label className="props-label">Internal IP</label>}
                                <input className="props-input" value={pf.intIp} placeholder="192.168.1.10" style={{ fontFamily: "monospace" }}
                                  onChange={(e) => {
                                    const updated = [...portForwards];
                                    updated[i] = { ...pf, intIp: e.target.value };
                                    update("portForwards", updated);
                                  }}
                                />
                              </div>
                              <div className="props-field" style={{ flex: "0 0 50px" }}>
                                {<label className="props-label">Int Port</label>}
                                <input className="props-input" value={pf.intPort} placeholder="80" style={{ fontFamily: "monospace" }}
                                  onChange={(e) => {
                                    const updated = [...portForwards];
                                    updated[i] = { ...pf, intPort: e.target.value };
                                    update("portForwards", updated);
                                  }}
                                />
                              </div>
                            </div>
                            <button
                              style={{ background: "none", border: "none", color: "var(--troshka-red)", cursor: "pointer", padding: "4px", alignSelf: "end" }}
                              onClick={() => {
                                const updated = portForwards.filter((_, idx) => idx !== i);
                                update("portForwards", updated);
                              }}
                            >✕</button>
                            {(() => {
                              const errors: string[] = [];
                              if (!(pf as Record<string, string>).extIpId) errors.push("External IP required");
                              if (!pf.extPort) errors.push("External port required");
                              if (!pf.intIp) errors.push("Internal IP required");
                              if (!pf.intPort) errors.push("Internal port required");
                              if (pf.extPort && !/^\d+$/.test(pf.extPort)) errors.push("External port must be a number");
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
                          </div>
                        ));
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
          </>
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
                if (!window.confirm(`Redeploy ${vmName}? This will destroy and recreate this VM (disk data will be lost).`)) return;
                const projectId = useCanvasStore.getState().currentProjectId;
                updateNodeData(node.id, { status: "redeploying" });
                const resp = await fetch(`/api/v1/projects/${projectId}/vms/${vmName}/redeploy`, { method: "POST" });
                const result = await resp.json();
                if (result.status === "redeploying") {
                  updateNodeData(node.id, { status: "redeploying" });
                } else {
                  updateNodeData(node.id, { status: "stopped" });
                  alert(`Redeploy failed: ${result.output || result.error || "unknown error"}`);
                }
              }}
            >
              🔄 Redeploy This VM
            </button>
          </div>
        </>
      )}

      {/* Delete button */}
      <div className="props-divider" />
      <div className="props-section">
        <button
          className="props-delete-btn"
          onClick={() => deleteNode(node.id)}
        >
          Delete {nodeType === "vmNode" ? "VM" : nodeType === "networkNode" ? (
            (data as unknown as NetworkNodeData).subtype === "router" ? "Router" :
            (data as unknown as NetworkNodeData).subtype === "gateway" ? "Gateway" : "Network"
          ) : "Storage"}
        </button>
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
                        alert(`Failed to refresh logs: ${resp.statusText}`);
                        return;
                      }
                      const data = await resp.json();
                      setContainerLogs({ ...containerLogs, logs: data.logs });
                    } catch (err) {
                      console.error("Failed to refresh logs:", err);
                      alert(`Error refreshing logs: ${err}`);
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
    </div>
  );
}
