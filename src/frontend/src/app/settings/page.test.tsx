import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import SettingsPage from "./page";
const confirm = vi.hoisted(() => vi.fn());
vi.mock("@/lib/confirm", () => ({ appConfirm: confirm }));
const response = (data: unknown, status = 200) => ({ ok: status < 400, json: async () => data }) as Response;
let routes: Record<string, Response>;
let calls: ReturnType<typeof vi.fn>;
beforeEach(() => {
  vi.clearAllMocks(); confirm.mockResolvedValue(true);
  routes = {
    "/api/v1/api-keys/": response([{ id: "key-1", name: "CI", key_prefix: "prefix", is_active: true, created_at: "2026-10-01" }]),
    "/api/v1/auth/rh-offline-token": response({ has_token: false }),
    "/api/v1/auth/ocp-pull-secret": response({ has_secret: false }),
    "/api/v1/auth/registry-credentials": response([]),
    "/api/v1/auth/ssh-keys": response([]),
    "/api/v1/auth/me": response({ role: "admin" }),
    "/api/v1/admin/settings": response({ ingress_controller: "default" }),
    "/api/v1/metering/rates": response({ kubevirt: { vcpu_hour: 0.1 } }),
  };
  calls = vi.fn(async (url: string, options?: RequestInit) => routes[`${options?.method || "GET"} ${url}`] || routes[url] || response({}));
  vi.stubGlobal("fetch", calls);
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: vi.fn().mockResolvedValue(undefined) } });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });
function click(name: string) { fireEvent.click(screen.getByRole("button", { name })); }
function change(label: string, value: string) { fireEvent.change(screen.getByLabelText(label), { target: { value } }); }
async function section(name: string) { render(<SettingsPage />); await screen.findByRole("button", { name: "OpenShift IngressController" }); click(name); }
function body(method: string, path: string) { const call = calls.mock.calls.find(([url, opts]) => url === path && opts?.method === method); expect(call).toBeDefined(); return JSON.parse(call![1]!.body as string); }
describe("user settings", () => {
  it("validates API key name and creates an expiring key", async () => {
    routes["POST /api/v1/api-keys/"] = response({ id: "key-2", name: "New key", key: "secret-once", key_prefix: "new", created_at: "2026-10-01" });
    await section("API Keys"); click("Create Key"); expect(screen.getByText("Name is required")).toBeInTheDocument();
    change("Name", " New key "); change("Expires", "30"); click("Create Key");
    expect(await screen.findByText("secret-once")).toBeInTheDocument(); expect(body("POST", "/api/v1/api-keys/")).toEqual({ name: "New key", expires_days: 30 });
  });
  it("reports rejected API key creation", async () => {
    routes["POST /api/v1/api-keys/"] = response({}, 400);
    await section("API Keys"); change("Name", "Demo"); click("Create Key"); expect(await screen.findByText("Failed to create API key")).toBeInTheDocument();
  });
  it.each([true, false])("revokes API keys with confirmation=%s", async (accepted) => {
    confirm.mockResolvedValue(accepted); await section("API Keys"); click("Revoke"); await waitFor(() => expect(confirm).toHaveBeenCalled());
    if (accepted) await waitFor(() => expect(screen.queryByText("CI")).not.toBeInTheDocument());
    else expect(calls.mock.calls.some(([, opts]) => opts?.method === "DELETE")).toBe(false);
  });
  it("validates and adds an SSH public key", async () => {
    await section("SSH Public Keys"); click("Add Key"); expect(screen.getByText("Name and key are required")).toBeInTheDocument();
    change("Name", "Laptop"); change("Public Key", "ssh-ed25519 public"); click("Add Key");
    await waitFor(() => expect(screen.getByLabelText("Name")).toHaveValue(""));
    expect(body("POST", "/api/v1/auth/ssh-keys")).toEqual({ name: "Laptop", public_key: "ssh-ed25519 public" });
  });
  it("shows SSH key rejection and deletes an existing key", async () => {
    routes["/api/v1/auth/ssh-keys"] = response([{ id: 1, name: "Old key", public_key: "ssh-rsa " + "a".repeat(100) }]);
    routes["POST /api/v1/auth/ssh-keys"] = response({ detail: "Invalid key" }, 400);
    await section("SSH Public Keys"); change("Name", "Bad"); change("Public Key", "invalid"); click("Add Key");
    expect(await screen.findByText("Invalid key")).toBeInTheDocument(); click("Delete");
    await waitFor(() => expect(calls).toHaveBeenCalledWith("/api/v1/auth/ssh-keys/1", { method: "DELETE" }));
  });
  it.each([true, false])("saves offline token with success=%s", async (ok) => {
    routes["PUT /api/v1/auth/rh-offline-token"] = response({ detail: "Token rejected" }, ok ? 200 : 400);
    await section("Red Hat Offline Token"); fireEvent.change(screen.getByPlaceholderText("eyJhbGci..."), { target: { value: "offline-token" } }); click("Save Token");
    if (ok) await waitFor(() => expect(screen.queryByPlaceholderText("eyJhbGci...")).not.toBeInTheDocument());
    else expect(await screen.findByText("Token rejected")).toBeInTheDocument();
    expect(body("PUT", "/api/v1/auth/rh-offline-token")).toEqual({ offline_token: "offline-token" });
  });
  it("replaces and deletes a saved offline token", async () => {
    routes["/api/v1/auth/rh-offline-token"] = response({ has_token: true, masked: "masked-token" });
    await section("Red Hat Offline Token"); click("Replace"); click("Cancel"); expect(screen.getByText("masked-token")).toBeInTheDocument(); click("Delete");
    await screen.findByPlaceholderText("eyJhbGci...");
  });
  it.each([true, false])("saves pull secret with success=%s", async (ok) => {
    routes["PUT /api/v1/auth/ocp-pull-secret"] = response({ detail: "Secret rejected" }, ok ? 200 : 400);
    await section("OCP Pull Secret"); fireEvent.change(screen.getByPlaceholderText('{"auths":{"cloud.openshift.com":...}}'), { target: { value: '{"auths":{}}' } }); click("Save Pull Secret");
    if (ok) await waitFor(() => expect(screen.queryByRole("button", { name: "Save Pull Secret" })).not.toBeInTheDocument());
    else expect(await screen.findByText("Secret rejected")).toBeInTheDocument();
    expect(body("PUT", "/api/v1/auth/ocp-pull-secret")).toEqual({ pull_secret: '{"auths":{}}' });
  });
  it("creates pull-through registry credentials", async () => {
    await section("OCP Pull Secret"); fireEvent.click(screen.getByRole("switch", { name: "Use a pull-through registry" }));
    change("Registry URL", "registry.example.com"); change("Username", "user"); change("Password", "password"); click("Save");
    await waitFor(() => expect(calls.mock.calls.some(([, opts]) => opts?.method === "PUT")).toBe(true));
    expect(body("PUT", "/api/v1/auth/ocp-pull-secret")).toMatchObject({ pull_through_registry: true, pull_through_registry_url: "registry.example.com", pull_through_registry_user: "user", pull_through_registry_password: "password" });
  });
  it("adds a registry credential", async () => {
    await section("Registry Credentials"); click("+ Add Registry Credential"); change("Name", "Quay"); change("Registry", "quay.io"); change("Username", "robot"); change("Password", "secret"); click("Add");
    await waitFor(() => expect(screen.queryByLabelText("Password")).not.toBeInTheDocument());
    expect(body("POST", "/api/v1/auth/registry-credentials")).toEqual({ name: "Quay", registry: "quay.io", username: "robot", password: "secret" });
  });
  it("edits and deletes registry credentials", async () => {
    routes["/api/v1/auth/registry-credentials"] = response([{ id: "cred-1", name: "Quay", registry: "quay.io", username: "robot" }]);
    await section("Registry Credentials"); click("Edit"); change("Username", "new-robot"); click("Update");
    await waitFor(() => expect(screen.queryByLabelText("Password")).not.toBeInTheDocument()); expect(body("PUT", "/api/v1/auth/registry-credentials/cred-1")).toMatchObject({ username: "new-robot", password: "" });
    click("Delete"); await waitFor(() => expect(calls).toHaveBeenCalledWith("/api/v1/auth/registry-credentials/cred-1", { method: "DELETE" }));
  });
  it.each([true, false])("saves ingress controller with success=%s", async (ok) => {
    routes["PUT /api/v1/admin/settings"] = response({ ingress_controller: "public", detail: "Forbidden" }, ok ? 200 : 403);
    await section("OpenShift IngressController"); change("Controller name", " public "); click("Save");
    expect(await screen.findByText(ok ? "Saved. New routes use this IngressController on next deploy." : "Forbidden")).toBeInTheDocument();
    expect(body("PUT", "/api/v1/admin/settings")).toEqual({ ingress_controller: "public" });
  });
  it.each([true, false])("saves metering rates with success=%s", async (ok) => {
    routes["PUT /api/v1/metering/rates"] = response({ kubevirt: { vcpu_hour: 0.2 }, detail: "Invalid rate" }, ok ? 200 : 400);
    await section("Kubevirt Metering Rates"); change("CPU per core per hour ($)", "0.2"); click("Save");
    expect(await screen.findByText(ok ? "Rates saved" : "Invalid rate")).toBeInTheDocument();
    expect(body("PUT", "/api/v1/metering/rates")).toEqual({ rates: { kubevirt: { vcpu_hour: 0.2 } } });
  });
});
