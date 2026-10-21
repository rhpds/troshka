/** Shared storage/disk edge styling (VM, pod, and disk nodes). */
import type { Edge, Node } from "@xyflow/react";
import type { CSSProperties } from "react";

export const STORAGE_EDGE_STYLE = {
  stroke: "rgba(251, 191, 36, 0.6)",
  strokeWidth: 2,
  strokeDasharray: "4 4",
};

export const STORAGE_EDGE_CLASS = "edge-storage-pulse";

function edgeDashCycle(edge: Edge): number {
  const parts = String(edge.style?.strokeDasharray ?? "5 5").trim().split(/[\s,]+/).map(Number);
  const cycle = parts.reduce((sum, value) => sum + value, 0) * (parts.length % 2 ? 2 : 1);
  return Number.isFinite(cycle) && cycle > 0 ? cycle : 10;
}

/** Display-only animation for saved and newly created infrastructure links. */
export function withStorageFlowAnimation(edge: Edge, nodes: Node[]): Edge {
  const source = nodes.find((node) => node.id === edge.source);
  const target = nodes.find((node) => node.id === edge.target);
  if (!source || !target) return edge;
  const pair = [source, target];
  const showroomStorage = pair.some((node) => node.type === "storageNode") &&
    pair.some((node) => node.type === "containerNode" &&
      (node.data.isShowroom || node.data.name === "showroom"));
  const cephCluster = pair.some((node) => node.type === "cephClusterNode") &&
    pair.some((node) => node.type === "clusterNode");
  const showroomGateway = pair.some((node) => node.type === "containerNode" &&
    (node.data.isShowroom || node.data.name === "showroom")) &&
    pair.some((node) => node.data.subtype === "gateway");
  if (!showroomStorage && !cephCluster && !showroomGateway) return edge;
  const classes = new Set((edge.className || "").split(/\s+/).filter(Boolean));
  classes.add("edge-storage-flow");
  const style = { ...edge.style, "--troshka-edge-dash-offset": `${-edgeDashCycle(edge)}px` } as CSSProperties;
  return { ...edge, animated: true, className: [...classes].join(" "), style };
}

export const STORAGE_SOURCE_HANDLE = "right";

export function isStorageSourceHandle(handle: string | null | undefined): boolean {
  return handle === STORAGE_SOURCE_HANDLE;
}

export function isVmDiskLeftHandle(handle: string | null | undefined): boolean {
  return !!handle && handle.startsWith("dp-") && handle.endsWith("-left");
}

export function isContainerMountLeftHandle(handle: string | null | undefined): boolean {
  return !!handle && handle.startsWith("mnt-") && handle.endsWith("-left");
}

export function isPodContainer(node: Node | undefined): boolean {
  if (node?.type !== "containerNode") return false;
  const d = node.data as Record<string, unknown>;
  return !!d.isPod && !d.isShowroom && d.name !== "showroom";
}

/** VM disk edges must use dp-*-left; rewrite legacy right-side handles on load. */
export function normalizeVmDiskEdges(nodes: Node[], edges: Edge[]): Edge[] {
  let changed = false;
  const next = edges.map((edge) => {
    const source = nodes.find((n) => n.id === edge.source);
    const target = nodes.find((n) => n.id === edge.target);
    if (!source || !target) return edge;

    if (
      source.type === "vmNode" &&
      target.type === "storageNode" &&
      edge.sourceHandle?.startsWith("dp-") &&
      edge.sourceHandle.endsWith("-right")
    ) {
      changed = true;
      return {
        ...edge,
        sourceHandle: edge.sourceHandle.replace(/-right$/, "-left"),
      };
    }
    if (
      target.type === "vmNode" &&
      source.type === "storageNode" &&
      edge.targetHandle?.startsWith("dp-") &&
      edge.targetHandle.endsWith("-right")
    ) {
      changed = true;
      return {
        ...edge,
        targetHandle: edge.targetHandle.replace(/-right$/, "-left"),
      };
    }
    return edge;
  });
  return changed ? next : edges;
}

/** Pod mount edges must use mnt-*-left; rewrite legacy right-side handles on load. */
export function normalizePodMountEdges(nodes: Node[], edges: Edge[]): Edge[] {
  let changed = false;
  const next = edges.map((edge) => {
    const source = nodes.find((n) => n.id === edge.source);
    const target = nodes.find((n) => n.id === edge.target);
    if (!source || !target) return edge;

    const targetPod = isPodContainer(target) ? target : undefined;
    const pod = isPodContainer(source) ? source : targetPod;
    if (!pod) return edge;

    if (source.id === pod.id) {
      const handle = edge.sourceHandle || "";
      if (handle.startsWith("mnt-") && handle.endsWith("-right")) {
        changed = true;
        return {
          ...edge,
          sourceHandle: handle.replace(/-right$/, "-left"),
        };
      }
    } else {
      const handle = edge.targetHandle || "";
      if (handle.startsWith("mnt-") && handle.endsWith("-right")) {
        changed = true;
        return {
          ...edge,
          targetHandle: handle.replace(/-right$/, "-left"),
        };
      }
    }
    return edge;
  });
  return changed ? next : edges;
}

/** Disk nodes only expose a right-side anchor; normalize legacy left-handle edges on load. */
export function normalizeStorageDiskEdges(nodes: Node[], edges: Edge[]): Edge[] {
  let changed = false;
  const next = edges.map((edge) => {
    const source = nodes.find((n) => n.id === edge.source);
    const target = nodes.find((n) => n.id === edge.target);
    if (!source || !target) return edge;

    if (source.type === "storageNode" && edge.sourceHandle !== STORAGE_SOURCE_HANDLE) {
      changed = true;
      return { ...edge, sourceHandle: STORAGE_SOURCE_HANDLE };
    }
    if (target.type === "storageNode" && edge.targetHandle !== STORAGE_SOURCE_HANDLE) {
      changed = true;
      return { ...edge, targetHandle: STORAGE_SOURCE_HANDLE };
    }
    return edge;
  });
  return changed ? next : edges;
}
