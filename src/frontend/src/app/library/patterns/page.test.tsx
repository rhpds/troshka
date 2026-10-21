import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import PatternsPage from "./page";
const harness = vi.hoisted(() => ({ confirm: vi.fn(), push: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: harness.push }) }));
vi.mock("@/lib/confirm", () => ({ appConfirm: harness.confirm }));
vi.mock("@/components/TagEditor", () => ({ default: ({ onAdd, onRemove }: { onAdd: (tag: string) => void; onRemove: (tag: string) => void }) => <><button onClick={() => onAdd("new")}>Add tag</button><button onClick={() => onRemove("old")}>Remove tag</button></> }));
vi.mock("@/components/canvas/PatternPreviewModal", () => ({ default: ({ patternName, onClose }: { patternName: string; onClose: () => void }) => <div role="dialog">{patternName}<button onClick={onClose}>Close preview</button></div> }));
vi.mock("@/components/canvas/BulkDeployModal", () => ({ default: ({ onDeployed }: { onDeployed: (count: number) => void }) => <button onClick={() => onDeployed(3)}>Complete bulk</button> }));
vi.mock("@/components/canvas/SharePatternModal", () => ({ default: ({ onClose }: { onClose: () => void }) => <button onClick={onClose}>Close sharing</button> }));
vi.mock("@patternfly/react-core/dist/esm/components/EmptyState/EmptyStateHeader", () => ({ EmptyStateHeader: () => null }));
vi.mock("@patternfly/react-core/dist/esm/components/EmptyState/EmptyStateIcon", () => ({ EmptyStateIcon: () => null }));
const response = (data: unknown, status = 200) => ({ ok: status < 400, json: async () => data }) as Response;
let pattern: Record<string, unknown>;
let routes: Record<string, Response | Error>;
let calls: ReturnType<typeof vi.fn>;
beforeEach(() => {
  vi.clearAllMocks(); harness.confirm.mockResolvedValue(true);
  pattern = { id: "pattern-1", name: "Saved lab", description: "Demo environment", state: "available", visibility: "private", owner_id: "me", owner_email: "alice@example.com", tags: {}, disks: [], total_size_bytes: 1024 ** 3, total_vcpus: 4, total_ram_gb: 8, total_disk_gb: 100, vm_count: 1, created_at: "2025-01-01", sources: ["gold", "central", "obc", "unknown"] };
  routes = { "/api/v1/auth/me": response({ id: "me", role: "admin" }), "/api/v1/dns-providers": response([{ id: "dns", name: "Demo DNS" }]), "/api/v1/hosts/": response([{ id: "host-1", state: "active", agent_status: "connected", host_type: "dedicated", total_vcpus: 16, used_vcpus: 4, total_ram_mb: 32768, used_ram_mb: 8192, ip_address: "192.0.2.1" }]), "POST /api/v1/patterns/pattern-1/deploy": response({ id: "project-1" }) };
  calls = vi.fn(async (url: string, options?: RequestInit) => {
    const result = routes[`${options?.method || "GET"} ${url}`] || routes[url];
    if (result instanceof Error) throw result;
    if (result) return result;
    if (url === "/api/v1/patterns/") return response([pattern]);
    return response({});
  });
  vi.stubGlobal("fetch", calls);
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });
async function loaded() { render(<PatternsPage />); await screen.findByRole("button", { name: "Saved lab" }); }
function click(name: string) { fireEvent.click(screen.getByRole("button", { name })); }
function change(label: string, value: string) { fireEvent.change(screen.getByLabelText(label), { target: { value } }); }
describe("pattern library", () => {
  it("updates tags while preserving other metadata", async () => {
    pattern.tags = { user_tags: ["old"] }; await loaded(); click("Add tag"); click("Remove tag");
    await waitFor(() => expect(calls.mock.calls.filter(([, opts]) => opts?.method === "PATCH")).toHaveLength(2));
    expect(calls).toHaveBeenCalledWith("/api/v1/patterns/pattern-1", expect.objectContaining({ body: JSON.stringify({ tags: { user_tags: ["old", "new"] } }) }));
  });
  it.each([true, false])("bulk deletes selected patterns with confirmation=%s", async (accepted) => {
    harness.confirm.mockResolvedValue(accepted); await loaded(); fireEvent.click(screen.getByLabelText("Select all")); click("Delete (1)");
    await waitFor(() => expect(harness.confirm).toHaveBeenCalled());
    expect(calls.mock.calls.some(([, opts]) => opts?.method === "DELETE")).toBe(accepted);
  });
  it.each(["public", "shared", "private"])("respects %s ownership filters", async visibility => {
    pattern.owner_id = "other"; pattern.visibility = visibility;
    render(<PatternsPage />); await screen.findByLabelText("Other users");
    if (visibility === "private") fireEvent.click(screen.getByLabelText("Other users"));
    await screen.findByRole("button", { name: "Saved lab" });
    if (visibility !== "private") expect(screen.getByText(visibility)).toBeInTheDocument();
  });
  it("imports a pattern archive using form data", async () => {
    const clickInput = vi.spyOn(HTMLInputElement.prototype, "click").mockImplementation(function(this: HTMLInputElement) {
      Object.defineProperty(this, "files", { value: [new File(["archive"], "pattern.tar")] });
      this.dispatchEvent(new Event("change"));
    });
    await loaded(); click("Import Pattern");
    await waitFor(() => expect(calls).toHaveBeenCalledWith("/api/v1/patterns/import", expect.objectContaining({ method: "POST", body: expect.any(FormData) })));
    clickInput.mockRestore();
  });
  it("filters by name and description and restores an empty search", async () => {
    await loaded(); const search = screen.getByPlaceholderText("Search patterns...");
    fireEvent.change(search, { target: { value: "missing" } }); expect(screen.getByText("No patterns match your search.")).toBeInTheDocument();
    fireEvent.change(search, { target: { value: "environment" } }); expect(screen.getByRole("button", { name: "Saved lab" })).toBeInTheDocument();
  });
  it("filters owned patterns and exposes sharing to owners", async () => {
    await loaded(); click("Share"); click("Close sharing"); fireEvent.click(screen.getByLabelText("Mine"));
    expect(screen.getByText("No patterns match the selected ownership filters.")).toBeInTheDocument();
  });
  it("opens and closes an accessible preview action", async () => {
    await loaded(); click("Preview"); expect(screen.getByRole("dialog")).toHaveTextContent("Saved lab"); click("Close preview"); expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
  it("renames on enter", async () => {
    await loaded(); click("Saved lab"); const input = screen.getByDisplayValue("Saved lab");
    fireEvent.change(input, { target: { value: " Renamed " } }); fireEvent.blur(input);
    await waitFor(() => expect(calls).toHaveBeenCalledWith("/api/v1/patterns/pattern-1", expect.objectContaining({ method: "PATCH", body: JSON.stringify({ name: "Renamed" }) })));
  });
  it.each([true, false])("deletes after confirmation=%s", async (accepted) => {
    harness.confirm.mockResolvedValue(accepted); await loaded(); click("Delete");
    await waitFor(() => expect(harness.confirm).toHaveBeenCalled());
    if (accepted) await waitFor(() => expect(screen.queryByRole("button", { name: "Saved lab" })).not.toBeInTheDocument());
    else expect(calls.mock.calls.some(([, opts]) => opts?.method === "DELETE")).toBe(false);
  });
  it("shows capture progress and cancels capture", async () => {
    pattern.state = "capturing"; pattern.capture_progress = { detail: "Saving disks", disks: [{ name: "disk1", status: "done" }, { name: "disk2", status: "pending" }], vms: ["vm1"] };
    await loaded(); expect(screen.getByText("Saving disks")).toBeInTheDocument(); expect(screen.getByRole("button", { name: "Create Project" })).toBeDisabled(); click("Cancel");
    await waitFor(() => expect(screen.queryByText("Saving disks")).not.toBeInTheDocument());
  });
  it.each([["importing", "importing…"], ["error", "error"], ["unknown", "unknown"]])("renders %s state", async (state, label) => {
    pattern.state = state; await loaded(); expect(screen.getByText(label)).toBeInTheDocument();
  });
  it("shows age warning for OCP patterns and export size", async () => {
    pattern.is_ocp = true; await loaded(); expect(screen.getByText(/cert age:/)).toBeInTheDocument(); click("Export");
    expect(screen.getByRole("dialog", { name: "Export Pattern" })).toHaveTextContent("1.0 GB"); click("Cancel"); expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
  it("creates a project with DNS and host placement", async () => {
    await loaded(); click("Create Project"); await screen.findByLabelText("Host");
    change("Project Name", "New project"); change("GUID", "demo"); change("Domain", "example.com"); change("DNS Provider", "dns"); change("Host", "host-1");
    const buttons = screen.getAllByRole("button", { name: "Create Project" }); fireEvent.click(buttons[buttons.length - 1]);
    await waitFor(() => expect(harness.push).toHaveBeenCalledWith("/projects/project-1"));
    expect(calls).toHaveBeenCalledWith("/api/v1/patterns/pattern-1/deploy", expect.objectContaining({ body: JSON.stringify({ name: "New project", auto_deploy: true, auto_start: true, guid: "demo", domain: "example.com", dns_provider_id: "dns", host_id: "host-1" }) }));
  });
  it.each([response({ detail: "Quota exceeded" }, 400), new Error("offline")])("reports deployment failure", async (failure) => {
    routes["POST /api/v1/patterns/pattern-1/deploy"] = failure;
    await loaded(); click("Create Project"); await screen.findByLabelText("Host"); fireEvent.click(screen.getByLabelText("Deploy immediately"));
    fireEvent.keyDown(screen.getByLabelText("Project Name"), { key: "Enter" });
    expect(await screen.findByText(failure instanceof Error ? "Failed to connect to server" : "Quota exceeded")).toBeInTheDocument();
    expect(harness.push).not.toHaveBeenCalled();
  });
  it("reports a completed bulk deployment", async () => {
    await loaded(); click("Bulk Deploy"); click("Complete bulk");
    expect(screen.getByText(/3 projects/)).toBeInTheDocument();
  });
});
