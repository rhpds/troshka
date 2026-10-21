import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import AdminHostsPage from "./page";

const confirm = vi.hoisted(() => vi.fn());
vi.mock("@/lib/confirm", () => ({ appConfirm: confirm }));
const response = (data: unknown, status = 200) => ({ ok: status < 400, status, json: async () => data }) as Response;
let host: Record<string, unknown>;
let routes: Record<string, Response | Error>;
let calls: ReturnType<typeof vi.fn>;
beforeEach(() => {
  vi.clearAllMocks(); confirm.mockResolvedValue(true);
  host = { id: "host-1", provider_id: "provider-1", instance_id: "demo-host", instance_type: "m8i.xlarge", state: "active", host_type: "dedicated", region: "us-east-1", agent_status: "connected", agent_version: "v1", total_vcpus: 16, used_vcpus: 4, total_ram_mb: 32768, used_ram_mb: 4096, running_vms: 2, total_vms: 3, running_projects: 1, total_projects: 1, ip_address: "192.0.2.1", storage_size_gb: 100, accepting_work: true, storage_pool_id: "pool", auto_extend_enabled: false, auto_extend_threshold_pct: 80, auto_extend_increment_gb: 50, max_eips: 10, used_eips: 1, created_at: "2026-10-01T00:00:00Z" };
  routes = {
    "/api/v1/providers/": response([{ id: "provider-1", name: "Demo AWS", type: "ec2", default_image: "image", vpc_id: "vpc", default_region: "us-east-1" }]),
    "/api/v1/storage-pools": response([]),
    "/api/v1/hosts/expected-agent-version": response({ version: "v1" }),
    "/api/v1/hosts/storage": response({ "host-1": { used_pct: 85, free_gb: 15, total_gb: 100 } }),
    "/api/v1/hosts/overcommit": response({ cpu_ratio: 4, ram_ratio: 1.5 }),
    "/api/v1/projects/": response([]),
  };
  calls = vi.fn(async (url: string, options?: RequestInit) => {
    const result = routes[`${options?.method || "GET"} ${url}`] || routes[url];
    if (result instanceof Error) throw result;
    if (result) return result;
    if (url === "/api/v1/hosts/" && !options?.method) return response([host]);
    return response({});
  });
  vi.stubGlobal("fetch", calls);
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: vi.fn().mockResolvedValue(undefined) } });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });
async function loaded() { render(<AdminHostsPage />); await screen.findByText("demo-host"); }
function click(name: string) { fireEvent.click(screen.getByRole("button", { name })); }

describe("host inventory", () => {
  it("loads provider capacity and filters by host text", async () => {
    await loaded();
    expect(screen.getByText("Demo AWS")).toBeInTheDocument();
    fireEvent.change(screen.getByPlaceholderText("Search hosts..."), { target: { value: "192.0.2.1" } });
    expect(screen.getByText("demo-host")).toBeInTheDocument();
    fireEvent.change(screen.getByPlaceholderText("Search hosts..."), { target: { value: "missing" } });
    expect(screen.getByPlaceholderText("Search hosts...")).toHaveValue("missing");
    expect(screen.queryByText("demo-host")).not.toBeInTheDocument();
    fireEvent.change(screen.getByPlaceholderText("Search hosts..."), { target: { value: "" } });
    expect(screen.getByText("demo-host")).toBeInTheDocument();
  });
  it("shows failed access lookup without exposing an empty key", async () => {
    routes["/api/v1/hosts/host-1/ssh-key"] = response({}, 404);
    await loaded(); click("Host Access Info");
    expect(await screen.findByText("No SSH key available for this host")).toBeInTheDocument();
  });
  it("caches SSH access details across hide and show", async () => {
    routes["/api/v1/hosts/host-1/ssh-key"] = response({ key_pair_name: "demo-key", private_key: "example-private-key", ssh_command: "ssh demo-host" });
    await loaded(); click("Host Access Info");
    await screen.findByText("ssh demo-host"); click("Hide Access Info"); click("Host Access Info");
    expect(calls.mock.calls.filter(([url]) => url === "/api/v1/hosts/host-1/ssh-key")).toHaveLength(1);
  });
});

describe("host operations", () => {
  it.each([["active", "Power Off", "poweroff"], ["stopped", "Power On", "poweron"], ["paused", "Resume", "poweron"]])("handles %s power actions", async (state, label, endpoint) => {
    host.state = state;
    await loaded(); click(label);
    await waitFor(() => expect(calls).toHaveBeenCalledWith(`/api/v1/hosts/host-1/${endpoint}`, { method: "POST" }));
  });
  it("honors cancellation before powering off", async () => {
    confirm.mockResolvedValue(false);
    await loaded(); click("Power Off");
    await waitFor(() => expect(confirm).toHaveBeenCalled());
    expect(calls.mock.calls.some(([url]) => url.endsWith("/poweroff"))).toBe(false);
  });
  it.each([false, true])("reports power action failures network=%s", async (network) => {
    routes["/api/v1/hosts/host-1/poweroff"] = network ? new Error("offline") : response({ detail: "Provider refused request" }, 409);
    await loaded(); click("Power Off");
    expect(await screen.findByText(network ? "Failed to connect to server" : "Provider refused request")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Power Off" })).toBeEnabled();
  });
  it("warns about project disk loss before removal", async () => {
    routes["/api/v1/projects/"] = response([{ name: "Training lab", host_id: "host-1", state: "active" }]);
    await loaded(); click("Remove");
    await waitFor(() => expect(calls).toHaveBeenCalledWith("/api/v1/hosts/host-1", { method: "DELETE" }));
    expect(confirm).toHaveBeenCalledWith(expect.objectContaining({ message: expect.stringContaining("Training lab") }));
  });
  it("reports failed removal", async () => {
    routes["DELETE /api/v1/hosts/host-1"] = response({ detail: "Host locked" }, 409);
    await loaded(); click("Remove");
    expect(await screen.findByText("Host locked")).toBeInTheDocument();
  });
  it("evacuates a shared-storage host", async () => {
    await loaded(); click("Evacuate");
    await waitFor(() => expect(calls).toHaveBeenCalledWith("/api/v1/hosts/host-1/evacuate", { method: "POST" }));
  });
  it("requires both destructive confirmations before wiping", async () => {
    confirm.mockResolvedValueOnce(true).mockResolvedValueOnce(false);
    await loaded(); click("Wipe Host");
    await waitFor(() => expect(confirm).toHaveBeenCalledTimes(2));
    expect(calls.mock.calls.some(([url]) => url.endsWith("/wipe"))).toBe(false);
  });
  it("reports wipe results after backend completion", async () => {
    routes["/api/v1/hosts/host-1/wipe"] = response({ projects_destroyed: 2, projects_reset: 1, nft_reset: { flushed_chains: 3 } });
    await loaded(); click("Wipe Host");
    expect(await screen.findByText("Wiped: 2 destroyed, 1 reset, 3 nft chains flushed")).toBeInTheDocument();
  });
  it("summarizes garbage collection outcomes and copies the confirmed report", async () => {
    routes["/api/v1/hosts/host-1/gc"] = response({ capacity: { changed: true, old: { used_vcpus: 4, used_ram_mb: 4096 }, new: { used_vcpus: 2, used_ram_mb: 2048 } }, orphans_found: 3, cleanup: { cleaned: 2, output: "Removed stale disks", cache_cleaned: 1 }, network_repair: { repaired: 1 }, s3_cleanup: { deleted: 4, deleted_gb: 2 }, shared_cache_entries_cleaned: 3 });
    await loaded(); click("Clean");
    await waitFor(() => expect(navigator.clipboard.writeText).toHaveBeenCalled());
    const report = vi.mocked(navigator.clipboard.writeText).mock.calls[0][0];
    expect(report).toContain("Warning: 1 orphan(s) could not be cleaned");
    expect(report).toContain("Capacity synced: 4→2 vCPUs, 4096→2048 MB RAM");
    expect(report).toContain("Network bridges repaired: 1");
    expect(report).toContain("S3 cleaned: 4 objects (2 GB)");
  });
  it("reports a clean host after a no-op collection", async () => {
    await loaded(); click("Clean");
    await waitFor(() => expect(confirm).toHaveBeenCalledWith(expect.objectContaining({ message: expect.stringContaining("Nothing to do — host is clean") })));
  });
});

describe("host provisioning and settings", () => {
  it.each(["success", "rejected", "offline"])("handles provisioning %s", async (result) => {
    routes["POST /api/v1/hosts/"] = result === "offline" ? new Error("offline") : response({ detail: "Quota exhausted" }, result === "success" ? 200 : 409);
    await loaded(); click("+ Add Host");
    fireEvent.change(screen.getByLabelText("Provider"), { target: { value: "provider-1" } });
    click("Provision Host");
    await waitFor(() => expect(calls).toHaveBeenCalledWith("/api/v1/hosts/", expect.objectContaining({ method: "POST" })));
    if (result !== "success") expect(await screen.findByText(result === "offline" ? "Failed to connect to server" : "Quota exhausted")).toBeInTheDocument();
  });
  it("provisions an OCP Virt host using the selected capacity preset", async () => {
    routes["/api/v1/providers/"] = response([{ id: "provider-1", name: "Demo Virt", type: "ocpvirt" }]);
    await loaded(); click("+ Add Host");
    fireEvent.change(screen.getByLabelText("Provider"), { target: { value: "provider-1" } });
    click("8c / 32G"); click("Provision Host");
    await waitFor(() => expect(calls).toHaveBeenCalledWith("/api/v1/hosts/", expect.objectContaining({ body: JSON.stringify({ provider_id: "provider-1", instance_type: "8c-32g" }) })));
  });
  it.each([[false, false, "Provider needs setup."], [false, true, "No image set."], [true, false, "No VPC set."]])("explains missing provider prerequisites image=%s vpc=%s", async (image, vpc, message) => {
    routes["/api/v1/providers/"] = response([{ id: "provider-1", name: "Demo AWS", type: "ec2", default_image: image ? "image" : null, vpc_id: vpc ? "vpc" : null }]);
    await loaded(); click("+ Add Host");
    fireEvent.change(screen.getByLabelText("Provider"), { target: { value: "provider-1" } });
    expect(screen.getByText(new RegExp(message))).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Provision Host" })).toBeDisabled();
  });
  it.each([true, false])("saves billing mode success=%s", async (success) => {
    routes["PATCH /api/v1/hosts/host-1"] = response({ detail: "Billing update denied" }, success ? 200 : 403);
    await loaded(); click("Rates ▼");
    fireEvent.change(screen.getByLabelText("Billing mode"), { target: { value: "dedicated" } });
    await waitFor(() => expect(calls).toHaveBeenCalledWith("/api/v1/hosts/host-1", expect.objectContaining({ body: JSON.stringify({ billing_mode: "dedicated" }) })));
    if (!success) expect(await screen.findByText("Billing update denied")).toBeInTheDocument();
  });
  it.each(["0.125", ""])("stores explicit or inherited metering rate %s", async (value) => {
    await loaded(); click("Rates ▼");
    const input = screen.getByLabelText("vcpu_hour");
    fireEvent.change(input, { target: { value } }); fireEvent.blur(input);
    await waitFor(() => expect(calls).toHaveBeenCalledWith("/api/v1/hosts/host-1", expect.objectContaining({ body: JSON.stringify({ metering_rates: value ? { vcpu_hour: 0.125 } : null }) })));
  });
  it("rolls back pool exclusion when the backend rejects it", async () => {
    routes["PATCH /api/v1/hosts/host-1"] = response({ detail: "Pool update denied" }, 403);
    await loaded(); fireEvent.click(screen.getByRole("switch", { name: "In pool" }));
    expect(await screen.findByText("Pool update denied")).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "In pool" })).toBeChecked();
  });
});
