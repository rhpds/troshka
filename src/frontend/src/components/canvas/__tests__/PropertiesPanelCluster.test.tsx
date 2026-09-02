import { describe, it, expect, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useCanvasStore } from "@/stores/canvasStore";
import PropertiesPanel from "@/components/canvas/PropertiesPanel";

beforeEach(() => {
  useCanvasStore.setState({
    nodes: [
      {
        id: "cluster-prod",
        type: "clusterNode",
        position: { x: 0, y: 0 },
        data: {
          name: "prod",
          clusterId: "prod",
          type: "standard",
          controlPlane: 3,
          workers: 2,
          baseDomain: "ocp.local",
          apiVip: "",
          ingressVip: "",
        },
      },
    ],
    edges: [],
    selectedNodeId: "cluster-prod",
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
});

describe("PropertiesPanel cluster editor", () => {
  it("edits worker count into the cluster", async () => {
    render(<PropertiesPanel />);
    const workers = screen.getByLabelText(/^workers$/i);
    await userEvent.clear(workers);
    await userEvent.type(workers, "4");
    (workers as HTMLInputElement).blur();
    expect(useCanvasStore.getState().clusters[0].workers).toBe(4);
  });

  it("changing type to sno sets controlPlane to 1 (read-only, derived)", async () => {
    render(<PropertiesPanel />);
    const typeSelect = screen.getByLabelText(/cluster type/i);
    await userEvent.selectOptions(typeSelect, "sno");
    expect(useCanvasStore.getState().clusters[0].type).toBe("sno");
    expect(useCanvasStore.getState().clusters[0].controlPlane).toBe(1);
  });

  it("mirrors summary fields onto the cluster node data", async () => {
    render(<PropertiesPanel />);
    const workers = screen.getByLabelText(/^workers$/i);
    await userEvent.clear(workers);
    await userEvent.type(workers, "5");
    const node = useCanvasStore.getState().nodes.find((n) => n.id === "cluster-prod")!;
    expect((node.data as Record<string, unknown>).workers).toBe(5);
  });

  it("materializes worker member VMs when the count grows", async () => {
    render(<PropertiesPanel />);
    const workers = screen.getByLabelText(/^workers$/i);
    await userEvent.clear(workers);
    await userEvent.type(workers, "3");
    const members = useCanvasStore
      .getState()
      .nodes.filter(
        (n) =>
          n.type === "vmNode" &&
          (n.data as Record<string, unknown>).clusterId === "prod" &&
          (n.data as Record<string, unknown>).clusterRole === "worker",
      );
    expect(members).toHaveLength(3);
  });

  it("flags a VIP that collides with another cluster", async () => {
    useCanvasStore.setState({
      clusters: [
        ...useCanvasStore.getState().clusters,
        {
          id: "dev",
          nodeId: "cluster-dev",
          name: "dev",
          type: "standard",
          controlPlane: 3,
          workers: 0,
          apiVip: "10.0.0.5",
        },
      ],
    } as never);
    render(<PropertiesPanel />);
    const apiVip = screen.getByLabelText(/api vip/i);
    await userEvent.type(apiVip, "10.0.0.5");
    // typing is never blocked
    expect(useCanvasStore.getState().clusters[0].apiVip).toBe("10.0.0.5");
    // and an inline collision error is shown
    expect(screen.getByText(/already used by cluster/i)).toBeInTheDocument();
  });
});
