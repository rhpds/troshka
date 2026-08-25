/** Shared storage/disk edge styling (VM, pod, and disk nodes). */
import type { Edge, Node } from "@xyflow/react";

export const STORAGE_EDGE_STYLE = {
  stroke: "rgba(251, 191, 36, 0.6)",
  strokeWidth: 2,
  strokeDasharray: "4 4",
};

export const STORAGE_EDGE_CLASS = "edge-storage-pulse";

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
  if (!node || node.type !== "containerNode") return false;
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

    const pod = isPodContainer(source) ? source : isPodContainer(target) ? target : undefined;
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
