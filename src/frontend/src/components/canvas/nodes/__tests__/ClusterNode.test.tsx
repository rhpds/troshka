import { describe, it, expect, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { ReactFlowProvider } from "@xyflow/react";
import { useCanvasStore, type ClusterConfig } from "@/stores/canvasStore";
import ClusterNode, {
  clusterIsRecertPath,
  formatClusterOcpStatusLabel,
} from "../ClusterNode";

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

describe("formatClusterOcpStatusLabel", () => {
  it("maps each install status to a clear label", () => {
    expect(formatClusterOcpStatusLabel("ready")).toBe("Ready");
    expect(formatClusterOcpStatusLabel("error")).toBe("Error");
    expect(formatClusterOcpStatusLabel("monitoring")).toBe("Deploying");
    expect(formatClusterOcpStatusLabel("monitoring", { recert: true })).toBe("Re-Cert");
    expect(formatClusterOcpStatusLabel(null)).toBe("Status");
    expect(formatClusterOcpStatusLabel(null, { recert: true })).toBe("Status");
  });
});

describe("clusterIsRecertPath", () => {
  it("detects pattern-backed disks and stored kubeconfigs", () => {
    expect(
      clusterIsRecertPath(
        "ocp",
        [
          {
            id: "vm1",
            type: "vmNode",
            position: { x: 0, y: 0 },
            data: { clusterId: "ocp", ocpKubeconfig: "kc" },
          },
        ] as never,
        [],
      ),
    ).toBe(true);
    expect(
      clusterIsRecertPath(
        "ocp",
        [
          {
            id: "vm1",
            type: "vmNode",
            position: { x: 0, y: 0 },
            data: { clusterId: "ocp" },
          },
          {
            id: "disk1",
            type: "storageNode",
            position: { x: 0, y: 0 },
            data: { source: "pattern", patternId: "p1" },
          },
        ] as never,
        [{ id: "e1", source: "disk1", target: "vm1" }] as never,
      ),
    ).toBe(true);
    expect(
      clusterIsRecertPath(
        "ocp",
        [
          {
            id: "vm1",
            type: "vmNode",
            position: { x: 0, y: 0 },
            data: { clusterId: "ocp" },
          },
        ] as never,
        [],
      ),
    ).toBe(false);
  });
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

  it("uses per-cluster install status for the status button label and color", () => {
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
      {
        id: "busy",
        nodeId: "cluster-busy",
        name: "busy",
        type: "sno",
        controlPlane: 1,
        workers: 0,
        ocpInstallStatus: "monitoring",
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
    expect(goodBtn).toHaveTextContent("Ready");
    expect(goodBtn.style.border).toContain("34, 197, 94");

    const { container: badBox } = renderNode(
      { name: "bad", type: "sno", controlPlane: 1, workers: 0 },
      "cluster-bad",
    );
    const badBtn = badBox.querySelector("button.nodrag") as HTMLButtonElement;
    expect(badBtn).toHaveTextContent("Error");
    expect(badBtn.style.border).toContain("239, 68, 68");

    const { container: busyBox } = renderNode(
      { name: "busy", type: "sno", controlPlane: 1, workers: 0 },
      "cluster-busy",
    );
    const busyBtn = busyBox.querySelector("button.nodrag") as HTMLButtonElement;
    expect(busyBtn).toHaveTextContent("Deploying");
  });

  it("labels monitoring pattern clusters as Re-Cert", () => {
    useCanvasStore.setState({
      clusters: [
        {
          id: "ocp",
          nodeId: "cluster-ocp",
          name: "ocp",
          type: "sno",
          controlPlane: 1,
          workers: 0,
          ocpInstallStatus: "monitoring",
        },
      ],
      nodes: [
        {
          id: "vm1",
          type: "vmNode",
          position: { x: 0, y: 0 },
          data: { clusterId: "ocp", name: "cp-0", ocpKubeconfig: "kc" },
        },
      ],
      projectState: "active",
    } as never);
    const { container } = renderNode(
      { name: "ocp", type: "sno", controlPlane: 1, workers: 0 },
      "cluster-ocp",
    );
    expect(container.querySelector("button.nodrag")).toHaveTextContent("Re-Cert");
  });
});
