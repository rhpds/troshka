import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ProjectsPage, { NewProjectModal } from "./page";

const harness = vi.hoisted(() => ({ push: vi.fn(), confirm: vi.fn(), created: vi.fn(), close: vi.fn(), alert: vi.fn(), load: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: harness.push }) }));
vi.mock("@/lib/confirm", () => ({ appConfirm: harness.confirm }));
vi.mock("@/stores/canvasStore", () => ({ useCanvasStore: { getState: () => ({ loadProject: harness.load }) } }));
vi.mock("@/components/TagEditor", () => ({ default: () => null }));
vi.mock("@patternfly/react-core/dist/esm/components/EmptyState/EmptyStateHeader", () => ({ EmptyStateHeader: ({ titleText }: { titleText: string }) => <h1>{titleText}</h1> }));
vi.mock("@patternfly/react-core/dist/esm/components/EmptyState/EmptyStateIcon", () => ({ EmptyStateIcon: () => null }));
const response = (data: unknown, status = 200) => ({ ok: status < 400, status, json: async () => data }) as Response;
let routes: Record<string, Response | Error>;
let calls: ReturnType<typeof vi.fn>;
const row = (id: string, state = "draft") => ({ id, name: id, state, owner_id: "me", owner_email: "alice@example.com", created_at: "2026-10-01T12:00:00Z", description: `${id} description`, tags: {} });
beforeEach(() => {
  vi.clearAllMocks(); localStorage.clear();
  harness.confirm.mockResolvedValue(true);
  harness.load.mockResolvedValue(undefined);
  routes = {
    "/api/v1/auth/me": response({ id: "me", role: "user" }),
    "/api/v1/projects/": response([row("Alpha"), row("Beta", "active")]),
    "/api/v1/storage-pools/": response([]),
    "/api/v1/patterns/": response([{ id: "pattern", name: "Saved environment", description: "Reusable", created_at: "2026-10-01" }]),
    "/api/v1/projects/templates": response([{ id: "simple", name: "Simple lab", category: "generic", description: "A minimal environment" }]),
    "/api/v1/ocp/versions": response([{ minor: "4.20", latest: "4.20.1" }]),
    "/api/v1/library/": response([]),
    "/api/v1/auth/ssh-keys": response([{ id: "ssh", name: "Laptop" }]),
    "/api/v1/auth/ocp-pull-secret": response({ has_secret: true }),
    "POST /api/v1/projects/": response({ id: "new" }),
    "/api/v1/projects/from-template": response({ id: "new" }),
  };
  calls = vi.fn(async (url: string, options?: RequestInit) => {
    const result = routes[`${options?.method || "GET"} ${url}`] || routes[url];
    if (result instanceof Error) throw result;
    return result || response({});
  });
  vi.stubGlobal("fetch", calls);
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });
function click(name: string) { fireEvent.click(screen.getByRole("button", { name })); }
async function modal(mode: string) {
  render(<NewProjectModal onClose={harness.close} onCreated={harness.created} setAlertMsg={harness.alert} availableHosts={[]} userRole="user" />);
  await screen.findByText("Quick Starts");
  fireEvent.click(screen.getByText(mode));
}
function nameProject() { fireEvent.change(screen.getByPlaceholderText("My Project"), { target: { value: "Example" } }); }

describe("new project creation", () => {
  it("requires a name and creates a blank project", async () => {
    await modal("Blank Project");
    expect(screen.getByRole("button", { name: "Create Project" })).toBeDisabled();
    nameProject(); click("Create Project");
    await waitFor(() => expect(harness.created).toHaveBeenCalledWith("new"));
    expect(calls).toHaveBeenCalledWith("/api/v1/projects/", expect.objectContaining({ method: "POST", body: JSON.stringify({ name: "Example" }) }));
  });
  it.each([false, true])("shows creation errors, network=%s", async (network) => {
    routes["POST /api/v1/projects/"] = network ? new Error("offline") : response({ detail: "Name already exists" }, 409);
    await modal("Blank Project"); nameProject(); click("Create Project");
    expect(await screen.findByText(network ? "Request failed" : "Name already exists")).toBeInTheDocument();
    expect(harness.created).not.toHaveBeenCalled();
  });
  it("returns to mode selection and closes without creating", async () => {
    await modal("Blank Project"); click("Back"); click("Cancel");
    expect(harness.close).toHaveBeenCalledOnce();
    expect(harness.created).not.toHaveBeenCalled();
  });
  it.each([["[broken", "Invalid YAML syntax in template file"], ["scalar", "Template must be a YAML mapping"]])("validates YAML %s", async (yaml, error) => {
    await modal("From Template"); nameProject();
    fireEvent.change(screen.getByRole("textbox", { name: "Template YAML" }), { target: { value: yaml } });
    click("Import & Create");
    expect(await screen.findByText(error)).toBeInTheDocument();
  });
  it("imports YAML atomically and deploys after creation", async () => {
    await modal("From Template"); nameProject();
    fireEvent.change(screen.getByRole("textbox", { name: "Template YAML" }), { target: { value: "networks: {}\nvms: []" } });
    click("Import & Create");
    await waitFor(() => expect(harness.created).toHaveBeenCalledWith("new"));
    expect(calls).toHaveBeenCalledWith("/api/v1/projects/new/deploy", { method: "POST" });
    expect(calls.mock.calls.some(([url, options]) => url === "/api/v1/projects/" && options?.method === "POST")).toBe(false);
  });
  it.each(["import", "deploy"])("retains modal when %s fails", async (stage) => {
    routes[stage === "import" ? "/api/v1/projects/from-template" : "/api/v1/projects/new/deploy"] = response({ detail: "Capacity unavailable" }, 409);
    await modal("From Template"); nameProject();
    fireEvent.change(screen.getByRole("textbox", { name: "Template YAML" }), { target: { value: "networks: {}\nvms: []" } });
    click("Import & Create");
    expect(await screen.findByText("Capacity unavailable")).toBeInTheDocument();
    expect(harness.created).not.toHaveBeenCalled();
  });
  it("creates a quickstart using stored topology and preserves import warnings", async () => {
    routes["/api/v1/projects/from-template"] = response({ id: "new", warnings: ["Reserved IP"] });
    await modal("Quick Starts"); fireEvent.click(screen.getByText("Simple lab"));
    click("Create");
    await waitFor(() => expect(harness.created).toHaveBeenCalledWith("new"));
    expect(harness.load).toHaveBeenCalledWith("new");
    expect(harness.alert).toHaveBeenCalledWith("Created with warnings: Reserved IP");
  });
  it("creates a selected pattern with automatic deployment disabled", async () => {
    routes["/api/v1/patterns/pattern/deploy"] = response({ id: "from-pattern" });
    await modal("From Pattern");
    fireEvent.focus(screen.getByPlaceholderText("Search patterns..."));
    fireEvent.click(screen.getByText("Saved environment"));
    fireEvent.click(screen.getByLabelText("Deploy immediately"));
    click("Create from Pattern");
    await waitFor(() => expect(harness.created).toHaveBeenCalledWith("from-pattern"));
    expect(calls).toHaveBeenCalledWith("/api/v1/patterns/pattern/deploy", expect.objectContaining({ body: JSON.stringify({ name: "Saved environment", auto_deploy: false, auto_start: false }) }));
  });
  it("shows pattern creation rejection without navigating", async () => {
    routes["/api/v1/patterns/pattern/deploy"] = response({ detail: "Pattern unavailable" }, 409);
    await modal("From Pattern");
    fireEvent.focus(screen.getByPlaceholderText("Search patterns..."));
    fireEvent.click(screen.getByText("Saved environment")); click("Create from Pattern");
    expect(await screen.findByText("Pattern unavailable")).toBeInTheDocument();
    expect(harness.created).not.toHaveBeenCalled();
  });
  it("creates a quickstart draft without requesting deployment", async () => {
    await modal("Quick Starts"); fireEvent.click(screen.getByText("Simple lab"));
    fireEvent.click(screen.getByLabelText("Deploy immediately after creation")); click("Create");
    await waitFor(() => expect(harness.created).toHaveBeenCalledWith("new"));
    expect(calls.mock.calls.some(([url]) => url === "/api/v1/projects/new/deploy")).toBe(false);
  });
  it.each(["creation", "deployment"])("keeps quickstart errors visible during %s", async (stage) => {
    const url = stage === "creation" ? "/api/v1/projects/from-template" : "/api/v1/projects/new/deploy";
    routes[url] = response({ detail: { message: "No suitable host" } }, 409);
    await modal("Quick Starts"); fireEvent.click(screen.getByText("Simple lab")); click("Create");
    expect(await screen.findByText("No suitable host")).toBeInTheDocument();
    expect(harness.created).not.toHaveBeenCalled();
  });
  it("blocks OpenShift creation without a required pull secret", async () => {
    routes["/api/v1/auth/ocp-pull-secret"] = response({ has_secret: false });
    routes["/api/v1/projects/templates"] = response([{ id: "ocp", name: "OpenShift SNO", category: "openshift" }]);
    await modal("Quick Starts"); fireEvent.click(screen.getByText("OpenShift SNO"));
    expect(screen.getByRole("button", { name: "Create" })).toBeDisabled();
  });
  it("selects a default RHEL image and its matching DVD for YAML imports", async () => {
    routes["/api/v1/library/"] = response([
      { id: "image", name: "RHEL 9.6 Cloud Image", format: "qcow2", state: "ready", size_bytes: 1073741824 },
      { id: "iso", name: "RHEL 9.6 DVD", format: "iso", state: "ready", size_bytes: 1073741824 },
      { id: "other", name: "RHEL 9.5 DVD", format: "iso", state: "ready" },
    ]);
    await modal("From Template"); nameProject();
    fireEvent.change(screen.getByRole("textbox", { name: "Template YAML" }), { target: { value: "networks: {}\nvms: []" } });
    fireEvent.click(screen.getByLabelText("Deploy immediately after import"));
    click("Import & Create");
    await waitFor(() => expect(harness.created).toHaveBeenCalledWith("new"));
    const request = calls.mock.calls.find(([url]) => url === "/api/v1/projects/from-template");
    expect(JSON.parse(request?.[1]?.body as string)).toMatchObject({ bastion_image_id: "image", bastion_iso_id: "iso" });
  });
});

describe("projects listing", () => {
  async function loaded() { render(<ProjectsPage />); await screen.findByText("Alpha"); }
  it("filters names and descriptions and opens a project", async () => {
    await loaded();
    fireEvent.change(screen.getByPlaceholderText("Search projects..."), { target: { value: "Beta description" } });
    expect(screen.queryByText("Alpha")).not.toBeInTheDocument();
    fireEvent.click(screen.getByText("Beta"));
    expect(harness.push).toHaveBeenCalledWith("/projects/Beta");
    fireEvent.change(screen.getByPlaceholderText("Search projects..."), { target: { value: "missing" } });
    expect(screen.getByText('No projects match "missing"')).toBeInTheDocument();
  });
  it.each([[], null])("handles empty or malformed project results", async (data) => {
    routes["/api/v1/projects/"] = response(data);
    render(<ProjectsPage />);
    expect(await screen.findByText("No projects yet")).toBeInTheDocument();
    click("New Project");
    expect(screen.getByText("Blank Project")).toBeInTheDocument();
  });
  it.each([["active", "Stop", "stop"], ["stopped", "Start", "start"], ["error", "Start", "start"]])("requests %s lifecycle action", async (state, button, endpoint) => {
    routes["/api/v1/projects/"] = response([row("Alpha", state)]);
    await loaded(); click(button);
    await waitFor(() => expect(calls).toHaveBeenCalledWith(`/api/v1/projects/Alpha/${endpoint}`, { method: "POST" }));
  });
  it("restores a draft after deployment rejection", async () => {
    routes["/api/v1/projects/"] = response([row("Alpha")]);
    routes["/api/v1/projects/Alpha/deploy"] = response({ detail: "No capacity" }, 409);
    await loaded(); click("Deploy");
    expect(await screen.findByText("No capacity")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Deploy" })).toBeEnabled();
  });
  it.each([["active", "Stop", "stop"], ["stopped", "Start", "start"], ["draft", "Deploy", "deploy"]])("bulk %s sends one request per selected project", async (state, button, endpoint) => {
    routes["/api/v1/projects/"] = response([row("Alpha", state), row("Beta", state)]);
    await loaded(); fireEvent.click(screen.getByLabelText("Select all")); click(`${button} (2)`);
    await waitFor(() => expect(calls).toHaveBeenCalledWith(`/api/v1/projects/Alpha/${endpoint}`, { method: "POST" }));
    expect(calls).toHaveBeenCalledWith(`/api/v1/projects/Beta/${endpoint}`, { method: "POST" });
  });
});
