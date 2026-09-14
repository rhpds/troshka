import { describe, it, expect, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { ReactFlowProvider } from "@xyflow/react";
import { useCanvasStore, type ClusterConfig } from "@/stores/canvasStore";
import ClusterNode from "../ClusterNode";

function renderNode(data: Record<string, unknown>, id = "cluster-prod") {
  return render(
    <ReactFlowProvider>
      {/* @ts-expect-error minimal NodeProps for test */}
      <ClusterNode id={id} selected={false} data={data} />
    </ReactFlowProvider>,
  );
}

beforeEach(() => {
  useCanvasStore.setState({
    nodes: [],
    edges: [],
    clusters: [],
    projectState: "draft",
    ocpHealth: null,
    clusterOcpPhases: {},
    openClusterLog: () => {},
  } as never);
});

describe("ClusterNode", () => {
  it("shows the cluster name and a type/count badge", () => {
    renderNode({ name: "prod", type: "standard", controlPlane: 3, workers: 2 });
    expect(screen.getByText(/prod/)).toBeInTheDocument();
    expect(screen.getByText(/standard/)).toBeInTheDocument();
    expect(screen.getByText(/3cp/)).toBeInTheDocument();
    expect(screen.getByText(/2\s*wrk/)).toBeInTheDocument();
  });

  it("shows the OCP version badge from clusters[] config", () => {
    useCanvasStore.setState({
      clusters: [{ id: "prod", name: "prod", type: "standard", controlPlane: 3, workers: 2, ocpVersion: "4.22" }],
    } as never);
    renderNode({ name: "prod", type: "standard", controlPlane: 3, workers: 2 });
    expect(screen.getByText("v4.22")).toBeInTheDocument();
  });

  it("formats 5.0 preview version as v5.0", () => {
    useCanvasStore.setState({
      clusters: [{ id: "prod", name: "prod", type: "sno", controlPlane: 1, workers: 0, ocpVersion: "5.0" }],
    } as never);
    renderNode({ name: "prod", type: "sno", controlPlane: 1, workers: 0 });
    expect(screen.getByText("v5.0")).toBeInTheDocument();
  });

  it("omits version badge when ocpVersion is unset", () => {
    renderNode({ name: "prod", type: "standard", controlPlane: 3, workers: 2 });
    expect(screen.queryByText(/^v\d/)).not.toBeInTheDocument();
  });

  it("uses per-cluster install status for the status button, not project-wide health", () => {
    const clusters: ClusterConfig[] = [
      {
        id: "good",
        nodeId: "cluster-good",
        name: "good",
        type: "sno",
        controlPlane: 1,
        workers: 0,
        ocpInstallStatus: "ready",
      },
      {
        id: "bad",
        nodeId: "cluster-bad",
        name: "bad",
        type: "sno",
        controlPlane: 1,
        workers: 0,
        ocpInstallStatus: "error",
      },
    ];
    useCanvasStore.setState({
      clusters,
      projectState: "active",
      ocpHealth: { phase: "error", detail: "install failed" },
    } as never);
    const { container: goodBox } = renderNode(
      { name: "good", type: "sno", controlPlane: 1, workers: 0 },
      "cluster-good",
    );
    const goodBtn = goodBox.querySelector("button.nodrag") as HTMLButtonElement;
    expect(goodBtn.style.border).toContain("34, 197, 94");

    const { container: badBox } = renderNode(
      { name: "bad", type: "sno", controlPlane: 1, workers: 0 },
      "cluster-bad",
    );
    const badBtn = badBox.querySelector("button.nodrag") as HTMLButtonElement;
    expect(badBtn.style.border).toContain("239, 68, 68");
  });
});
