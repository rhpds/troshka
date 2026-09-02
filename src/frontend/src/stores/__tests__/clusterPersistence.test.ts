import { describe, it, expect, beforeEach, vi } from "vitest";
import type { Node } from "@xyflow/react";
import {
  useCanvasStore,
  _saveTopologyToApi,
  stableStringify,
  stableNodeData,
  type ClusterConfig,
} from "@/stores/canvasStore";

// helper: reset store between tests
beforeEach(() => {
  useCanvasStore.setState({
    nodes: [],
    edges: [],
    clusters: [],
    deployedNodeData: {},
    deployedEdgeKey: "",
    deployedExternalIps: "[]",
    deployedClusters: "[]",
    externalIps: [],
    topologyDirty: false,
  });
});

const prodCluster: ClusterConfig = {
  id: "prod",
  name: "prod",
  nodeId: "cluster-prod",
  type: "standard",
  controlPlane: 3,
  workers: 2,
};

interface SavedTopologyBody {
  topology: {
    clusters: unknown[];
    nodes: Array<{ parentId?: string; data: { clusterId?: string } }>;
  };
}

describe("cluster persistence in store", () => {
  it("addCluster / updateCluster / removeCluster mutate state", () => {
    const s = useCanvasStore.getState();
    s.addCluster({ ...prodCluster });
    expect(useCanvasStore.getState().clusters).toHaveLength(1);
    useCanvasStore.getState().updateCluster("prod", { workers: 3 });
    expect(useCanvasStore.getState().clusters[0].workers).toBe(3);
    useCanvasStore.getState().removeCluster("prod");
    expect(useCanvasStore.getState().clusters).toHaveLength(0);
  });

  it("updateCluster sizing edit sets topologyDirty vs deployed baseline", () => {
    const clusterNode: Node = {
      id: "cluster-prod",
      type: "clusterNode",
      position: { x: 0, y: 0 },
      data: { name: "prod", type: "standard", controlPlane: 3, workers: 2 },
    };
    useCanvasStore.setState({
      nodes: [clusterNode],
      edges: [],
      clusters: [{ ...prodCluster, workerCpu: 4 }],
      // Deployed baseline matches current node data + clusters, so only a
      // clusters[]-only edit can flip the dirty flag.
      deployedNodeData: {
        "cluster-prod": stableStringify(
          stableNodeData(clusterNode.data as Record<string, unknown>),
        ),
      },
      deployedEdgeKey: "",
      deployedExternalIps: "[]",
      deployedClusters: stableStringify([{ ...prodCluster, workerCpu: 4 }]),
      externalIps: [],
      topologyDirty: false,
    });
    expect(useCanvasStore.getState().topologyDirty).toBe(false);
    // A clusters-only sizing change (not mirrored on any node.data) must dirty.
    useCanvasStore.getState().updateCluster("prod", { workerCpu: 16 });
    expect(useCanvasStore.getState().topologyDirty).toBe(true);
  });

  it("_saveTopologyToApi includes clusters and node clusterId/parentId", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
    vi.stubGlobal("fetch", fetchMock);
    const vmNode: Node = {
      id: "n1",
      type: "vmNode",
      position: { x: 0, y: 0 },
      parentId: "cluster-prod",
      data: { os: "rhcos", clusterId: "prod" },
    };
    useCanvasStore.setState({
      nodes: [vmNode],
      edges: [],
      hiddenNodeIds: [],
      startOrder: [],
      externalIps: [],
      clusters: [{ ...prodCluster }],
    });
    await _saveTopologyToApi("proj1", useCanvasStore.getState());
    const body = JSON.parse(fetchMock.mock.calls[0][1].body as string) as SavedTopologyBody;
    expect(body.topology.clusters).toHaveLength(1);
    const savedNode = body.topology.nodes[0];
    expect(savedNode.parentId).toBe("cluster-prod");
    expect(savedNode.data.clusterId).toBe("prod");
    vi.unstubAllGlobals();
  });
});
