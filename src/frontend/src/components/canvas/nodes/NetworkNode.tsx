"use client";

import React, { memo, useState } from "react";
import { createPortal } from "react-dom";
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
  const [routesOpen, setRoutesOpen] = useState(false);
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
                  <div style={{ fontSize: 9, fontFamily: "monospace", color: "var(--troshka-green)", lineHeight: 1.3, cursor: "pointer", textDecoration: "underline", opacity: 0.8 }}
                    onClick={(e) => { e.stopPropagation(); setRoutesOpen(true); }}>
                    {withIps.length > 0 && <span>{withIps.length} IP{withIps.length !== 1 ? "s" : ""}</span>}
                    {withIps.length > 0 && routeHostnames.length > 0 && " + "}
                    {routeHostnames.length > 0 && <span>{routeHostnames.length} route{routeHostnames.length !== 1 ? "s" : ""}</span>}
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
                    {portForwards.length > 0 && (() => {
                      const eps = (gw.externalEndpoints as Array<{type?: string}>) || [];
                      const hasAccess = externalIps.some((e) => e.ip) || eps.some((e) => e.type === "route");
                      return !hasAccess ? (
                        <div style={{ fontSize: 9, color: "var(--troshka-text-dim)", fontFamily: "monospace", cursor: "pointer", userSelect: "none" }}
                          onClick={(e) => { e.stopPropagation(); setRoutesOpen(true); }}>
                          {portForwards.length} forward{portForwards.length !== 1 ? "s" : ""}
                        </div>
                      ) : null;
                    })()}
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

      {routesOpen && (() => {
        const gw = d as Record<string, any>;
        const endpoints = (gw.externalEndpoints as Array<{hostname?: string; vmName?: string; port?: number; type?: string}>) || [];
        const routes = endpoints.filter((ep) => ep.type === "route" && ep.hostname);
        const eips = useCanvasStore.getState().externalIps.filter((eip) => eip.ip);
        const pfs = (gw.portForwards as Array<{extPort: string; intIp: string; intPort: string; proto: string; extIpId?: string}>) || [];
        const allEips = useCanvasStore.getState().externalIps;
        return createPortal(
          <div className="nodrag nopan" onClick={(e) => { e.stopPropagation(); setRoutesOpen(false); }}
            style={{ position: "fixed", inset: 0, zIndex: 9999, display: "flex", alignItems: "center", justifyContent: "center", background: "rgba(0,0,0,0.5)" }}>
            <div onClick={(e) => e.stopPropagation()}
              style={{ background: "var(--troshka-surface)", border: "1px solid var(--troshka-border)", borderRadius: 12, padding: 20, width: 720, maxWidth: "90vw", maxHeight: "70vh", overflow: "auto" }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
                <span style={{ fontWeight: 600, fontSize: 15 }}>External Access</span>
                <button onClick={() => setRoutesOpen(false)} style={{ background: "none", border: "none", color: "var(--troshka-text-dim)", cursor: "pointer", fontSize: 18 }}>✕</button>
              </div>
              {eips.length > 0 && (
                <>
                  <div style={{ fontSize: 12, fontWeight: 600, color: "var(--troshka-text-dim)", marginBottom: 6 }}>Elastic IPs</div>
                  <table style={{ width: "100%", fontSize: 12, borderCollapse: "collapse", marginBottom: 16 }}>
                    <thead>
                      <tr style={{ borderBottom: "1px solid var(--troshka-border)", textAlign: "left" }}>
                        <th style={{ padding: "6px 8px", color: "var(--troshka-text-dim)", fontWeight: 500 }}>Name</th>
                        <th style={{ padding: "6px 8px", color: "var(--troshka-text-dim)", fontWeight: 500 }}>IP</th>
                      </tr>
                    </thead>
                    <tbody>
                      {eips.map((eip) => (
                        <tr key={eip.id} style={{ borderBottom: "1px solid var(--troshka-border)" }}>
                          <td style={{ padding: "6px 8px" }}>{eip.name}</td>
                          <td style={{ padding: "6px 8px", fontFamily: "monospace" }}>
                            {eip.ip}
                            <span style={{ cursor: "pointer", marginLeft: 8, opacity: 0.5, fontSize: 10 }}
                              onClick={() => navigator.clipboard.writeText(eip.ip)} title="Copy">Copy</span>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </>
              )}
              {pfs.length > 0 && (
                <>
                  <div style={{ fontSize: 12, fontWeight: 600, color: "var(--troshka-text-dim)", marginBottom: 6 }}>Port Forwards</div>
                  <table style={{ width: "100%", fontSize: 12, borderCollapse: "collapse", marginBottom: 16 }}>
                    <thead>
                      <tr style={{ borderBottom: "1px solid var(--troshka-border)", textAlign: "left" }}>
                        <th style={{ padding: "6px 8px", color: "var(--troshka-text-dim)", fontWeight: 500 }}>External</th>
                        <th style={{ padding: "6px 8px", color: "var(--troshka-text-dim)", fontWeight: 500 }}>Internal</th>
                      </tr>
                    </thead>
                    <tbody>
                      {pfs.map((pf, i) => {
                        const routeMatch = routes.find((ep) => String(ep.port) === String(pf.extPort));
                        const eip = allEips.find((e) => e.id === pf.extIpId);
                        return (
                          <tr key={i} style={{ borderBottom: "1px solid var(--troshka-border)" }}>
                            <td style={{ padding: "6px 8px", fontFamily: "monospace", fontSize: 11 }}>
                              {(() => {
                                if (routeMatch) {
                                  const port = String(pf.extPort);
                                  const url = port === "443" ? `https://${routeMatch.hostname}` : `https://${routeMatch.hostname}:${port}`;
                                  return (
                                    <>
                                      <a href={url} target="_blank" rel="noopener noreferrer" style={{ color: "var(--troshka-green)", textDecoration: "none" }}>{url}</a>
                                      <span style={{ cursor: "pointer", marginLeft: 8, opacity: 0.5, fontSize: 10 }}
                                        onClick={() => navigator.clipboard.writeText(url)} title="Copy">Copy</span>
                                    </>
                                  );
                                }
                                if (eip?.ip) {
                                  const addr = `${eip.ip}:${pf.extPort}`;
                                  return (
                                    <>
                                      <span>{addr}</span>
                                      <span style={{ cursor: "pointer", marginLeft: 8, opacity: 0.5, fontSize: 10 }}
                                        onClick={() => navigator.clipboard.writeText(addr)} title="Copy">Copy</span>
                                    </>
                                  );
                                }
                                return eip?.name || "—";
                              })()}
                            </td>
                            <td style={{ padding: "6px 8px", fontFamily: "monospace" }}>{pf.intIp || "—"}:{pf.intPort || "—"}</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </>
              )}
            </div>
          </div>,
          document.body,
        );
      })()}
    </div>
  );
}

export default memo(NetworkNodeComponent);
