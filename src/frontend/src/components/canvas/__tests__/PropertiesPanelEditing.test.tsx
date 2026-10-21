import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import PropertiesPanel from "../PropertiesPanel";
import { useCanvasStore } from "@/stores/canvasStore";
import { appConfirm } from "@/lib/confirm";
vi.mock("@/lib/confirm", () => ({ appConfirm: vi.fn().mockResolvedValue(true) }));

const initial = useCanvasStore.getState();
const nic = { id: "nic-1", name: "eth0", mac: "52:54:00:00:00:01", model: "virtio" };
function seed(type: string, data: Record<string, unknown>) {
  useCanvasStore.setState({ ...initial, currentProjectId: null, projectState: "draft", nodes: [{ id: "subject", type, position: { x: 0, y: 0 }, data }], edges: [], selectedNodeId: "subject", clusters: [], externalIps: [], deployedNodeData: {}, deployedDiskSizes: {}, deployedClusterRows: [] });
}
function value() { return useCanvasStore.getState().nodes.find(n => n.id === "subject")!.data; }
function change(label: string | RegExp, text: string) { fireEvent.change(screen.getByLabelText(label), { target: { value: text } }); }
function section(name: string) { fireEvent.click(screen.getByText(name, { exact: true })); }
function vm(extra: Record<string, unknown> = {}) { seed("vmNode", { name: "server", os: "rhel9", vcpus: 2, ram: 4, bootDevices: [], nics: [nic], diskControllers: [{ id: "disk-1", name: "disk0", bus: "virtio" }], ...extra }); }

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => [] }));
});
afterEach(async () => { await act(async () => {}); vi.unstubAllGlobals(); vi.useRealTimers(); vi.clearAllMocks(); });

describe("PropertiesPanel persistent editing", () => {
  it("keeps an unsaved forwarding row attached to its rule when a preceding rule is deleted", () => {
    seed("networkNode", { name: "gateway", subtype: "gateway", gatewayMode: "nat-portforward", portForwards: [
      { extPort: "8080", intPort: "80", intIp: "10.0.0.10", proto: "tcp" },
      { extPort: "8443", intPort: "443", intIp: "10.0.0.11", proto: "tcp" },
    ] });
    render(<PropertiesPanel />);
    const retainedInput = screen.getAllByLabelText("Ext Port")[1];
    fireEvent.change(retainedInput, { target: { value: "9443" } });
    fireEvent.click(screen.getAllByRole("button", { name: /Remove/ })[0]);
    expect(screen.getByLabelText("Ext Port")).toBe(retainedInput);
    expect(retainedInput).toHaveValue("9443");
    fireEvent.submit(retainedInput.closest("form")!);
    expect(value().portForwards).toEqual([{ extPort: "9443", intPort: "443", intIp: "10.0.0.11", proto: "tcp", extIpId: "" }]);
    fireEvent.change(retainedInput, { target: { value: "8443" } });
    expect(retainedInput.closest("form")!.querySelector("[data-apply]")).toBeVisible();
  });
  it("validates a custom MTU before persisting and restores automatic mode", () => {
    seed("networkNode", { name: "lab", subtype: "network", cidr: "10.0.0.0/24", mtu: "auto" }); render(<PropertiesPanel />); change("MTU", "custom");
    const input = screen.getByRole("spinbutton"); fireEvent.change(input, { target: { value: "1000" } }); expect(screen.getByText(/MTU must be between 1280 and 9000/)).toBeInTheDocument(); expect(value().mtu).toBe(1500);
    fireEvent.change(input, { target: { value: "9000" } }); expect(value().mtu).toBe(9000); change("MTU", "auto"); expect(value().mtu).toBe("auto"); expect(screen.queryByRole("spinbutton")).not.toBeInTheDocument();
  });

  it("selects registry credentials for a private container image and clears them", async () => {
    seed("containerNode", { name: "app", image: "quay.io/private/app", nics: [nic] });
    vi.mocked(fetch).mockImplementation(async url => ({ ok: true, json: async () => String(url).endsWith("registry-credentials") ? [{ id: "credential", name: "Private quay", registry: "quay.io" }] : [] }) as Response);
    render(<PropertiesPanel />); await screen.findByRole("option", { name: "Private quay (quay.io)" });
    change("Registry Credential", "credential"); expect(value().registryCredentialId).toBe("credential"); change("Registry Credential", ""); expect(value().registryCredentialId).toBeNull();
  });

  it("adds and removes cluster network anchors and enables DNS on the primary network", () => {
    seed("clusterNode", { name: "ocp", clusterId: "ocp" });
    useCanvasStore.setState({ clusters: [{ id: "ocp", nodeId: "subject", name: "ocp", type: "sno", controlPlane: 1, workers: 0, baseDomain: "example.test" }], nodes: [...useCanvasStore.getState().nodes,
      { id: "management", type: "networkNode", position: { x: 0, y: 0 }, data: { name: "management", subtype: "network", cidr: "10.0.0.0/24" } },
      { id: "data", type: "networkNode", position: { x: 0, y: 0 }, data: { name: "data", subtype: "network", cidr: "10.0.1.0/24" } },
    ] }); render(<PropertiesPanel />);
    fireEvent.click(screen.getByRole("checkbox", { name: /management/ })); fireEvent.click(screen.getByRole("checkbox", { name: /^data/ }));
    expect(useCanvasStore.getState().clusters[0].networkIds).toEqual(["management", "data"]);
    expect(useCanvasStore.getState().nodes.find(n => n.id === "management")!.data.dns).toBe(true);
    expect(useCanvasStore.getState().edges).toEqual(expect.arrayContaining([expect.objectContaining({ source: "management", target: "subject", targetHandle: "cluster-net-top" }), expect.objectContaining({ source: "data", target: "subject", targetHandle: "cluster-net-bottom" })]));
    fireEvent.click(screen.getByRole("checkbox", { name: /^data/ })); expect(useCanvasStore.getState().clusters[0].networkIds).toEqual(["management"]); expect(useCanvasStore.getState().edges.some(e => e.source === "data" && e.target === "subject")).toBe(false);
  });

  it("edits cluster role disks and reassigns boot when the original disk is removed", () => {
    seed("clusterNode", { name: "ocp", clusterId: "ocp" });
    useCanvasStore.setState({ clusters: [{ id: "ocp", nodeId: "subject", name: "ocp", type: "sno", controlPlane: 1, workers: 0, baseDomain: "example.test", controlPlaneDisks: [{ sizeGb: 120, bus: "virtio", bootable: true }] }] }); render(<PropertiesPanel />);
    fireEvent.click(screen.getByRole("button", { name: "+ Add Disk" }));
    const added = screen.getAllByRole("checkbox", { name: "Boot" })[1].closest("label")!.parentElement!;
    fireEvent.change(within(added).getByRole("spinbutton"), { target: { value: "200" } }); fireEvent.change(within(added).getByRole("combobox"), { target: { value: "scsi" } });
    expect(useCanvasStore.getState().clusters[0].controlPlaneDisks![1]).toMatchObject({ sizeGb: 200, bus: "scsi", bootable: false });
    fireEvent.click(screen.getAllByRole("button", { name: "Remove disk" })[0]);
    expect(useCanvasStore.getState().clusters[0].controlPlaneDisks).toEqual([{ sizeGb: 200, bus: "scsi", bootable: true }]); expect(useCanvasStore.getState().clusters[0].controlPlaneDisk).toBe(200);
  });

  it.each([["10.0.0.1", "Already used by gateway IP"], ["10.0.0.2", "Already used by DNS server IP"], ["10.0.0.3", "Already used by other"], ["192.168.0.1", "IP not in 10.0.0.0/24"]])("validates connected NIC address %s", (ip, warning) => {
    vm(); useCanvasStore.setState({ nodes: [...useCanvasStore.getState().nodes,
      { id: "network", type: "networkNode", position: { x: 0, y: 0 }, data: { name: "lab", subtype: "network", cidr: "10.0.0.0/24", dnsServerIp: "10.0.0.2" } },
      { id: "other", type: "vmNode", position: { x: 0, y: 0 }, data: { name: "other", nics: [{ ...nic, id: "other-nic", ip: "10.0.0.3" }] } },
    ], edges: [{ id: "nic-edge", source: "network", target: "subject", targetHandle: "nic-nic-1-top" }] }); render(<PropertiesPanel />); section("Network Interfaces");
    change("IP Address (10.0.0.0/24)", ip); expect(screen.getByText(warning)).toBeInTheDocument();
    expect(value().nics).toEqual([expect.objectContaining({ ip })]);
  });

  it("adds, edits and removes user-authored DNS records", () => {
    seed("networkNode", { name: "lab", subtype: "network", cidr: "10.0.0.0/24", dns: true, dnsRecords: [{ name: "original", ip: "10.0.0.2" }] }); render(<PropertiesPanel />);
    fireEvent.change(screen.getByPlaceholderText("hostname"), { target: { value: "alias" } });
    const row = screen.getByPlaceholderText("hostname").parentElement!; fireEvent.change(within(row).getByRole("combobox"), { target: { value: "CNAME" } });
    fireEvent.change(screen.getByPlaceholderText("target"), { target: { value: "backend.lab.local" } }); expect(value().dnsRecords).toEqual([{ name: "alias", ip: "backend.lab.local", type: "CNAME" }]);
    fireEvent.click(screen.getByTitle("Add record")); expect(value().dnsRecords).toHaveLength(2);
    fireEvent.click(screen.getAllByTitle("Remove")[1]); expect(value().dnsRecords).toHaveLength(1);
  });

  it("creates gateway forwarding rules and applies edits only when submitted", () => {
    seed("networkNode", { name: "gateway", subtype: "gateway", gatewayMode: "nat-portforward" }); render(<PropertiesPanel />);
    fireEvent.click(screen.getByRole("button", { name: "+ Add Port Forward" })); fireEvent.click(screen.getByTitle("Create an external IP"));
    change("Ext Port", "8443"); change("Int Port", "443");
    const form = screen.getByLabelText("Ext Port").closest("form")!;
    fireEvent.change(form.querySelector('[name="intIp"]')!, { target: { value: "__other__" } });
    fireEvent.change(screen.getByPlaceholderText("e.g. 192.168.1.10"), { target: { value: "10.0.0.10" } });
    expect((value().portForwards as Record<string, unknown>[])[0].extPort).toBe("");
    fireEvent.submit(form); expect(value().portForwards).toEqual([expect.objectContaining({ extPort: "8443", intPort: "443", intIp: "10.0.0.10", proto: "tcp" })]);
    fireEvent.click(screen.getByRole("button", { name: /Remove/ })); expect(value().portForwards).toEqual([]);
  });

  it("allocates and removes BMC network membership when toggled", async () => {
    vi.useFakeTimers(); vm(); render(<PropertiesPanel />); section("BMC"); fireEvent.click(screen.getByLabelText("Enable BMC")); await act(async () => vi.runOnlyPendingTimersAsync());
    expect(value().bmcEnabled).toBe(true); expect(value().bmcIp).toMatch(/^\d+\.\d+\.\d+\.\d+$/); expect(useCanvasStore.getState().nodes.some(n => n.data.networkType === "bmc")).toBe(true);
    fireEvent.click(screen.getByLabelText("Enable BMC")); await act(async () => vi.runOnlyPendingTimersAsync()); expect(value()).toMatchObject({ bmcEnabled: false, bmcIp: "" });
    expect(useCanvasStore.getState().nodes.some(n => n.data.networkType === "bmc")).toBe(false);
  });

  it("reorders, removes and restores connected boot devices", () => {
    vm({ bootDevices: ["root", "network"], cloudInit: true });
    useCanvasStore.setState({ nodes: [...useCanvasStore.getState().nodes, { id: "root", type: "storageNode", position: { x: 0, y: 0 }, data: { name: "root", format: "qcow2", size: 40, bootable: true } }], edges: [{ id: "disk", source: "root", target: "subject" }] }); render(<PropertiesPanel />); section("Boot");
    fireEvent.click(screen.getAllByTitle("Move down")[0]); expect(value().bootDevices).toEqual(["network", "root"]); expect(screen.getByText(/Network boot is before disk/)).toBeInTheDocument();
    fireEvent.click(screen.getAllByTitle("Move up")[1]); expect(value().bootDevices).toEqual(["root", "network"]);
    fireEvent.click(screen.getAllByTitle("Remove from boot order")[1]); expect(value().bootDevices).toEqual(["root"]);
    fireEvent.click(screen.getByRole("button", { name: "+ Add Boot Device" })); expect(value().bootDevices).toEqual(["root", "network"]);
  });

  it("configures custom legacy PXE, iPXE and UEFI HTTP boot", () => {
    vm({ bootDevices: ["network"], pxeServerMode: "custom" }); render(<PropertiesPanel />); section("Boot");
    change("Next Server (TFTP)", "10.0.0.2"); change("Boot Filename", "pxelinux.0"); change("Boot Method", "ipxe"); change("iPXE Script URL", "http://boot.example.test/boot.ipxe"); change("Boot Method", "uefi-http"); change("Boot URL", "http://boot.example.test/grub.efi");
    expect(value()).toMatchObject({ pxeNextServer: "10.0.0.2", pxeBootFile: "pxelinux.0", pxeMethod: "uefi-http", ipxeScriptUrl: "http://boot.example.test/boot.ipxe", uefiBootUrl: "http://boot.example.test/grub.efi" });
  });

  it.each(["iso", "qcow2"])("selects a library %s and persists its image metadata", async format => {
    seed("storageNode", { name: "media", format, size: 2, source: "library" });
    vi.mocked(fetch).mockImplementation(async url => ({ ok: true, json: async () => String(url).includes("/library/") ? [{ id: "image", name: "Test image", format, size_bytes: 4 * 1024 ** 3, state: "ready" }] : [] }) as Response);
    render(<PropertiesPanel />); fireEvent.click(screen.getByRole("button", { name: /Select from Library/ })); fireEvent.click(await screen.findByText("Test image"));
    expect(value()).toMatchObject({ libraryItemId: "image", libraryItemName: "Test image", libraryItemSize: 4, source: "library", size: 4, format }); expect(screen.queryByPlaceholderText("Search...")).not.toBeInTheDocument();
  });

  it("creates, renames, edits and removes VM tags on blur", () => {
    vm(); render(<PropertiesPanel />); section("Tags"); fireEvent.click(screen.getByRole("button", { name: "+ Add tag" }));
    fireEvent.change(screen.getByPlaceholderText("Key"), { target: { value: "owner" } }); fireEvent.blur(screen.getByPlaceholderText("Key"));
    fireEvent.change(screen.getByPlaceholderText("Value"), { target: { value: "test-team" } }); fireEvent.blur(screen.getByPlaceholderText("Value")); expect(value().tags).toEqual({ owner: "test-team" });
    fireEvent.click(screen.getByTitle("Remove tag")); expect(value().tags).toEqual({});
  });

  it.each(["redeploying", "failed"])("tracks the VM redeploy response %s", async status => {
    vm(); useCanvasStore.setState({ currentProjectId: "project", deployedVmIds: new Set(["subject"]) });
    vi.mocked(fetch).mockImplementation(async url => ({ ok: true, json: async () => String(url).endsWith("/redeploy") ? { status, error: "image unavailable" } : [] }) as Response);
    render(<PropertiesPanel />); fireEvent.click(screen.getByRole("button", { name: /Redeploy This VM/ }));
    await waitFor(() => expect(fetch).toHaveBeenCalledWith("/api/v1/projects/project/vms/subject/redeploy", { method: "POST" }));
    await waitFor(() => expect(value().status).toBe(status === "redeploying" ? "redeploying" : "stopped"));
    expect(appConfirm).toHaveBeenCalledOnce(); expect(fetch).toHaveBeenCalledWith("/api/v1/projects/project/vms/subject/redeploy", { method: "POST" });
    expect(value().status).toBe(status === "redeploying" ? "redeploying" : "stopped");
    if (status === "failed") expect(await screen.findByText("Redeploy failed: image unavailable")).toBeInTheDocument();
  });

  it("reports a container redeploy failure", async () => {
    seed("containerNode", { name: "app", nics: [nic] }); useCanvasStore.setState({ currentProjectId: "project", projectState: "active" });
    vi.mocked(fetch).mockImplementation(async url => ({ ok: !String(url).endsWith("/redeploy"), json: async () => String(url).endsWith("/redeploy") ? { detail: "Registry unavailable" } : [] }) as Response);
    render(<PropertiesPanel />); fireEvent.click(screen.getByRole("button", { name: /Redeploy This Container/ }));
    expect(await screen.findByText("Registry unavailable")).toBeInTheDocument(); expect(fetch).toHaveBeenCalledWith("/api/v1/projects/project/containers/subject/redeploy", { method: "POST" });
  });

  it.each(["done", "error"])("waits for disk wipe completion before displaying %s", async status => {
    vi.useFakeTimers(); seed("storageNode", { name: "data", format: "qcow2", size: 40 });
    useCanvasStore.setState({ currentProjectId: "project", deployedVmIds: new Set(["server"]), nodes: [...useCanvasStore.getState().nodes, { id: "server", type: "vmNode", position: { x: 0, y: 0 }, data: { name: "server", status: "running" } }], edges: [{ id: "disk", source: "subject", target: "server" }] });
    vi.mocked(fetch).mockImplementation(async url => ({ ok: true, json: async () => String(url).endsWith("wipe-status") ? { status, detail: "Disk unavailable" } : [] }) as Response);
    render(<PropertiesPanel />); fireEvent.click(screen.getByRole("button", { name: "Wipe Disk" }));
    expect(screen.getByLabelText("Restart VM after wipe")).toBeChecked(); fireEvent.click(screen.getByLabelText("Restart VM after wipe"));
    fireEvent.click(screen.getByRole("button", { name: "Wipe" }));
    expect(screen.getByText(/Wiping disk/)).toBeInTheDocument(); expect(fetch).toHaveBeenCalledWith("/api/v1/projects/project/vms/server/disks/subject/wipe?restart=false", { method: "POST" });
    await act(async () => vi.advanceTimersByTimeAsync(3000));
    expect(screen.getByText(status === "done" ? 'Disk "data" wiped successfully.' : "Wipe failed: Disk unavailable")).toBeInTheDocument();
    expect(screen.queryByText(/Wiping disk/)).not.toBeInTheDocument();
  });

  it("adds and removes static router routes", () => {
    seed("networkNode", { name: "router", subtype: "router" }); render(<PropertiesPanel />);
    fireEvent.click(screen.getByRole("button", { name: "+ Add Static Route" }));
    fireEvent.change(screen.getByPlaceholderText("Destination CIDR"), { target: { value: "0.0.0.0/0" } }); fireEvent.change(screen.getByPlaceholderText("Next hop IP"), { target: { value: "10.0.0.1" } });
    expect(value().staticRoutes).toEqual([{ dest: "0.0.0.0/0", nextHop: "10.0.0.1" }]);
    fireEvent.click(within(screen.getByPlaceholderText("Destination CIDR").parentElement!).getByRole("button")); expect(value().staticRoutes).toEqual([]);
  });

  it("persists load-balancer listeners and DNS templates then removes them", () => {
    seed("networkNode", { name: "lb", subtype: "loadbalancer", networkType: "loadbalancer" }); render(<PropertiesPanel />);
    change("Internal Load Balancer IP Address", "10.0.0.5"); fireEvent.click(screen.getByLabelText("External access via EIP"));
    fireEvent.click(screen.getByRole("button", { name: "+ Add Frontend" }));
    fireEvent.change(screen.getByPlaceholderText("name"), { target: { value: "https" } }); fireEvent.change(screen.getByPlaceholderText("bind"), { target: { value: "443" } }); fireEvent.change(screen.getByPlaceholderText("back"), { target: { value: "8443" } });
    fireEvent.click(screen.getByRole("button", { name: "+ Add DNS Record" })); change("Name Template", "api.{guid}.example.test"); change("Type", "CNAME"); change("Target", "backend.example.test"); change("Default TTL", "120");
    expect(value()).toMatchObject({ lbIp: "10.0.0.5", external: false, frontends: [{ name: "https", bindPort: 443, backendPort: 8443, mode: "tcp" }], dnsRecords: [{ name: "api.{guid}.example.test", type: "CNAME", target: "backend.example.test" }], dnsTtl: 120 });
    fireEvent.click(screen.getByTitle("Remove")); fireEvent.click(screen.getByRole("button", { name: "Remove" })); expect(value()).toMatchObject({ frontends: [], dnsRecords: [] });
  });

  it("renders safe defaults for malformed Showroom metadata", () => {
    seed("containerNode", { name: "showroom", isShowroom: true, isPod: true, showroomTabs: [], initContainers: [], podContainers: [], contentRepo: {}, contentRef: {}, dnsNetwork: {} });
    render(<PropertiesPanel />);
    expect(screen.getByLabelText("Content repo")).toHaveValue("");
    expect(screen.getByLabelText("Content ref")).toHaveValue("main");
    expect(screen.getByLabelText("DNS network")).not.toHaveValue("[object Object]");
    expect(screen.queryByText("[object Object]")).not.toBeInTheDocument();
  });

  it("edits Showroom content and manages external and terminal tabs", () => {
    seed("containerNode", { name: "showroom", isShowroom: true, isPod: true, showroomTabs: [], initContainers: [], podContainers: [] }); render(<PropertiesPanel />);
    change("Content repo", "https://github.com/example/lab.git"); change("Content ref", "release");
    expect(value()).toMatchObject({ contentRepo: "https://github.com/example/lab.git", contentRef: "release" });
    fireEvent.click(screen.getByRole("button", { name: "+ External Tab" }));
    const tab = screen.getByPlaceholderText("https://...").closest("details")!; tab.open = true;
    fireEvent.change(screen.getByPlaceholderText("e.g. control"), { target: { value: "Documentation" } });
    fireEvent.change(screen.getByPlaceholderText("https://..."), { target: { value: "https://example.test/docs" } });
    expect(value().showroomTabs).toEqual([expect.objectContaining({ type: "external", name: "Documentation", url: "https://example.test/docs" })]);
    fireEvent.change(within(tab).getByRole("combobox"), { target: { value: "terminal" } });
    fireEvent.change(screen.getByPlaceholderText("cloud-user"), { target: { value: "admin" } });
    fireEvent.change(screen.getByPlaceholderText("optional"), { target: { value: "test-secret" } });
    fireEvent.change(screen.getByPlaceholderText("22"), { target: { value: "2222" } });
    expect(value().showroomTabs).toEqual([expect.objectContaining({ type: "terminal", sshUser: "admin", sshPass: "test-secret", sshPort: 2222 })]);
    fireEvent.click(screen.getByTitle("Remove tab")); expect(value().showroomTabs).toEqual([]);
    fireEvent.click(screen.getByRole("button", { name: "+ OpenShift Cluster Terminal Tab" }));
    expect(value().showroomTabs).toEqual([expect.objectContaining({ target: "clusters", type: "terminal" })]);
    expect(screen.queryByRole("button", { name: "+ OpenShift Cluster Terminal Tab" })).not.toBeInTheDocument();
  });

  it("configures a Showroom web proxy and its backend transport", () => {
    seed("containerNode", { name: "showroom", isShowroom: true, isPod: true, showroomTabs: [], initContainers: [], podContainers: [] }); render(<PropertiesPanel />);
    fireEvent.click(screen.getByRole("button", { name: "+ Web Proxy Tab" })); const tab = screen.getByPlaceholderText("/vscode/").closest("details")!; tab.open = true;
    fireEvent.change(screen.getByPlaceholderText("/vscode/"), { target: { value: "/app/" } });
    fireEvent.change(screen.getByPlaceholderText("80"), { target: { value: "8443" } });
    fireEvent.click(screen.getByRole("checkbox", { name: /^TLS to VM/ }));
    expect(value().showroomTabs).toEqual([expect.objectContaining({ type: "proxy", proxyPath: "/app/", proxyPort: 8443, proxyTls: true })]);
    const mode = within(tab).getAllByRole("combobox")[1]; fireEvent.change(mode, { target: { value: "oauth" } });
    const hosts = screen.getAllByPlaceholderText("console-openshift-console.apps.ocp.ocp.local");
    fireEvent.change(hosts[0], { target: { value: "console.apps.example.test" } }); fireEvent.change(hosts[1], { target: { value: "oauth.apps.example.test" } });
    expect(value().showroomTabs).toEqual([expect.objectContaining({ proxyHosts: ["console.apps.example.test", "oauth.apps.example.test"] })]);
    fireEvent.click(screen.getAllByTitle("Remove host")[1]); expect(value().showroomTabs).toEqual([expect.objectContaining({ proxyHosts: ["console.apps.example.test"] })]);
  });

  it.each([["Init Containers", "initContainers", "init-0"], ["Main Containers", "podContainers", "container-0"]])("adds, edits and removes %s including environment and mounts", (title, key, defaultName) => {
    seed("containerNode", { name: "pod", isPod: true, nics: [nic], initContainers: [], podContainers: [] }); render(<PropertiesPanel />);
    const editor = screen.getByText(title).closest(".props-section")!;
    fireEvent.click(within(editor as HTMLElement).getByRole("button", { name: "+ Add" }));
    const details = screen.getByText(defaultName).closest("details")!; details.open = true;
    const fields = within(details);
    fireEvent.change(fields.getByLabelText("Name"), { target: { value: "setup" } });
    fireEvent.change(fields.getByLabelText("Image"), { target: { value: "quay.io/example/setup:v1" } });
    fireEvent.change(fields.getByLabelText("Command"), { target: { value: "/bin/setup" } });
    if (key === "podContainers") {
      fireEvent.change(fields.getByLabelText("CPUs"), { target: { value: "3" } });
      fireEvent.change(fields.getByLabelText("Memory (MB)"), { target: { value: "1024" } });
      fireEvent.click(fields.getByRole("button", { name: "+ Port" }));
      fireEvent.change(fields.getByPlaceholderText("Port"), { target: { value: "8080" } });
    }
    fireEvent.click(fields.getByRole("button", { name: "+ Env Var" }));
    fireEvent.change(fields.getByPlaceholderText("KEY"), { target: { value: "MODE" } });
    fireEvent.change(fields.getByPlaceholderText("value"), { target: { value: "test" } });
    fireEvent.click(fields.getByRole("button", { name: "+ Mount" }));
    fireEvent.change(fields.getByPlaceholderText("/mount/path"), { target: { value: "/data" } });
    expect(value()[key]).toEqual([expect.objectContaining({ name: "setup", image: "quay.io/example/setup:v1", command: "/bin/setup", envVars: [{ key: "MODE", value: "test" }], mounts: [{ diskNodeId: "", mountPath: "/data" }] })]);
    for (const placeholder of ["KEY", "/mount/path"]) fireEvent.click(within(fields.getByPlaceholderText(placeholder).parentElement!).getByRole("button"));
    expect((value()[key] as Record<string, unknown>[])[0]).toMatchObject({ envVars: [], mounts: [] });
    fireEvent.click(within(details.querySelector("summary")!).getByRole("button")); expect(value()[key]).toEqual([]);
  });

  it("opens identity on the first click and generates then clears a hardware UUID", () => {
    vm(); render(<PropertiesPanel />); section("Identity");
    fireEvent.click(screen.getByTitle("Generate UUID")); expect(value().smbiosUuid).toMatch(/^[0-9a-f-]{36}$/);
    fireEvent.click(screen.getByTitle("Clear UUID (will be auto-generated at deploy)")); expect(value().smbiosUuid).toBeUndefined();
    fireEvent.change(screen.getByPlaceholderText("auto-generated at deploy"), { target: { value: "11111111-1111-1111-1111-111111111111" } });
    expect(value().smbiosUuid).toBe("11111111-1111-1111-1111-111111111111");
  });

  it("opens IO on the first click and makes EOS serial-only with a working console", () => {
    vm({ serialConsole: false }); render(<PropertiesPanel />); section("I/O"); change("Input", "usb"); change("Serial", "virtio"); change("Serial Exec CLI", "eos");
    expect(value()).toMatchObject({ inputModel: "usb", serialModel: "virtio", serialExecType: "eos", headless: true, serialConsole: true });
    expect(screen.getByRole("checkbox", { name: /^Serial only/ })).toBeDisabled();
    change("Serial Exec CLI", "linux"); fireEvent.click(screen.getByRole("checkbox", { name: /^Serial only/ })); expect(value().headless).toBeUndefined();
  });

  it("clamps disk capacity to its deployed size on blur", () => {
    seed("storageNode", { name: "data", format: "qcow2", size: 40 }); useCanvasStore.setState({ deployedDiskSizes: { subject: 40 } }); render(<PropertiesPanel />);
    const size = screen.getByRole("spinbutton"); fireEvent.change(size, { target: { value: "10" } });
    expect(screen.getByText("Cannot be smaller than 40 GB")).toBeInTheDocument(); expect(value().size).toBe(40);
    fireEvent.blur(size); expect(size).toHaveValue(40);
    fireEvent.change(size, { target: { value: "80" } }); fireEvent.blur(size); expect(value().size).toBe(80);
  });

  it.each([
    ["Range Start", "bad", "Invalid start IP"],
    ["Range End", "bad", "Invalid end IP"],
    ["Gateway IP", "bad", "Invalid gateway IP"],
    ["Range Start", "192.168.1.2", "Start IP outside subnet"],
    ["Range End", "192.168.1.2", "End IP outside subnet"],
    ["Gateway IP", "192.168.1.2", "Gateway IP outside subnet"],
    ["Range Start", "10.0.0.220", "Start must be less than end"],
    ["Gateway IP", "10.0.0.150", "Gateway conflicts with DHCP range"],
  ])("warns about invalid DHCP %s=%s while preserving the edit", (label, input, warning) => {
    seed("networkNode", { name: "lab", subtype: "network", cidr: "10.0.0.0/24", dhcp: true, dhcpRangeStart: "10.0.0.100", dhcpRangeEnd: "10.0.0.200", dhcpGateway: "10.0.0.1" }); render(<PropertiesPanel />);
    change(label, input); expect(screen.getByText(new RegExp(warning))).toBeInTheDocument(); expect(screen.getByLabelText(label)).toHaveValue(input);
  });

  it.each([["bad", "Invalid IP address"], ["192.168.1.2", "Outside the subnet"], ["10.0.0.150", "Inside the DHCP range"]])("warns about DNS IP %s", (ip, warning) => {
    seed("networkNode", { name: "lab", subtype: "network", cidr: "10.0.0.0/24", dns: true, dhcpRangeStart: "10.0.0.100", dhcpRangeEnd: "10.0.0.200" }); render(<PropertiesPanel />);
    change("DNS Server IP", ip); expect(screen.getByText(new RegExp(warning))).toBeInTheDocument(); expect(value().dnsServerIp).toBe(ip);
  });

  it("clamps Ceph OSD counts and capacity while editing the monitor address", () => {
    seed("cephClusterNode", { name: "ceph", capacityGi: 120, osdCount: 3 }); render(<PropertiesPanel />);
    change("OSD count (1–6)", "12"); expect(value().osdCount).toBe(6);
    change("OSD count (1–6)", "-1"); expect(value().osdCount).toBe(1);
    change("Capacity (Gi)", "1"); expect(Number(value().capacityGi)).toBeGreaterThan(1);
    change("Lab IP (mon)", "10.0.0.4"); expect(value().labIp).toBe("10.0.0.4");
  });

  it("edits VM identity, compute and affinity with sensible empty numeric defaults", () => {
    vm(); render(<PropertiesPanel />); section("General"); section("Compute");
    change("Name", "database"); change("OS Type", "rhel10"); change("vCPUs", "8"); change("RAM (GB)", "32");
    change("Affinity Group", "rack-a"); change("Anti-Affinity Group", "replicas");
    expect(value()).toMatchObject({ name: "database", os: "rhel10", vcpus: 8, ram: 32, affinityGroup: "rack-a", separateHost: "replicas" });
    change("vCPUs", ""); change("RAM (GB)", ""); change("Affinity Group", ""); change("Anti-Affinity Group", "");
    expect(value()).toMatchObject({ vcpus: 1, ram: 1, affinityGroup: undefined, separateHost: undefined });
    section("Compute"); expect(screen.queryByLabelText("vCPUs")).not.toBeInTheDocument();
  });

  it("clears secure boot when switching back to BIOS", () => {
    vm(); render(<PropertiesPanel />); section("Boot");
    change("Firmware", "uefi"); fireEvent.click(screen.getByLabelText("Secure Boot"));
    expect(value().secureBoot).toBe(true);
    change("Firmware", "bios"); expect(value()).toMatchObject({ firmware: "bios", secureBoot: false });
    expect(screen.queryByLabelText("Secure Boot")).not.toBeInTheDocument();
  });

  it("edits NICs and adds independent interfaces", () => {
    vm(); render(<PropertiesPanel />); section("Network Interfaces");
    change("Model", "e1000e"); change("MAC Address", "52:54:00:12:34:56");
    fireEvent.change(screen.getByDisplayValue("eth0"), { target: { value: "management" } });
    fireEvent.click(screen.getByRole("button", { name: /Add NIC/ }));
    expect(value().nics).toEqual([expect.objectContaining({ ...nic, name: "management", model: "e1000e", mac: "52:54:00:12:34:56" }), expect.objectContaining({ name: "eth1", model: "virtio" })]);
  });

  it("sets rotational defaults for SATA and removes them for virtio", () => {
    vm(); render(<PropertiesPanel />); section("Disk Controllers");
    change("Bus", "sata"); expect(value().diskControllers).toEqual([expect.objectContaining({ bus: "sata", rotationRate: 1 })]);
    change("Rotation Rate", "7200"); expect(value().diskControllers).toEqual([expect.objectContaining({ rotationRate: 7200 })]);
    change("Bus", "virtio"); expect((value().diskControllers as Record<string, unknown>[])[0]).not.toHaveProperty("rotationRate");
    fireEvent.click(screen.getByRole("button", { name: /Add Controller/ })); expect(value().diskControllers).toHaveLength(2);
  });

  it("edits cloud-init credentials, hostname and custom userdata", () => {
    vm(); render(<PropertiesPanel />); section("Cloud-Init"); fireEvent.click(screen.getByLabelText("Enabled"));
    change("Hostname", "database.local"); change("root password", "test-root"); change("cloud-user password", "test-cloud");
    change("Custom User-Data (YAML)", "#cloud-config\npackages: [vim]");
    expect(value()).toMatchObject({ ciHostname: "database.local", ciRootPassword: "test-root", ciCloudUserPassword: "test-cloud", ciUserData: "#cloud-config\npackages: [vim]" });
  });

  it("edits container image, resources, environment, ports and command", () => {
    seed("containerNode", { name: "web", image: "nginx", cpus: 1, memory: 512, nics: [nic] });
    render(<PropertiesPanel />);
    change("Image", "quay.io/example/web:v2"); change("CPUs", "4"); change("Memory (MB)", "2048");
    change("Restart Policy", "on-failure"); change("Command Override", "/bin/server");
    fireEvent.click(screen.getByRole("button", { name: /Add Variable/ }));
    fireEvent.change(screen.getByPlaceholderText("KEY"), { target: { value: "MODE" } });
    fireEvent.change(screen.getByPlaceholderText("value"), { target: { value: "test" } });
    fireEvent.click(screen.getByRole("button", { name: /Add Port/ }));
    fireEvent.change(screen.getByPlaceholderText("Container"), { target: { value: "8080" } });
    fireEvent.change(screen.getByPlaceholderText("Host (opt)"), { target: { value: "18080" } });
    expect(value()).toMatchObject({ image: "quay.io/example/web:v2", cpus: 4, memory: 2048, restartPolicy: "on-failure", command: "/bin/server", envVars: [{ key: "MODE", value: "test" }], ports: [{ containerPort: 8080, hostPort: 18080, protocol: "tcp" }] });
    const envRow = screen.getByPlaceholderText("KEY").parentElement!;
    fireEvent.click(within(envRow).getByRole("button"));
    const portRow = screen.getByPlaceholderText("Container").parentElement!;
    fireEvent.click(within(portRow).getByRole("button"));
    change("Command Override", ""); expect(value()).toMatchObject({ envVars: [], ports: [], command: null });
  });

  it("clears image metadata when changing a storage node back to blank", () => {
    seed("storageNode", { name: "root", format: "qcow2", size: 40, source: "library", libraryItemId: "image", libraryItemName: "RHEL", libraryItemSize: 20 });
    render(<PropertiesPanel />); change("Name", "data"); change("Format", "raw"); change("Source", "blank");
    expect(value()).toMatchObject({ name: "data", format: "raw", source: "blank", libraryItemId: undefined, libraryItemName: undefined, libraryItemSize: undefined });
  });

  it("configures DHCP and DNS services with persisted ranges", () => {
    seed("networkNode", { name: "lab", subtype: "network", cidr: "10.0.0.0/24" }); render(<PropertiesPanel />);
    fireEvent.click(screen.getByLabelText("DHCP")); change("Range Start", "10.0.0.100"); change("Range End", "10.0.0.200"); change("Gateway IP", "10.0.0.1"); change("Lease Time", "24h");
    fireEvent.click(screen.getByLabelText("DNS")); change("DNS Server IP", "10.0.0.2"); change("DNS Domain", "example.test"); fireEvent.click(screen.getByLabelText("Forward to upstream (internet)"));
    expect(value()).toMatchObject({ dhcp: true, dhcpRangeStart: "10.0.0.100", dhcpRangeEnd: "10.0.0.200", dhcpGateway: "10.0.0.1", dns: true, dnsServerIp: "10.0.0.2", dnsDomain: "example.test", dnsUpstream: true });
  });

  it("validates, deduplicates and removes outbound port rules", () => {
    seed("networkNode", { name: "internet", subtype: "gateway", outboundPolicy: "restrict" }); render(<PropertiesPanel />);
    change("Outbound port", "65536"); fireEvent.click(screen.getByRole("button", { name: "Add" })); expect(value().outboundPorts).toBeUndefined();
    change("Outbound protocol", "tcp"); change("Outbound port", "443"); fireEvent.keyDown(screen.getByLabelText("Outbound port"), { key: "Enter" });
    expect(value().outboundPorts).toBe("443/tcp");
    change("Outbound port", "443"); fireEvent.click(screen.getByRole("button", { name: "Add" })); expect(value().outboundPorts).toBe("443/tcp");
    change("Outbound protocol", "icmp"); fireEvent.click(screen.getByRole("button", { name: "Add" })); expect(value().outboundPorts).toBe("443/tcp,icmp");
    fireEvent.click(screen.getByRole("button", { name: /Remove.*443/ })); expect(value().outboundPorts).toBe("icmp");
    change("Mode", "nat-portforward"); expect(screen.getByText(/No external IPs allocated/)).toBeInTheDocument();
  });
});
