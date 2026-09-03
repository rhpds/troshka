import { describe, it, expect, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
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

describe("PropertiesPanel cluster role (read-only)", () => {
  // The role is managed by the cluster (its type + worker count), not editable
  // per-VM, so it renders as a read-only "(managed by cluster)" label.
  it("shows the read-only role for a VM inside a cluster", () => {
    render(<PropertiesPanel />);
    expect(screen.getByText(/cluster role/i)).toBeInTheDocument();
    expect(screen.getByText(/managed by cluster/i)).toBeInTheDocument();
    expect(screen.getByText(/control plane/i)).toBeInTheDocument();
  });

  it("shows Worker for a worker member", () => {
    seedStore({ name: "vm-1", os: "rhcos", clusterId: "prod", clusterRole: "worker" });
    render(<PropertiesPanel />);
    expect(screen.getByText(/worker/i)).toBeInTheDocument();
    expect(screen.getByText(/managed by cluster/i)).toBeInTheDocument();
  });

  it("does not show the cluster role for a VM not in a cluster", () => {
    seedStore({ name: "vm-1", os: "rhcos" });
    render(<PropertiesPanel />);
    expect(screen.queryByText(/managed by cluster/i)).not.toBeInTheDocument();
  });
});
