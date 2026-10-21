import type { ReactFlowProps, Node } from "@xyflow/react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Canvas from "../Canvas";
import { requestDuplicateVM, useCanvasStore } from "@/stores/canvasStore";
import { appConfirm } from "@/lib/confirm";
import { confirmPowerWarn } from "@/lib/powerWarn";

const flow = vi.hoisted(() => ({ props: {} as ReactFlowProps }));
vi.mock("@xyflow/react", async (original) => ({
  ...await original<typeof import("@xyflow/react")>(),
  useReactFlow: () => ({ screenToFlowPosition: () => ({ x: 100, y: 200 }) }),
  ReactFlow: (props: ReactFlowProps) => { flow.props = props; return <div data-testid="flow" onDrop={props.onDrop} onDragOver={props.onDragOver}>{props.nodes?.map(node => <button key={node.id} onClick={event => props.onNodeClick?.(event, node)}>{String(node.data.name)}</button>)}</div>; },
}));
vi.mock("../ClusterInstallLogModal", () => ({ default: () => null }));
vi.mock("../NodeContextMenu", () => ({ default: ({ nodeId, onClose }: { nodeId: string; onClose: () => void }) => <button onClick={onClose}>Close menu for {nodeId}</button> }));
vi.mock("../EdgeContextMenu", () => ({ default: ({ edgeId, onClose }: { edgeId: string; onClose: () => void }) => <button onClick={onClose}>Close edge {edgeId}</button> }));
vi.mock("../DuplicateVMModal", () => ({ default: ({ onConfirm, onCancel }: { onConfirm: (sameUuid: boolean) => void; onCancel: () => void }) => <><button onClick={() => onConfirm(false)}>Clone VM</button><button onClick={onCancel}>Cancel clone</button></> }));
vi.mock("@/lib/confirm", () => ({ appConfirm: vi.fn().mockResolvedValue(true) }));
vi.mock("@/lib/powerWarn", () => ({ confirmPowerWarn: vi.fn().mockResolvedValue(true) }));
vi.mock("@/hooks/useIsDarkTheme", () => ({ useIsDarkTheme: () => false }));
const initial = useCanvasStore.getState();
beforeEach(() => { useCanvasStore.setState({ ...initial, nodes: [], edges: [], clusters: [], hiddenNodeIds: [], projectState: "draft", currentProjectId: null }); });
afterEach(() => { vi.unstubAllGlobals(); vi.clearAllMocks(); });
function drop(type: string, defaults = {}) { fireEvent.drop(screen.getByTestId("flow"), { dataTransfer: { getData: () => JSON.stringify({ type, label: type, defaults }) } }); }
const node = (id: string, type = "vmNode", data = {}): Node => ({ id, type, position: { x: 0, y: 0 }, data: { name: id, ...data } });

describe("Canvas interactions", () => {
  it("preserves cluster membership and NIC wiring when anchor deletion is cancelled", async () => {
    render(<Canvas />); drop("cluster"); drop("network");
    const state = useCanvasStore.getState(), cluster = state.clusters[0], network = state.nodes.find(n => n.type === "networkNode" && n.data.networkType !== "bmc")!;
    act(() => flow.props.onConnect?.({ source: network.id, target: cluster.nodeId!, sourceHandle: "bottom", targetHandle: "cluster-net-top" }));
    const before = useCanvasStore.getState(), anchor = before.edges.find(e => e.target === cluster.nodeId && e.source === network.id)!;
    const membership = structuredClone(before.clusters[0].networkIds), edges = structuredClone(before.edges), members = structuredClone(before.nodes.filter(n => n.data.clusterId === cluster.id));
    vi.mocked(appConfirm).mockResolvedValueOnce(false);
    await act(async () => flow.props.onEdgesChange?.([{ id: anchor.id, type: "remove" }]));
    expect(useCanvasStore.getState().clusters[0].networkIds).toEqual(membership);
    expect(useCanvasStore.getState().edges).toEqual(edges);
    expect(useCanvasStore.getState().nodes.filter(n => n.data.clusterId === cluster.id)).toEqual(members);
  });

  it("connects a cluster network once and removes its membership with the anchor", async () => {
    render(<Canvas />); drop("cluster"); drop("network");
    const state = useCanvasStore.getState(), cluster = state.clusters[0], network = state.nodes.find(n => n.type === "networkNode" && n.data.networkType !== "bmc")!;
    const connection = { source: network.id, target: cluster.nodeId!, sourceHandle: "bottom", targetHandle: "cluster-net-top" };
    act(() => flow.props.onConnect?.(connection)); act(() => flow.props.onConnect?.(connection));
    expect(useCanvasStore.getState().clusters[0].networkIds).toEqual([network.id]);
    const anchor = useCanvasStore.getState().edges.find(e => e.target === cluster.nodeId && e.source === network.id)!;
    expect(anchor).toBeDefined(); expect(flow.props.edges?.some(e => e.id === anchor.id)).toBe(true);
    await act(async () => flow.props.onEdgesChange?.([{ id: anchor.id, type: "remove" }]));
    expect(useCanvasStore.getState().clusters[0].networkIds).toEqual([]); expect(useCanvasStore.getState().edges.some(e => e.id === anchor.id)).toBe(false);
  });

  it("updates RHCOS membership and coordinates when dragged into and out of a cluster", () => {
    const cluster = { ...node("cluster", "clusterNode", { clusterId: "ocp", type: "standard" }), position: { x: 100, y: 100 }, width: 520, height: 400 };
    const vm = { ...node("vm", "vmNode", { os: "rhcos" }), position: { x: 150, y: 150 } };
    useCanvasStore.setState({ nodes: [vm, cluster] }); render(<Canvas />);
    act(() => flow.props.onNodeDragStop?.(new MouseEvent("mouseup"), vm, [vm]));
    let updated = useCanvasStore.getState().nodes.find(n => n.id === "vm")!;
    expect(updated.parentId).toBe("cluster"); expect(updated.position).toEqual({ x: 50, y: 50 }); expect(updated.data.clusterId).toBe("ocp");
    const outside = { ...updated, position: { x: 800, y: 800 } };
    act(() => flow.props.onNodeDragStop?.(new MouseEvent("mouseup"), outside, [outside]));
    updated = useCanvasStore.getState().nodes.find(n => n.id === "vm")!; expect(updated.parentId).toBeUndefined(); expect(updated.data.clusterId).toBeNull();
  });

  it("clones full clusters with independent names, members and disk specs", () => {
    render(<Canvas />); drop("cluster"); const source = useCanvasStore.getState().clusters[0];
    act(() => useCanvasStore.getState().duplicateNode(source.nodeId!));
    act(() => useCanvasStore.getState().duplicateNode(source.nodeId!));
    const cloned = useCanvasStore.getState().clusters[1];
    act(() => useCanvasStore.getState().duplicateNode(cloned.nodeId!));
    const state = useCanvasStore.getState(); expect(state.clusters.map(c => c.name)).toEqual(["ocp", "ocp-2", "ocp-3", "ocp-4"]);
    expect(cloned).toMatchObject({ type: source.type, controlPlane: source.controlPlane, workers: source.workers, networkIds: [], configureBastionBrowser: false });
    expect(state.nodes.some(n => n.data.clusterId === cloned.id && n.type === "vmNode")).toBe(true);
  });

  it("opens and closes context menus, protecting undeployed cluster members", () => {
    const vm = node("vm"), member = node("member", "vmNode", { clusterId: "ocp" });
    useCanvasStore.setState({ nodes: [vm, member], edges: [{ id: "wire", source: "vm", target: "member" }] }); render(<Canvas />);
    const event = { preventDefault: vi.fn(), clientX: 50, clientY: 70 } as unknown as React.MouseEvent;
    act(() => flow.props.onNodeContextMenu?.(event, member)); expect(screen.queryByText("Close menu for member")).not.toBeInTheDocument();
    act(() => flow.props.onNodeContextMenu?.(event, vm)); fireEvent.click(screen.getByText("Close menu for vm")); expect(screen.queryByText("Close menu for vm")).not.toBeInTheDocument();
    act(() => { useCanvasStore.setState({ deployedVmIds: new Set(["member"]) }); });
    act(() => flow.props.onNodeContextMenu?.(event, member)); expect(screen.getByText("Close menu for member")).toBeInTheDocument();
    act(() => flow.props.onEdgeContextMenu?.(event, { id: "wire", source: "vm", target: "member" })); expect(screen.queryByText("Close menu for member")).not.toBeInTheDocument();
    fireEvent.click(screen.getByText("Close edge wire")); expect(screen.queryByText("Close edge wire")).not.toBeInTheDocument();
  });

  it("asks for VM cloning options and supports cancellation", () => {
    useCanvasStore.setState({ nodes: [node("vm", "vmNode", { os: "rhel9", nics: [], diskControllers: [] })] }); render(<Canvas />);
    act(() => requestDuplicateVM("vm")); fireEvent.click(screen.getByText("Cancel clone")); expect(useCanvasStore.getState().nodes).toHaveLength(1);
    act(() => requestDuplicateVM("vm")); fireEvent.click(screen.getByText("Clone VM")); expect(useCanvasStore.getState().nodes).toHaveLength(2); expect(screen.queryByText("Clone VM")).not.toBeInTheDocument();
  });

  it("starts and resumes selected deployed VMs without acting on undeployed nodes", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true }));
    const selected = [node("paused", "vmNode", { status: "paused" }), node("stopped", "vmNode", { status: "stopped" }), node("draft")];
    useCanvasStore.setState({ nodes: selected, currentProjectId: "project", deployedVmIds: new Set(["paused", "stopped"]) }); render(<Canvas />);
    act(() => flow.props.onSelectionChange?.({ nodes: selected, edges: [] })); fireEvent.click(screen.getByTitle("Start/Resume VMs"));
    expect(fetch).toHaveBeenCalledWith("/api/v1/projects/project/vms/paused/unpause", { method: "POST" }); expect(fetch).toHaveBeenCalledWith("/api/v1/projects/project/vms/stopped/start", { method: "POST" }); expect(fetch).toHaveBeenCalledTimes(2);
    await act(async () => {});
  });

  it.each([["Graceful Shutdown VMs", "stop"], ["Pause VMs", "pause"], ["Hibernate VMs", "hibernate"]])("confirms %s before acting on selected VMs", async (title, action) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true })); const selected = [node("one"), node("two")];
    useCanvasStore.setState({ nodes: selected, currentProjectId: "project", deployedVmIds: new Set(["one", "two"]), supportsHibernate: true }); render(<Canvas />);
    act(() => flow.props.onSelectionChange?.({ nodes: selected, edges: [] })); fireEvent.click(screen.getByTitle(title));
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(`/api/v1/projects/project/vms/two/${action}`, { method: "POST" }));
    expect(fetch).toHaveBeenCalledWith(`/api/v1/projects/project/vms/one/${action}`, { method: "POST" });
    if (action === "stop") expect(appConfirm).toHaveBeenCalledOnce(); else expect(confirmPowerWarn).toHaveBeenCalledOnce();
  });

  it("materializes distinct clusters with generated members and attached disks", () => {
    render(<Canvas />); drop("cluster"); drop("cluster");
    const state = useCanvasStore.getState(); expect(state.clusters.map(c => c.name)).toEqual(["ocp", "ocp-2"]);
    for (const cluster of state.clusters) {
      const members = state.nodes.filter(n => n.type === "vmNode" && n.data.clusterId === cluster.id);
      expect(members.length).toBeGreaterThan(0); expect(members.every(n => n.parentId === cluster.nodeId)).toBe(true);
      expect(state.edges.some(e => members.some(n => n.id === e.target))).toBe(true);
    }
  });

  it("undoes and redoes an added disk with keyboard shortcuts", () => {
    render(<Canvas />); drop("disk"); expect(useCanvasStore.getState().nodes).toHaveLength(1);
    fireEvent.keyDown(window, { key: "z", ctrlKey: true }); expect(useCanvasStore.getState().nodes).toHaveLength(0);
    fireEvent.keyDown(window, { key: "z", metaKey: true, shiftKey: true }); expect(useCanvasStore.getState().nodes).toHaveLength(1);
  });

  it("deletes a multi-node selection and dismisses the selection toolbar", () => {
    const selected = [node("one", "storageNode"), node("two", "storageNode")]; useCanvasStore.setState({ nodes: selected }); render(<Canvas />);
    act(() => flow.props.onSelectionChange?.({ nodes: selected, edges: [] })); expect(screen.getByText("2 selected")).toBeInTheDocument();
    fireEvent.click(screen.getByTitle("Delete All")); expect(useCanvasStore.getState().nodes).toEqual([]); expect(screen.queryByText("2 selected")).not.toBeInTheDocument();
  });

  it.each([
    ["vm", "disk", "dp-root-left", "right", true],
    ["vm", "disk", "nic-main-top", "right", false],
    ["disk", "vm", "right", "dp-root-left", true],
    ["disk", "vm", "right", "dp-root-right", false],
    ["disk", "vm", "left", "dp-root-left", false],
    ["vm", "disk", "dp-root-left", "left", false],
    ["vm", "network", "nic-main-top", "bottom", true],
    ["network", "vm", "bottom", "nic-main-top", true],
    ["vm", "network", "dp-root-left", "bottom", false],
    ["network", "vm", "bottom", "dp-root-left", false],
    ["vm", "network", "nic-main-top", "left", false],
    ["network", "vm", "right", "nic-main-top", false],
    ["router", "network", "right", "left", true],
    ["network", "router", "right", "left", true],
    ["router", "network", "right", "top", false],
    ["network", "router", "top", "left", false],
    ["gateway", "network", "bottom", "top", true],
    ["network", "gateway", "top", "bottom", true],
    ["gateway", "network", "left", "top", false],
    ["network", "gateway", "top", "right", false],
    ["showroom", "network", "nic-main-top", "bottom", false],
    ["network", "showroom", "bottom", "nic-main-top", false],
    ["showroom", "disk", "mnt-data-left", "right", true],
    ["disk", "showroom", "right", "mnt-data-left", true],
    ["showroom", "disk", "mnt-data-right", "right", false],
    ["showroom", "gateway", "gateway-link-right", "showroom-link", true],
    ["gateway", "showroom", "showroom-link", "gateway-link-right", true],
    ["showroom", "gateway", "wrong", "showroom-link", false],
    ["showroom", "vm", "top", "bottom", false],
    ["pod", "disk", "mnt-data-left", "right", true],
    ["disk", "pod", "right", "mnt-data-right", false],
    ["pod", "network", "nic-main-top", "bottom", true],
    ["pod", "vm", "top", "bottom", false],
    ["ceph", "cluster", "right", "ceph-left", true],
    ["cluster", "ceph", "ceph-right", "left", true],
    ["ceph", "cluster", "left", "ceph-left", false],
    ["ceph", "network", "right", "left", false],
    ["loadbalancer", "network", "top", "bottom", false],
  ])("validates %s → %s ports %s/%s as %s", (source, target, sourceHandle, targetHandle, expected) => {
    useCanvasStore.setState({ nodes: [
      node("vm"), node("disk", "storageNode"), node("network", "networkNode", { subtype: "network" }),
      node("gateway", "networkNode", { subtype: "gateway" }), node("router", "networkNode", { subtype: "router" }),
      node("loadbalancer", "networkNode", { subtype: "loadbalancer" }), node("showroom", "containerNode", { isShowroom: true }),
      node("pod", "containerNode", { isPod: true }), node("ceph", "cephClusterNode"), node("cluster", "clusterNode"),
    ] }); render(<Canvas />);
    expect(flow.props.isValidConnection?.({ source, target, sourceHandle, targetHandle })).toBe(expected);
  });

  it.each([
    ["vm-linux", "vmNode", { name: "vm-00", os: "rhel10", cloudInit: true }],
    ["vm-windows", "vmNode", { name: "vm-00", status: "stopped" }],
    ["disk", "storageNode", { name: "disk-00", size: 20, format: "qcow2" }],
    ["iso", "storageNode", { name: "boot-00", size: 4, format: "iso" }],
    ["network", "networkNode", { name: "network-00", cidr: "10.0.0.0/24", dhcp: true }],
    ["dns", "networkNode", { dns: true, dnsDomain: "lab.local" }],
    ["dhcp", "networkNode", { dhcp: true }],
    ["router", "networkNode", { subtype: "router", cidr: "" }],
    ["gateway", "networkNode", { subtype: "gateway", outboundPolicy: "restrict" }],
    ["loadbalancer", "networkNode", { subtype: "loadbalancer", dnsTtl: 30 }],
    ["container", "containerNode", { name: "ctr-00", memory: 512, restartPolicy: "always" }],
    ["pod", "containerNode", { name: "pod-00", isPod: true }],
  ])("creates a %s with persisted defaults at flow coordinates", (type, expectedType, defaults) => {
    render(<Canvas />); drop(type); const created = useCanvasStore.getState().nodes[0];
    expect(created).toMatchObject({ type: expectedType, position: { x: 100, y: 200 }, data: defaults });
  });

  it("preserves template sizing and selects the next free VM name", () => {
    useCanvasStore.setState({ nodes: [node("existing", "vmNode", { name: "vm-00" })] }); render(<Canvas />); drop("template-rhel", { vcpus: 8, ram: 16, os: "rhel9" });
    expect(useCanvasStore.getState().nodes[1].data).toMatchObject({ name: "vm-01", vcpus: 8, ram: 16, os: "rhel9" });
  });

  it("allocates a fresh subnet and refuses a second gateway", () => {
    render(<Canvas />); drop("network"); drop("network"); drop("gateway"); drop("gateway");
    const nodes = useCanvasStore.getState().nodes; expect(nodes).toHaveLength(3); expect(nodes[1].data.cidr).toBe("10.0.1.0/24");
  });

  it("ignores malformed, empty and unsupported palette payloads", () => {
    render(<Canvas />); for (const raw of ["", "{", JSON.stringify({ type: "unsupported" })]) fireEvent.drop(screen.getByTestId("flow"), { dataTransfer: { getData: () => raw } });
    expect(useCanvasStore.getState().nodes).toEqual([]);
  });

  it("creates one Ceph storage node and selects the lab network", () => {
    useCanvasStore.setState({ nodes: [node("lab", "networkNode", { subtype: "network", cidr: "10.2.3.0/24" })] }); render(<Canvas />); drop("project-ceph"); drop("project-ceph");
    expect(useCanvasStore.getState().nodes.filter(n => n.type === "cephClusterNode")).toEqual([expect.objectContaining({ data: expect.objectContaining({ networkRef: "lab", labIp: "10.2.3.3", osdCount: 3 }) })]);
  });

  it("selects a clicked node and clears selection when the pane is clicked", () => {
    useCanvasStore.setState({ nodes: [node("server")] }); render(<Canvas />); fireEvent.click(screen.getByRole("button", { name: "server" })); expect(useCanvasStore.getState().selectedNodeId).toBe("server");
    act(() => flow.props.onPaneClick?.({} as React.MouseEvent)); expect(useCanvasStore.getState().selectedNodeId).toBeNull();
  });

  it("keeps hidden nodes and their edges out of the flow until restored", () => {
    useCanvasStore.setState({ nodes: [node("one"), node("two")], hiddenNodeIds: ["two"], edges: [{ id: "wire", source: "one", target: "two" }] }); render(<Canvas />);
    expect(flow.props.nodes).toHaveLength(1); expect(flow.props.edges).toHaveLength(0);
    fireEvent.click(screen.getByTitle("Show two")); expect(flow.props.nodes).toHaveLength(2); expect(flow.props.edges).toHaveLength(1);
  });

  it.each(["deploying", "reconfiguring", "starting", "stopping"])("disables topology mutation while %s", projectState => {
    useCanvasStore.setState({ projectState }); render(<Canvas />); drop("disk"); expect(useCanvasStore.getState().nodes).toEqual([]);
    expect(flow.props.nodesDraggable).toBe(false); expect(flow.props.onConnect).toBeUndefined(); expect(flow.props.deleteKeyCode).toBeNull();
  });
});
