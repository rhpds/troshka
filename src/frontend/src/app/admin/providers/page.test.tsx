import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import AdminProvidersPage from "./page";

const confirm = vi.hoisted(() => vi.fn());
vi.mock("@/lib/confirm", () => ({ appConfirm: confirm }));
const response = (data: unknown, status = 200): Response => ({ ok: status < 400, status, json: async () => data }) as Response;
let provider: Record<string, unknown>;
let routes: Record<string, Response | Error>;
let calls: ReturnType<typeof vi.fn>;

beforeEach(() => {
  confirm.mockReset().mockResolvedValue(true);
  provider = { id: "provider", name: "Example Provider", type: "ec2", state: "active", host_count: 0, default_region: "us-east-1", has_credentials: true, default_image: "image", vpc_id: "vpc", subnet_id: "subnet", security_group_id: "sg" };
  routes = {};
  calls = vi.fn(async (url: string, options?: RequestInit) => {
    const result = routes[`${options?.method || "GET"} ${url}`] || routes[url];
    if (result instanceof Error) throw result;
    if (result) return result;
    if (url === "/api/v1/providers/" && !options?.method) return response([provider]);
    if (url.endsWith("/build-image/status")) return response({ status: "idle" });
    return response({});
  });
  vi.stubGlobal("fetch", calls);
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });
async function loaded(type = "ec2") { provider.type = type; render(<AdminProvidersPage />); await screen.findByText("Example Provider"); }
function click(name: string) { fireEvent.click(screen.getByRole("button", { name })); }
function change(label: string | RegExp, value: string) { fireEvent.change(screen.getByLabelText(label), { target: { value } }); }

describe("provider forms", () => {
  it.each([
    ["kubevirt", { "API URL": "https://cluster.example.com", Token: "example", "Cache Namespace": "cache", "Project Namespace Prefix": "lab-" }],
    ["ocpvirt", { "API URL": "https://cluster.example.com", Token: "example", "Package Repo URL": "https://packages.example.com" }],
    ["gcp", { "GCP Project ID": "project", "Service Account JSON": "{}" }],
    ["azure", { "Tenant ID": "tenant", "Client ID": "client", "Client Secret": "example", "Subscription ID": "subscription", "Location (optional)": "eastus" }],
    ["ec2", { "Access Key ID": "example", "Secret Access Key": "example" }],
    ["s3_readonly", { "Access Key ID": "example", "Secret Access Key": "example", "S3 Bucket": "images" }],
  ] as const)("creates %s through the actual form", async (type, fields) => {
    await loaded(); click("+ Add Provider"); change("Name", "New provider"); change("Type", type);
    for (const [label, value] of Object.entries(fields)) change(label, value);
    click("Create Provider");
    await waitFor(() => expect(calls).toHaveBeenCalledWith("/api/v1/providers/", expect.objectContaining({ method: "POST" })));
    const call = calls.mock.calls.find(([url, options]) => url === "/api/v1/providers/" && options?.method === "POST");
    expect(JSON.parse(call?.[1]?.body as string)).toMatchObject({ name: "New provider", type });
    await waitFor(() => expect(screen.queryByRole("button", { name: "Create Provider" })).not.toBeInTheDocument());
  });
  it("shows validation and server failures without dismissing the form", async () => {
    await loaded(); click("+ Add Provider"); click("Create Provider");
    expect(screen.getByText("Name, API URL, and Token are required")).toBeInTheDocument();
    change("Name", "New"); change("API URL", "https://cluster.example.com"); change("Token", "example");
    routes["POST /api/v1/providers/"] = response({ detail: "Duplicate provider" }, 409);
    click("Create Provider"); expect(await screen.findByText("Duplicate provider")).toBeInTheDocument();
  });
  it.each(["ec2", "kubevirt", "ocpvirt"])("edits %s while preserving blank secrets", async type => {
    await loaded(type); click("Edit"); change("Name", "Renamed"); click("Save");
    await waitFor(() => expect(calls).toHaveBeenCalledWith("/api/v1/providers/provider", expect.objectContaining({ method: "PATCH" })));
    const call = calls.mock.calls.find(([, options]) => options?.method === "PATCH");
    const body = JSON.parse(call?.[1]?.body as string);
    expect(body.name).toBe("Renamed"); expect(body).not.toHaveProperty("token"); expect(body).not.toHaveProperty("secret_access_key");
  });
  it("keeps edits available after save failure and supports cancel", async () => {
    routes["PATCH /api/v1/providers/provider"] = response({ detail: "Cannot update" }, 400);
    await loaded(); click("Edit"); click("Save");
    expect(await screen.findByText("Cannot update")).toBeInTheDocument(); click("Cancel");
    expect(screen.getByRole("button", { name: "Edit" })).toBeInTheDocument();
  });
});

describe("provider operations", () => {
  it.each([
    ["s3", "Create Bucket", "/api/v1/providers/provider/create-bucket", { bucket: "images", status: "created" }, "Bucket: images (created)"],
    ["s3", "Scan S3", "/api/v1/library/scan-s3", { imported: 2, snapshots: 1, patterns: 1 }, "Imported: 2 library item(s), 1 snapshot(s), 1 pattern(s)"],
    ["s3_readonly", "Sync Library", "/api/v1/library/sync-central", { created: 1, updated: 2, skipped: 3 }, "Synced: 1 new, 2 updated, 3 unchanged"],
    ["kubevirt", "Install Operator", "/api/v1/providers/provider/install-operator", {}, "Operator installed successfully"],
  ] as const)("performs %s %s", async (type, button, url, data, message) => {
    routes[url] = response(data); await loaded(type); click(button);
    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(calls).toHaveBeenCalledWith(url, { method: "POST" });
  });
  it.each([200, 400])("reports provider test outcome status %i", async status => {
    routes["/api/v1/providers/provider/test"] = response({ message: "Connected" }, status);
    await loaded(); click("Test"); expect(await screen.findByText(status === 200 ? "Connected" : "FAILED")).toBeInTheDocument();
  });
  it("honors cancelled deletion without sending the destructive request", async () => {
    confirm.mockResolvedValue(false); await loaded(); click("Delete");
    await waitFor(() => expect(confirm).toHaveBeenCalled());
    expect(calls.mock.calls.some(([, options]) => options?.method === "DELETE")).toBe(false);
  });
  it("reports failed deletion and disables deletion of occupied cloud providers", async () => {
    routes["DELETE /api/v1/providers/provider"] = response({ detail: "Still in use" }, 409);
    await loaded(); click("Delete"); expect(await screen.findByText("Still in use")).toBeInTheDocument();
  });
  it("keeps an occupied cloud provider undeletable", async () => {
    provider.host_count = 2; await loaded(); expect(screen.getByRole("button", { name: "Has Hosts" })).toBeDisabled();
  });
  it("discovers and selects an image from expanded provider details", async () => {
    routes["/api/v1/providers/provider/discover-images"] = response({ images: [{ type: "BYOS", label: "RHEL 10 Gold", image_id: "gold", name: "rhel-10.2", created: "2026-01-01" }] });
    await loaded(); const summary = screen.getByText(/Select Image/); const details = summary.closest("details")!;
    details.open = true; fireEvent(details, new Event("toggle"));
    expect(await screen.findByText("RHEL 10 Gold")).toBeInTheDocument(); click("Select");
    await waitFor(() => expect(calls).toHaveBeenCalledWith("/api/v1/providers/provider/set-image?image_id=gold", { method: "POST" }));
  });
});
