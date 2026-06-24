"use client";

import React, { memo, useState } from "react";
import { Handle, Position, type NodeProps } from "@xyflow/react";
import type { NetworkNodeData } from "@/stores/canvasStore";
import { useCanvasStore } from "@/stores/canvasStore";

function RJ45Icon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <rect x="4" y="2" width="16" height="16" rx="2" />
      <line x1="8" y1="18" x2="8" y2="22" />
      <line x1="12" y1="18" x2="12" y2="22" />
      <line x1="16" y1="18" x2="16" y2="22" />
      <rect x="6" y="5" width="12" height="6" rx="1" />
      <line x1="9" y1="5" x2="9" y2="11" />
      <line x1="12" y1="5" x2="12" y2="11" />
      <line x1="15" y1="5" x2="15" y2="11" />
    </svg>
  );
}

function BmcWarning({ nodeId }: { nodeId: string }) {
  const hasConnection = useCanvasStore((s) => s.edges.some((e) => e.source === nodeId || e.target === nodeId));
  if (hasConnection) return null;
  return (
    <div style={{ background: "rgba(251,191,36,0.15)", color: "#fbbf24", fontSize: 9, padding: "3px 6px", borderRadius: 4, marginTop: 4, textAlign: "center" }}>
      Add NIC on provisioner VM and connect
    </div>
  );
}

function NetworkNodeComponent({ data, selected, id }: NodeProps) {
  const d = data as unknown as NetworkNodeData;
  const [fwdExpanded, setFwdExpanded] = useState(false);
  const projectState = useCanvasStore((s) => s.projectState);
  const deployedNodeData = useCanvasStore((s) => s.deployedNodeData);
  const isDirty = React.useMemo(() => {
    const deployed = deployedNodeData[id];
    if (!deployed) return false;
    return JSON.stringify(d) !== deployed;
  }, [id, d, deployedNodeData]);

  const networkType = (data as Record<string, any>).networkType;
  const isBmc = networkType === "bmc";

  return (
    <div
      className="network-node-card"
      style={(() => {
        const colors = {
          router:  { bg: "rgba(251,146,60,0.08)", border: "rgba(251,146,60,0.6)",  glow: "rgba(251,146,60,0.2)",  selected: "#fb923c" },
          gateway: { bg: "rgba(74,222,128,0.08)",  border: "rgba(74,222,128,0.6)",   glow: "rgba(74,222,128,0.2)",  selected: "#4ade80" },
          network: { bg: "rgba(34,211,238,0.08)",  border: "rgba(34,211,238,0.4)",   glow: "rgba(34,211,238,0.2)",  selected: "var(--troshka-cyan)" },
          bmc:     { bg: "rgba(168,85,247,0.08)",  border: "rgba(168,85,247,0.6)",   glow: "rgba(168,85,247,0.2)",  selected: "#a855f7" },
          loadbalancer: { bg: "rgba(59,130,246,0.08)", border: "rgba(59,130,246,0.6)", glow: "rgba(59,130,246,0.2)", selected: "#3b82f6" },
        };
        const isLb = (d as any).networkType === "loadbalancer";
        const c = isBmc ? colors.bmc : isLb ? colors.loadbalancer : (colors[d.subtype as keyof typeof colors] || colors.network);
        return {
          background: c.bg,
          borderColor: selected ? c.selected : c.border,
          boxShadow: selected ? `0 0 0 3px ${c.glow}` : "none",
          opacity: projectState === "draft" ? 0.55 : 1,
          transition: "opacity 0.3s",
        };
      })()}
    >
      <span className="network-node-icon">
        {d.subtype === "router" ? "🔀" : d.subtype === "gateway" ? "🌐" : isBmc ? (
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <rect x="2" y="4" width="20" height="16" rx="2" />
            <circle cx="6" cy="12" r="1.5" fill="currentColor" />
            <line x1="10" y1="9" x2="20" y2="9" />
            <line x1="10" y1="12" x2="20" y2="12" />
            <line x1="10" y1="15" x2="20" y2="15" />
          </svg>
        ) : (d as any).networkType === "loadbalancer" ? (
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="4" cy="12" r="2" />
            <circle cx="20" cy="6" r="2" />
            <circle cx="20" cy="12" r="2" />
            <circle cx="20" cy="18" r="2" />
            <line x1="6" y1="12" x2="11" y2="12" />
            <line x1="11" y1="12" x2="18" y2="6" />
            <line x1="11" y1="12" x2="18" y2="12" />
            <line x1="11" y1="12" x2="18" y2="18" />
          </svg>
        ) : <RJ45Icon />}
      </span>
      <div className="network-node-info">
        <div className="network-node-name">{d.name}{isDirty && <span title="Unsaved changes" style={{ fontSize: 9, marginLeft: 4 }}>💾</span>}</div>
        {d.subtype === "network" && (
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <span className="network-node-cidr">{d.cidr}</span>
            {isBmc ? (
              <span style={{ background: "rgba(168,85,247,0.2)", color: "rgba(168,85,247,1)", padding: "1px 6px", borderRadius: 4, fontSize: 9, fontWeight: 600 }}>
                BMC
              </span>
            ) : (
              <>
                {d.dhcp && <span className="network-node-badge dhcp">DHCP</span>}
                {d.dns && <span className="network-node-badge dns">DNS</span>}
              </>
            )}
          </div>
        )}
        {isBmc && <BmcWarning nodeId={id} />}
        {d.subtype === "router" && (
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <span className="network-node-cidr" style={{ fontSize: 10 }}>L3 Router</span>
          </div>
        )}
        {d.subtype === "gateway" && (() => {
          const gw = d as unknown as Record<string, any>;
          const isPortFwd = gw.gatewayMode === "nat-portforward";
          const portForwards = (gw.portForwards as Array<{extPort: string; intIp: string; intPort: string}>) || [];
          return (
            <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
              <span className="network-node-cidr" style={{ fontSize: 10 }}>
                {isPortFwd ? "NAT + Port Fwd" : "NAT Outbound"}
              </span>
              {(() => {
                const projectIps = useCanvasStore.getState().externalIps;
                const withIps = projectIps.filter((eip) => eip.ip);
                const endpoints = (gw.externalEndpoints as Array<{hostname?: string; vmName?: string; port?: number; type?: string}>) || [];
                const routeHostnames = endpoints.filter((ep) => ep.type === "route" && ep.hostname);
                return (withIps.length > 0 || routeHostnames.length > 0) ? (
                  <div style={{ fontSize: 9, fontFamily: "monospace", color: "var(--troshka-green)", lineHeight: 1.3 }}>
                    {withIps.map((eip) => <div key={eip.id}>{eip.ip}</div>)}
                    {routeHostnames.map((ep, i) => <div key={`rt-${i}`} style={{ wordBreak: "break-all" }}><a href={`https://${ep.hostname}`} target="_blank" rel="noopener noreferrer" style={{ color: "var(--troshka-green)", textDecoration: "none" }} onClick={(e) => e.stopPropagation()}>{ep.hostname}</a></div>)}
                  </div>
                ) : null;
              })()}
              {isPortFwd && (() => {
                const externalIps = useCanvasStore.getState().externalIps;
                const hasIncomplete = portForwards.some((pf) =>
                  !(pf as Record<string, string>).extIpId || !pf.extPort || !pf.intIp || !pf.intPort
                );
                const gwEndpoints = (gw.externalEndpoints as Array<{type?: string}>) || [];
                const hasRoutes = gwEndpoints.some((ep) => ep.type === "route");
                const noIps = externalIps.length === 0 && portForwards.length > 0 && !hasRoutes;
                return (
                  <>
                    {portForwards.length > 0 && (
                      <div style={{ fontSize: 9, color: "var(--troshka-text-dim)", fontFamily: "monospace", lineHeight: 1.4 }}>
                        <div
                          style={{ cursor: "pointer", userSelect: "none", color: "var(--troshka-text-dim)", marginBottom: 2 }}
                          onClick={(e) => { e.stopPropagation(); setFwdExpanded(!fwdExpanded); }}
                        >
                          {fwdExpanded ? "▾" : "▸"} {portForwards.length} forward{portForwards.length !== 1 ? "s" : ""}
                        </div>
                        {fwdExpanded && portForwards.map((pf, i) => {
                          const extIpId = (pf as Record<string, string>).extIpId;
                          const eip = externalIps.find((e) => e.id === extIpId);
                          const routeEndpoints = (gw.externalEndpoints as Array<{hostname?: string; vmName?: string; port?: number; type?: string}>) || [];
                          const routeMatch = routeEndpoints.find((ep) => ep.type === "route" && String(ep.port) === String(pf.extPort));
                          const ipLabel = routeMatch ? "route" : eip ? (eip.ip || "auto") : "";
                          return (
                            <div key={i} style={{ marginBottom: 2 }}>
                              {ipLabel ? `${ipLabel}:` : ""}{pf.extPort || "?"} →
                              <div style={{ paddingLeft: 10 }}>{pf.intIp || "?"}:{pf.intPort || "?"}</div>
                            </div>
                          );
                        })}
                      </div>
                    )}
                    {portForwards.length === 0 && (
                      <div style={{ fontSize: 9, color: "var(--troshka-yellow)" }}>⚠ No port forwards</div>
                    )}
                    {hasIncomplete && (
                      <div style={{ fontSize: 9, color: "var(--troshka-yellow)" }}>⚠ Incomplete rules</div>
                    )}
                    {noIps && (
                      <div style={{ fontSize: 9, color: "var(--troshka-yellow)" }}>⚠ No external IPs</div>
                    )}
                  </>
                );
              })()}
            </div>
          );
        })()}
        {(d as any).networkType === "loadbalancer" && (() => {
          const frontends = ((d as any).frontends as Array<{name: string; bindPort: number}>) || [];
          return (
            <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <span className="network-node-cidr" style={{ fontSize: 10 }}>HAProxy L4</span>
                {((d as any).external ?? true) && (
                  <span style={{ background: "rgba(59,130,246,0.2)", color: "#3b82f6", padding: "0px 5px", borderRadius: 3, fontSize: 8, fontWeight: 600 }}>
                    EXT
                  </span>
                )}
              </div>
              {(d as any).lbIp && (
                <div style={{ fontSize: 9, color: "var(--troshka-text-dim)", fontFamily: "monospace" }}>
                  {(d as any).lbIp}
                </div>
              )}
              {frontends.length > 0 && (
                <div style={{ fontSize: 9, color: "var(--troshka-text-dim)", fontFamily: "monospace" }}>
                  {frontends.map((fe, i) => (
                    <span key={i}>{i > 0 ? ", " : ""}{fe.bindPort}</span>
                  ))}
                </div>
              )}
            </div>
          );
        })()}
      </div>

      {/* Networks: top/bottom (blue) for VMs, left/right (orange) for routers/gateways */}
      {d.subtype === "network" && (
        <>
          <Handle type="source" position={Position.Top} id="top" className="canvas-handle canvas-handle-network" />
          <Handle type="source" position={Position.Bottom} id="bottom" className="canvas-handle canvas-handle-network" />
          <Handle type="source" position={Position.Left} id="left" className="canvas-handle canvas-handle-router" />
          <Handle type="source" position={Position.Right} id="right" className="canvas-handle canvas-handle-router" />
        </>
      )}
      {/* Routers: orange handles on all 4 sides */}
      {d.subtype === "router" && (
        <>
          <Handle type="source" position={Position.Top} id="top" className="canvas-handle canvas-handle-router" />
          <Handle type="source" position={Position.Bottom} id="bottom" className="canvas-handle canvas-handle-router" />
          <Handle type="source" position={Position.Left} id="left" className="canvas-handle canvas-handle-router" />
          <Handle type="source" position={Position.Right} id="right" className="canvas-handle canvas-handle-router" />
        </>
      )}
      {/* Gateways: left/right only */}
      {d.subtype === "gateway" && (
        <>
          <Handle type="source" position={Position.Left} id="left" className="canvas-handle canvas-handle-router" />
          <Handle type="source" position={Position.Right} id="right" className="canvas-handle canvas-handle-router" />
        </>
      )}
      {/* Load Balancers: top/bottom for VM connections */}
      {(d as any).networkType === "loadbalancer" && (
        <>
          <Handle type="source" position={Position.Top} id="top" className="canvas-handle canvas-handle-network" />
          <Handle type="source" position={Position.Bottom} id="bottom" className="canvas-handle canvas-handle-network" />
        </>
      )}
    </div>
  );
}

export default memo(NetworkNodeComponent);
