import type { Connection, Edge, Node } from "@xyflow/react";
import { isShowroomContainer } from "@/lib/showroomValidation";
import { isPodContainer } from "@/lib/storageEdgeStyle";

export const LB_BACKEND_EDGE_STYLE = {
  stroke: "rgba(59, 130, 246, 0.5)",
  strokeWidth: 2,
  strokeDasharray: "6 4",
};

/** @deprecated Use LB_BACKEND_EDGE_STYLE */
export const LB_VM_EDGE_STYLE = LB_BACKEND_EDGE_STYLE;

export const LB_EDGE_HANDLE = "top";
export const LB_EDGE_HANDLES = new Set([LB_EDGE_HANDLE]);

export function isLoadBalancerNode(node: Node | undefined): boolean {
  if (!node || node.type !== "networkNode") return false;
  const d = node.data as Record<string, unknown>;
  return d.subtype === "loadbalancer" || d.networkType === "loadbalancer";
}

export function isLbEdgeHandle(handle: string | null | undefined): boolean {
  return !!handle && LB_EDGE_HANDLES.has(handle);
}

export function isLbWorkloadNicHandle(handle: string | null | undefined): boolean {
  return !!handle && handle.startsWith("nic-") && handle.endsWith("-bottom");
}

/** @deprecated Use isLbWorkloadNicHandle */
export const isLbVmNicHandle = isLbWorkloadNicHandle;

export function isLbBackendNode(node: Node | undefined): boolean {
  if (!node) return false;
  if (node.type === "vmNode") return true;
  return isPodContainer(node);
}

export function isValidLbBackendConnection(
  connection: Connection,
  sourceNode: Node,
  targetNode: Node,
): boolean {
  const sIsLb = isLoadBalancerNode(sourceNode);
  const tIsLb = isLoadBalancerNode(targetNode);
  if (!sIsLb && !tIsLb) return false;

  const lbNode = sIsLb ? sourceNode : targetNode;
  const other = sIsLb ? targetNode : sourceNode;
  if (!isLbBackendNode(other) || isShowroomContainer(other)) return false;

  const lbHandle = sIsLb ? connection.sourceHandle : connection.targetHandle;
  const workloadHandle = sIsLb ? connection.targetHandle : connection.sourceHandle;
  return isLbEdgeHandle(lbHandle) && isLbWorkloadNicHandle(workloadHandle);
}

/** @deprecated Use isValidLbBackendConnection */
export const isValidLbVmConnection = isValidLbBackendConnection;

/** Drop invalid LB edges; normalize handles for LB top ↔ workload NIC bottom. */
export function sanitizeLbEdges(nodes: Node[], edges: Edge[]): Edge[] {
  return edges
    .map((edge) => {
      const source = nodes.find((n) => n.id === edge.source);
      const target = nodes.find((n) => n.id === edge.target);
      if (!source || !target) return edge;

      const lb = isLoadBalancerNode(source)
        ? source
        : isLoadBalancerNode(target)
          ? target
          : undefined;
      if (!lb) return edge;

      const other = source.id === lb.id ? target : source;
      if (!isLbBackendNode(other) || isShowroomContainer(other)) return edge;

      const lbKey = source.id === lb.id ? "sourceHandle" : "targetHandle";
      const workloadKey = source.id === lb.id ? "targetHandle" : "sourceHandle";
      let next = edge;
      if (next[lbKey] !== LB_EDGE_HANDLE) {
        next = { ...next, [lbKey]: LB_EDGE_HANDLE };
      }
      const workloadHandle = next[workloadKey];
      if (workloadHandle?.endsWith("-top")) {
        next = {
          ...next,
          [workloadKey]: workloadHandle.replace(/-top$/, "-bottom"),
        };
      }
      return next;
    })
    .filter((edge) => {
      const source = nodes.find((n) => n.id === edge.source);
      const target = nodes.find((n) => n.id === edge.target);
      if (!source || !target) return true;

      const lb = isLoadBalancerNode(source)
        ? source
        : isLoadBalancerNode(target)
          ? target
          : undefined;
      if (!lb) return true;

      const other = source.id === lb.id ? target : source;
      if (!isLbBackendNode(other) || isShowroomContainer(other)) return false;

      const lbHandle = source.id === lb.id ? edge.sourceHandle : edge.targetHandle;
      const workloadHandle =
        source.id === lb.id ? edge.targetHandle : edge.sourceHandle;
      return lbHandle === LB_EDGE_HANDLE && isLbWorkloadNicHandle(workloadHandle);
    });
}
