import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ProjectCanvasPage from "./page";
import { exampleUsage } from "@/components/quotas/test-fixtures";

const harness = vi.hoisted(() => ({
  store: {} as Record<string, any>, socket: {} as Record<string, any>,
  push: vi.fn(), confirm: vi.fn(), save: vi.fn(),
  projectId: "project-1",
}));
vi.mock("next/navigation", () => ({ useParams: () => ({ id: harness.projectId }), useRouter: () => ({ push: harness.push }) }));
vi.mock("@xyflow/react", () => ({ ReactFlowProvider: ({ children }: any) => children }));
vi.mock("@/hooks/useVmStateSocket", () => ({ useVmStateSocket: () => harness.socket }));
vi.mock("@/lib/confirm", () => ({ appConfirm: harness.confirm }));
vi.mock("@/stores/canvasStore", () => ({
  useCanvasStore: Object.assign((selector: any) => selector(harness.store), {
    getState: () => harness.store,
    setState: (patch: any) => Object.assign(harness.store, patch),
  }),
  computeTopologyDirty: () => false, computeTopologyDiff: () => [],
  setLatestVmStates: vi.fn(), setLatestContainerStates: vi.fn(),
  _saveTopologyToApi: harness.save, applyDeployedTopologyFromServer: vi.fn(),
  resetClustersForOcpRedeploy: (clusters: any[]) => clusters,
}));
vi.mock("@/components/canvas/Canvas", () => ({ default: () => <div>Topology canvas</div> }));
vi.mock("@/components/canvas/Palette", () => ({ default: ({ canEditBudget, onBudgetChange }: { canEditBudget?: boolean; onBudgetChange?: (value: string | null) => void }) => <div>Project palette<button disabled={!canEditBudget} onClick={() => onBudgetChange?.("11")}>Save palette budget</button></div> }));
vi.mock("@/components/canvas/PropertiesPanel", () => ({ default: () => <div>Node properties</div> }));
vi.mock("@/components/canvas/StartOrderPanel", () => ({ default: () => null }));
vi.mock("@/components/canvas/ExternalIpsPanel", () => ({ default: () => null }));
vi.mock("@/components/canvas/SavePatternModal", () => ({ default: ({ onSaved, onClose }: any) => <div><button onClick={() => onSaved("pattern", false)}>Save pattern result</button><button onClick={onClose}>Close pattern</button></div> }));
vi.mock("@/components/canvas/RunWorkloadModal", () => ({ default: ({ onLaunched, onClose }: any) => <div><button onClick={() => onLaunched(["run-1"])}>Launch workload result</button><button onClick={onClose}>Close workload</button></div> }));
vi.mock("@/components/canvas/WorkloadRunsModal", () => ({ default: ({ onClose }: any) => <button onClick={onClose}>Close runs</button> }));
vi.mock("@/components/canvas/WorkloadRunDetailModal", () => ({ default: ({ runId, onClose }: any) => <button onClick={onClose}>Close run {runId}</button> }));
vi.mock("@/components/canvas/SnapshotVMModal", () => ({ default: () => null }));
vi.mock("@/components/canvas/ReconfigureWarningModal", () => ({ default: ({ changes, onConfirm, onCancel }: { changes: Array<{ type: string; storageName: string; vmName: string }>; onConfirm: (ids: string[]) => void; onCancel: () => void }) => <section aria-label="Review storage changes">{changes.map((change) => <p key={change.storageName}>{change.type}: {change.storageName} on {change.vmName}</p>)}<button onClick={() => onConfirm(["vm-1"])}>Confirm storage changes</button><button onClick={onCancel}>Cancel storage changes</button></section> }));

const base = "/api/v1/projects/project-1";
const response = (data: unknown, status = 200) => ({ ok: status < 400, status, json: async () => data, text: async () => String(data) }) as Response;
let project: Record<string, any>;
let routes: Record<string, Response | Error>;
let calls: ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.clearAllMocks();
  harness.projectId = "project-1";
  harness.confirm.mockResolvedValue(true);
  harness.save.mockResolvedValue(undefined);
  harness.store = {
    nodes: [{ id: "vm-1", type: "vmNode", data: { name: "server", status: "running" } }],
    edges: [], clusters: [], clusterOcpPhases: {}, externalIps: [], deployedClusterRows: [],
    topologyWorkloads: [], topologyPlacement: {}, hiddenNodeIds: [], startOrder: [],
    currentProjectId: "project-1", loadProject: vi.fn().mockResolvedValue(undefined),
    setAllVmStatus: vi.fn(), setExternalIps: vi.fn(), updateVmStates: vi.fn(),
    updateContainerStates: vi.fn(), topologyDirty: false,
  };
  harness.socket = { vmStates: {}, containerStates: {}, clusterOcpPhases: {}, connected: true };
  project = { id: "project-1", name: "Test environment", state: "draft", owner_email: "alice@example.com" };
  routes = { "/api/v1/auth/me": response({ role: "user" }), [`${base}/metering`]: response({}, 404), [`${base}/workloads`]: response([]), "/api/v1/quotas/project/project-1/usage": new Response(JSON.stringify({ ...exampleUsage, scope: "project", scope_id: "project-1", reset_at: null, can_edit: false })) };
  calls = vi.fn(async (url: string, options?: RequestInit) => {
    const result = routes[`${options?.method || "GET"} ${url}`] || routes[url];
    if (result instanceof Error) throw result;
    if (result) return typeof result.clone === "function" ? result.clone() : result;
    if (url === base) return response(project);
    if (url.endsWith("/vm-states")) return response({ vm_states: {}, container_states: {} });
    return response({});
  });
  vi.stubGlobal("fetch", calls);
  localStorage.clear();
  vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:template");
  vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });
async function loaded(state = "draft") {
  project.state = state;
  const view = render(<ProjectCanvasPage />);
  await screen.findByText("Test environment");
  return view;
}
function click(name: string | RegExp) { fireEvent.click(screen.getByRole("button", { name })); }

describe("project canvas navigation and deployment", () => {
  it("preserves stopped state and explains structured start rejection", async () => {
    routes[`POST ${base}/start`] = response({ detail: { reason: "missing_organization" } }, 409);
    await loaded("stopped");
    click("▶ Start");
    expect(await screen.findByText(/Ask a platform administrator to assign/)).toBeVisible();
    expect(screen.getByRole("button", { name: "▶ Start" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Undeploy" })).toBeEnabled();
  });
  it("uses server quota edit permission and displays project lifetime usage", async () => {
    routes["/api/v1/quotas/project/project-1/usage"] = new Response(JSON.stringify({ ...exampleUsage, scope: "project", scope_id: "project-1", can_edit: false, reset_at: null }));
    await loaded();
    expect(screen.queryByRole("dialog", { name: "Project quotas" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Quotas" }));
    expect(screen.getByRole("dialog", { name: "Project quotas" })).toBeVisible();
    expect(await screen.findByText(/Lifetime spending/)).toBeVisible();
    expect(screen.getByLabelText("CPU limit")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Save limits", hidden: true })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    expect(screen.queryByRole("dialog", { name: "Project quotas" })).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Quotas" })).toHaveFocus());
  });
  it("loads metadata, renames, toggles panels and returns to projects", async () => {
    await loaded();
    expect(document.title).toBe("Test environment — Troshka");
    expect(screen.getByTitle("Owned by alice@example.com")).toHaveTextContent("alice");
    fireEvent.click(screen.getByTitle("Hide palette"));
    expect(screen.queryByText("Project palette")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTitle("Show palette"));
    fireEvent.click(screen.getByTitle("Hide properties"));
    expect(screen.queryByText("Node properties")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTitle("Show properties"));
    vi.spyOn(window, "prompt").mockReturnValue("Renamed");
    fireEvent.click(screen.getByTitle("Click to rename"));
    await screen.findByText("Renamed");
    expect(calls).toHaveBeenCalledWith(base, expect.objectContaining({ method: "PATCH", body: JSON.stringify({ name: "Renamed" }) }));
    fireEvent.click(screen.getByTitle("Back to projects"));
    expect(harness.push).toHaveBeenCalledWith("/projects");
  });
  it("redirects missing projects", async () => {
    routes[base] = response({}, 404);
    render(<ProjectCanvasPage />);
    await waitFor(() => expect(harness.push).toHaveBeenCalledWith("/projects"));
  });
  it("rejects deploying an empty canvas", async () => {
    harness.store.nodes = [];
    await loaded(); click(/Deploy$/);
    expect(await screen.findByText("Add at least one VM or container before publishing.")).toBeInTheDocument();
    expect(harness.confirm).not.toHaveBeenCalled();
  });
  it("saves topology before deployment and displays distributed placement", async () => {
    routes[`${base}/deploy`] = response({ multi_host: true, host_count: 2 });
    await loaded(); click(/Deploy$/);
    expect(await screen.findByText("Deploying across 2 hosts")).toBeInTheDocument();
    expect(harness.save).toHaveBeenCalled();
    expect(calls).toHaveBeenCalledWith(`${base}/deploy`, { method: "POST" });
  });
  it.each([false, true])("returns to draft for deployment failure, network=%s", async (network) => {
    routes[`${base}/deploy`] = network ? new Error("offline") : response({ detail: "No capacity" }, 409);
    await loaded(); click(/Deploy$/);
    expect(await screen.findByText(network ? "Failed to connect to server" : "No capacity")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Deploy$/ })).toBeEnabled();
  });
  it("honors deployment cancellation", async () => {
    harness.confirm.mockResolvedValue(false);
    await loaded(); click(/Deploy$/);
    await waitFor(() => expect(harness.confirm).toHaveBeenCalled());
    expect(harness.save).not.toHaveBeenCalled();
  });
});

describe("environment lifecycle", () => {
  it.each(["stopped", "error"])("shows accounting unavailability on %s starts without optimistic state", async state => {
    project.deployed_topology = { nodes: harness.store.nodes, edges: [] };
    routes[`${base}/start`] = response({ detail: { reason: "accounting_unavailable" } }, 409);
    await loaded(state); click(state === "error" ? /Retry Start/ : /Start$/);
    expect(await screen.findByText(/Accounting unavailable\. New growth is blocked/)).toBeVisible();
    expect(screen.queryByText("Starting...")).not.toBeInTheDocument();
  });
  it("preserves stopped state when republish is denied", async () => {
    routes[`${base}/redeploy`] = response({ detail: { reason: "unsupported_quota_operation" } }, 409);
    await loaded("stopped"); click(/Republish/);
    expect(await screen.findByText(/This operation is not supported/)).toBeVisible();
    expect(screen.getByRole("button", { name: "▶ Start" })).toBeEnabled();
  });
  it("shows unsupported per-VM resume denial while keeping stop available", async () => {
    harness.store.nodes = [{ id: "paused", type: "vmNode", data: { name: "paused", status: "paused" } }];
    routes[`${base}/vms/paused/unpause`] = response({ detail: { reason: "unsupported_quota_operation" } }, 409);
    await loaded("active"); click(/Start$/);
    expect(await screen.findByText(/This operation is not supported with quota enforcement/)).toBeVisible();
    expect(screen.getByRole("button", { name: /Stop$/ })).toBeEnabled();
  });
  it.each(["draft", "stopped", "active"])("formats a binding constraint on %s growth operations", async state => {
    harness.store.topologyDirty = true;
    const denial = response({ detail: { reason: "resource_limit", scope: "organization", dimension: "ram_bytes", used: "8", reserved: "2", requested: "4", limit: "10" } }, 409);
    routes[`${base}/${state === "draft" ? "deploy" : "reconfigure"}`] = denial;
    await loaded(state); click(state === "draft" ? /Deploy$/ : "Apply Changes");
    expect(await screen.findByText(/organization RAM bytes limit: used 8 \+ reserved 2, requested 4, limit 10/)).toBeVisible();
    expect(screen.queryByText("reconfiguring")).not.toBeInTheDocument();
  });
  it("confirms stopping an active environment", async () => {
    await loaded("active"); click(/Stop$/);
    await screen.findByText("Stopping...");
    expect(calls).toHaveBeenCalledWith(`${base}/stop`, { method: "POST" });
  });
  it.each(["message", "code", "string"])("shows structured off failure: %s", async (kind) => {
    routes[`${base}/stop`] = response({ detail: kind === "string" ? "Cannot stop" : { [kind]: "Cannot stop" } }, 409);
    await loaded("active"); click(/Stop$/);
    expect(await screen.findByText("Cannot stop")).toBeInTheDocument();
    expect(screen.getByText("active")).toBeInTheDocument();
  });
  it("does not stop when confirmation is cancelled", async () => {
    harness.confirm.mockResolvedValue(false);
    await loaded("active"); click(/Stop$/);
    await waitFor(() => expect(harness.confirm).toHaveBeenCalled());
    expect(calls.mock.calls.some(([url]) => url === `${base}/stop`)).toBe(false);
  });
  it("changes off mode and preserves active state after pause", async () => {
    await loaded("active"); click("Choose off mode"); click(/Pause$/);
    await waitFor(() => expect(calls).toHaveBeenCalledWith(base, expect.objectContaining({ body: JSON.stringify({ off_action: "pause" }) })));
    click(/Pause$/);
    await waitFor(() => expect(calls).toHaveBeenCalledWith(`${base}/stop`, { method: "POST" }));
    expect(screen.getByText("active")).toBeInTheDocument();
  });
  it("rolls back an unsuccessful mode preference", async () => {
    routes[`PATCH ${base}`] = response({}, 500);
    await loaded("active"); click("Choose off mode"); click(/Pause$/);
    expect(await screen.findByRole("button", { name: /Stop$/ })).toBeEnabled();
  });
  it("disables hibernation when the provider cannot support it", async () => {
    project.supports_hibernate = false;
    await loaded("active"); click("Choose off mode");
    expect(screen.getByRole("button", { name: /Hibernate$/ })).toBeDisabled();
    fireEvent.mouseDown(document.body);
    expect(screen.queryByRole("button", { name: /Hibernate$/ })).not.toBeInTheDocument();
  });
  it("starts a stopped environment", async () => {
    await loaded("stopped"); click(/Start$/);
    expect(await screen.findByText("Starting...")).toBeInTheDocument();
    expect(calls).toHaveBeenCalledWith(`${base}/start`, { method: "POST" });
  });
  it("undeploys back to editable draft", async () => {
    await loaded("stopped"); click("Undeploy");
    expect(await screen.findByRole("button", { name: /Deploy$/ })).toBeInTheDocument();
    expect(calls).toHaveBeenCalledWith(`${base}/undeploy`, { method: "POST" });
  });
  it("applies stopped topology changes", async () => {
    routes[`${base}/reconfigure`] = response({ status: "reconfiguring" });
    await loaded("stopped"); click("Apply Changes");
    expect(await screen.findByText("reconfiguring")).toBeInTheDocument();
    expect(calls).toHaveBeenCalledWith(base, expect.objectContaining({ method: "PATCH" }));
  });
  it("opens pattern and workload dialogs and handles completion", async () => {
    await loaded("active"); click("Save as Pattern"); click("Save pattern result");
    expect(screen.getByText("Pattern saved successfully")).toBeInTheDocument();
    click("Run Workload"); click("Launch workload result");
    click("Close run run-1"); click("Workloads"); click("Close runs");
    expect(screen.queryByText("Close runs")).not.toBeInTheDocument();
  });
});

describe("portable template export", () => {
  it.each(["current", "custom", "none"])("exports %s password mode and cleans up its download", async (mode) => {
    await loaded(); click("Export Template");
    if (mode === "custom") {
      fireEvent.click(screen.getByLabelText("Set a single password for all"));
      expect(screen.getByRole("button", { name: "Download YAML" })).toBeDisabled();
      fireEvent.change(screen.getByPlaceholderText("Enter password"), { target: { value: "example-password" } });
    }
    if (mode === "none") fireEvent.click(screen.getByLabelText("Omit all passwords"));
    fireEvent.click(screen.getByLabelText("Include library item IDs"));
    click("Download YAML");
    await waitFor(() => expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:template"));
    expect(calls).toHaveBeenCalledWith(`${base}/export-template`, expect.objectContaining({ body: JSON.stringify({ password_mode: mode, include_ids: true, ...(mode === "custom" ? { custom_password: "example-password" } : {}) }) }));
  });
});

describe("template import", () => {
  async function openImport(yaml: string) {
    harness.store.nodes = [];
    routes["/api/v1/auth/ssh-keys"] = response([{ id: "key-1", name: "Laptop" }]);
    await loaded(); click("Import Template YAML");
    fireEvent.change(screen.getByRole("textbox", { name: "" }), { target: { value: yaml } });
  }
  it.each([
    ["[broken", "Invalid YAML syntax"],
    ["hello", "Template must be a YAML mapping"],
    ["networks: {}", "Template must contain 'networks' and either 'vms' or 'ocp' sections"],
  ])("validates input before making import requests: %s", async (yaml, error) => {
    await openImport(yaml); click("Import");
    expect(await screen.findByText(error)).toBeInTheDocument();
    expect(calls.mock.calls.some(([url]) => url.endsWith("/import-template"))).toBe(false);
  });
  it("imports credentials and topology without deploying when unchecked", async () => {
    await openImport("networks: {}\nvms: []");
    await screen.findByRole("option", { name: "Laptop" });
    fireEvent.change(screen.getByLabelText("SSH Key"), { target: { value: "key-1" } });
    fireEvent.change(screen.getByPlaceholderText("Used for console login"), { target: { value: "test-password" } });
    fireEvent.click(screen.getByLabelText("Deploy immediately after import"));
    click("Import");
    await waitFor(() => expect(calls).toHaveBeenCalledWith(`${base}/import-template`, expect.objectContaining({ body: JSON.stringify({ template_yaml: { networks: {}, vms: [] }, common_password: "test-password", bastion_ssh_key_id: "key-1" }) })));
    expect(calls.mock.calls.some(([url]) => url.endsWith("/deploy"))).toBe(false);
  });
  it("reports backend import failures", async () => {
    routes[`${base}/import-template`] = response({ detail: "Missing library item" }, 422);
    await openImport("networks: {}\nvms: []"); click("Import");
    expect(await screen.findByText("Missing library item")).toBeInTheDocument();
  });
  it("surfaces nonblocking warnings and an auto-deploy failure", async () => {
    const alert = vi.spyOn(window, "alert").mockImplementation(() => {});
    routes[`${base}/import-template`] = response({ warnings: ["IP overlaps gateway"] });
    routes[`${base}/deploy`] = response({ detail: "No hosts" }, 409);
    await openImport("networks: {}\nocp: []"); click("Import");
    expect(await screen.findByText("No hosts")).toBeInTheDocument();
    expect(alert).toHaveBeenCalledWith("Imported with warnings:\n\nIP overlaps gateway");
  });
  it("formats accounting failure from template auto-deploy", async () => {
    routes[`${base}/import-template`] = response({});
    routes[`${base}/deploy`] = response({ detail: { reason: "accounting_unavailable" } }, 409);
    await openImport("networks: {}\nocp: []"); click("Import");
    expect(await screen.findByText(/Accounting unavailable\. New growth is blocked/)).toBeVisible();
    expect(screen.queryByText("Deploying...")).not.toBeInTheDocument();
  });
});

describe("live project synchronization", () => {
  it("extends a warning timer by one hour", async () => {
    harness.socket.timerWarning = { timer: "auto_stop", minutes_remaining: 5 };
    await loaded("active"); click("Extend 1h");
    expect(calls).toHaveBeenCalledWith(`${base}/extend-timer`, expect.objectContaining({ body: JSON.stringify({ timer: "auto_stop", add_minutes: 60 }) }));
    expect(screen.queryByRole("button", { name: "Extend 1h" })).not.toBeInTheDocument();
  });
  it("preserves gateway endpoints when topology frames omit them", async () => {
    const endpoints = [{ name: "ssh", port: 22 }];
    harness.store.nodes = [{ id: "gateway", type: "networkNode", data: { subtype: "gateway", externalEndpoints: endpoints } }];
    harness.socket.topologyUpdate = { nodes: [{ id: "gateway", type: "networkNode", data: { subtype: "gateway", name: "updated" } }], edges: [] };
    await loaded();
    expect(harness.store.nodes[0].data).toMatchObject({ name: "updated", externalEndpoints: endpoints });
  });
  it("merges external IPs without discarding allocation when an update omits it", async () => {
    harness.store.externalIps = [{ id: "one", name: "Existing", ip: "192.0.2.1", state: "allocated" }];
    harness.socket.externalIpsUpdate = [
      { id: "one", name: "Updated", state: "unknown" },
      { id: "two", name: "New", ip: "192.0.2.2", state: "associated", _private_ip: "10.0.0.2" },
    ];
    await loaded();
    expect(harness.store.setExternalIps).toHaveBeenCalledWith([
      { id: "one", name: "Updated", ip: "192.0.2.1", state: "allocated" },
      { id: "two", name: "New", ip: "192.0.2.2", state: "associated", _private_ip: "10.0.0.2" },
    ]);
  });
  it("records both VM and container identities from REST polling", async () => {
    routes[`${base}/vm-states`] = response({ states: { "vm-1": "running" }, container_states: { "pod-1": "running" } });
    await loaded("active");
    await waitFor(() => expect(harness.store.deployedVmIds).toEqual(new Set(["vm-1", "pod-1"])));
  });
  it.each([["failed", "error"], ["cancelled", "error"], ["timeout", "error"], ["complete", "ready"], ["waiting", "monitoring"]])("mirrors OCP phase %s into cluster status", async (phase, expectedStatus) => {
    harness.store.clusters = [{ id: "cluster", name: "Cluster", ocpInstallStatus: "pending" }];
    harness.socket.clusterOcpPhases = { cluster: phase };
    await loaded("active");
    expect(harness.store.clusters[0].ocpInstallStatus).toBe(expectedStatus);
  });
  it.each(["ready", "error"])("does not demote terminal %s clusters on stale progress", async (status) => {
    harness.store.clusters = [{ id: "cluster", ocpInstallStatus: status }];
    harness.socket.clusterOcpPhases = { cluster: "waiting" };
    await loaded("active");
    expect(harness.store.clusters[0].ocpInstallStatus).toBe(status);
  });
  it("redirects when a socket announces project deletion", async () => {
    harness.socket.deleted = true;
    await loaded();
    expect(harness.push).toHaveBeenCalledWith("/projects");
  });
  it.each(["auto_stop", "auto_delete"])("handles timer event %s", async (event) => {
    const view = await loaded("active");
    harness.socket = { ...harness.socket, timerFired: event };
    view.rerender(<ProjectCanvasPage />);
    if (event === "auto_stop") expect(screen.getByText("Stopping...")).toBeInTheDocument();
    else expect(harness.push).toHaveBeenCalledWith("/projects");
  });
  it.each([60, 600, 7200, -60])("renders shutdown countdown at %s seconds", async (seconds) => {
    project.auto_stop_expires_at = new Date(Date.now() + seconds * 1000).toISOString();
    await loaded("active");
    const timer = await screen.findByTitle("Time remaining (click to open Project settings)");
    expect(timer).toHaveTextContent(seconds < 0 ? "Auto-Shutdown" : "Shutdown in");
  });
  it("blocks disruptive operations while an install is in progress", async () => {
    project.ocp_status = "monitoring";
    await loaded("active");
    expect(screen.getByRole("button", { name: "Save as Pattern" })).toBeDisabled();
    expect(screen.getByRole("button", { name: /Republish/ })).toBeDisabled();
  });
  it("shows finalized install failures without blocking recovery", async () => {
    project.ocp_status = "monitoring";
    project.deployed_topology = { clusters: [{ ocpInstallStatus: "error" }] };
    await loaded("active");
    expect(screen.getByText("install failed")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Republish/ })).toBeEnabled();
  });
});

describe("reconfiguration and budget", () => {
  it.each([["badge", 403], ["badge", 404], ["palette", 403], ["palette", 404]] as const)("keeps %s budget writes revoked after a newer %i despite a late save refresh", async (source, status) => {
    const usageUrl = "/api/v1/quotas/project/project-1/usage";
    const authorized = { ...exampleUsage, scope: "project", scope_id: "project-1", reset_at: null };
    let finish: (response: Response) => void = () => {};
    let quotaCalls = 0;
    const prior = vi.mocked(fetch).getMockImplementation()!;
    calls.mockImplementation(async (url: string, options?: RequestInit) => {
      if (url === usageUrl) {
        quotaCalls++;
        if (quotaCalls === 2) return new Promise<Response>(resolve => { finish = resolve; });
        if (quotaCalls >= 3) return new Response('{"detail":"Revoked"}', { status });
        return new Response(JSON.stringify(authorized));
      }
      if (url === "/api/v1/quotas/project/project-1" && options?.method === "PATCH") return new Response(JSON.stringify(authorized.limits));
      return prior(url, options);
    });
    routes[`${base}/metering`] = response({ total_usd: 1.25, budget_usd: 10 });
    await loaded("stopped");
    await waitFor(() => expect(screen.getByRole("button", { name: "Save palette budget" })).toBeEnabled());
    if (source === "badge") { fireEvent.click(screen.getByTitle("$1.25 lifetime spend — click to set budget")); click("Save"); }
    else click("Save palette budget");
    await waitFor(() => expect(quotaCalls).toBe(2));
    fireEvent.click(screen.getByRole("button", { name: "Quotas" }));
    fireEvent.click(screen.getByRole("button", { name: "Refresh quotas" }));
    await screen.findByText(/Quota usage unavailable: Revoked/);
    await act(async () => { finish(new Response(JSON.stringify(authorized))); });
    expect(screen.queryByLabelText("CPU limit")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save palette budget" })).toBeDisabled();
    expect(screen.getByTitle("$1.25 lifetime spend — owner budget is read-only")).toBeDisabled();
    expect(screen.queryByLabelText("Stop the project at this spend (USD)")).not.toBeInTheDocument();
    expect(quotaCalls).toBe(3);
  });
  it("discards the prior project's pending budget refresh after project navigation", async () => {
    const authorized = { ...exampleUsage, scope: "project", scope_id: "project-1", reset_at: null };
    let finish: (response: Response) => void = () => {};
    let quotaCalls = 0;
    let nextProjectCalls = 0;
    const prior = vi.mocked(fetch).getMockImplementation()!;
    calls.mockImplementation(async (url: string, options?: RequestInit) => {
      if (url === "/api/v1/quotas/project/project-1/usage") {
        if (++quotaCalls === 2) return new Promise<Response>(resolve => { finish = resolve; });
        return new Response(JSON.stringify(authorized));
      }
      if (url === "/api/v1/quotas/project/project-1" && options?.method === "PATCH") return new Response(JSON.stringify(authorized.limits));
      if (url === "/api/v1/quotas/project/project-2/usage") { nextProjectCalls++; return new Response(JSON.stringify({ ...authorized, scope_id: "project-2", can_edit: false })); }
      return prior(url, options);
    });
    const view = await loaded("stopped");
    await waitFor(() => expect(screen.getByRole("button", { name: "Save palette budget" })).toBeEnabled());
    click("Save palette budget");
    await waitFor(() => expect(quotaCalls).toBe(2));
    harness.projectId = "project-2";
    routes["/api/v1/projects/project-2"] = response({ ...project, id: "project-2", name: "Second environment" });
    routes["/api/v1/projects/project-2/metering"] = response({}, 404);
    routes["/api/v1/projects/project-2/workloads"] = response([]);
    routes["/api/v1/quotas/project/project-2/usage"] = new Response(JSON.stringify({ ...authorized, scope_id: "project-2", can_edit: false }));
    view.rerender(<ProjectCanvasPage />);
    await screen.findByText("Second environment");
    await waitFor(() => expect(nextProjectCalls).toBe(1));
    await act(async () => { finish(new Response(JSON.stringify(authorized))); });
    expect(screen.getByRole("button", { name: "Save palette budget" })).toBeDisabled();
    expect(quotaCalls).toBe(2);
    expect(nextProjectCalls).toBe(1);
  });
  it.each(["iso", "qcow2"])("reviews %s replacement before restarting the attached VM", async (format) => {
    harness.store.topologyDirty = true;
    harness.store.nodes.push({ id: "disk", type: "storageNode", data: { name: "Root disk", format, libraryItemId: "replacement" } });
    harness.store.edges = [{ source: "disk", target: "vm-1" }];
    project.deployed_topology = { nodes: [{ id: "disk", type: "storageNode", data: { libraryItemId: "original" } }] };
    routes[`${base}/reconfigure`] = response({ status: "reconfiguring" });
    await loaded("active"); click("Apply Changes");
    expect(await screen.findByText(`${format === "iso" ? "iso" : "disk"}: Root disk on server`)).toBeInTheDocument();
    expect(calls.mock.calls.some(([url]) => url === `${base}/reconfigure`)).toBe(false);
    click("Confirm storage changes");
    await waitFor(() => expect(calls).toHaveBeenCalledWith(`${base}/reconfigure`, expect.objectContaining({ body: JSON.stringify({ restart_vm_ids: ["vm-1"] }) })));
  });
  it.each([true, false])("republishes an environment and handles success=%s", async (success) => {
    routes[`${base}/redeploy`] = response({ detail: "Redeploy rejected" }, success ? 200 : 409);
    await loaded("active"); click(/Republish/);
    await waitFor(() => expect(calls).toHaveBeenCalledWith(`${base}/redeploy`, expect.objectContaining({ method: "POST" })));
    if (success) expect(harness.store.deployedVmIds).toEqual(new Set());
    else expect(await screen.findByText("Redeploy rejected")).toBeInTheDocument();
  });
  it.each(["success", "rejected", "offline"])("applies active topology: %s", async (result) => {
    harness.store.topologyDirty = true;
    const payload = result === "success" ? { status: "reconfiguring" } : { errors: ["Disk unavailable"] };
    routes[`${base}/reconfigure`] = result === "offline" ? new Error("offline") : response(payload);
    await loaded("active"); click("Apply Changes");
    await waitFor(() => expect(calls).toHaveBeenCalledWith(`${base}/reconfigure`, expect.objectContaining({ method: "POST" })));
    if (result === "success") expect(await screen.findByText("reconfiguring")).toBeInTheDocument();
    else {
      expect(await screen.findByRole("alert")).toHaveTextContent(result === "offline" ? "Failed to connect to server" : "Disk unavailable");
      click("Dismiss");
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Apply Changes" })).toBeEnabled();
    }
  });
  it.each(["25", ""])("saves budget %s from live spend", async (value) => {
    routes["/api/v1/quotas/project/project-1/usage"] = new Response(JSON.stringify({ ...exampleUsage, scope: "project", scope_id: "project-1", reset_at: null }));
    routes["PATCH /api/v1/quotas/project/project-1"] = new Response(JSON.stringify(exampleUsage.limits));
    routes[`${base}/metering`] = response({ total_usd: 1.25, budget_usd: 10, running_since: "2026-10-01T00:00:00Z", running_until: "2026-10-02T02:03:00Z", running_seconds: 93780 });
    await loaded("stopped");
    const badge = await screen.findByTitle("$1.25 lifetime spend — click to set budget");
    await waitFor(() => expect(badge).toBeEnabled());
    fireEvent.click(badge);
    fireEvent.change(screen.getByLabelText("Stop the project at this spend (USD)"), { target: { value } });
    click("Save");
    await waitFor(() => expect(calls).toHaveBeenCalledWith("/api/v1/quotas/project/project-1", expect.objectContaining({ method: "PATCH", body: JSON.stringify({ spending_usd: value || null }) })));
  });
  it("resumes paused and hibernated VMs with distinct operations", async () => {
    harness.store.nodes = [
      { id: "paused", type: "vmNode", data: { name: "paused", status: "paused" } },
      { id: "sleeping", type: "vmNode", data: { name: "sleeping", status: "hibernated" } },
    ];
    await loaded("active"); click(/Start$/);
    await waitFor(() => expect(calls).toHaveBeenCalledWith(`${base}/vms/paused/unpause`, { method: "POST" }));
    expect(calls).toHaveBeenCalledWith(`${base}/vms/sleeping/start`, { method: "POST" });
  });
  it("deletes after confirmation and clears the deletion marker", async () => {
    await loaded(); click("Delete");
    const buttons = screen.getAllByRole("button", { name: "Delete" });
    fireEvent.click(buttons[buttons.length - 1]);
    await waitFor(() => expect(calls).toHaveBeenCalledWith(base, { method: "DELETE" }));
    await waitFor(() => expect(localStorage.getItem("troshka-deleting-projects")).toBe("[]"));
    expect(harness.push).toHaveBeenCalledWith("/projects");
  });
});

describe("administrator host placement", () => {
  const host = (id: string, pool = "shared") => ({ id, storage_pool_id: pool, instance_id: id, ip_address: "192.0.2.1", state: "active", agent_status: "connected", provider_id: "provider-1", provider_type: "aws", used_vcpus: 2, total_vcpus: 16, used_ram_mb: 2048, total_ram_mb: 32768 });
  beforeEach(() => {
    routes["/api/v1/auth/me"] = response({ role: "admin" });
    routes["/api/v1/providers/"] = response([{ id: "provider-1", name: "AWS Demo" }]);
    routes["/api/v1/hosts/"] = response([host("source"), host("target"), host("other", "different"), { ...host("disabled"), accepting_work: false }]);
    project.host_id = "source";
  });
  it.each(["target", "provider:provider-1"])("passes selected placement %s to deploy", async (target) => {
    await loaded();
    await screen.findByRole("option", { name: "Auto (AWS Demo)" });
    fireEvent.change(screen.getByRole("combobox"), { target: { value: target } });
    click(/Deploy$/);
    const query = target.startsWith("provider:") ? "provider_id=provider-1" : "host_id=target";
    await waitFor(() => expect(calls).toHaveBeenCalledWith(`${base}/deploy?${query}`, { method: "POST" }));
  });
  it.each([true, false])("migrates within the same pool, success=%s", async (success) => {
    routes[`${base}/migrate`] = response({ detail: "Migration unavailable" }, success ? 200 : 409);
    await loaded("active"); click("Migrate");
    await screen.findByText("Migrate Project");
    const destination = screen.getByLabelText("Destination:");
    expect(within(destination).getAllByRole("option")).toHaveLength(2);
    fireEvent.change(destination, { target: { value: "target" } });
    const buttons = screen.getAllByRole("button", { name: "Migrate" });
    fireEvent.click(buttons[buttons.length - 1]);
    await waitFor(() => expect(calls).toHaveBeenCalledWith(`${base}/migrate`, expect.objectContaining({ body: JSON.stringify({ target_host_id: "target" }) })));
    expect(await screen.findByText(success ? "migrating" : "Migration unavailable")).toBeInTheDocument();
  });
  it("explains when the shared storage pool has no destination", async () => {
    routes["/api/v1/hosts/"] = response([host("source"), host("other", "different")]);
    await loaded("active"); click("Migrate");
    expect(await screen.findByText("No available hosts in the same storage pool.")).toBeInTheDocument();
  });
});
