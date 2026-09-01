import type { Edge, Node } from "@xyflow/react";
import {
  injectShowroomOutboundPorts,
  stripShowroomOutboundPorts,
} from "@/lib/gatewayValidation";
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

// Providers with OpenShift ingress: 443/80 forwards are served by a Route and
// must never bind to the EIP (mirror backend deploy_topology._ROUTE_PROVIDERS).
// Cloud providers have no ingress, so their 443/80 forwards stay on the EIP.
const ROUTE_PROVIDERS = new Set(["ocpvirt", "kubevirt"]);

function isWebForward(pf: PortForward): boolean {
  return pf.extPort === "443" || pf.extPort === "80";
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
  const managedOutbound = (gwData.showroomManagedOutbound as string[]) || [];
  const strippedOutbound =
    gwData.outboundPolicy === "restrict" && managedOutbound.length
      ? stripShowroomOutboundPorts(String(gwData.outboundPorts || ""), managedOutbound)
      : String(gwData.outboundPorts || "");

  const pfChanged = !portForwardsEqual(stripped, existing);
  const outboundChanged = strippedOutbound !== String(gwData.outboundPorts || "");
  if (!pfChanged && !outboundChanged) return nodes;

  const nextData: Record<string, unknown> = { ...gwData };
  if (pfChanged) {
    nextData.portForwards = stripped;
    if (stripped.length === 0 && gwData.gatewayMode === "nat-portforward") {
      nextData.gatewayMode = "nat";
    }
  }
  if (outboundChanged) {
    nextData.outboundPorts = strippedOutbound;
    delete nextData.showroomManagedOutbound;
  }

  return nodes.map((n) =>
    n.id === gateway.id ? { ...n, data: { ...n.data, ...nextData } } : n,
  );
}

function ensureShowroomGatewayPortForwardsOnNodes(
  nodes: Node[],
  externalIps: ShowroomExternalIp[],
  vniMap: Record<string, number>,
  providerType?: string | null,
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
  const routeWeb = ROUTE_PROVIDERS.has(providerType || "");
  const withEip = merged.map((pf) => {
    const entry: PortForward = { ...pf };
    if (routeWeb && isWebForward(pf)) {
      // On OpenShift-ingress providers, 443/80 are served by a Route — never
      // bind them to the EIP. Remove the key entirely (not "") to match the
      // backend's entry.pop("extIpId") so the topology doesn't read dirty.
      delete entry.extIpId;
    } else {
      entry.extIpId = pf.extIpId || eipId;
    }
    if (isShowroomInfraForward(pf) || pf.managedByShowroom) {
      entry.managedByShowroom = true;
    }
    return entry;
  });

  // nat-portforward only when a forward is actually EIP-bound; route-served
  // (no extIpId) forwards need only plain NAT. Mirrors the backend so the
  // topology doesn't read dirty.
  const desiredMode = withEip.some((pf) => pf.extIpId) ? "nat-portforward" : "nat";
  const needsMode = gwData.gatewayMode !== desiredMode;
  const needsPf = !portForwardsEqual(withEip, existing);
  const outboundInject =
    gwData.outboundPolicy === "restrict"
      ? injectShowroomOutboundPorts(String(gwData.outboundPorts || ""))
      : { ports: String(gwData.outboundPorts || ""), added: [] as string[] };
  const needsOutbound = outboundInject.added.length > 0;
  if (!needsMode && !needsPf && !needsOutbound) return nodes;

  return nodes.map((n) => {
    if (n.id !== gateway.id) return n;
    const managed = [
      ...new Set([
        ...((gwData.showroomManagedOutbound as string[]) || []),
        ...outboundInject.added,
      ]),
    ];
    return {
      ...n,
      data: {
        ...n.data,
        ...(needsMode ? { gatewayMode: desiredMode } : {}),
        ...(needsPf ? { portForwards: withEip } : {}),
        ...(needsOutbound
          ? {
              outboundPorts: outboundInject.ports,
              showroomManagedOutbound: managed,
            }
          : {}),
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
  providerType?: string | null,
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

  const routeWeb = ROUTE_PROVIDERS.has(providerType || "");
  const gwForwards =
    ((gateway.data as Record<string, unknown>)?.portForwards as
      | PortForward[]
      | undefined) || [];
  // On route providers the showroom 443/80 is served by an OpenShift Route, so
  // an EIP is only needed when a non-web forward requires one.
  const needEip = !routeWeb || gwForwards.some((pf) => !isWebForward(pf));
  const ips = needEip
    ? ensureShowroomExternalIps(externalIps)
    : stripShowroomAutoExternalIps(externalIps, []);
  const withPf = ensureShowroomGatewayPortForwardsOnNodes(
    nodes,
    ips,
    vniMap,
    providerType,
  );
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
