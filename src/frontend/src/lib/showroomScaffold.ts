import type { Edge, Node } from "@xyflow/react";
import type { StartOrderEntry } from "@/stores/canvasStore";
import {
  generateDiskControllerId,
  generateNodeId,
} from "@/stores/canvasStore";
import type { ShowroomTab } from "@/lib/showroomTabs";
import { buildNginxConfig, buildUiConfigYaml } from "@/lib/showroomTabs";
import { isGatewayNode } from "@/lib/showroomValidation";
import { STORAGE_EDGE_STYLE } from "@/lib/storageEdgeStyle";

export type { ShowroomTab };

export interface ShowroomConfig {
  enabled: boolean;
  content_repo: string;
  content_ref: string;
  build_content: boolean;
  tabs?: ShowroomTab[];
}

export const DEFAULT_SHOWROOM_CONFIG: ShowroomConfig = {
  enabled: true,
  content_repo: "",
  content_ref: "main",
  build_content: true,
};

const LAYOUT_VM_W = 200;
const LAYOUT_GAP_X = 40;
const SHOWROOM_DISK_Y_OFFSET = 70;

export function defaultShowroomScaffoldPosition(nodes: Node[]): { x: number; y: number } {
  const gateway = nodes.find((n) => isGatewayNode(n));
  if (gateway) {
    return {
      x: gateway.position.x - LAYOUT_GAP_X - LAYOUT_VM_W,
      y: gateway.position.y,
    };
  }
  return { x: 40, y: 40 };
}

const NOOKBAG_BUNDLE =
  "https://github.com/rhpds/nookbag/releases/download/nookbag-v0.3.2/nookbag-v0.3.2.zip";

function mountRef(diskId: string, mountPath: string) {
  return { diskNodeId: diskId, mountPath };
}

function buildInitContainers(contentRepo: string, contentRef: string) {
  const nginxB64 = btoa(buildNginxConfig([]));
  const uiConfigB64 = btoa(buildUiConfigYaml([]));
  const showroomMount = (diskId: string) => [mountRef(diskId, "/showroom")];

  return [
    {
      name: "git-cloner",
      image: "quay.io/rhpds/git-cloner:v1.1.4",
      cpus: 1,
      memory: 256,
      envVars: [
        { key: "GIT_REPO_URL", value: contentRepo },
        { key: "GIT_REPO_REF", value: contentRef },
        { key: "CLONE_DIR", value: "/showroom/repo" },
      ],
      ports: [],
      command: null,
      mounts: [] as Array<{ diskNodeId: string; mountPath: string }>,
    },
    {
      name: "nginx-config",
      image: "docker.io/library/busybox:1.36",
      cpus: 1,
      memory: 64,
      envVars: [
        { key: "NGINX_B64", value: nginxB64 },
        { key: "UI_CONFIG_B64", value: uiConfigB64 },
      ],
      ports: [],
      command:
        'mkdir -p /showroom/nginx /showroom/repo && echo "$NGINX_B64" | base64 -d > /showroom/nginx/nginx.conf && echo "$UI_CONFIG_B64" | base64 -d > /showroom/repo/ui-config.yml',
      mounts: [] as Array<{ diskNodeId: string; mountPath: string }>,
    },
    {
      name: "antora-builder",
      image: "quay.io/rhpds/antora:v1.2.2",
      cpus: 1,
      memory: 512,
      envVars: [
        { key: "FILES_DIR", value: "/showroom/repo" },
        { key: "OUTPUT_DIR", value: "/showroom/www" },
        { key: "ANTORA_PLAYBOOK", value: "site.yml" },
        { key: "ZT_UI_ENABLED", value: "true" },
        { key: "ZT_BUNDLE", value: NOOKBAG_BUNDLE },
      ],
      ports: [],
      command: null,
      mounts: [] as Array<{ diskNodeId: string; mountPath: string }>,
    },
  ].map((ic) => ({
    ...ic,
    mounts: showroomMount("__DISK__"),
  }));
}

function buildPodContainers(diskId: string) {
  const showroomMount = [mountRef(diskId, "/showroom")];
  return [
    {
      name: "proxy",
      image: "quay.io/rhpds/nginx:1.25",
      cpus: 1,
      memory: 256,
      envVars: [],
      ports: [{ containerPort: 80, hostPort: null, protocol: "tcp" }],
      command: ["nginx", "-c", "/showroom/nginx/nginx.conf", "-g", "daemon off;"],
      mounts: showroomMount,
    },
    {
      name: "content",
      image: "quay.io/rhpds/showroom-content:v1.4.1",
      cpus: 1,
      memory: 256,
      envVars: [
        { key: "ANTORA_PLAYBOOK", value: "site.yml" },
        { key: "ZT_BUNDLE", value: NOOKBAG_BUNDLE },
        { key: "ZT_UI_ENABLED", value: "true" },
        { key: "GUID", value: "workshop" },
        { key: "DOMAIN", value: "lab.local" },
      ],
      ports: [{ containerPort: 8000, hostPort: null, protocol: "tcp" }],
      mounts: showroomMount,
    },
  ];
}

export interface ShowroomScaffoldResult {
  showroomNode: Node;
  diskNode: Node;
  diskEdge: Edge;
  showroom: ShowroomConfig;
  startOrderEntry: StartOrderEntry;
}

export function buildShowroomScaffold(
  position: { x: number; y: number },
  config: ShowroomConfig = DEFAULT_SHOWROOM_CONFIG,
): ShowroomScaffoldResult {
  const showroomId = generateNodeId();
  const diskId = generateNodeId();

  const initContainers = buildInitContainers(config.content_repo, config.content_ref).map((ic) => ({
    ...ic,
    mounts: ic.mounts.map((m) =>
      m.diskNodeId === "__DISK__" ? { ...m, diskNodeId: diskId } : m,
    ),
  }));

  const showroomNode: Node = {
    id: showroomId,
    type: "containerNode",
    position,
    data: {
      label: "showroom",
      name: "showroom",
      image: "",
      registryCredentialId: null,
      cpus: 1,
      memory: 512,
      status: "stopped",
      icon: "📖",
      isPod: true,
      isShowroom: true,
      buildContent: config.build_content,
      contentRepo: config.content_repo,
      contentRef: config.content_ref,
      showroomTabs: config.tabs || [],
      nics: [],
      infraNetworking: true,
      envVars: [],
      ports: [],
      command: null,
      restartPolicy: "always",
      privileged: false,
      mounts: [mountRef(diskId, "/showroom")],
      initContainers,
      podContainers: buildPodContainers(diskId),
    },
  };

  const diskNode: Node = {
    id: diskId,
    type: "storageNode",
    position: { x: position.x - 190, y: position.y + SHOWROOM_DISK_Y_OFFSET },
    data: {
      label: "showroom-vol0",
      name: "showroom-vol0",
      size: 5,
      format: "raw",
      icon: "🛢",
    },
  };

  const diskEdge: Edge = {
    id: generateDiskControllerId(),
    source: diskId,
    target: showroomId,
    sourceHandle: "right",
    targetHandle: `mnt-${diskId}-left`,
    type: "smoothstep",
    style: STORAGE_EDGE_STYLE,
  };

  const startOrderEntry: StartOrderEntry = {
    vmId: showroomId,
    containerId: showroomId,
    entryType: "container",
    autoStart: true,
    waitForVm: null,
    waitForService: "none",
    waitForPort: "",
    delaySeconds: 15,
  };

  return {
    showroomNode,
    diskNode,
    diskEdge,
    showroom: { ...config, enabled: true },
    startOrderEntry,
  };
}

export function syncShowroomContentEnv(
  nodeData: Record<string, unknown>,
  contentRepo: string,
  contentRef: string,
  buildContent: boolean,
): Record<string, unknown> {
  const initContainers = ((nodeData.initContainers || []) as Array<Record<string, unknown>>).map(
    (ic) => {
      if (ic.name !== "git-cloner") return ic;
      const envVars = ((ic.envVars || []) as Array<{ key: string; value: string }>).map((ev) => {
        if (ev.key === "GIT_REPO_URL") return { ...ev, value: contentRepo };
        if (ev.key === "GIT_REPO_REF") return { ...ev, value: contentRef };
        return ev;
      });
      return { ...ic, envVars };
    },
  );
  return {
    ...nodeData,
    contentRepo: contentRepo,
    contentRef: contentRef,
    buildContent: buildContent,
    initContainers,
  };
}

export function hasShowroomNode(nodes: Node[]): boolean {
  return nodes.some(
    (n) =>
      n.type === "containerNode" &&
      ((n.data as Record<string, unknown>).isShowroom ||
        (n.data as Record<string, unknown>).name === "showroom"),
  );
}
