import type { Edge, Node } from "@xyflow/react";
import {
  buildWettyCommand,
  WETTY_IMAGE,
  type WettyAttrs,
} from "@/lib/wettyContainer";

export interface ShowroomTab {
  id: string;
  name: string;
  type: "terminal" | "proxy" | "external";
  vmId?: string;
  /** Template / export network name (e.g. mgmt). */
  network?: string;
  /** Canvas network node id. */
  networkId?: string;
  sshUser?: string;
  sshPass?: string;
  sshPort?: number;
  proxyPort?: number;
  proxyPath?: string;
  proxyTls?: boolean;
  url?: string;
}

export interface ResolvedShowroomTab {
  tab: ShowroomTab;
  wettyPath?: string;
  wettyPort?: number;
  wettyHost?: string;
  proxyTarget?: string;
  proxyPath?: string;
  proxyTls?: boolean;
  warning?: string;
}

const WETTY_BASE_PORT = 8001;

function slugify(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_|_$/g, "") || "vm";
}

function isPlainNetwork(node: Node | undefined): boolean {
  if (node?.type !== "networkNode") return false;
  const subtype = (node.data as Record<string, unknown>).subtype as string | undefined;
  return !subtype || subtype === "network" || subtype === "dhcp" || subtype === "dns";
}

function networkIdByName(name: string, nodes: Node[]): string | null {
  const match = nodes.find(
    (node) =>
      node.type === "networkNode" &&
      isPlainNetwork(node) &&
      ((node.data as Record<string, unknown>).name as string) === name,
  );
  return match?.id ?? null;
}

function networkNameById(networkId: string, nodes: Node[]): string {
  const node = nodes.find((n) => n.id === networkId);
  if (!node) return networkId;
  const d = node.data as Record<string, unknown>;
  return (d.name as string) || (d.label as string) || networkId;
}

function resolveTabNetworkId(tab: ShowroomTab, nodes: Node[]): string | null {
  if (tab.networkId) return tab.networkId;
  if (tab.network) return networkIdByName(tab.network, nodes);
  return null;
}

export function getVmNicIpOnNetwork(
  vmId: string,
  networkId: string,
  nodes: Node[],
  edges: Edge[],
): string {
  const vm = nodes.find((n) => n.id === vmId);
  if (!vm) return "";
  const nics = ((vm.data as Record<string, unknown>).nics || []) as Array<{
    id: string;
    ip?: string;
  }>;
  for (const nic of nics) {
    const handles = [`nic-${nic.id}-top`, `nic-${nic.id}-bottom`];
    for (const edge of edges) {
      const onVm =
        (edge.source === vmId && handles.includes(edge.sourceHandle || "")) ||
        (edge.target === vmId && handles.includes(edge.targetHandle || ""));
      if (!onVm) continue;
      const otherId = edge.source === vmId ? edge.target : edge.source;
      if (otherId === networkId && nic.ip) return nic.ip;
    }
  }
  return "";
}

export function resolveShowroomTabs(
  tabs: ShowroomTab[],
  nodes: Node[],
  edges: Edge[],
): ResolvedShowroomTab[] {
  const nodesById = new Map(nodes.map((n) => [n.id, n]));
  let wettyPort = WETTY_BASE_PORT;

  return tabs.map((tab) => {
    if (tab.type === "external") {
      return { tab };
    }

    const vm = tab.vmId ? nodesById.get(tab.vmId) : undefined;
    if (!vm || vm.type !== "vmNode") {
      return { tab, warning: "Select a VM for this tab" };
    }

    const vmName = ((vm.data as Record<string, unknown>).name as string) || "vm";
    if (!tab.network && !tab.networkId) {
      return { tab, warning: "Select a network for this tab" };
    }
    const networkId = resolveTabNetworkId(tab, nodes);
    if (!networkId) {
      return { tab, warning: `Unknown network '${tab.network || ""}'` };
    }
    const networkName = networkNameById(networkId, nodes);
    const vmIp = getVmNicIpOnNetwork(vm.id, networkId, nodes, edges);
    if (!vmIp) {
      return { tab, warning: `${vmName} has no IP on ${networkName}` };
    }

    if (tab.type === "terminal") {
      const wettyPath = `/wetty_${slugify(vmName)}`;
      const resolved: ResolvedShowroomTab = {
        tab,
        wettyPath,
        wettyPort: wettyPort++,
        wettyHost: vmIp,
      };
      return resolved;
    }

    const proxyPath = tab.proxyPath || `/${slugify(vmName)}/`;
    const port = tab.proxyPort || 80;
    const scheme = tab.proxyTls ? "https" : "http";
    return {
      tab,
      proxyPath: proxyPath.endsWith("/") ? proxyPath : `${proxyPath}/`,
      proxyTarget: `${scheme}://${vmIp}:${port}`,
      proxyTls: tab.proxyTls,
    };
  });
}

function yamlName(name: string): string {
  return JSON.stringify(name).slice(1, -1);
}

/** Browser-facing port for showroom tabs (gateway ext port → showroom IP). */
export function getShowroomExternalPort(nodes: Node[], showroomIp: string): number {
  for (const n of nodes) {
    const d = n.data as Record<string, unknown>;
    if (d.subtype !== "gateway") continue;
    for (const pf of (d.portForwards || []) as Array<{ extPort?: string; intIp?: string }>) {
      if (showroomIp && pf.intIp === showroomIp && pf.extPort) {
        const port = parseInt(String(pf.extPort), 10);
        if (port > 0) return port;
      }
    }
  }
  for (const n of nodes) {
    const d = n.data as Record<string, unknown>;
    if (d.subtype !== "gateway") continue;
    for (const pf of (d.portForwards || []) as Array<{ extPort?: string; intPort?: string }>) {
      if (String(pf.intPort) === "80" && pf.extPort) {
        const port = parseInt(String(pf.extPort), 10);
        if (port === 80 || port === 443) return port;
      }
    }
  }
  return 443;
}

/** Port on the showroom pod the gateway forwards to (nginx :80). */
export function getShowroomGatewayTargetPort(nodes: Node[], showroomIp: string): number {
  for (const n of nodes) {
    const d = n.data as Record<string, unknown>;
    if (d.subtype !== "gateway") continue;
    for (const pf of (d.portForwards || []) as Array<{
      intPort?: string;
      intIp?: string;
      extPort?: string;
    }>) {
      const intPort = parseInt(String(pf.intPort || ""), 10);
      if (intPort <= 0) continue;
      if (showroomIp && pf.intIp === showroomIp) return intPort;
      if (
        String(pf.intPort) === "80" &&
        pf.extPort &&
        (String(pf.extPort) === "80" || String(pf.extPort) === "443")
      ) {
        return intPort;
      }
    }
  }
  return 80;
}

/** Port(s) to show on pod/showroom cards (gateway target or host-published ports). */
export function getPodDisplayPorts(
  nodeData: Record<string, unknown>,
  podContainers: Array<{ ports?: Array<{ containerPort?: number; hostPort?: number | null }> }>,
  nodes: Node[],
  isShowroom: boolean,
): number[] {
  if (isShowroom) {
    const ip =
      ((nodeData.nics || []) as Array<{ ip?: string }>).find((n) => n.ip)?.ip || "";
    return [getShowroomGatewayTargetPort(nodes, ip)];
  }
  const hostPorts = podContainers
    .flatMap((c) => (c.ports || []).map((p) => p.hostPort))
    .filter((p): p is number => typeof p === "number" && p > 0);
  if (hostPorts.length > 0) return hostPorts;
  const direct = ((nodeData.ports || []) as Array<{ hostPort?: number | null }>)
    .map((p) => p.hostPort)
    .filter((p): p is number => typeof p === "number" && p > 0);
  return direct;
}

export function buildUiConfigYaml(
  resolved: ResolvedShowroomTab[],
  externalPort = 443,
): string {
  const lines = [
    "---",
    "type: showroom",
    "",
    "default_width: 30",
    "persist_url_state: true",
    "",
    "view_switcher:",
    "  enabled: true",
    "  default_mode: split",
    "",
    "antora:",
    "  name: modules",
    "  dir: www",
    "",
    "tabs:",
  ];

  for (const item of resolved) {
    if (item.tab.type === "external") {
      lines.push(`  - name: ${yamlName(item.tab.name)}`);
      lines.push(`    url: ${item.tab.url || ""}`);
      lines.push("    external: true");
      continue;
    }
    if (item.tab.type === "terminal" && item.wettyPath) {
      lines.push(`  - name: ${yamlName(item.tab.name)}`);
      lines.push(`    path: ${item.wettyPath}`);
      lines.push(`    port: ${externalPort}`);
      continue;
    }
    if (item.tab.type === "proxy" && item.proxyPath) {
      lines.push(`  - name: ${yamlName(item.tab.name)}`);
      lines.push(`    url: '${item.proxyPath}'`);
    }
  }

  return `${lines.join("\n")}\n`;
}

export function buildNginxConfig(resolved: ResolvedShowroomTab[]): string {
  const blocks: string[] = [
    "user root;",
    "events {}",
    "http {",
    "  include /etc/nginx/mime.types;",
    "  proxy_cache off;",
    "  map $http_upgrade $connection_upgrade {",
    "    default upgrade;",
    "    '' close;",
    "  }",
    "  server {",
    "    listen 80;",
    "    location / {",
    "      proxy_pass http://127.0.0.1:8000;",
    "      proxy_set_header Host $host;",
    "      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
    "      proxy_set_header X-Forwarded-Proto $scheme;",
    "    }",
  ];

  for (const item of resolved) {
    if (item.tab.type === "terminal" && item.wettyPath && item.wettyPort) {
      const path = item.wettyPath.replace(/\/$/, "");
      blocks.push(
        `    location ^~ ${path} {`,
        `      proxy_pass http://127.0.0.1:${item.wettyPort}${path};`,
        "      proxy_http_version 1.1;",
        "      proxy_set_header Upgrade $http_upgrade;",
        "      proxy_set_header Connection $connection_upgrade;",
        "      proxy_set_header Host $host;",
        "      proxy_read_timeout 43200000;",
        "    }",
      );
    }
    if (item.tab.type === "proxy" && item.proxyPath && item.proxyTarget) {
      const loc = item.proxyPath.endsWith("/") ? item.proxyPath : `${item.proxyPath}/`;
      blocks.push(
        `    location ${loc} {`,
        `      proxy_pass ${item.proxyTarget};`,
        "      proxy_http_version 1.1;",
        "      proxy_set_header Upgrade $http_upgrade;",
        "      proxy_set_header Connection $connection_upgrade;",
        "      proxy_set_header Host $host;",
        "      proxy_read_timeout 86400;",
      );
      if (item.proxyTls) {
        blocks.push("      proxy_ssl_verify off;");
      }
      blocks.push("    }");
    }
  }

  blocks.push("  }", "}");
  return `${blocks.join("\n")}\n`;
}

type PodContainer = {
  name: string;
  image: string;
  cpus: number;
  memory: number;
  envVars: Array<{ key: string; value: string }>;
  ports: Array<{ containerPort: number; hostPort: number | null; protocol: string }>;
  command: string | string[] | null;
  mounts: Array<{ diskNodeId: string; mountPath: string }>;
};

export function buildWettyContainers(
  resolved: ResolvedShowroomTab[],
  tabs: ShowroomTab[],
  nodes: Node[],
  diskId: string,
): PodContainer[] {
  const nodesById = new Map(nodes.map((n) => [n.id, n]));
  const containers: PodContainer[] = [];

  for (const item of resolved) {
    if (item.tab.type !== "terminal" || !item.wettyPort || !item.wettyHost) continue;
    const tab = tabs.find((t) => t.id === item.tab.id);
    const vm = tab?.vmId ? nodesById.get(tab.vmId) : undefined;
    const vmData = (vm?.data || {}) as Record<string, unknown>;
    const vmName = (vmData.name as string) || "vm";
    const sshUser =
      tab?.sshUser ||
      (vmData.ciLoginUser as string) ||
      (vmData.os === "rhel10" || vmData.os === "rhel" ? "cloud-user" : "cloud-user");
    const sshPass = tab?.sshPass || (vmData.ciCloudUserPassword as string) || "";

    const basePath = item.wettyPath?.replace(/^\//, "") || `wetty_${slugify(vmName)}`;
    const sshPort = tab?.sshPort ?? 22;
    const wettyAttrs: WettyAttrs = {
      basePath,
      port: item.wettyPort,
      sshHost: item.wettyHost,
      sshPort,
      sshUser,
      sshPass,
    };

    containers.push({
      name: `wetty-${slugify(vmName)}`,
      image: WETTY_IMAGE,
      cpus: 1,
      memory: 512,
      envVars: [],
      ports: [{ containerPort: item.wettyPort, hostPort: null, protocol: "tcp" }],
      command: buildWettyCommand(wettyAttrs),
      mounts: [],
    });
  }

  return containers;
}

export function applyShowroomTabsToNode(
  nodeData: Record<string, unknown>,
  tabs: ShowroomTab[],
  nodes: Node[],
  edges: Edge[],
): Record<string, unknown> {
  const diskId = ((nodeData.mounts || []) as Array<{ diskNodeId: string }>)[0]?.diskNodeId;
  if (!diskId) return { ...nodeData, showroomTabs: tabs };

  const resolved = resolveShowroomTabs(tabs, nodes, edges);
  const showroomIp =
    ((nodeData.nics || []) as Array<{ ip?: string }>).find((n) => n.ip)?.ip || "";
  const externalPort = getShowroomExternalPort(nodes, showroomIp);
  const nginxConf = buildNginxConfig(resolved);
  const uiConfig = buildUiConfigYaml(resolved, externalPort);
  const nginxB64 = btoa(nginxConf);
  const uiConfigB64 = btoa(uiConfig);

  const initContainers = ((nodeData.initContainers || []) as Array<Record<string, unknown>>).map(
    (ic) => {
      if (ic.name !== "nginx-config") return ic;
      return {
        ...ic,
        envVars: [
          { key: "NGINX_B64", value: nginxB64 },
          { key: "UI_CONFIG_B64", value: uiConfigB64 },
        ],
        command:
          'mkdir -p /showroom/nginx /showroom/repo && echo "$NGINX_B64" | base64 -d > /showroom/nginx/nginx.conf && echo "$UI_CONFIG_B64" | base64 -d > /showroom/repo/ui-config.yml',
      };
    },
  );

  const basePodContainers = ((nodeData.podContainers || []) as PodContainer[]).filter(
    (pc) => !pc.name.startsWith("wetty-"),
  );
  const wettyContainers = buildWettyContainers(resolved, tabs, nodes, diskId);

  return {
    ...nodeData,
    showroomTabs: tabs,
    initContainers,
    podContainers: [...basePodContainers, ...wettyContainers],
  };
}

export function newShowroomTab(
  type: ShowroomTab["type"],
  name: string,
): ShowroomTab {
  return {
    id: crypto.randomUUID(),
    name,
    type,
    proxyPort: type === "proxy" ? 80 : undefined,
    proxyPath: type === "proxy" ? "/" : undefined,
    proxyTls: false,
    url: type === "external" ? "https://example.com" : undefined,
  };
}
