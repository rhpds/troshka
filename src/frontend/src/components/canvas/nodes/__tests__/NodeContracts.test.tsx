import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ReactFlowProvider, type NodeProps } from "@xyflow/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useCanvasStore } from "@/stores/canvasStore";
import VMNode from "../VMNode";
import StorageNode from "../StorageNode";
import { ContainerNode } from "../ContainerNode";

vi.mock("@/lib/confirm", () => ({ appConfirm: vi.fn().mockResolvedValue(true) }));
vi.mock("@/lib/powerWarn", () => ({ confirmPowerWarn: vi.fn().mockResolvedValue(true) }));
const initial = useCanvasStore.getState();
const fetchMock = vi.fn();
const data = { name: "vm", status: "running", vcpus: 2, ram: 4, os: "rhel9", nics: [], diskControllers: [], bootDevices: [] };
function props(nodeData: Record<string, unknown>): NodeProps {
  return { id: "vm", type: "vmNode", data: nodeData, selected: false, dragging: false, draggable: true, selectable: true, deletable: true, isConnectable: true, positionAbsoluteX: 0, positionAbsoluteY: 0, zIndex: 0 };
}
function renderVm(status = "running") {
  const vmData = { ...data, status };
  useCanvasStore.setState({ nodes: [{ id: "vm", type: "vmNode", position: { x: 0, y: 0 }, data: vmData }] });
  render(<ReactFlowProvider><VMNode {...props(vmData)} /></ReactFlowProvider>);
}
async function clickAction(title: string) {
  fireEvent.click(screen.getByTitle(title));
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
}
beforeEach(() => {
  vi.useFakeTimers();
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  useCanvasStore.setState({ ...initial, currentProjectId: "project", projectState: "active", nodes: [], edges: [], clusters: [], deployedVmIds: new Set(["vm"]), supportsHibernate: true });
});
afterEach(() => {
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("VM action result contracts", () => {
  it.each([ ["Pause", "paused"], ["Hibernate", "hibernated"] ])("applies a successful %s response", async (title, status) => {
    fetchMock.mockResolvedValue({ json: async () => ({ success: true }) });
    renderVm();
    await clickAction(title);
    expect(useCanvasStore.getState().nodes[0].data.status).toBe(status);
  });
  it("surfaces detailed pause failures without changing the VM state", async () => {
    fetchMock.mockResolvedValue({ json: async () => ({ success: false, detail: { message: "Host rejected pause" } }) });
    renderVm();
    await clickAction("Pause");
    expect(screen.getByText("Pause failed: Host rejected pause")).toBeInTheDocument();
    expect(useCanvasStore.getState().nodes[0].data.status).toBe("running");
  });
  it("resumes paused VMs immediately after successful unpause", async () => {
    fetchMock.mockResolvedValue({ json: async () => ({ success: true }) });
    renderVm("paused");
    await clickAction("Resume");
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/projects/project/vms/vm/unpause", { method: "POST" });
    expect(useCanvasStore.getState().nodes[0].data.status).toBe("running");
  });
  it("accepts an already stopped domain after force power off", async () => {
    fetchMock.mockResolvedValue({ json: async () => ({ success: false, output: "domain is not running" }) });
    renderVm();
    await clickAction("Force Power Off");
    expect(useCanvasStore.getState().nodes[0].data.status).toBe("stopped");
  });
  it("keeps graceful shutdown transitional until polling confirms shutdown", async () => {
    fetchMock.mockImplementation(async (url: string) => ({ json: async () => url.endsWith("/status") ? { state: "shut off" } : { success: true } }));
    renderVm();
    await clickAction("Graceful Shutdown");
    expect(useCanvasStore.getState().nodes[0].data.status).toBe("stopping");
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    expect(useCanvasStore.getState().nodes[0].data.status).toBe("stopped");
  });
  it("polls an already active start until running is confirmed", async () => {
    fetchMock.mockImplementation(async (url: string) => ({ json: async () => url.endsWith("/status") ? { state: "running" } : { success: false, output: "already active" } }));
    renderVm("stopped");
    await clickAction("Start");
    expect(useCanvasStore.getState().nodes[0].data.status).toBe("stopped");
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    expect(useCanvasStore.getState().nodes[0].data.status).toBe("running");
  });
});

describe("node details and keyboard controls", () => {
  it("opens the pod container list from the keyboard", async () => {
    vi.useRealTimers();
    const user = userEvent.setup();
    render(<ReactFlowProvider><ContainerNode {...props({ name: "pod", isPod: true, podContainers: [{ name: "web", image: "nginx", ports: [] }], nics: [], mounts: [] })} /></ReactFlowProvider>);
    const toggle = screen.getByRole("button", { name: /1 container/ });
    toggle.focus();
    await user.keyboard("{Enter}");
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("web")).toBeInTheDocument();
  });
  it("shows ISO selection guidance and preserves non-ISO source labels", () => {
    const view = render(<ReactFlowProvider><StorageNode {...props({ name: "boot", format: "iso", size: 4 })} /></ReactFlowProvider>);
    expect(screen.getByText(/Select ISO from library/)).toBeInTheDocument();
    view.rerender(<ReactFlowProvider><StorageNode {...props({ name: "disk", format: "raw", source: "snapshot", size: 20 })} /></ReactFlowProvider>);
    expect(screen.getByText("RAW · snapshot")).toBeInTheDocument();
    expect(screen.getByText("20 GB")).toBeInTheDocument();
  });
});
