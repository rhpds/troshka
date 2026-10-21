import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import StoragePoolsPage from "./page";

const confirm = vi.hoisted(() => vi.fn());
vi.mock("@/lib/confirm", () => ({ appConfirm: confirm }));
const response = (data: unknown, status = 200) => ({ ok: status < 400, json: async () => data }) as Response;
let pool: Record<string, unknown>;
let routes: Record<string, Response>;
let calls: ReturnType<typeof vi.fn>;
beforeEach(() => {
  vi.clearAllMocks(); confirm.mockResolvedValue(true);
  pool = { id: "pool-1", name: "Demo pool", mode: "shared-fsx", az: "us-east-1a", fsx_filesystem_id: "fs-123", fsx_dns_name: "fs.example", fsx_throughput_mbps: 160, fsx_storage_gb: 128, status: "available", provider_id: "aws", host_count: 0, auto_extend_enabled: false, auto_extend_threshold_pct: 80, auto_extend_increment_gb: 64, auto_extend_max_gb: null, worker_host_id: null, worker_status: null, pb_auto_sleep_minutes: 0 };
  routes = {
    "/api/v1/providers/": response([
      { id: "aws", name: "AWS demo", type: "ec2" },
      { id: "azure", name: "Azure demo", type: "azure", azure_location: "eastus" },
      { id: "gcp", name: "GCP demo", type: "gcp", gcp_zone: "us-central1-a" },
      { id: "ocp", name: "OCP demo", type: "ocpvirt" },
    ]),
    "/api/v1/hosts/": response([{ id: "host-1", storage_pool_id: "pool-1", agent_version: "v2" }]),
    "/api/v1/hosts/storage": response({ "host-1": { partitions: [{ mount: "/shared", used_bytes: 96 * 1024 ** 3, total_bytes: 128 * 1024 ** 3, used_pct: 75 }] } }),
    "/api/v1/providers/aws/availability-zones": response(["us-east-1a"]),
  };
  calls = vi.fn(async (url: string, options?: RequestInit) => {
    const matched = routes[`${options?.method || "GET"} ${url}`] || routes[url];
    if (matched) return matched;
    if (url === "/api/v1/storage-pools/") return response([pool]);
    return response({});
  });
  vi.stubGlobal("fetch", calls);
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });
async function loaded() { render(<StoragePoolsPage />); await screen.findByText("Demo pool"); }
function click(name: string) { fireEvent.click(screen.getByRole("button", { name })); }
function change(label: string, value: string) { fireEvent.change(screen.getByLabelText(label), { target: { value } }); }
function requestBody(method: string, url = "/api/v1/storage-pools/pool-1") {
  const request = calls.mock.calls.find(([path, options]) => path === url && options?.method === method);
  expect(request).toBeDefined();
  return JSON.parse(request![1]!.body as string);
}
async function openCreate(mode: string) {
  await loaded(); click("Create Pool"); change("Mode", mode); change("Name", " New pool ");
}

describe("storage pool creation", () => {
  it("loads storage usage and filesystem details", async () => {
    await loaded();
    expect(await screen.findByText("Storage: 96 / 128 GB (75%)")).toBeInTheDocument();
    expect(screen.getByText("NFS: fs.example:/fsx")).toBeInTheDocument();
  });
  it.each([
    ["local", "Provider is required for local pools"],
    ["shared-fsx", "Provider is required for FSx pools"],
    ["shared-ceph-nfs", "Provider is required for Ceph-NFS pools"],
    ["shared-azure-files", "Provider is required for Azure Files pools"],
    ["shared-byo", "NFS endpoint is required"],
  ])("validates required fields for %s", async (mode, error) => {
    await openCreate(mode); click("Create Pool");
    expect(await screen.findByText(error)).toBeInTheDocument();
    expect(calls.mock.calls.some(([, opts]) => opts?.method === "POST")).toBe(false);
  });
  it("requires a name and AZ for FSx", async () => {
    await openCreate("shared-fsx"); change("Name", " "); click("Create Pool");
    expect(await screen.findByText("Name is required")).toBeInTheDocument();
    change("Name", "demo"); change("Provider", "aws");
    await screen.findByRole("option", { name: "us-east-1a" }); click("Create Pool");
    expect(await screen.findByText("AZ is required for FSx pools")).toBeInTheDocument();
  });
  it.each([
    ["local", "gcp", { az: "us-central1-a" }],
    ["shared-ceph-nfs", "ocp", { fsx_storage_gb: 500 }],
    ["shared-azure-files", "azure", { az: "eastus", azure_files_capacity_gb: 128, azure_files_throughput: 160 }],
  ])("creates %s with provider-specific parameters", async (mode, provider, expected) => {
    await openCreate(mode); change("Provider", provider); click("Create Pool");
    await waitFor(() => expect(screen.queryByLabelText("Name")).not.toBeInTheDocument());
    expect(requestBody("POST", "/api/v1/storage-pools")).toMatchObject({ name: "New pool", mode, provider_id: provider, ...expected });
  });
  it("creates FSx with chosen AZ and capacity", async () => {
    await openCreate("shared-fsx"); change("Provider", "aws");
    await screen.findByRole("option", { name: "us-east-1a" }); change("Availability Zone", "us-east-1a");
    change("Storage (GB)", "256"); change("Throughput (MBps)", "320"); click("Create Pool");
    await waitFor(() => expect(screen.queryByLabelText("Name")).not.toBeInTheDocument());
    expect(requestBody("POST", "/api/v1/storage-pools")).toMatchObject({ az: "us-east-1a", fsx_storage_gb: 256, fsx_throughput_mbps: 320 });
  });
  it("uses an AWS provider for BYO NFS and surfaces rejected creation", async () => {
    routes["POST /api/v1/storage-pools"] = response({ detail: "NFS unreachable" }, 400);
    await openCreate("shared-byo"); change("NFS Endpoint", "nfs.example:/exports"); click("Create Pool");
    expect(await screen.findByText("NFS unreachable")).toBeInTheDocument();
    expect(requestBody("POST", "/api/v1/storage-pools")).toMatchObject({ provider_id: "aws", nfs_endpoint: "nfs.example:/exports" });
  });
});

describe("pool editing and resizing", () => {
  it.each([
    ["Throughput (MBps)", "100", "Throughput must be at least 160 MBps"],
    ["Storage (GB)", "100", "Storage can only grow, not shrink"],
    ["Storage (GB)", "130", "Storage increase must be at least 10% (minimum 141 GB)"],
  ])("rejects invalid %s=%s", async (label, value, error) => {
    await loaded(); click("Edit"); change(label, value); click("Save");
    expect(await screen.findByText(error)).toBeInTheDocument();
  });
  it("saves capacity and auto-extend settings", async () => {
    await loaded(); click("Edit"); change("Storage (GB)", "256"); change("Threshold (%)", "90");
    change("Increment (GB)", "128"); change("Max (GB, optional)", "1024"); click("Save");
    await waitFor(() => expect(screen.queryByRole("button", { name: "Save" })).not.toBeInTheDocument());
    expect(requestBody("PATCH")).toMatchObject({ fsx_storage_gb: 256, auto_extend_threshold_pct: 90, auto_extend_increment_gb: 128, auto_extend_max_gb: 1024 });
  });
  it.each([["", "NFS endpoint is required"], ["bad", "NFS endpoint must be in host:/path format"]])("validates BYO NFS edit %s", async (value, error) => {
    pool.mode = "shared-byo"; await loaded(); click("Edit"); change("NFS Endpoint", value); click("Save");
    expect(await screen.findByText(error)).toBeInTheDocument();
  });
  it("keeps edits after rejection and allows cancel", async () => {
    pool.mode = "shared-byo"; routes["PATCH /api/v1/storage-pools/pool-1"] = response({ detail: "Mount failed" }, 400);
    await loaded(); click("Edit"); change("NFS Endpoint", "new:/exports"); click("Save");
    expect(await screen.findByText("Mount failed")).toBeInTheDocument();
    expect(requestBody("PATCH")).toMatchObject({ nfs_endpoint: "new:/exports" });
    click("Cancel"); expect(screen.queryByLabelText("NFS Endpoint")).not.toBeInTheDocument();
  });
  it.each([[true, "Pool storage resized: 128 GB → 256 GB"], [false, "Resize denied"]])("resizes with success=%s", async (ok, message) => {
    routes["POST /api/v1/storage-pools/pool-1/extend"] = response({ detail: "Resize denied" }, ok ? 200 : 400);
    await loaded(); fireEvent.change(screen.getByPlaceholderText("128 GB"), { target: { value: "256" } }); click("Resize");
    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(requestBody("POST", "/api/v1/storage-pools/pool-1/extend")).toEqual({ increment_gb: 128 });
  });
  it("blocks sub-10% resize without requesting it", async () => {
    await loaded(); fireEvent.change(screen.getByPlaceholderText("128 GB"), { target: { value: "130" } }); click("Resize");
    expect(await screen.findByText("FSx requires at least 10% growth (minimum 141 GB)")).toBeInTheDocument();
  });
});

describe("pool and pattern buffer lifecycle", () => {
  it.each([
    ["provisioning", "Pattern Buffer: provisioning... · 192.0.2.1"],
    ["disconnected", "Pattern Buffer: disconnected · 192.0.2.1"],
  ])("shows %s buffer status", async (status, expected) => {
    pool.worker_status = status; pool.worker_host_id = "host-1"; pool.worker_ip = "192.0.2.1";
    await loaded(); expect(screen.getByText(expected)).toBeInTheDocument();
  });
  it("reports failed buffer cleanup", async () => {
    pool.worker_status = "connected"; pool.worker_host_id = "host-1";
    routes["POST /api/v1/hosts/host-1/gc"] = response({}, 500);
    await loaded(); click("Clean"); expect(await screen.findByText("Clean failed")).toBeInTheDocument();
  });
  it.each([true, false])("updates auto-extend with success=%s", async (ok) => {
    routes["PATCH /api/v1/storage-pools/pool-1"] = response({ detail: "Settings denied" }, ok ? 200 : 400);
    await loaded(); fireEvent.click(screen.getByRole("switch", { name: "Auto-extend" }));
    await waitFor(() => expect(requestBody("PATCH")).toEqual({ auto_extend_enabled: true }));
    if (!ok) expect(await screen.findByText("Settings denied")).toBeInTheDocument();
  });
  it("updates expanded auto-extend controls and ignores an invalid threshold", async () => {
    pool.auto_extend_enabled = true; await loaded(); click("▼");
    change("Threshold (%)", "40");
    expect(calls.mock.calls.some(([, opts]) => opts?.method === "PATCH")).toBe(false);
    change("Threshold (%)", "90"); change("Increment (GB)", "128"); change("Max (GB)", "512");
    await waitFor(() => expect(calls.mock.calls.filter(([, opts]) => opts?.method === "PATCH")).toHaveLength(3));
    expect(requestBody("PATCH")).toEqual({ auto_extend_threshold_pct: 90 });
  });
  it.each([true, false])("extends storage after confirmation=%s and handles rejection", async (accepted) => {
    pool.auto_extend_enabled = true; confirm.mockResolvedValue(accepted);
    routes["POST /api/v1/storage-pools/pool-1/extend"] = response({ detail: "Wait six hours" }, 400);
    await loaded(); click("▼"); click("Extend Now (+64 GB)");
    await waitFor(() => expect(confirm).toHaveBeenCalled());
    if (accepted) expect(await screen.findByText("Wait six hours")).toBeInTheDocument();
    else expect(calls.mock.calls.some(([, opts]) => opts?.method === "POST")).toBe(false);
  });
  it("keeps extension pending until a shared partition reaches requested capacity", async () => {
    pool.auto_extend_enabled = true; await loaded(); vi.useFakeTimers(); click("▼");
    await act(async () => { click("Extend Now (+64 GB)"); });
    expect(screen.getByRole("button", { name: /Extend Now/ })).toBeDisabled();
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(screen.getByRole("button", { name: /Extend Now/ })).toBeDisabled();
    routes["/api/v1/hosts/storage"] = response({ "host-1": { partitions: [{ mount: "/shared", total_bytes: 192 * 1024 ** 3, used_bytes: 0, used_pct: 0 }] } });
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(screen.getByRole("button", { name: /Extend Now/ })).not.toBeDisabled();
  });
  it("polls newly created pools until they settle", async () => {
    await openCreate("local"); change("Provider", "gcp"); vi.useFakeTimers();
    await act(async () => { click("Create Pool"); });
    pool.status = "creating";
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(screen.getByText("creating")).toBeInTheDocument();
    pool.status = "available";
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(screen.getByText("available")).toBeInTheDocument();
  });
  it("updates a buffer agent and clears pending status after refresh", async () => {
    pool.worker_status = "connected"; pool.worker_host_id = "host-1"; pool.worker_agent_version = "v1";
    pool.pb_auto_sleep_minutes = 30; pool.pb_last_activity_at = new Date(Date.now() - 20 * 60000).toISOString();
    await loaded(); await screen.findByRole("button", { name: "Update Pattern Buffer Agent" });
    vi.useFakeTimers(); await act(async () => { click("Update Pattern Buffer Agent"); });
    expect(screen.getByRole("button", { name: /Update Pattern Buffer Agent/ })).toBeDisabled();
    pool.worker_agent_version = "v2";
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(screen.queryByRole("button", { name: /Update Pattern Buffer Agent/ })).not.toBeInTheDocument();
  });
  it.each([true, false])("deletes buffer with confirmation=%s", async (accepted) => {
    pool.worker_status = "connected"; pool.worker_host_id = "host-1"; confirm.mockResolvedValue(accepted);
    await loaded(); click("Delete Pattern Buffer"); await waitFor(() => expect(confirm).toHaveBeenCalled());
    expect(calls.mock.calls.some(([url, opts]) => url.endsWith("/pattern-buffer") && opts?.method === "DELETE")).toBe(accepted);
  });
  it.each([true, false])("deletes only after confirmation=%s", async (accepted) => {
    confirm.mockResolvedValue(accepted); await loaded(); click("Delete");
    await waitFor(() => expect(confirm).toHaveBeenCalled());
    expect(calls.mock.calls.some(([, options]) => options?.method === "DELETE")).toBe(accepted);
  });
  it("reports rejected deletion", async () => {
    routes["DELETE /api/v1/storage-pools/pool-1"] = response({ detail: "Pool is busy" }, 409);
    await loaded(); click("Delete"); expect(await screen.findByText("Pool is busy")).toBeInTheDocument();
  });
  it("provisions a buffer with the selected instance type", async () => {
    await loaded(); click("Add Pattern Buffer");
    const dialog = screen.getByRole("dialog", { name: "Add Pattern Buffer" });
    fireEvent.change(within(dialog).getByLabelText("Instance Type"), { target: { value: "i4i.xlarge" } }); click("Provision");
    expect(requestBody("POST", "/api/v1/storage-pools/pool-1/pattern-buffer")).toEqual({ instance_type: "i4i.xlarge" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
  it("shows and dismisses buffer provisioning errors", async () => {
    pool.worker_status = "error"; pool.worker_error = "Quota exceeded";
    await loaded(); expect(screen.getByRole("dialog", { name: "Pattern Buffer Error" })).toHaveTextContent("Quota exceeded");
    click("Close"); expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
  it.each([["connected", "Sleep Pattern Buffer", "stop"], ["stopped", "Wake Pattern Buffer", "wake"]])("requests %s transition and keeps action pending", async (status, button, action) => {
    pool.worker_status = status; pool.worker_host_id = "host-1";
    await loaded(); click(button);
    await waitFor(() => expect(calls).toHaveBeenCalledWith(`/api/v1/storage-pools/pool-1/pattern-buffer/${action}`, { method: "POST" }));
    expect(screen.getByRole("button", { name: new RegExp(button) })).toBeDisabled();
  });
  it("cleans connected pattern buffer and configures auto-sleep", async () => {
    pool.worker_status = "connected"; pool.worker_host_id = "host-1";
    routes["POST /api/v1/hosts/host-1/gc"] = response({ cleanup: { cleaned: 3, cache_cleaned: 2 } });
    await loaded(); click("Clean"); expect(await screen.findByText("Cleaned: 3 orphans, 2 cache items")).toBeInTheDocument();
    click("OK"); fireEvent.change(screen.getByRole("combobox"), { target: { value: "30" } });
    expect(requestBody("PATCH")).toEqual({ pb_auto_sleep_minutes: 30 });
  });
});
