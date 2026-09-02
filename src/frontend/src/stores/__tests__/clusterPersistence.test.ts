import { describe, it, expect, beforeEach, vi } from "vitest";
import { useCanvasStore, _saveTopologyToApi } from "@/stores/canvasStore";

// helper: reset store between tests
beforeEach(() => {
  useCanvasStore.setState({ nodes: [], edges: [], clusters: [] } as any);
});

describe("cluster persistence in store", () => {
  it("addCluster / updateCluster / removeCluster mutate state", () => {
    const s = useCanvasStore.getState();
    s.addCluster({ id: "prod", name: "prod", nodeId: "cluster-prod", type: "standard", controlPlane: 3, workers: 2 } as any);
    expect(useCanvasStore.getState().clusters).toHaveLength(1);
    useCanvasStore.getState().updateCluster("prod", { workers: 3 });
    expect(useCanvasStore.getState().clusters[0].workers).toBe(3);
    useCanvasStore.getState().removeCluster("prod");
    expect(useCanvasStore.getState().clusters).toHaveLength(0);
  });

  it("_saveTopologyToApi includes clusters and node clusterId/parentId", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
    vi.stubGlobal("fetch", fetchMock);
    useCanvasStore.setState({
      nodes: [{ id: "n1", type: "vmNode", position: { x: 0, y: 0 }, parentId: "cluster-prod", data: { os: "rhcos", clusterId: "prod" } }],
      edges: [], hiddenNodeIds: [], startOrder: [], externalIps: [],
      clusters: [{ id: "prod", name: "prod", nodeId: "cluster-prod", type: "standard", controlPlane: 3, workers: 2 }],
    } as any);
    await _saveTopologyToApi("proj1", useCanvasStore.getState() as any);
    const body = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(body.topology.clusters).toHaveLength(1);
    const savedNode = body.topology.nodes[0];
    expect(savedNode.parentId).toBe("cluster-prod");
    expect(savedNode.data.clusterId).toBe("prod");
    vi.unstubAllGlobals();
  });
});
