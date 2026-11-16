import type { Edge, Node } from "@xyflow/react";

export interface VmNetworkIssue {
  vmId: string;
  nicId: string;
  nicName: string;
  message: string;
}

export function vmNetworkIssues(nodes: Node[], edges: Edge[]): VmNetworkIssue[] {
  const networks = new Set(nodes.filter((node) => node.type === "networkNode").map((node) => node.id));
  return nodes.filter((node) => node.type === "vmNode").flatMap((vm) => {
    const connected = new Set<string>();
    for (const edge of edges) {
      if (edge.source === vm.id && networks.has(edge.target)) connected.add(edge.sourceHandle || "");
      if (edge.target === vm.id && networks.has(edge.source)) connected.add(edge.targetHandle || "");
    }
    const nics = (vm.data.nics || []) as Array<{ id: string; name?: string }>;
    return nics.filter((nic) => !["", "-top", "-bottom"].some((suffix) => connected.has(`nic-${nic.id}${suffix}`)))
      .map((nic) => ({
        vmId: vm.id,
        nicId: nic.id,
        nicName: nic.name || nic.id,
        message: `VM ${vm.data.name || vm.id}: NIC ${nic.name || nic.id} (${nic.id}) is not connected to a network. Connect it to a network or remove the unused NIC.`,
      }));
  });
}
