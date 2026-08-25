import type { Edge, Node } from "@xyflow/react";
import {
  ensureShowroomGatewayEdge,
  getGatewayNode,
  getShowroomNode,
  isShowroomGatewayEdge,
} from "@/lib/showroomValidation";

export type PortForward = {
  extPort: string;
  intIp: string;
  intPort: string;
  proto: string;
  extIpId?: string;
  managedByShowroom?: boolean;
};

export type ShowroomExternalIp = {
  id: string;
  name: string;
  ip: string;
};

export function showroomInfraIpFromVni(vni: number): string {
  const octet3 = vni & 0xff;
  return `172.30.${octet3}.3`;
}

export function firstProjectVni(vniMap: Record<string, number>): number | null {
  const values = Object.values(vniMap);
  if (!values.length) return null;
  return Math.min(...values);
}

export function isShowroomInfraForward(pf: PortForward): boolean {
  const intIp = (pf.intIp || "").trim();
  if (!intIp.startsWith("172.30.") || !intIp.endsWith(".3")) return false;
  return pf.extPort === "443" && pf.intPort === "80";
}

export function isShowroomManagedForward(
  pf: PortForward & { managedByShowroom?: boolean },
): boolean {
  return pf.managedByShowroom === true || isShowroomInfraForward(pf);
}

/** Mirror backend _inject_showroom_port_forward (vxlan.py). */
export function injectShowroomPortForwards(
  portForwards: PortForward[],
  firstVni: number | null,
): PortForward[] {
  if (!firstVni) return portForwards;
  const infraIp = showroomInfraIpFromVni(firstVni);

  const out = portForwards.filter(
    (pf) =>
      !(
        pf.extPort === "443" &&
        pf.intPort === "80" &&
        (pf.intIp || "").trim() !== infraIp
      ),
  );

  const has443 = out.some(
    (pf) =>
      pf.extPort === "443" &&
      (pf.intIp || "").trim() === infraIp &&
      pf.intPort === "80",
  );
  if (!has443) {
    out.push({
      extPort: "443",
      intIp: infraIp,
      intPort: "80",
      proto: "tcp",
      extIpId: "",
      managedByShowroom: true,
    });
  }
  return out;
}

function portForwardsEqual(a: PortForward[], b: PortForward[]): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

export function ensureShowroomExternalIps(
  externalIps: ShowroomExternalIp[],
): ShowroomExternalIp[] {
  if (externalIps.length > 0) return externalIps;
  return [{ id: crypto.randomUUID(), name: "IP-1", ip: "" }];
}

function stripShowroomAutoExternalIps(
  externalIps: ShowroomExternalIp[],
  gatewayPortForwards: PortForward[],
): ShowroomExternalIp[] {
  if (externalIps.length !== 1) return externalIps;
  const only = externalIps[0];
  if (only.name !== "IP-1" || only.ip) return externalIps;
  if (gatewayPortForwards.some((pf) => pf.extIpId === only.id)) return externalIps;
  return [];
}

function stripShowroomGatewayPortForwardsOnNodes(nodes: Node[]): Node[] {
  const gateway = getGatewayNode(nodes);
  if (!gateway) return nodes;

  const gwData = gateway.data as Record<string, unknown>;
  const existing = ((gwData.portForwards as PortForward[]) || []).map((pf) => ({
    ...pf,
  }));
  const stripped = existing.filter((pf) => !isShowroomInfraForward(pf));
  if (portForwardsEqual(stripped, existing)) return nodes;

  const nextData: Record<string, unknown> = { ...gwData, portForwards: stripped };
  if (stripped.length === 0 && gwData.gatewayMode === "nat-portforward") {
    nextData.gatewayMode = "nat";
  }

  return nodes.map((n) =>
    n.id === gateway.id ? { ...n, data: { ...n.data, ...nextData } } : n,
  );
}

function ensureShowroomGatewayPortForwardsOnNodes(
  nodes: Node[],
  externalIps: ShowroomExternalIp[],
  vniMap: Record<string, number>,
): Node[] {
  const showroom = getShowroomNode(nodes);
  const gateway = getGatewayNode(nodes);
  if (!showroom || !gateway) return nodes;

  const firstVni = firstProjectVni(vniMap);
  if (!firstVni) return nodes;

  const gwData = gateway.data as Record<string, unknown>;
  const existing = ((gwData.portForwards as PortForward[]) || []).map((pf) => ({
    ...pf,
  }));
  const merged = injectShowroomPortForwards(existing, firstVni);
  const eipId = externalIps[0]?.id || "";
  const withEip = merged.map((pf) => ({
    ...pf,
    extIpId: pf.extIpId || eipId,
    ...(isShowroomInfraForward(pf) || pf.managedByShowroom
      ? { managedByShowroom: true }
      : {}),
  }));

  const needsMode = gwData.gatewayMode !== "nat-portforward";
  const needsPf = !portForwardsEqual(withEip, existing);
  if (!needsMode && !needsPf) return nodes;

  return nodes.map((n) => {
    if (n.id !== gateway.id) return n;
    return {
      ...n,
      data: {
        ...n.data,
        ...(needsMode ? { gatewayMode: "nat-portforward" } : {}),
        ...(needsPf ? { portForwards: withEip } : {}),
      },
    };
  });
}

/** Showroom + gateway: sync external IP, 443→infra:80 forward, cosmetic edge. */
export function syncShowroomGatewayAccess(
  nodes: Node[],
  edges: Edge[],
  externalIps: ShowroomExternalIp[],
  vniMap: Record<string, number>,
): { nodes: Node[]; edges: Edge[]; externalIps: ShowroomExternalIp[] } {
  const showroom = getShowroomNode(nodes);
  const gateway = getGatewayNode(nodes);

  if (!showroom || !gateway) {
    const strippedNodes = stripShowroomGatewayPortForwardsOnNodes(nodes);
    const gatewayNode = getGatewayNode(strippedNodes);
    const pfs =
      ((gatewayNode?.data as Record<string, unknown> | undefined)?.portForwards as
        | PortForward[]
        | undefined) || [];
    const strippedEdges = edges.filter((e) => !isShowroomGatewayEdge(e));
    return {
      nodes: strippedNodes,
      edges: strippedEdges,
      externalIps: stripShowroomAutoExternalIps(externalIps, pfs),
    };
  }

  const ips = ensureShowroomExternalIps(externalIps);
  const withPf = ensureShowroomGatewayPortForwardsOnNodes(nodes, ips, vniMap);
  const withEdge = ensureShowroomGatewayEdge(withPf, edges);
  return { nodes: withPf, edges: withEdge, externalIps: ips };
}

export function topologyHasLabNetwork(nodes: Node[]): boolean {
  return nodes.some((n) => {
    if (n.type !== "networkNode") return false;
    const d = n.data as Record<string, unknown>;
    return d.subtype === "network" && d.networkType !== "bmc";
  });
}
