import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Edge, Node } from "@xyflow/react";

let useCanvasStore: typeof import("../canvasStore").useCanvasStore;
const node = (id: string, type = "vmNode", data: Record<string, unknown> = {}): Node => ({
  id, type, data: { name: id, ...data }, position: { x: 100, y: 100 },
});
const edge = (id: string, source: string, target: string, handles: Partial<Edge> = {}): Edge => ({
  id, source, target, ...handles,
});

beforeEach(async () => {
  vi.resetModules();
  vi.useFakeTimers();
  vi.stubGlobal("fetch", vi.fn());
  ({ useCanvasStore } = await import("../canvasStore"));
  useCanvasStore.setState({
    nodes: [], edges: [], clusters: [], hiddenNodeIds: [], currentProjectId: null,
    selectedNodeId: null, externalIps: [], startOrder: [], showroom: null, vniMap: {},
    deployedNodeData: {}, deployedEdgeKey: "", deployedClusters: "[]",
    deployedExternalIps: "[]", topologyDirty: false,
  });
});

afterEach(() => {
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("canvas visibility and history", () => {
  it("hides a selected node and its connected edges without deleting topology", () => {
    useCanvasStore.setState({ nodes: [node("a"), node("b"), node("c")],
      edges: [edge("ab", "a", "b"), edge("bc", "b", "c")], selectedNodeId: "a" });
    const store = useCanvasStore.getState();
    store.hideNode("a");
    store.hideNode("a");
    expect(useCanvasStore.getState().hiddenNodeIds).toEqual(["a"]);
    expect(useCanvasStore.getState().selectedNodeId).toBeNull();
    expect(store.getVisibleNodes().map((n) => n.id)).toEqual(["b", "c"]);
    expect(store.getVisibleEdges().map((e) => e.id)).toEqual(["bc"]);
    store.hideNode("c");
    expect(store.getVisibleEdges()).toEqual([]);
    store.unhideNode("a");
    expect(store.getVisibleNodes().map((n) => n.id)).toEqual(["a", "b"]);
    store.unhideAll();
    expect(store.getVisibleNodes()).toHaveLength(3);
  });
  it("restores deleted nodes, connections, and visibility with undo/redo", () => {
    useCanvasStore.setState({ nodes: [node("a"), node("b")], edges: [edge("ab", "a", "b")], hiddenNodeIds: ["b"], selectedNodeId: "a" });
    const store = useCanvasStore.getState();
    store.deleteNode("a");
    expect(useCanvasStore.getState().nodes.map((n) => n.id)).toEqual(["b"]);
    expect(useCanvasStore.getState().edges).toEqual([]);
    store.undo();
    expect(useCanvasStore.getState().nodes.map((n) => n.id)).toEqual(["a", "b"]);
    expect(useCanvasStore.getState().edges).toHaveLength(1);
    expect(useCanvasStore.getState().hiddenNodeIds).toEqual(["b"]);
    store.redo();
    expect(useCanvasStore.getState().nodes.map((n) => n.id)).toEqual(["b"]);
    expect(useCanvasStore.getState().selectedNodeId).toBeNull();
  });
  it("clears redo history after a new edit and tolerates empty history", () => {
    const store = useCanvasStore.getState();
    store.undo();
    store.redo();
    store.addNode(node("first"));
    store.undo();
    store.addNode(node("second"));
    store.redo();
    expect(useCanvasStore.getState().nodes.map((n) => n.id)).toEqual(["second"]);
    expect(useCanvasStore.getState().canRedo).toBe(false);
  });
  it("selects nodes and toggles the minimap", () => {
    const vm = node("a");
    useCanvasStore.setState({ nodes: [vm], showMinimap: true });
    const store = useCanvasStore.getState();
    store.setSelectedNode("a");
    expect(store.getSelectedNode()).toBe(vm);
    store.setSelectedNode(null);
    expect(store.getSelectedNode()).toBeUndefined();
    store.toggleMinimap();
    expect(useCanvasStore.getState().showMinimap).toBe(false);
  });
  it("updates only VM runtime status", () => {
    const network = node("network", "networkNode");
    useCanvasStore.setState({ nodes: [node("a"), network] });
    useCanvasStore.getState().setAllVmStatus("running");
    expect(useCanvasStore.getState().nodes[0].data.status).toBe("running");
    expect(useCanvasStore.getState().nodes[1]).toBe(network);
  });
  it("deletes only the selected edge and can undo it", () => {
    useCanvasStore.setState({ edges: [edge("one", "a", "b"), edge("two", "b", "c")] });
    useCanvasStore.getState().deleteEdge("one");
    expect(useCanvasStore.getState().edges.map((e) => e.id)).toEqual(["two"]);
    useCanvasStore.getState().undo();
    expect(useCanvasStore.getState().edges).toHaveLength(2);
  });
});

describe("VM duplication", () => {
  it.each([false, true])("remaps NICs, disk handles, and boot references (reverse edges=%s)", (reverse) => {
    const source = node("vm", "vmNode", { name: "web-01", domainUuid: "deployed-uuid",
      nics: [{ id: "nic", mac: "52:54:00:00:00:01", ip: "192.168.1.10" }],
      diskControllers: [{ id: "disk-controller", bus: "virtio" }], bootDevices: ["disk", "network"] });
    const disk = node("disk", "storageNode", { name: "root-01", size: 30 });
    const network = node("network", "networkNode");
    let edges = [edge("nic-edge", "vm", "network", { sourceHandle: "nic-nic-top", targetHandle: "top" }),
      edge("disk-edge", "disk", "vm", { sourceHandle: "bottom", targetHandle: "dp-disk-controller-top" })];
    if (reverse) edges = edges.map((e) => ({ ...e, source: e.target, target: e.source, sourceHandle: e.targetHandle, targetHandle: e.sourceHandle }));
    useCanvasStore.setState({ nodes: [source, disk, network], edges });
    useCanvasStore.getState().duplicateNode("vm");
    const state = useCanvasStore.getState();
    const clone = state.nodes.find((n) => n.id === state.selectedNodeId)!;
    const clonedDisk = state.nodes.find((n) => n.type === "storageNode" && n.id !== "disk")!;
    const nics = clone.data.nics as { id: string; mac: string; ip: string }[];
    const controllers = clone.data.diskControllers as { id: string }[];
    expect(clone.data.name).toBe("web-02");
    expect(clone.position).toEqual({ x: 140, y: 140 });
    expect(clone.data.domainUuid).toBeUndefined();
    expect(clone.data.smbiosUuid).toBeTruthy();
    expect(nics[0].id).not.toBe("nic");
    expect(nics[0].mac).not.toBe("52:54:00:00:00:01");
    expect(nics[0].ip).toBe("192.168.1.11");
    expect(controllers[0].id).not.toBe("disk-controller");
    expect(clone.data.bootDevices).toEqual([clonedDisk.id, "network"]);
    expect(clonedDisk.data.name).toBe("root-02");
    const newEdges = state.edges.slice(2);
    expect(newEdges).toHaveLength(2);
    expect(newEdges.some((e) => [e.sourceHandle, e.targetHandle].includes(`nic-${nics[0].id}-top`))).toBe(true);
    expect(newEdges.some((e) => [e.sourceHandle, e.targetHandle].includes(`dp-${controllers[0].id}-top`))).toBe(true);
    expect(source.data.bootDevices).toEqual(["disk", "network"]);
  });
  it("skips occupied names and addresses", () => {
    useCanvasStore.setState({ nodes: [node("source", "vmNode", { name: "vm", nics: [{ id: "nic", ip: "192.168.1.10" }] }),
      node("used", "vmNode", { name: "vm-2", nics: [{ id: "used-nic", ip: "192.168.1.11" }] })] });
    useCanvasStore.getState().duplicateNode("source");
    expect(useCanvasStore.getState().nodes[2].data).toMatchObject({ name: "vm-3", nics: [{ ip: "192.168.1.12" }] });
  });
  it.each(["smbiosUuid", "domainUuid"])("preserves explicitly cloned UUID from %s", (field) => {
    useCanvasStore.setState({ nodes: [node("source", "vmNode", { [field]: "retained" })] });
    useCanvasStore.getState().duplicateNode("source", { cloneUuid: true });
    expect(useCanvasStore.getState().nodes[1].data.smbiosUuid).toBe("retained");
    expect(useCanvasStore.getState().nodes[1].data.domainUuid).toBeUndefined();
  });
  it("duplicates ordinary nodes and ignores missing nodes", () => {
    useCanvasStore.setState({ nodes: [node("net", "networkNode")] });
    useCanvasStore.getState().duplicateNode("missing");
    expect(useCanvasStore.getState().nodes).toHaveLength(1);
    useCanvasStore.getState().duplicateNode("net");
    expect(useCanvasStore.getState().nodes[1].data.name).toBe("net-2");
    expect(useCanvasStore.getState().edges).toEqual([]);
  });
  it("does not create orphan connections to absent nodes", () => {
    useCanvasStore.setState({ nodes: [node("vm")], edges: [edge("orphan", "vm", "gone")] });
    useCanvasStore.getState().duplicateNode("vm");
    expect(useCanvasStore.getState().nodes).toHaveLength(2);
    expect(useCanvasStore.getState().edges).toHaveLength(1);
  });
});

describe("canvas topology updates", () => {
  it("protects the managed BMC network until the last BMC-enabled VM is deleted", () => {
    useCanvasStore.setState({ nodes: [node("vm", "vmNode", { bmcEnabled: true }),
      node("bmc", "networkNode", { networkType: "bmc" }), node("unrelated")], edges: [edge("bmc-link", "vm", "bmc")] });
    useCanvasStore.getState().deleteNode("bmc");
    expect(useCanvasStore.getState().nodes).toHaveLength(3);
    useCanvasStore.getState().deleteNode("vm");
    expect(useCanvasStore.getState().nodes.map((n) => n.id)).toEqual(["unrelated"]);
    expect(useCanvasStore.getState().edges).toEqual([]);
  });
  it("refreshes edge references when handles change but preserves runtime-only updates", () => {
    const link = edge("link", "vm", "net");
    useCanvasStore.setState({ nodes: [node("vm"), node("net", "networkNode")], edges: [link] });
    useCanvasStore.getState().updateNodeData("vm", { status: "running" });
    expect(useCanvasStore.getState().edges[0]).toBe(link);
    expect(useCanvasStore.getState().topologyDirty).toBe(false);
    useCanvasStore.getState().updateNodeData("vm", { nics: [{ id: "nic" }] });
    expect(useCanvasStore.getState().edges[0]).not.toBe(link);
    expect(useCanvasStore.getState().nodes[0].data.nics).toEqual([{ id: "nic" }]);
  });
  it("applies successful auto-layout results and keeps the current layout on failure", async () => {
    const original = node("a");
    const positioned = { ...original, position: { x: 200, y: 300 } };
    useCanvasStore.setState({ nodes: [original] });
    vi.mocked(fetch).mockResolvedValueOnce({ ok: true, json: async () => ({ nodes: [positioned], edges: [] }) } as Response);
    await useCanvasStore.getState().autoLayout();
    expect(useCanvasStore.getState().nodes[0].position).toEqual({ x: 200, y: 300 });
    vi.mocked(fetch).mockResolvedValueOnce({ ok: false } as Response);
    await useCanvasStore.getState().autoLayout();
    expect(useCanvasStore.getState().nodes[0].position).toEqual({ x: 200, y: 300 });
    vi.mocked(fetch).mockRejectedValueOnce(new Error("offline"));
    await useCanvasStore.getState().autoLayout();
    expect(useCanvasStore.getState().nodes[0].position).toEqual({ x: 200, y: 300 });
    useCanvasStore.setState({ nodes: [] });
    await useCanvasStore.getState().autoLayout();
    expect(fetch).toHaveBeenCalledTimes(3);
  });
});
