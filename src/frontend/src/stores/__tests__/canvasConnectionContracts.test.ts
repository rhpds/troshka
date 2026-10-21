import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Connection, Node } from "@xyflow/react";

let useCanvasStore: typeof import("../canvasStore").useCanvasStore;
const node = (id: string, type: string, data: Record<string, unknown> = {}): Node => ({
  id, type, position: { x: 0, y: 0 }, data: { name: id, ...data },
});
const connection = (source: string, target: string, sourceHandle: string | null = null, targetHandle: string | null = null): Connection => ({
  source, target, sourceHandle, targetHandle,
});

beforeEach(async () => {
  vi.resetModules();
  vi.useFakeTimers();
  vi.stubGlobal("fetch", vi.fn());
  ({ useCanvasStore } = await import("../canvasStore"));
  useCanvasStore.setState({ nodes: [], edges: [], clusters: [], currentProjectId: null, externalIps: [], vniMap: {} });
});

afterEach(() => {
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("canvas connection contracts", () => {
  it("allocates a separate NIC when an existing NIC is already connected", () => {
    useCanvasStore.setState({ nodes: [node("vm", "vmNode", { nics: [{ id: "one", name: "eth0", mac: "00:11:22:33:44:55", model: "virtio" }] }), node("net", "networkNode"), node("net2", "networkNode")] });
    const connect = useCanvasStore.getState().onConnect;
    connect(connection("net", "vm", "bottom", "nic-one-top"));
    connect(connection("net2", "vm", "bottom", "nic-one-top"));
    const state = useCanvasStore.getState();
    const nics = state.nodes[0].data.nics as Array<{ id: string; name: string }>;
    expect(nics).toHaveLength(2);
    expect(nics[1].name).toBe("eth1");
    expect(state.edges.map((edge) => edge.targetHandle)).toEqual(["nic-one-top", `nic-${nics[1].id}-top`]);
  });

  it.each(["qcow2", "raw"])("rejects a second owner of a %s disk in either direction", (format) => {
    useCanvasStore.setState({ nodes: [node("disk", "storageNode", { format }), node("vm", "vmNode"), node("other", "vmNode")] });
    const connect = useCanvasStore.getState().onConnect;
    connect(connection("disk", "vm", "right", "dp-controller-left"));
    connect(connection("other", "disk", "dp-controller-left", "right"));
    expect(useCanvasStore.getState().edges).toHaveLength(1);
    expect(useCanvasStore.getState().edges[0].target).toBe("vm");
  });

  it("permits multiple owners of an ISO", () => {
    useCanvasStore.setState({ nodes: [node("disk", "storageNode", { format: "iso" }), node("vm", "vmNode"), node("other", "vmNode")] });
    const connect = useCanvasStore.getState().onConnect;
    connect(connection("disk", "vm", "right", "dp-controller-left"));
    connect(connection("other", "disk", "dp-controller-left", "right"));
    expect(useCanvasStore.getState().edges).toHaveLength(2);
  });

  it("requires a left mount for pods and converts writable container disks to raw", () => {
    useCanvasStore.setState({ nodes: [node("disk", "storageNode", { format: "qcow2" }), node("pod", "containerNode", { isPod: true })] });
    const connect = useCanvasStore.getState().onConnect;
    connect(connection("disk", "pod", "right", "mnt-data-right"));
    expect(useCanvasStore.getState().edges).toEqual([]);
    connect(connection("disk", "pod", "right", "mnt-data-left"));
    expect(useCanvasStore.getState().edges).toHaveLength(1);
    expect(useCanvasStore.getState().nodes[0].data.format).toBe("raw");
  });

  it("rejects invalid gateway handles and reverse duplicate network links", () => {
    useCanvasStore.setState({ nodes: [node("gateway", "networkNode", { subtype: "gateway" }), node("net", "networkNode")] });
    const connect = useCanvasStore.getState().onConnect;
    connect(connection("gateway", "net", "top", "bottom"));
    expect(useCanvasStore.getState().edges).toEqual([]);
    connect(connection("gateway", "net", "bottom", "top"));
    connect(connection("net", "gateway", "top", "bottom"));
    expect(useCanvasStore.getState().edges).toHaveLength(1);
  });

  it("normalizes reverse Ceph links and records the cluster identity", () => {
    useCanvasStore.setState({ nodes: [node("ceph", "cephClusterNode"), node("cluster", "clusterNode", { clusterId: "cluster-id" })] });
    const connect = useCanvasStore.getState().onConnect;
    connect(connection("cluster", "ceph", "wrong", "right"));
    expect(useCanvasStore.getState().edges).toEqual([]);
    connect(connection("cluster", "ceph", "ceph-left", "right"));
    expect(useCanvasStore.getState().edges[0]).toMatchObject({ source: "ceph", target: "cluster", sourceHandle: "right", targetHandle: "ceph-left" });
    expect(useCanvasStore.getState().nodes[0].data.linkedClusters).toEqual(["cluster-id"]);
  });

  it("rejects direct VM, container, and network peer links without creating NICs", () => {
    useCanvasStore.setState({ nodes: [node("vm", "vmNode"), node("other", "vmNode"), node("pod", "containerNode"), node("net", "networkNode"), node("net2", "networkNode")] });
    const connect = useCanvasStore.getState().onConnect;
    connect(connection("vm", "other"));
    connect(connection("pod", "vm"));
    connect(connection("net", "net2"));
    expect(useCanvasStore.getState().edges).toEqual([]);
    expect(useCanvasStore.getState().nodes[0].data.nics).toBeUndefined();
  });
});
