import type { Edge, Node } from "@xyflow/react";

export interface ShowroomReadiness {
  hasNetwork: boolean;
  hasGatewayNetwork: boolean;
  hasPortForward: boolean;
  showroomIp: string;
  networkName: string;
  gatewayName: string;
  issues: string[];
}

function isGatewayNode(node: Node | undefined): boolean {
  return (
    node?.type === "networkNode" &&
    (node.data as Record<string, unknown>).subtype === "gateway"
  );
}

function isPlainNetwork(node: Node | undefined): boolean {
  if (node?.type !== "networkNode") return false;
  const subtype = (node.data as Record<string, unknown>).subtype as string | undefined;
  return !subtype || subtype === "network" || subtype === "dhcp" || subtype === "dns";
}

function nicNetworkId(
  showroomId: string,
  edges: Edge[],
  nodesById: Map<string, Node>,
): string | null {
  for (const edge of edges) {
    const showroomIsSource = edge.source === showroomId;
    const showroomIsTarget = edge.target === showroomId;
    if (!showroomIsSource && !showroomIsTarget) continue;

    const handle = showroomIsSource ? edge.sourceHandle || "" : edge.targetHandle || "";
    if (!handle.includes("nic-")) continue;

    const otherId = showroomIsSource ? edge.target : edge.source;
    const other = nodesById.get(otherId);
    if (isPlainNetwork(other)) return otherId;
  }
  return null;
}

function gatewayForNetwork(
  networkId: string,
  edges: Edge[],
  nodesById: Map<string, Node>,
): Node | null {
  for (const edge of edges) {
    if (edge.source !== networkId && edge.target !== networkId) continue;
    const otherId = edge.source === networkId ? edge.target : edge.source;
    const other = nodesById.get(otherId);
    if (!other || !isGatewayNode(other)) continue;
    return other;
  }
  return null;
}

export function getShowroomNode(nodes: Node[]): Node | undefined {
  return nodes.find(
    (n) =>
      n.type === "containerNode" &&
      ((n.data as Record<string, unknown>).isShowroom ||
        (n.data as Record<string, unknown>).name === "showroom"),
  );
}

export function getShowroomReadiness(nodes: Node[], edges: Edge[]): ShowroomReadiness {
  const showroom = getShowroomNode(nodes);
  const empty: ShowroomReadiness = {
    hasNetwork: false,
    hasGatewayNetwork: false,
    hasPortForward: false,
    showroomIp: "",
    networkName: "",
    gatewayName: "",
    issues: ["Add a showroom to the canvas"],
  };
  if (!showroom) return empty;

  const nodesById = new Map(nodes.map((n) => [n.id, n]));
  const data = showroom.data as Record<string, unknown>;
  const nics = (data.nics || []) as Array<{ ip?: string }>;
  const showroomIp = nics.find((n) => n.ip)?.ip || "";

  const networkId = nicNetworkId(showroom.id, edges, nodesById);
  const network = networkId ? nodesById.get(networkId) : undefined;
  const networkName = (network?.data as Record<string, unknown> | undefined)?.name as string || "";

  const gateway = networkId ? gatewayForNetwork(networkId, edges, nodesById) : null;
  const gatewayName = (gateway?.data as Record<string, unknown> | undefined)?.name as string || "";

  const portForwards =
    ((gateway?.data as Record<string, unknown> | undefined)?.portForwards as Array<{
      extPort?: string;
      intIp?: string;
      intPort?: string;
    }>) || [];

  const hasPortForward =
    !!showroomIp &&
    portForwards.some(
      (pf) =>
        pf.intIp?.trim() === showroomIp &&
        String(pf.intPort) === "80",
    );

  const issues: string[] = [];
  if (!networkId) issues.push("Connect showroom to a network");
  else if (!gateway) issues.push(`Connect network '${networkName || "network"}' to a gateway`);
  if (!showroomIp) issues.push("Assign an IP to the showroom NIC (connect to a network with DHCP or set manually)");
  else if (!hasPortForward) issues.push(`Add gateway port forward: 80 → ${showroomIp}:80`);

  return {
    hasNetwork: !!networkId,
    hasGatewayNetwork: !!gateway,
    hasPortForward,
    showroomIp,
    networkName,
    gatewayName,
    issues,
  };
}

export function isShowroomReady(nodes: Node[], edges: Edge[]): boolean {
  const readiness = getShowroomReadiness(nodes, edges);
  return readiness.issues.length === 0;
}

/** Add gateway port forward 80→showroom:80 when showroom has an IP on a gateway network. */
export function applyShowroomPortForward(nodes: Node[], edges: Edge[]): Node[] {
  const readiness = getShowroomReadiness(nodes, edges);
  if (!readiness.showroomIp || !readiness.hasGatewayNetwork) return nodes;

  const showroom = getShowroomNode(nodes);
  if (!showroom) return nodes;

  const nodesById = new Map(nodes.map((n) => [n.id, n]));
  const networkId = (() => {
    for (const edge of edges) {
      if (edge.source !== showroom.id && edge.target !== showroom.id) continue;
      const handle =
        edge.source === showroom.id ? edge.sourceHandle || "" : edge.targetHandle || "";
      if (!handle.includes("nic-")) continue;
      const otherId = edge.source === showroom.id ? edge.target : edge.source;
      const other = nodesById.get(otherId);
      if (isPlainNetwork(other)) return otherId;
    }
    return null;
  })();
  if (!networkId) return nodes;

  const gateway = gatewayForNetwork(networkId, edges, nodesById);
  if (!gateway) return nodes;

  const gwData = gateway.data as Record<string, unknown>;
  const portForwards = (gwData.portForwards || []) as Array<{
    extPort?: string;
    intIp?: string;
    intPort?: string;
    proto?: string;
    extIpId?: string;
  }>;

  if (
    portForwards.some(
      (pf) =>
        pf.intIp?.trim() === readiness.showroomIp &&
        String(pf.intPort) === "80",
    )
  ) {
    return nodes;
  }

  return nodes.map((n) => {
    if (n.id !== gateway.id) return n;
    return {
      ...n,
      data: {
        ...n.data,
        portForwards: [
          ...portForwards,
          {
            extPort: "80",
            intIp: readiness.showroomIp,
            intPort: "80",
            proto: "tcp",
            extIpId: "",
          },
        ],
      },
    };
  });
}
