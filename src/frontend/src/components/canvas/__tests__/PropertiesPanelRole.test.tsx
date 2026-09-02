import { describe, it, expect, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useCanvasStore } from "@/stores/canvasStore";
import PropertiesPanel from "@/components/canvas/PropertiesPanel";

function seedStore(nodeData: Record<string, unknown>) {
  useCanvasStore.setState({
    nodes: [
      {
        id: "vm-1",
        type: "vmNode",
        position: { x: 0, y: 0 },
        data: nodeData,
      },
    ],
    edges: [],
    selectedNodeId: "vm-1",
    projectState: "draft",
    clusters: [
      {
        id: "prod",
        nodeId: "cluster-prod",
        name: "prod",
        type: "standard",
        controlPlane: 3,
        workers: 2,
        baseDomain: "ocp.local",
      },
    ],
    // Not deployed — keep dirty comparison inert.
    deployedNodeData: {},
    deployedEdgeKey: "",
    deployedExternalIps: "[]",
    deployedClusters: "[]",
    externalIps: [],
  } as never);
}

beforeEach(() => {
  seedStore({
    name: "vm-1",
    os: "rhcos",
    clusterId: "prod",
    clusterRole: "control-plane",
  });
});

describe("PropertiesPanel cluster role dropdown", () => {
  it("shows the role dropdown for a VM inside a cluster", () => {
    render(<PropertiesPanel />);
    expect(screen.getByLabelText(/cluster role/i)).toBeInTheDocument();
  });

  it("selecting worker sets clusterRole and AnsibleGroup=workers", async () => {
    render(<PropertiesPanel />);
    const roleSelect = screen.getByLabelText(/cluster role/i);
    await userEvent.selectOptions(roleSelect, "worker");
    const node = useCanvasStore.getState().nodes.find((n) => n.id === "vm-1")!;
    const vmData = node.data as Record<string, unknown>;
    expect(vmData.clusterRole).toBe("worker");
    expect((vmData.tags as Record<string, unknown>).AnsibleGroup).toBe("workers");
  });

  it("preserves other existing tags when changing role", async () => {
    seedStore({
      name: "vm-1",
      os: "rhcos",
      clusterId: "prod",
      clusterRole: "worker",
      tags: { AnsibleGroup: "workers", environment: "prod" },
    });
    render(<PropertiesPanel />);
    const roleSelect = screen.getByLabelText(/cluster role/i);
    await userEvent.selectOptions(roleSelect, "control-plane");
    const node = useCanvasStore.getState().nodes.find((n) => n.id === "vm-1")!;
    const vmData = node.data as Record<string, unknown>;
    expect(vmData.clusterRole).toBe("control-plane");
    const tags = vmData.tags as Record<string, unknown>;
    expect(tags.AnsibleGroup).toBe("controllers");
    expect(tags.environment).toBe("prod");
  });

  it("hides the role dropdown for a VM not in a cluster", () => {
    seedStore({ name: "vm-1", os: "rhcos" });
    render(<PropertiesPanel />);
    expect(screen.queryByLabelText(/cluster role/i)).not.toBeInTheDocument();
  });
});
