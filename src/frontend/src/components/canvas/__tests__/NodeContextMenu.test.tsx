import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import NodeContextMenu from "@/components/canvas/NodeContextMenu";
import { useCanvasStore } from "@/stores/canvasStore";

describe("NodeContextMenu - Run Workload menu item", () => {
  beforeEach(() => {
    useCanvasStore.setState({
      nodes: [],
      edges: [],
      clusters: [],
      projectState: "active",
      deployedVmIds: new Set(),
    } as never);
  });
  afterEach(() => vi.unstubAllGlobals());

  it("calls onRunWorkload with cluster target for clusterNode", () => {
    useCanvasStore.setState({
      nodes: [
        {
          id: "cluster-node-1",
          type: "clusterNode",
          position: { x: 0, y: 0 },
          data: { name: "prod-cluster" },
        },
      ],
      clusters: [
        {
          id: "c1",
          name: "prod",
          nodeId: "cluster-node-1",
          type: "ocp",
          controlPlane: 1,
          workers: 0,
        },
      ],
      projectState: "active",
      deployedVmIds: new Set(),
    } as never);

    const onRunWorkload = vi.fn();
    render(
      <NodeContextMenu
        nodeId="cluster-node-1"
        x={100}
        y={100}
        onClose={() => {}}
        onRunWorkload={onRunWorkload}
      />,
    );

    fireEvent.click(screen.getByText(/run workload/i));
    expect(onRunWorkload).toHaveBeenCalledWith({
      mode: "cluster",
      clusterIds: ["c1"],
    });
  });

  it("calls onRunWorkload with vms target for standalone vmNode", () => {
    useCanvasStore.setState({
      nodes: [
        {
          id: "vm-1",
          type: "vmNode",
          position: { x: 0, y: 0 },
          data: { name: "web1" },
        },
      ],
      clusters: [],
      projectState: "active",
      deployedVmIds: new Set(),
    } as never);

    const onRunWorkload = vi.fn();
    render(
      <NodeContextMenu
        nodeId="vm-1"
        x={100}
        y={100}
        onClose={() => {}}
        onRunWorkload={onRunWorkload}
      />,
    );

    fireEvent.click(screen.getByText(/run workload/i));
    expect(onRunWorkload).toHaveBeenCalledWith({
      mode: "vms",
      vmNames: ["web1"],
    });
  });

  it("calls onRunWorkload with cluster target for cluster-member vmNode", () => {
    useCanvasStore.setState({
      nodes: [
        {
          id: "vm-member",
          type: "vmNode",
          position: { x: 0, y: 0 },
          data: { name: "master-0", clusterId: "c1" },
        },
      ],
      clusters: [
        {
          id: "c1",
          name: "ocp-prod",
          nodeId: "cluster-1",
          type: "ocp",
          controlPlane: 1,
          workers: 0,
        },
      ],
      projectState: "active",
      deployedVmIds: new Set(["vm-member"]),
    } as never);

    const onRunWorkload = vi.fn();
    render(
      <NodeContextMenu
        nodeId="vm-member"
        x={100}
        y={100}
        onClose={() => {}}
        onRunWorkload={onRunWorkload}
      />,
    );

    fireEvent.click(screen.getByText(/run workload/i));
    expect(onRunWorkload).toHaveBeenCalledWith({
      mode: "cluster",
      clusterIds: ["c1"],
    });
  });

  it("does not show Run Workload when projectState is not active", () => {
    useCanvasStore.setState({
      nodes: [
        {
          id: "vm-1",
          type: "vmNode",
          position: { x: 0, y: 0 },
          data: { name: "web1" },
        },
      ],
      clusters: [],
      projectState: "draft",
      deployedVmIds: new Set(),
    } as never);

    const onRunWorkload = vi.fn();
    render(
      <NodeContextMenu
        nodeId="vm-1"
        x={100}
        y={100}
        onClose={() => {}}
        onRunWorkload={onRunWorkload}
      />,
    );

    expect(screen.queryByText(/run workload/i)).not.toBeInTheDocument();
  });

  it("does not show Run Workload when onRunWorkload is not provided", () => {
    useCanvasStore.setState({
      nodes: [
        {
          id: "vm-1",
          type: "vmNode",
          position: { x: 0, y: 0 },
          data: { name: "web1" },
        },
      ],
      clusters: [],
      projectState: "active",
      deployedVmIds: new Set(),
    } as never);

    render(
      <NodeContextMenu
        nodeId="vm-1"
        x={100}
        y={100}
        onClose={() => {}}
      />,
    );

    expect(screen.queryByText(/run workload/i)).not.toBeInTheDocument();
  });
});
