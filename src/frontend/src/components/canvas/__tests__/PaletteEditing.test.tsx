import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Palette from "../Palette";
import { useCanvasStore } from "@/stores/canvasStore";

const initial = useCanvasStore.getState();
const click = (name: string) => fireEvent.click(screen.getByText(name, { exact: true }));
const item = (name: string) => screen.getByText(name, { exact: true }).closest(".palette-item") as HTMLElement;
beforeEach(() => { useCanvasStore.setState({ ...initial, nodes: [], edges: [], clusters: [], projectState: "draft" }); vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => [] })); });
afterEach(async () => { await act(async () => {}); vi.unstubAllGlobals(); vi.useRealTimers(); });

describe("Palette editing and node creation", () => {
  it("shows deployed BMC endpoints and copies their actual URL", async () => {
    vi.useFakeTimers(); const writeText = vi.fn(); vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    vi.stubGlobal("__deployedTopology", { bmc: { username: "deployed-admin", password: "deployed-password", vms: { vm: { ip: "10.0.0.3", redfish_url: "http://10.0.0.3:8000/redfish/v1", redfish_url_ssl: "https://10.0.0.3:8443/redfish/v1", ipmi_address: "10.0.0.3:623" } } } });
    useCanvasStore.setState({ nodes: [
      { id: "bmc", type: "networkNode", position: { x: 0, y: 0 }, data: { networkType: "bmc" } },
      { id: "vm", type: "vmNode", position: { x: 0, y: 0 }, data: { name: "server" } },
    ] }); render(<Palette />); click("BMC INFO");
    expect(screen.getByText("deployed-admin")).toBeInTheDocument(); expect(screen.getByText("https://10.0.0.3:8443/redfish/v1")).toBeInTheDocument();
    fireEvent.click(screen.getByTitle("Copy Redfish URL")); expect(writeText).toHaveBeenCalledWith("http://10.0.0.3:8000/redfish/v1"); expect(screen.getByTitle("Copy Redfish URL")).toHaveTextContent("Copied");
    await act(async () => vi.advanceTimersByTimeAsync(1000)); expect(screen.getByTitle("Copy Redfish URL")).toHaveTextContent("Copy");
    fireEvent.resize(window); fireEvent.scroll(window); expect(screen.getByText("http://10.0.0.3:8000/redfish/v1")).toBeInTheDocument();
  });

  it("shows custom timer values accurately and supports opening a clock editor", () => {
    const clock = vi.fn(); render(<Palette autoStopMinutes={125} autoDeleteMinutes={45} onClockTargetChange={clock} />); click("Project");
    expect(within(item("Auto-Stop")).getByRole("option", { name: "2h 5m" })).toBeInTheDocument(); expect(within(item("Auto-Delete")).getByRole("option", { name: "45m" })).toBeInTheDocument();
    fireEvent.click(screen.getByText("Clock").nextElementSibling!); expect(item("Clock").querySelector('input[type="datetime-local"]')).not.toBeNull();
    fireEvent.click(within(item("Clock")).getByRole("button", { name: "Set" })); expect(clock).toHaveBeenCalledWith(expect.stringMatching(/:00Z$/));
  });

  it("fetches the host provider and enables its Ceph storage palette entry", async () => {
    vi.mocked(fetch).mockImplementation(async url => ({ ok: true, json: async () => String(url).includes("/hosts/") ? { provider_id: "provider", instance_id: "instance", ip_address: "10.0.0.5" } : [{ id: "provider", type: "kubevirt", name: "KubeVirt" }] }) as Response);
    render(<Palette hostId="host" />); click("Storage"); expect(await screen.findByText("Ceph Storage")).toBeInTheDocument();
    const dataTransfer = { setData: vi.fn(), effectAllowed: "" }; fireEvent.dragStart(item("Ceph Storage"), { dataTransfer }); expect(JSON.parse(dataTransfer.setData.mock.calls[0][1]).type).toBe("project-ceph");
    expect(fetch).toHaveBeenCalledWith("/api/v1/hosts/host"); expect(fetch).toHaveBeenCalledWith("/api/v1/providers/");
  });

  it("blocks dragging singleton Showroom and Ceph components already on the canvas", () => {
    useCanvasStore.setState({ providerType: "kubevirt", nodes: [
      { id: "showroom", type: "containerNode", position: { x: 0, y: 0 }, data: { name: "showroom", isShowroom: true } },
      { id: "ceph", type: "cephClusterNode", position: { x: 0, y: 0 }, data: { name: "ceph" } },
    ] }); render(<Palette />); click("Project"); click("Storage");
    const dataTransfer = { setData: vi.fn() }; for (const name of ["Showroom", "Ceph Storage"]) { expect(item(name)).toHaveAttribute("draggable", "false"); fireEvent.dragStart(item(name), { dataTransfer }); }
    expect(dataTransfer.setData).not.toHaveBeenCalled();
  });

  it("reveals a VM password, copies it, and masks it again", () => {
    const writeText = vi.fn(); vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    useCanvasStore.setState({ nodes: [{ id: "vm", type: "vmNode", position: { x: 0, y: 0 }, data: { name: "server", ciCloudUserPassword: "test-password" } }] }); render(<Palette />);
    click("PASSWORDS"); click("••••••"); expect(screen.getByText("test-password")).toBeInTheDocument(); fireEvent.click(screen.getByTitle("Copy")); expect(writeText).toHaveBeenCalledWith("test-password");
    click("test-password"); expect(screen.queryByText("test-password")).not.toBeInTheDocument();
  });

  it("reveals OpenShift access details and expands local oc instructions", () => {
    useCanvasStore.setState({ clusters: [{ id: "ocp", nodeId: "cluster-ocp", name: "ocp", type: "sno", controlPlane: 1, workers: 0, baseDomain: "example.test" }], nodes: [
      { id: "cluster-ocp", type: "clusterNode", position: { x: 0, y: 0 }, data: { clusterId: "ocp", name: "ocp" } },
      { id: "master", type: "vmNode", position: { x: 0, y: 0 }, data: { name: "master", clusterId: "ocp", ocpKubeadminPassword: "test-admin", ocpKubeconfig: "apiVersion: v1" } },
    ] }); render(<Palette projectId="12345678-long-project" />); click("OPENSHIFT INFO");
    expect(screen.getByText("https://api.ocp.example.test:6443")).toBeInTheDocument(); click("••••••"); expect(screen.getByText("test-admin")).toBeInTheDocument();
    click("Local oc"); expect(screen.getByText('eval "$(./scripts/troshka-oc use 12345678)"')).toBeInTheDocument();
    click("Local oc"); expect(screen.queryByText('eval "$(./scripts/troshka-oc use 12345678)"')).not.toBeInTheDocument();
    fireEvent.mouseDown(document.body); expect(screen.queryByText("https://api.ocp.example.test:6443")).not.toBeInTheDocument();
  });

  it("reveals and hides BMC credentials and dismisses the flyout outside", () => {
    useCanvasStore.setState({ nodes: [
      { id: "bmc", type: "networkNode", position: { x: 0, y: 0 }, data: { networkType: "bmc", bmcUsername: "operator", bmcPassword: "test-secret" } },
      { id: "vm", type: "vmNode", position: { x: 0, y: 0 }, data: { name: "server", bmcEnabled: true, bmcIp: "10.10.0.2" } },
    ] }); render(<Palette />); click("BMC INFO");
    expect(screen.getByText("http://10.10.0.2:8000")).toBeInTheDocument(); expect(screen.getByText("10.10.0.2:623")).toBeInTheDocument();
    expect(screen.queryByText("test-secret")).not.toBeInTheDocument(); click("••••••"); expect(screen.getByText("test-secret")).toBeInTheDocument();
    click("test-secret"); expect(screen.queryByText("test-secret")).not.toBeInTheDocument();
    fireEvent.mouseDown(document.body); expect(screen.queryByText("http://10.10.0.2:8000")).not.toBeInTheDocument();
  });

  it("creates a portal link and updates its access level", async () => {
    vi.mocked(fetch).mockResolvedValue({ ok: true, json: async () => ({ token: "test-token", access_level: "console", portal_url: "https://portal.example.test/lab" }) } as Response);
    render(<Palette projectId="project-1" />); click("Lab Portal");
    expect(await screen.findByRole("link", { name: /Open Portal/ })).toHaveAttribute("href", `${window.location.origin}/portal/test-token`);
    expect(fetch).toHaveBeenCalledWith("/api/v1/projects/project-1/portal-token", expect.objectContaining({ method: "POST", body: JSON.stringify({ access_level: "console" }) }));
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "readonly" } });
    await act(async () => {});
    expect(fetch).toHaveBeenLastCalledWith("/api/v1/projects/project-1/portal-token", expect.objectContaining({ body: JSON.stringify({ access_level: "readonly" }) }));
  });

  it("sets and clears backdated clocks and changes guest command execution", () => {
    const clock = vi.fn(), exec = vi.fn(); render(<Palette clockTarget="2025-01-01T12:00:00Z" onClockTargetChange={clock} guestExecEnabled onGuestExecChange={exec} />); click("Project");
    const clockItem = item("Clock"); const date = clockItem.querySelector("input")!;
    fireEvent.change(date, { target: { value: "2024-06-15T09:30" } }); fireEvent.click(within(clockItem).getByRole("button", { name: "Set" })); expect(clock).toHaveBeenLastCalledWith("2024-06-15T09:30:00Z");
    fireEvent.click(screen.getByText("Clock").nextElementSibling!); expect(clock).toHaveBeenLastCalledWith(null);
    fireEvent.click(screen.getByText("Guest Exec").nextElementSibling!); expect(exec).toHaveBeenCalledWith(false);
  });

  it("saves edited descriptions on blur and cancels edits on escape", () => {
    const changed = vi.fn(); render(<Palette projectDescription="Original description" projectGuid="lab-123" onDescriptionChange={changed} />);
    click("INFO"); expect(screen.getByText("lab-123")).toBeInTheDocument(); click("Original description");
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "Updated description" } }); fireEvent.blur(screen.getByRole("textbox"));
    expect(changed).toHaveBeenCalledWith("Updated description"); changed.mockClear();
    click("Original description"); fireEvent.keyDown(screen.getByRole("textbox"), { key: "Escape" }); expect(changed).not.toHaveBeenCalled(); expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("passes node definitions through drag data and opens project dialogs", () => {
    const order = vi.fn(), ips = vi.fn(); render(<Palette onOpenStartOrder={order} onOpenExternalIps={ips} />);
    click("Project"); const dataTransfer = { setData: vi.fn(), effectAllowed: "" };
    fireEvent.dragStart(item("Showroom"), { dataTransfer }); expect(dataTransfer.setData).toHaveBeenCalledWith("application/troshka-node", expect.any(String));
    expect(JSON.parse(dataTransfer.setData.mock.calls[0][1])).toMatchObject({ type: "showroom", label: "Showroom" }); expect(dataTransfer.effectAllowed).toBe("move");
    click("Start Order"); expect(order).toHaveBeenCalledOnce(); click("Networking"); click("External IPs"); expect(ips).toHaveBeenCalledOnce();
  });

  it("loads only ready snapshots and drags their identifiers", async () => {
    vi.mocked(fetch).mockResolvedValue({ ok: true, json: async () => [{ id: "ready", name: "Golden image", state: "ready", vm_config: { vcpus: 4, ram: 8 } }, { id: "pending", name: "Pending image", state: "building" }] } as Response);
    render(<Palette />); click("Compute"); click("Snapshots"); await screen.findByText("Golden image"); expect(screen.queryByText("Pending image")).not.toBeInTheDocument();
    const dataTransfer = { setData: vi.fn(), effectAllowed: "" }; fireEvent.dragStart(item("Golden image"), { dataTransfer });
    expect(JSON.parse(dataTransfer.setData.mock.calls[0][1])).toMatchObject({ type: "snapshot", defaults: { snapshotId: "ready" } });
    click("Snapshots"); click("Snapshots"); expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("handles failed snapshot fetches without showing stale nodes", async () => {
    vi.mocked(fetch).mockRejectedValue(new Error("offline")); render(<Palette />); click("Compute"); click("Snapshots"); expect(await screen.findByText("No snapshots available")).toBeInTheDocument();
  });

  it.each(["Auto-Stop", "Auto-Delete"])("sets presets, custom durations and disables %s", (timer) => {
    const change = vi.fn(); render(<Palette onAutoStopChange={change} onAutoDeleteChange={change} />); click("Project");
    const panel = within(item(timer)); const select = panel.getByRole("combobox");
    fireEvent.change(select, { target: { value: "60" } }); expect(change).toHaveBeenLastCalledWith(60);
    fireEvent.change(select, { target: { value: "null" } }); expect(change).toHaveBeenLastCalledWith(null);
    fireEvent.change(select, { target: { value: "-1" } });
    fireEvent.click(panel.getByRole("button", { name: "Set" })); expect(change).toHaveBeenCalledTimes(2);
    const inputs = panel.getAllByRole("spinbutton"); fireEvent.change(inputs[0], { target: { value: "2" } }); fireEvent.change(inputs[1], { target: { value: "15" } });
    fireEvent.click(panel.getByRole("button", { name: "Set" })); expect(change).toHaveBeenLastCalledWith(135); expect(panel.queryByRole("button", { name: "Set" })).not.toBeInTheDocument();
  });

  it("saves positive budgets and clears an unset budget", () => {
    const change = vi.fn(); render(<Palette budgetUsd={10} onBudgetChange={change} />); click("Project"); const input = within(item("Budget (USD)")).getByRole("spinbutton");
    expect(input).toHaveValue(10); fireEvent.change(input, { target: { value: "25.50" } }); fireEvent.blur(input); expect(change).toHaveBeenLastCalledWith(25.5);
    fireEvent.change(input, { target: { value: "" } }); fireEvent.blur(input); expect(change).toHaveBeenLastCalledWith(null);
    fireEvent.change(input, { target: { value: "-1" } }); fireEvent.blur(input); expect(change).toHaveBeenCalledTimes(2);
  });
});
