"use client";

import React, { useState, useEffect, useRef } from "react";
import LibraryPicker from "./LibraryPicker";
import { useCanvasStore, generateNicId, generateDiskControllerId, generateMac } from "@/stores/canvasStore";
import type {
  VMNodeData,
  NetworkNodeData,
  StorageNodeData,
} from "@/stores/canvasStore";

function cidrToRange(cidr: string): [number, number] | null {
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

export default function PropertiesPanel() {
  const selectedNodeId = useCanvasStore((s) => s.selectedNodeId);
  const nodes = useCanvasStore((s) => s.nodes);
  const edges = useCanvasStore((s) => s.edges);
  const updateNodeData = useCanvasStore((s) => s.updateNodeData);
  const deleteNode = useCanvasStore((s) => s.deleteNode);
  const projectState = useCanvasStore((s) => s.projectState);
  const panelLocked = ["deploying", "reconfiguring", "starting", "stopping"].includes(projectState);
  const [showLibraryPicker, setShowLibraryPicker] = useState<"iso" | "image" | null>(null);
  const [showPassword, setShowPassword] = useState(false);
  const [sshKeys, setSshKeys] = useState<SshKeyOption[]>([]);

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

  const data = node.data as Record<string, unknown>;
  const nodeType = node.type;

  const update = (field: string, value: unknown) => {
    updateNodeData(node.id, { [field]: value });
  };

  return (
    <div className="canvas-properties" style={panelLocked ? { pointerEvents: "none", opacity: 0.6 } : {}}>
      {/* Header */}
      <div className="props-header">
        <div
          className={`props-header-icon ${
            nodeType === "vmNode"
              ? "props-icon-vm"
              : nodeType === "networkNode"
                ? "props-icon-network"
                : "props-icon-storage"
          }`}
        >
          {nodeType === "vmNode"
            ? ((data as unknown as VMNodeData).icon || "🖥")
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
            <div className="props-section-title">General</div>
            <div className="props-field">
              <label className="props-label">Name</label>
              <input
                className="props-input"
                value={(data.name as string) || ""}
                onChange={(e) => update("name", e.target.value)}
              />
            </div>
          </div>
          <div className="props-divider" />

          <div className="props-section">
            <div className="props-section-title">Compute</div>
            <div className="props-row">
              <div className="props-field">
                <label className="props-label">vCPUs</label>
                <input
                  className="props-input"
                  type="number"
                  min={1}
                  max={64}
                  value={(data as unknown as VMNodeData).vcpus}
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
                  onChange={(e) =>
                    update("ram", parseInt(e.target.value) || 1)
                  }
                />
              </div>
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
                  <option value="generic">Generic OS</option>
                </optgroup>
              </select>
            </div>
          </div>
          <div className="props-divider" />

          <div className="props-section">
            <div className="props-section-title">Boot Devices</div>
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
                  .filter((n) => n && (n.data as Record<string, unknown>).bootable !== false)
                  .map((n) => ({
                    id: n!.id,
                    name: (n!.data as Record<string, unknown>).name as string,
                    format: (n!.data as Record<string, unknown>).format as string,
                    size: (n!.data as Record<string, unknown>).size as number,
                    type: (n!.data as Record<string, unknown>).format === "iso" ? "cdrom" as const : "disk" as const,
                  }));

                let bootDevices = (data as Record<string, unknown>).bootDevices as string[] | null;
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
            <div className="props-field">
              <label className="props-label" style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <input
                  type="checkbox"
                  checked={(data as unknown as VMNodeData).cloudInit ?? false}
                  onChange={(e) => update("cloudInit", e.target.checked)}
                />
                Cloud-init enabled
              </label>
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
            {(data as unknown as VMNodeData).cloudInit && useCanvasStore.getState().deployedVmIds.has(node.id) && (
              <span style={{ fontSize: 10, color: "var(--troshka-text-dim)", display: "block", marginTop: 4 }}>
                Cloud-init runs on first boot only. Changes here require Republish to take effect.
              </span>
            )}
            {(data as unknown as VMNodeData).cloudInit && (
              <>
                <div className="props-field">
                  <label className="props-label">Hostname</label>
                  <input className="props-input" value={(data as Record<string, unknown>).ciHostname as string || ""} onChange={(e) => update("ciHostname", e.target.value)} placeholder={`${(data as unknown as VMNodeData).name}`} />
                </div>
                <div className="props-field">
                  <label className="props-label">root password</label>
                  <div style={{ display: "flex", gap: 4 }}>
                    <input className="props-input" style={{ flex: 1 }} type={showPassword ? "text" : "password"} value={(data as Record<string, unknown>).ciRootPassword as string || ""} onChange={(e) => update("ciRootPassword", e.target.value)} placeholder="Leave blank for key-only auth" />
                    <button onClick={() => setShowPassword(!showPassword)} style={{ background: "none", border: "none", cursor: "pointer", fontSize: 14, padding: "0 4px" }} title={showPassword ? "Hide" : "Show"}>
                      {showPassword ? "🙈" : "👁"}
                    </button>
                  </div>
                </div>
                <div className="props-field">
                  <label className="props-label">cloud-user password</label>
                  <div style={{ display: "flex", gap: 4 }}>
                    <input className="props-input" style={{ flex: 1 }} type={showPassword ? "text" : "password"} value={(data as Record<string, unknown>).ciCloudUserPassword as string || ""} onChange={(e) => update("ciCloudUserPassword", e.target.value)} placeholder="Leave blank for key-only auth" />
                    <button onClick={() => setShowPassword(!showPassword)} style={{ background: "none", border: "none", cursor: "pointer", fontSize: 14, padding: "0 4px" }} title={showPassword ? "Hide" : "Show"}>
                      {showPassword ? "🙈" : "👁"}
                    </button>
                  </div>
                </div>
                <div className="props-field">
                  <label className="props-label" style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <input
                      type="checkbox"
                      checked={(data as Record<string, unknown>).ciCloudUserSudo as boolean ?? true}
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
                        const selectedIds: number[] = (data as Record<string, unknown>).ciSshKeyIds as number[] || [];
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
                  <textarea className="props-input" style={{ minHeight: 60, fontFamily: "monospace", fontSize: 11 }} value={(data as Record<string, unknown>).ciUserData as string || ""} onChange={(e) => update("ciUserData", e.target.value)} placeholder="#cloud-config&#10;packages:&#10;  - vim" />
                </div>
              </>
            )}
          </div>
          <div className="props-divider" />

          <div className="props-section">
            <div className="props-section-title">Network Interfaces</div>
            {(() => {
              let nics = ((data as unknown as VMNodeData).nics || []) as Array<{id: string; name: string; mac: string; model: string}>;
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
                        const netCidr = netNode ? (netNode.data as Record<string, unknown>).cidr as string : "";
                        const nicIp = (nic as Record<string, unknown>).ip as string || "";
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
                        const ipConflict = nicIp ? (() => {
                          const nd = netNode!.data as Record<string, unknown>;
                          const gwIp = (nd.dhcpGateway as string) || (netCidr ? netCidr.replace(/\.\d+\/\d+$/, ".1") : "");
                          if (gwIp && gwIp === nicIp) return "gateway IP";
                          if (nd.dnsServerIp === nicIp) return "DNS server IP";
                          const ipToNum = (ip: string) => {
                            const p = ip.split(".").map(Number);
                            return p.length === 4 ? ((p[0] << 24) | (p[1] << 16) | (p[2] << 8) | p[3]) >>> 0 : 0;
                          };
                          const dhcpStart = (nd.dhcpRangeStart as string) || (netCidr ? netCidr.replace(/\.\d+\/\d+$/, ".100") : "");
                          const dhcpEnd = (nd.dhcpRangeEnd as string) || (netCidr ? netCidr.replace(/\.\d+\/\d+$/, ".200") : "");
                          if (dhcpStart && dhcpEnd) {
                            const ipN = ipToNum(nicIp);
                            const startN = ipToNum(dhcpStart);
                            const endN = ipToNum(dhcpEnd);
                            if (ipN >= startN && ipN <= endN) return "DHCP range";
                          }
                          for (const n of nodes) {
                            if (n.type !== "vmNode") continue;
                            const vmNics = ((n.data as Record<string, unknown>).nics || []) as Array<Record<string, unknown>>;
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
            <div className="props-section-title">Disk Controllers</div>
            {(() => {
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
        </>
      )}

      {/* Network Properties */}
      {nodeType === "networkNode" && (() => {
        const nd = data as unknown as NetworkNodeData;
        const subtype = nd.subtype || "network";
        const portForwards = (data as Record<string, unknown>).portForwards as Array<{extPort: string; intIp: string; intPort: string; proto: string}> || [];

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
                />
              </div>

              {/* Network: CIDR + Services */}
              {subtype === "network" && (
                <>
                  <div className="props-field">
                    <label className="props-label">CIDR</label>
                    {(() => {
                      const currentCidr = nd.cidr;
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
                      <input type="checkbox" checked={nd.dhcp ?? false} onChange={(e) => update("dhcp", e.target.checked)} />
                      DHCP
                    </label>
                  </div>
                  {nd.dhcp && (
                    <>
                      <div className="props-row">
                        <div className="props-field">
                          <label className="props-label">Range Start</label>
                          <input
                            className="props-input"
                            value={(data as Record<string, unknown>).dhcpRangeStart as string || ""}
                            onChange={(e) => update("dhcpRangeStart", e.target.value)}
                            placeholder={nd.cidr ? nd.cidr.replace(/\.\d+\/\d+$/, ".100") : "x.x.x.100"}
                            style={{ fontFamily: "monospace" }}
                          />
                        </div>
                        <div className="props-field">
                          <label className="props-label">Range End</label>
                          <input
                            className="props-input"
                            value={(data as Record<string, unknown>).dhcpRangeEnd as string || ""}
                            onChange={(e) => update("dhcpRangeEnd", e.target.value)}
                            placeholder={nd.cidr ? nd.cidr.replace(/\.\d+\/\d+$/, ".200") : "x.x.x.200"}
                            style={{ fontFamily: "monospace" }}
                          />
                        </div>
                      </div>
                      <div className="props-field">
                        <label className="props-label">Gateway IP</label>
                        <input
                          className="props-input"
                          value={(data as Record<string, unknown>).dhcpGateway as string || ""}
                          onChange={(e) => update("dhcpGateway", e.target.value)}
                          placeholder={nd.cidr ? nd.cidr.replace(/\.\d+\/\d+$/, ".1") : "x.x.x.1"}
                          style={{ fontFamily: "monospace" }}
                        />
                      </div>
                      {(() => {
                        const dhcpErrors = validateDhcpRangeFull(
                          nd.cidr,
                          (data as Record<string, unknown>).dhcpRangeStart as string || "",
                          (data as Record<string, unknown>).dhcpRangeEnd as string || "",
                          (data as Record<string, unknown>).dhcpGateway as string || "",
                          (data as Record<string, unknown>).dnsServerIp as string || "",
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
                          value={(data as Record<string, unknown>).dhcpLeaseTime as string || "24h"}
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
                            checked={(data as Record<string, unknown>).pxeEnabled as boolean ?? false}
                            onChange={(e) => update("pxeEnabled", e.target.checked)}
                          />
                          Enable Network Boot
                        </label>
                      </div>
                      {(data as Record<string, unknown>).pxeEnabled && (
                        <>
                          <div className="props-field">
                            <label className="props-label">Boot Method</label>
                            <select
                              className="props-select"
                              value={(data as Record<string, unknown>).pxeMethod as string || "legacy"}
                              onChange={(e) => update("pxeMethod", e.target.value)}
                            >
                              <option value="legacy">Legacy PXE (TFTP)</option>
                              <option value="ipxe">iPXE (HTTP)</option>
                              <option value="uefi-http">UEFI HTTP Boot</option>
                            </select>
                          </div>

                          {(() => {
                            const method = (data as Record<string, unknown>).pxeMethod as string || "legacy";
                            const isByo = (data as Record<string, unknown>).pxeServerMode === "custom";

                            return (
                              <>
                                {/* Firmware */}
                                {method !== "uefi-http" ? (
                                  <div className="props-field">
                                    <label className="props-label">Firmware</label>
                                    <select className="props-select" value={(data as Record<string, unknown>).pxeFirmware as string || "bios"} onChange={(e) => update("pxeFirmware", e.target.value)}>
                                      <option value="bios">BIOS (SeaBIOS)</option>
                                      <option value="uefi">UEFI (OVMF)</option>
                                    </select>
                                  </div>
                                ) : (
                                  <div className="props-field">
                                    <label className="props-label">Firmware</label>
                                    <span style={{ fontSize: 13, color: "var(--troshka-text-dim)" }}>UEFI (OVMF) — required for HTTP boot</span>
                                  </div>
                                )}

                                {method === "uefi-http" && (
                                  <div className="props-field">
                                    <label className="props-label" style={{ display: "flex", alignItems: "center", gap: 8 }}>
                                      <input type="checkbox" checked={(data as Record<string, unknown>).uefiSecureBoot as boolean ?? false} onChange={(e) => update("uefiSecureBoot", e.target.checked)} />
                                      Secure Boot
                                    </label>
                                  </div>
                                )}

                                <div className="props-divider" />
                                <div className="props-section-title">Boot Server</div>
                                <div className="props-field">
                                  <label className="props-label">Provider</label>
                                  <select className="props-select" value={(data as Record<string, unknown>).pxeServerMode as string || "builtin"} onChange={(e) => update("pxeServerMode", e.target.value)}>
                                    <option value="builtin">Troshka managed</option>
                                    <option value="custom">User provided (BYO)</option>
                                  </select>
                                </div>

                                {!isByo ? (
                                  <>
                                    <p style={{ fontSize: 11, color: "var(--troshka-text-dim)", marginBottom: 8 }}>
                                      Troshka runs {method === "legacy" ? "a TFTP server (dnsmasq)" : "an HTTP server (nginx)"} and serves boot images automatically.
                                    </p>
                                    <div className="props-field">
                                      <button className="props-library-btn" onClick={() => alert("Boot image manager — coming in Phase 7.\n\nUpload kernels, initrds, kickstart/preseed files, or iPXE scripts.")}>
                                        📚 Manage Boot Images...
                                      </button>
                                      <span style={{ fontSize: 10, color: "var(--troshka-text-dim)", marginTop: 4, display: "block" }}>
                                        Upload kernel, initrd, and kickstart/preseed files from your library.
                                      </span>
                                    </div>
                                  </>
                                ) : (
                                  <>
                                    <p style={{ fontSize: 11, color: "var(--troshka-text-dim)", marginBottom: 8 }}>
                                      You manage your own boot server VM. Configure the connection details below.
                                    </p>
                                    {method === "legacy" && (
                                      <>
                                        <div className="props-field">
                                          <label className="props-label">Next Server (TFTP)</label>
                                          <input className="props-input" value={(data as Record<string, unknown>).pxeNextServer as string || ""} onChange={(e) => update("pxeNextServer", e.target.value)} placeholder={nd.cidr ? nd.cidr.replace(/\.\d+\/\d+$/, ".1") : "TFTP server IP"} style={{ fontFamily: "monospace" }} />
                                        </div>
                                        <div className="props-field">
                                          <label className="props-label">Boot Filename</label>
                                          <input className="props-input" value={(data as Record<string, unknown>).pxeBootFile as string || ""} onChange={(e) => update("pxeBootFile", e.target.value)} placeholder="pxelinux.0" style={{ fontFamily: "monospace" }} />
                                        </div>
                                      </>
                                    )}
                                    {method === "ipxe" && (
                                      <div className="props-field">
                                        <label className="props-label">iPXE Script URL</label>
                                        <input className="props-input" value={(data as Record<string, unknown>).ipxeScriptUrl as string || ""} onChange={(e) => update("ipxeScriptUrl", e.target.value)} placeholder="http://10.0.0.1/boot.ipxe" style={{ fontFamily: "monospace" }} />
                                      </div>
                                    )}
                                    {method === "uefi-http" && (
                                      <div className="props-field">
                                        <label className="props-label">Boot URL</label>
                                        <input className="props-input" value={(data as Record<string, unknown>).uefiBootUrl as string || ""} onChange={(e) => update("uefiBootUrl", e.target.value)} placeholder="http://10.0.0.1/boot/grubx64.efi" style={{ fontFamily: "monospace" }} />
                                      </div>
                                    )}
                                  </>
                                )}
                              </>
                            );
                          })()}
                        </>
                      )}
                    </>
                  )}
                  <div className="props-divider" />
                  <div className="props-field">
                    <label className="props-label" style={{ display: "flex", alignItems: "center", gap: 8 }}>
                      <input type="checkbox" checked={nd.dns ?? false} onChange={(e) => update("dns", e.target.checked)} />
                      DNS
                    </label>
                  </div>
                  {nd.dns && (
                    <>
                      <div className="props-field">
                        <label className="props-label">DNS Server IP</label>
                        <input
                          className="props-input"
                          value={(data as Record<string, unknown>).dnsServerIp as string || ""}
                          onChange={(e) => update("dnsServerIp", e.target.value)}
                          placeholder={nd.cidr ? nd.cidr.replace(/\.\d+\/\d+$/, ".1") : "DNS server IP"}
                          style={{ fontFamily: "monospace" }}
                        />
                        <span style={{ fontSize: 10, color: "var(--troshka-text-dim)", marginTop: 2 }}>
                          Typically same as gateway. Must be outside DHCP range.
                        </span>
                      </div>
                      <div className="props-field">
                        <label className="props-label">DNS Domain</label>
                        <input className="props-input" value={nd.dnsDomain || ""} onChange={(e) => update("dnsDomain", e.target.value)} placeholder="lab.local" style={{ fontFamily: "monospace" }} />
                      </div>
                      <div className="props-field">
                        <label className="props-label" style={{ display: "flex", alignItems: "center", gap: 8 }}>
                          <input
                            type="checkbox"
                            checked={(data as Record<string, unknown>).dnsUpstream as boolean ?? false}
                            onChange={(e) => update("dnsUpstream", e.target.checked)}
                          />
                          Forward to upstream (internet)
                        </label>
                        <span style={{ fontSize: 10, color: "var(--troshka-text-dim)", marginTop: 2 }}>
                          When off, DNS only resolves internal names.
                        </span>
                      </div>
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
                        const routes = (data as Record<string, unknown>).staticRoutes as Array<{dest: string; nextHop: string}> || [];
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
                        const routes = (data as Record<string, unknown>).staticRoutes as Array<{dest: string; nextHop: string}> || [];
                        update("staticRoutes", [...routes, { dest: "", nextHop: "" }]);
                      }}
                    >
                      + Add Static Route
                    </button>
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
                      value={(data as Record<string, unknown>).gatewayMode as string || "nat"}
                      onChange={(e) => update("gatewayMode", e.target.value)}
                    >
                      <option value="nat">NAT (outbound only)</option>
                      <option value="nat-portforward">NAT + Port Forwarding</option>
                    </select>
                  </div>
                  {(data as Record<string, unknown>).gatewayMode === "nat-portforward" && (() => {
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
                      value={(data as Record<string, unknown>).outboundPolicy as string || "allow-all"}
                      onChange={(e) => update("outboundPolicy", e.target.value)}
                    >
                      <option value="allow-all">Allow all outbound</option>
                      <option value="restrict">Restrict by port</option>
                    </select>
                  </div>
                  {(data as Record<string, unknown>).outboundPolicy === "restrict" && (
                    <div className="props-field">
                      <label className="props-label">Allowed Ports</label>
                      <input
                        className="props-input"
                        value={(data as Record<string, unknown>).outboundPorts as string || ""}
                        onChange={(e) => update("outboundPorts", e.target.value)}
                        placeholder="e.g. 80,443,53/udp"
                        style={{ fontFamily: "monospace" }}
                      />
                      <span style={{ fontSize: 10, color: "var(--troshka-text-dim)", marginTop: 2 }}>
                        Comma-separated. Append /udp for UDP, default is TCP.
                      </span>
                    </div>
                  )}
                </div>

                {(data as Record<string, unknown>).gatewayMode === "nat-portforward" && (
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
                                {i === 0 && <label className="props-label">External IP</label>}
                                <select className="props-select" style={{ fontSize: 11 }}
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
                              </div>
                              <div className="props-field" style={{ flex: "0 0 50px" }}>
                                {i === 0 && <label className="props-label">Ext Port</label>}
                                <input className="props-input" value={pf.extPort} placeholder="80" style={{ fontFamily: "monospace" }}
                                  onChange={(e) => {
                                    const updated = [...portForwards];
                                    updated[i] = { ...pf, extPort: e.target.value };
                                    update("portForwards", updated);
                                  }}
                                />
                              </div>
                            </div>
                            <div className="props-row" style={{ alignItems: "end" }}>
                              <span style={{ padding: "0 4px", color: "var(--troshka-text-dim)", fontSize: 12 }}>→</span>
                              <div className="props-field" style={{ flex: 1 }}>
                                {i === 0 && <label className="props-label">Internal IP</label>}
                                <input className="props-input" value={pf.intIp} placeholder="192.168.1.10" style={{ fontFamily: "monospace" }}
                                  onChange={(e) => {
                                    const updated = [...portForwards];
                                    updated[i] = { ...pf, intIp: e.target.value };
                                    update("portForwards", updated);
                                  }}
                                />
                              </div>
                              <div className="props-field" style={{ flex: "0 0 50px" }}>
                                {i === 0 && <label className="props-label">Int Port</label>}
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
                />
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
                  {(data as Record<string, unknown>).libraryItemName ? (
                    <span style={{ fontSize: 12, marginTop: 4, display: "block", color: "var(--troshka-green)" }}>
                      💿 {(data as Record<string, unknown>).libraryItemName as string}
                    </span>
                  ) : (
                    <span style={{ fontSize: 11, color: "var(--troshka-text-dim)", marginTop: 4, display: "block" }}>
                      No ISO selected
                    </span>
                  )}
                </div>
                {(data as Record<string, unknown>).libraryItemSize && (
                  <div className="props-field">
                    <label className="props-label">Size</label>
                    <span style={{ fontSize: 13, color: "var(--troshka-text-dim)" }}>
                      {(data as Record<string, unknown>).libraryItemSize as number} GB (read-only)
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
                    value={(data as Record<string, unknown>).source as string || "blank"}
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
                {(data as Record<string, unknown>).source === "library" && (
                  <div className="props-field">
                    <button
                      className="props-library-btn"
                      onClick={() => setShowLibraryPicker("image")}
                    >
                      📚 Select from Library...
                    </button>
                    {(data as Record<string, unknown>).libraryItemName ? (
                      <span style={{ fontSize: 12, marginTop: 4, display: "block", color: "var(--troshka-green)" }}>
                        🛢 {(data as Record<string, unknown>).libraryItemName as string}
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
                      const isFromLibrary = (data as Record<string, unknown>).source === "library";
                      const sourceImageSize = (data as Record<string, unknown>).libraryItemSize as number || 0;
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
                      checked={(data as Record<string, unknown>).bootable as boolean ?? true}
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
      {showLibraryPicker && node && (
        <LibraryPicker
          type={showLibraryPicker}
          onSelect={(item) => {
            updateNodeData(node.id, {
              libraryItemId: item.id,
              libraryItemName: item.name,
              libraryItemSize: item.size_gb,
              source: "library",
              size: Math.max(item.size_gb, (data as Record<string, unknown>).size as number || 0),
              format: item.format === "iso" ? "iso" : item.format,
            });
          }}
          onClose={() => setShowLibraryPicker(null)}
        />
      )}
    </div>
  );
}
