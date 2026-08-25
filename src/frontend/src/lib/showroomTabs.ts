import type { Edge, Node } from "@xyflow/react";

export interface ShowroomTab {
  id: string;
  name: string;
  type: "terminal" | "proxy" | "external";
  vmId?: string;
  sshUser?: string;
  sshPass?: string;
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

const WETTY_IMAGE = "quay.io/rhpds/wetty:v2.5";
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

export function getShowroomNetworkId(
  showroomId: string,
  edges: Edge[],
  nodesById: Map<string, Node>,
): string | null {
  for (const edge of edges) {
    if (edge.source !== showroomId && edge.target !== showroomId) continue;
    const showroomIsSource = edge.source === showroomId;
    const handle = showroomIsSource ? edge.sourceHandle || "" : edge.targetHandle || "";
    if (!handle.includes("nic-")) continue;
    const otherId = showroomIsSource ? edge.target : edge.source;
    const other = nodesById.get(otherId);
    if (isPlainNetwork(other)) return otherId;
  }
  return null;
}

function nicNetworkForVm(
  vmId: string,
  edges: Edge[],
  nodesById: Map<string, Node>,
): string | null {
  for (const edge of edges) {
    if (edge.source !== vmId && edge.target !== vmId) continue;
    const vmIsSource = edge.source === vmId;
    const handle = vmIsSource ? edge.sourceHandle || "" : edge.targetHandle || "";
    if (!handle.includes("nic-")) continue;
    const otherId = vmIsSource ? edge.target : edge.source;
    const other = nodesById.get(otherId);
    if (isPlainNetwork(other)) return otherId;
  }
  return null;
}

export function getVmIpOnNetwork(
  vmId: string,
  networkId: string,
  nodes: Node[],
): string {
  const vm = nodes.find((n) => n.id === vmId);
  if (!vm) return "";
  const nics = ((vm.data as Record<string, unknown>).nics || []) as Array<{ ip?: string }>;
  return nics.find((n) => n.ip)?.ip || "";
}

export function resolveShowroomTabs(
  tabs: ShowroomTab[],
  showroomId: string,
  nodes: Node[],
  edges: Edge[],
): ResolvedShowroomTab[] {
  const nodesById = new Map(nodes.map((n) => [n.id, n]));
  const showroomNetworkId = getShowroomNetworkId(showroomId, edges, nodesById);
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
    let vmIp = "";
    if (showroomNetworkId) {
      const vmNet = nicNetworkForVm(vm.id, edges, nodesById);
      if (vmNet === showroomNetworkId) {
        vmIp = getVmIpOnNetwork(vm.id, showroomNetworkId, nodes);
      } else if (vmNet) {
        vmIp = getVmIpOnNetwork(vm.id, vmNet, nodes);
      }
    }
    if (!vmIp) {
      vmIp = getVmIpOnNetwork(vm.id, "", nodes);
    }
    if (!vmIp) {
      return { tab, warning: `${vmName} has no IP on the showroom network` };
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
      if (pf.intIp === showroomIp && pf.extPort) {
        const port = parseInt(String(pf.extPort), 10);
        if (port > 0) return port;
      }
    }
  }
  return 443;
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
    const cmd = [
      `--base=/${basePath}/`,
      `--port=${item.wettyPort}`,
      `--ssh-host=${item.wettyHost}`,
      "--ssh-port=22",
      `--ssh-user=${sshUser}`,
      "--ssh-auth=password",
      `--ssh-pass=${sshPass}`,
    ];

    containers.push({
      name: `wetty-${slugify(vmName)}`,
      image: WETTY_IMAGE,
      cpus: 1,
      memory: 512,
      envVars: [],
      ports: [{ containerPort: item.wettyPort, hostPort: null, protocol: "tcp" }],
      command: cmd,
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
  showroomId: string,
): Record<string, unknown> {
  const diskId = ((nodeData.mounts || []) as Array<{ diskNodeId: string }>)[0]?.diskNodeId;
  if (!diskId) return { ...nodeData, showroomTabs: tabs };

  const resolved = resolveShowroomTabs(tabs, showroomId, nodes, edges);
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
