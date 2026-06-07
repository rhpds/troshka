"use client";

import React from "react";
import type { Node } from "@xyflow/react";

interface ReadOnlyPropertiesPanelProps {
  node: Node;
  onClose: () => void;
}

function PropRow({ label, value }: { label: string; value: React.ReactNode }) {
  if (value === undefined || value === null || value === "") return null;
  return (
    <div style={{ display: "flex", justifyContent: "space-between", padding: "4px 0", fontSize: 13 }}>
      <span style={{ opacity: 0.6 }}>{label}</span>
      <span style={{ fontWeight: 500, textAlign: "right", maxWidth: "60%" }}>{value}</span>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 12 }}>
      <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", opacity: 0.5, marginBottom: 6 }}>{title}</div>
      {children}
    </div>
  );
}

function VMProperties({ data }: { data: Record<string, unknown> }) {
  const nics = (data.nics as Array<Record<string, string>>) || [];
  const dcs = (data.diskControllers as Array<Record<string, string>>) || [];

  return (
    <>
      <Section title="Compute">
        <PropRow label="vCPUs" value={String(data.vcpus)} />
        <PropRow label="RAM" value={`${data.ram} GB`} />
        <PropRow label="OS" value={String(data.os || "—")} />
      </Section>

      <Section title="Boot">
        <PropRow label="Method" value={String(data.bootMethod || "template")} />
        <PropRow label="Auto-start" value={data.autoStart ? "Yes" : "No"} />
      </Section>

      {data.cloudInit && (
        <Section title="Cloud-Init">
          <PropRow label="Enabled" value="Yes" />
          <PropRow label="Hostname" value={String((data as Record<string, unknown>).ciHostname || "—")} />
        </Section>
      )}

      {nics.length > 0 && (
        <Section title={`NICs (${nics.length})`}>
          {nics.map((nic, i) => (
            <div key={nic.id || i} style={{ padding: "4px 0", fontSize: 12, borderBottom: i < nics.length - 1 ? "1px solid rgba(255,255,255,0.05)" : undefined }}>
              <div style={{ fontWeight: 500 }}>{nic.name}</div>
              <div style={{ opacity: 0.5, fontSize: 11 }}>{nic.mac} · {nic.model}</div>
            </div>
          ))}
        </Section>
      )}

      {dcs.length > 0 && (
        <Section title={`Disk Controllers (${dcs.length})`}>
          {dcs.map((dc, i) => (
            <div key={dc.id || i} style={{ fontSize: 12, padding: "2px 0" }}>
              {dc.name} ({dc.bus})
            </div>
          ))}
        </Section>
      )}

      <Section title="Console">
        <PropRow label="Type" value={String(data.consoleType || "vnc")} />
      </Section>
    </>
  );
}

function NetworkProperties({ data }: { data: Record<string, unknown> }) {
  return (
    <>
      <Section title="Network">
        <PropRow label="Type" value={String(data.subtype || "network")} />
        <PropRow label="CIDR" value={String(data.cidr || "—")} />
        <PropRow label="Gateway" value={String(data.gateway || "—")} />
      </Section>

      <Section title="Services">
        <PropRow label="DHCP" value={data.dhcp ? "Enabled" : "Disabled"} />
        <PropRow label="DNS" value={data.dns ? "Enabled" : "Disabled"} />
        <PropRow label="DNS Domain" value={String(data.dnsDomain || "—")} />
        <PropRow label="PXE" value={data.pxe ? "Enabled" : "Disabled"} />
      </Section>

      {data.dhcpStart && (
        <Section title="DHCP Range">
          <PropRow label="Start" value={String(data.dhcpStart)} />
          <PropRow label="End" value={String(data.dhcpEnd || "—")} />
        </Section>
      )}
    </>
  );
}

function StorageProperties({ data }: { data: Record<string, unknown> }) {
  return (
    <Section title="Disk">
      <PropRow label="Size" value={`${data.size} GB`} />
      <PropRow label="Format" value={String(data.format || "qcow2")} />
      <PropRow label="Source" value={String(data.source || "blank")} />
      {data.libraryItemName && <PropRow label="Image" value={String(data.libraryItemName)} />}
    </Section>
  );
}

export default function ReadOnlyPropertiesPanel({ node, onClose }: ReadOnlyPropertiesPanelProps) {
  const data = node.data as Record<string, unknown>;
  const nodeType = node.type;

  const icon = nodeType === "vmNode"
    ? (data.icon as string || "🖥")
    : nodeType === "networkNode"
      ? "🔌"
      : (data.format === "iso" ? "💿" : "🛢");

  const subtitle = nodeType === "vmNode"
    ? "Virtual Machine"
    : nodeType === "networkNode"
      ? (data.subtype === "router" ? "Router" : data.subtype === "gateway" ? "Gateway" : "Network")
      : "Storage";

  return (
    <div style={{
      position: "absolute", top: 0, right: 0, bottom: 0, width: 300,
      background: "var(--pf-t--global--background--color--primary--default)",
      borderLeft: "1px solid var(--pf-t--global--border--color--default)",
      overflowY: "auto", zIndex: 10, padding: 16,
    }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ fontSize: 22 }}>{icon}</span>
          <div>
            <div style={{ fontWeight: 600, fontSize: 14 }}>{data.name as string}</div>
            <div style={{ fontSize: 11, opacity: 0.5 }}>{subtitle}</div>
          </div>
        </div>
        <button onClick={onClose} style={{ background: "none", border: "none", color: "var(--pf-t--global--text--color--regular)", cursor: "pointer", fontSize: 16 }}>✕</button>
      </div>

      <div style={{ borderTop: "1px solid var(--pf-t--global--border--color--default)", paddingTop: 12 }}>
        {nodeType === "vmNode" && <VMProperties data={data} />}
        {nodeType === "networkNode" && <NetworkProperties data={data} />}
        {nodeType === "storageNode" && <StorageProperties data={data} />}
      </div>
    </div>
  );
}
