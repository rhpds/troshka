import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
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

describe("NodeContextMenu - pause/hibernate/resume", () => {
  beforeEach(() => {
    useCanvasStore.setState({
      nodes: [],
      edges: [],
      clusters: [],
      projectState: "active",
      deployedVmIds: new Set(["vm-1"]),
      currentProjectId: "proj-1",
      supportsHibernate: true,
      powerWarnDismissed: true,
    } as never);
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve({ success: true }) })));
  });
  afterEach(() => vi.unstubAllGlobals());

  it("shows Pause and Hibernate for a running VM", () => {
    useCanvasStore.setState({
      nodes: [
        { id: "vm-1", type: "vmNode", position: { x: 0, y: 0 }, data: { name: "web1", status: "running" } },
      ],
    } as never);
    render(<NodeContextMenu nodeId="vm-1" x={0} y={0} onClose={() => {}} />);
    expect(screen.getByText(/pause/i)).toBeInTheDocument();
    expect(screen.getByText(/hibernate/i)).toBeInTheDocument();
  });

  it("disables Hibernate when supportsHibernate is false", () => {
    useCanvasStore.setState({
      nodes: [
        { id: "vm-1", type: "vmNode", position: { x: 0, y: 0 }, data: { name: "web1", status: "running" } },
      ],
      supportsHibernate: false,
    } as never);
    render(<NodeContextMenu nodeId="vm-1" x={0} y={0} onClose={() => {}} />);
    expect(screen.getByText(/hibernate/i).closest("button")).toBeDisabled();
  });

  it("calls pause when Pause is clicked (warning pre-dismissed)", async () => {
    useCanvasStore.setState({
      nodes: [
        { id: "vm-1", type: "vmNode", position: { x: 0, y: 0 }, data: { name: "web1", status: "running" } },
      ],
    } as never);
    const onClose = vi.fn();
    render(<NodeContextMenu nodeId="vm-1" x={0} y={0} onClose={onClose} />);
    fireEvent.click(screen.getByText(/pause/i));
    await waitFor(() =>
      expect(fetch).toHaveBeenCalledWith("/api/v1/projects/proj-1/vms/vm-1/pause", { method: "POST" }),
    );
  });

  it("shows Resume (not Start) for a paused VM and calls unpause", async () => {
    useCanvasStore.setState({
      nodes: [
        { id: "vm-1", type: "vmNode", position: { x: 0, y: 0 }, data: { name: "web1", status: "paused" } },
      ],
    } as never);
    render(<NodeContextMenu nodeId="vm-1" x={0} y={0} onClose={() => {}} />);
    expect(screen.getByText(/resume/i)).toBeInTheDocument();
    expect(screen.queryByText(/^▶ Start$/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByText(/resume/i));
    await waitFor(() =>
      expect(fetch).toHaveBeenCalledWith("/api/v1/projects/proj-1/vms/vm-1/unpause", { method: "POST" }),
    );
  });

  it("shows Start for a hibernated VM", () => {
    useCanvasStore.setState({
      nodes: [
        { id: "vm-1", type: "vmNode", position: { x: 0, y: 0 }, data: { name: "web1", status: "hibernated" } },
      ],
    } as never);
    render(<NodeContextMenu nodeId="vm-1" x={0} y={0} onClose={() => {}} />);
    expect(screen.getByText(/start/i)).toBeInTheDocument();
  });
});
