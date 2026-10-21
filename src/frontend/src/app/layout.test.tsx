import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import RootLayout from "./layout";

const harness = vi.hoisted(() => ({ path: "/projects", push: vi.fn(), confirm: vi.fn(), updateAgents: vi.fn() }));
vi.mock("next/navigation", () => ({ usePathname: () => harness.path, useRouter: () => ({ push: harness.push }) }));
vi.mock("next/link", () => ({ default: ({ children, href, onClick }: { children: ReactNode; href: string; onClick?: () => void }) => <a href={href} onClick={onClick}>{children}</a> }));
vi.mock("@/lib/confirm", () => ({ ConfirmHost: () => null, appConfirm: harness.confirm }));
vi.mock("@/lib/updateHostAgents", async (original) => ({ ...await original<typeof import("@/lib/updateHostAgents")>(), updateHostAgents: harness.updateAgents }));
vi.mock("@patternfly/react-core", () => {
  const Wrapper = ({ children }: { children?: ReactNode }) => <div>{children}</div>;
  return {
    Button: ({ children, onClick, isDisabled, "aria-label": label }: { children?: ReactNode; onClick?: () => void; isDisabled?: boolean; "aria-label"?: string }) => <button disabled={isDisabled} onClick={onClick} aria-label={label}>{children}</button>,
    Page: ({ children, masthead, sidebar }: { children?: ReactNode; masthead?: ReactNode; sidebar?: ReactNode }) => <main>{masthead}{sidebar}{children}</main>,
    NavItem: ({ children, to, onClick }: { children?: ReactNode; to?: string; onClick?: () => void }) => <a href={to} onClick={onClick}>{children}</a>,
    Masthead: Wrapper, MastheadBrand: Wrapper, MastheadContent: Wrapper, MastheadMain: Wrapper, MastheadToggle: Wrapper,
    Nav: Wrapper, NavList: Wrapper, PageSidebar: Wrapper, PageSidebarBody: Wrapper, PageToggleButton: Wrapper,
    Toolbar: Wrapper, ToolbarContent: Wrapper, ToolbarGroup: Wrapper, ToolbarItem: Wrapper,
  };
});
const response = (data: unknown, status = 200) => ({ ok: status < 400, status, json: async () => data }) as Response;
let routes: Record<string, Response | Error>;
let calls: ReturnType<typeof vi.fn>;
beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  harness.path = "/projects";
  harness.confirm.mockResolvedValue(true);
  harness.updateAgents.mockResolvedValue([]);
  routes = {
    "/api/v1/auth/me": response({ id: "u1", email: "alice@example.com", role: "user" }),
    "/api/v1/hosts/": response([]),
    "/api/v1/hosts/expected-agent-version": response({ version: "new" }),
    "/api/v1/hosts/storage": response({}),
    "/api/v1/update/status": response({ mode: "disabled" }),
  };
  calls = vi.fn(async (url: string) => {
    const result = routes[url] || response({});
    if (result instanceof Error) throw result;
    return result;
  });
  vi.stubGlobal("fetch", calls);
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });
function mount() { return render(<RootLayout><p>Page content</p></RootLayout>, { container: document.documentElement }); }
async function loaded(admin = false) {
  if (admin) routes["/api/v1/auth/me"] = response({ id: "admin", email: "admin@example.com", role: "admin" });
  mount();
  await screen.findByText(admin ? "admin@example.com" : "alice@example.com");
}
function click(name: string) { fireEvent.click(screen.getByRole("button", { name })); }

describe("application shell", () => {
  it("shows authenticated navigation and toggles the persisted theme", async () => {
    await loaded();
    expect(screen.getByText("Projects")).toBeInTheDocument();
    expect(screen.queryByText("Storage Pools")).not.toBeInTheDocument();
    click("Toggle theme");
    expect(localStorage.getItem("troshka-theme")).toBe("light");
    expect(document.documentElement).not.toHaveClass("pf-v6-theme-dark");
    click("Toggle theme");
    expect(localStorage.getItem("troshka-theme")).toBe("dark");
    expect(screen.getByAltText("Troshka").closest("a")).toHaveAttribute("href", "/");
  });
  it("restores a light theme and logs out clearing local credentials", async () => {
    localStorage.setItem("troshka-theme", "light");
    localStorage.setItem("troshka-token", "example");
    localStorage.setItem("troshka-user", "{}");
    await loaded();
    expect(screen.getByAltText("Troshka")).toHaveAttribute("src", "/images/troshka-logo-light-200.png");
    click("Log out");
    expect(localStorage.getItem("troshka-token")).toBeNull();
    expect(localStorage.getItem("troshka-user")).toBeNull();
    expect(harness.push).toHaveBeenCalledWith("/login");
  });
  it("switches development identity using backend issued token", async () => {
    routes["/api/v1/auth/dev-token/operator"] = response({ token: "example-token", user_id: "op", email: "operator@example.com", role: "operator" });
    await loaded();
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "operator" } });
    await screen.findByText("operator@example.com");
    expect(localStorage.getItem("troshka-token")).toBe("example-token");
    expect(JSON.parse(localStorage.getItem("troshka-user") || "{}").role).toBe("operator");
  });
  it("does not offer identity switching to SSO users", async () => {
    routes["/api/v1/auth/me"] = response({ email: "alice@example.com", role: "user", auth_source: "sso" });
    await loaded();
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  });
  it("shows denial detail without leaking page children", async () => {
    routes["/api/v1/auth/me"] = response({ detail: "Denied user blocked@example.com" }, 403);
    mount();
    expect(await screen.findByRole("heading")).toHaveTextContent("Denied user blocked@example.com");
    expect(screen.queryByText("Page content")).not.toBeInTheDocument();
  });
  it.each(["/console/vm", "/portal/lab"])("renders %s without navigation chrome", async (path) => {
    harness.path = path; mount();
    expect(await screen.findByText("Page content")).toBeInTheDocument();
    expect(screen.queryByAltText("Troshka")).not.toBeInTheDocument();
  });
  it("dismisses onboarding emphasis when getting started is visited", async () => {
    const view = mount();
    await screen.findByText("alice@example.com");
    harness.path = "/getting-started";
    view.rerender(<RootLayout><p>Page content</p></RootLayout>);
    await waitFor(() => expect(localStorage.getItem("troshka-seen-getting-started")).toBe("1"));
  });
});

describe("administrator update controls", () => {
  beforeEach(() => {
    routes["/api/v1/update/status"] = response({ mode: "dev", stale_key: "change-1", up_to_date: false });
  });
  it("dismisses only the current code revision", async () => {
    await loaded(true);
    await screen.findByText("A newer version of Troshka is available.");
    click("Dismiss");
    expect(localStorage.getItem("troshka-update-dismissed")).toBe("change-1");
    expect(screen.queryByText("A newer version of Troshka is available.")).not.toBeInTheDocument();
  });
  it.each([403, 500, 0])("shows update failure %s and permits retry", async (status) => {
    routes["/api/v1/update/apply"] = status ? response({}, status) : new Error("offline");
    await loaded(true);
    await screen.findByRole("button", { name: "Restart backend" });
    fireEvent.click(screen.getByLabelText("Also restart workers"));
    click("Restart backend");
    expect(await screen.findByText(/Update failed/)).toBeInTheDocument();
    expect(calls).toHaveBeenCalledWith("/api/v1/update/apply", expect.objectContaining({ body: JSON.stringify({ restart_workers: false }) }));
    expect(screen.getByRole("button", { name: "Restart backend" })).toBeEnabled();
  });
  it("keeps rollout failures visible despite an earlier dismissal", async () => {
    localStorage.setItem("troshka-update-dismissed", "change-1");
    routes["/api/v1/update/status"] = response({ mode: "image", stale_key: "change-1", up_to_date: false, rolling_out: true, rollout_error: "Node unavailable", components: { backend: { current: "sha256:0123456789abcdef", available: "sha256:fedcba9876543210" } } });
    await loaded(true);
    expect(await screen.findByText("Node unavailable")).toBeInTheDocument();
    expect(screen.getByText("backend 0123456789ab → fedcba987654")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Update in progress…" })).toBeDisabled();
  });
});

describe("host health and agent upgrades", () => {
  beforeEach(() => {
    routes["/api/v1/hosts/"] = response([
      { id: "one", agent_status: "connected", agent_version: "old", storage_pool_id: "pool", storage_warnings: [{ mount: "/shared", level: "critical" }] },
      { id: "two", agent_status: "connected", agent_version: "old", storage_warnings: [{ mount: "/var", level: "warning" }] },
      { id: "offline", agent_status: "disconnected", agent_version: "old" },
    ]);
    routes["/api/v1/hosts/storage"] = response({ one: { partitions: [{ mount: "/troshka", used_pct: 90 }] } });
  });
  it("keeps update actions busy until all requested host jobs finish", async () => {
    let finish!: (failed: string[]) => void;
    harness.updateAgents.mockReturnValue(new Promise<string[]>((resolve) => { finish = resolve; }));
    await loaded(true);
    click((await screen.findByRole("button", { name: "Update host agents" })).textContent!);
    expect(await screen.findByRole("button", { name: "Updating…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Dismiss" })).toBeDisabled();
    expect(harness.updateAgents).toHaveBeenCalledWith(["one", "two"]);
    routes["/api/v1/hosts/"] = response([]);
    await act(async () => finish([]));
    expect(screen.queryByRole("button", { name: "Updating…" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Update host agents" })).not.toBeInTheDocument();
  });
  it.each(["partial", "all", "exception"])("surfaces %s update failures and re-enables retry", async (mode) => {
    if (mode === "exception") harness.updateAgents.mockRejectedValue(new Error("Timed out waiting for hosts"));
    else harness.updateAgents.mockResolvedValue(mode === "partial" ? ["two"] : ["one", "two"]);
    await loaded(true);
    await screen.findByRole("button", { name: "Update host agents" });
    click("Update host agents");
    const error = mode === "partial" ? "1 of 2 host agent updates failed." : mode === "all" ? "Host agent update failed. Check Admin → Hosts." : "Timed out waiting for hosts";
    await waitFor(() => expect(screen.getAllByText(error).length).toBeGreaterThan(0));
    expect(screen.getByRole("button", { name: "Update host agents" })).toBeEnabled();
    click("Dismiss");
    expect(screen.queryByRole("button", { name: "Update host agents" })).not.toBeInTheDocument();
  });
  it("honors cancellation before starting agent updates", async () => {
    harness.confirm.mockResolvedValue(false);
    await loaded(true);
    await screen.findByRole("button", { name: "Update host agents" }); click("Update host agents");
    await waitFor(() => expect(harness.confirm).toHaveBeenCalled());
    expect(harness.updateAgents).not.toHaveBeenCalled();
  });
  it("reports repeated backend outages and clears the banner on recovery", async () => {
    await loaded();
    vi.useFakeTimers();
    // Intervals were registered with real timers; remount with fake timers.
    cleanup();
    routes["/api/v1/auth/me"] = new Error("offline");
    await act(async () => { mount(); });
    expect(screen.queryByText(/Backend is unreachable/)).not.toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(screen.getByText(/Backend is unreachable/)).toBeInTheDocument();
    routes["/api/v1/auth/me"] = response({}, 401);
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(screen.queryByText(/Backend is unreachable/)).not.toBeInTheDocument();
  });
});
