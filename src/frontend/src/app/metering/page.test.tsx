import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import MeteringPage from "./page";

const navigation = vi.hoisted(() => ({ tab: "active", push: vi.fn(), replace: vi.fn() }));
vi.mock("next/navigation", () => ({
  useRouter: () => navigation,
  useSearchParams: () => new URLSearchParams({ tab: navigation.tab }),
}));
const response = (data: unknown, status = 200): Response => ({ ok: status < 400, status, json: async () => data }) as Response;
const usage = [
  { kind: "vcpu", hours: 2, qty_hours: 4, unit_rate: 0.5, subtotal: 2 },
  { kind: "ram", qty_hours: 8, subtotal: 4 },
  { kind: "custom", qty_hours: 0, subtotal: 0 },
];
const projects = [
  { id: "alpha", name: "Alpha", state: "active", created_at: "2026-10-01T12:00:00Z", owner_email: "alice@example.com" },
  { id: "beta", name: "Beta", state: "stopped", created_at: "2026-09-01T12:00:00Z", owner_email: "bob@example.com" },
  { id: "draft", name: "Draft", state: "draft" },
];
const invoices = [
  { id: "i1", project_id: "alpha", project_name: "Alpha", total_usd: 20, currency: "USD", period_start: "2026-10-01T12:00:00Z", finalized_at: "2026-10-15T12:00:00Z", owner_email: "alice@example.com" },
  { id: "i2", project_id: "beta", project_name: "Beta", total_usd: 5, currency: "USD", period_start: null, finalized_at: null },
];
const statements = [
  { id: "s1", period_start: "2026-10-01T00:00:00Z", finalized_at: "2026-11-01T00:00:00Z", total_usd: 20, project_count: 2, owner_email: "alice@example.com" },
  { id: "s2", period_start: "2026-09-01T00:00:00Z", finalized_at: null, total_usd: 5, project_count: 1 },
];
let routes: Record<string, Response | Error>;
let createUrl: ReturnType<typeof vi.fn<(obj: Blob | MediaSource) => string>>;

beforeEach(() => {
  navigation.tab = "active";
  navigation.push.mockReset();
  navigation.replace.mockReset();
  routes = {
    "/api/v1/auth/me": response({ role: "admin" }),
    "/api/v1/projects/": response(projects),
    "/api/v1/projects/alpha/metering": response({ total_usd: 20, budget_usd: 100, line_items: { by_kind: usage } }),
    "/api/v1/projects/beta/metering": response({ total_usd: 5, budget_usd: null }),
    "/api/v1/metering/invoices": response(invoices),
    "/api/v1/metering/invoices/i1": response({ line_items: { by_kind: usage } }),
    "/api/v1/metering/invoices/i2": response({}),
    "/api/v1/metering/statements": response(statements),
    "/api/v1/metering/statements/s1": response({ line_items: { by_project: [
      { project_id: "alpha", project_name: 'Alpha, "demo"', subtotal: 20, by_kind: usage },
      { project_id: "empty", project_name: "Empty", subtotal: 0, by_kind: [] },
    ] } }),
    "/api/v1/metering/statements/s2": response({}),
  };
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    const result = routes[url];
    if (result instanceof Error) throw result;
    if (!result) throw new Error(`Unexpected test request: ${url}`);
    return result;
  }));
  createUrl = vi.fn(() => "blob:test");
  vi.stubGlobal("URL", class extends URL {
    static createObjectURL = createUrl;
    static revokeObjectURL = vi.fn();
  });
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
});

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

async function loaded(tab = "active") {
  navigation.tab = tab;
  render(<MeteringPage />);
  await screen.findByText("alice@example.com");
}

describe("active metering", () => {
  it("loads only non-draft projects and expands usage with explicit and derived rates", async () => {
    await loaded();
    expect(screen.queryByRole("button", { name: "Draft" })).not.toBeInTheDocument();
    expect(fetch).not.toHaveBeenCalledWith("/api/v1/projects/draft/metering");
    fireEvent.click(screen.getAllByRole("button", { name: "Expand breakdown" })[0]);
    expect(screen.getByText("vCPU")).toBeInTheDocument();
    expect(screen.getAllByText("$0.500000")).toHaveLength(2);
    expect(screen.getByText("custom")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Collapse breakdown" }));
    expect(screen.queryByText("vCPU")).not.toBeInTheDocument();
    fireEvent.click(screen.getAllByRole("button", { name: "Expand breakdown" })[1]);
    expect(screen.getByText("No usage yet.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Alpha" }));
    expect(navigation.push).toHaveBeenCalledWith("/projects/alpha");
  });
  it.each(["Project", "Owner", "State", "Spend", "Budget"])("sorts %s in both directions", async (column) => {
    await loaded();
    const header = screen.getByRole("columnheader", { name: new RegExp(`^${column}`) });
    fireEvent.click(within(header).getByRole("button"));
    const first = header.getAttribute("aria-sort");
    fireEvent.click(within(header).getByRole("button"));
    expect(header.getAttribute("aria-sort")).not.toBe(first);
    expect(screen.getAllByRole("button", { name: /^(Alpha|Beta)$/ })).toHaveLength(2);
  });
  it.each([
    ["Project name", "beta"], ["User", "bob"], ["Total max", "10"],
    ["Started to", "2026-09-30"],
  ])("filters active rows by %s and clears filters", async (label, value) => {
    await loaded();
    fireEvent.change(screen.getByLabelText(label), { target: { value } });
    expect(screen.queryByRole("button", { name: "Alpha" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Beta" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(screen.getByRole("button", { name: "Alpha" })).toBeInTheDocument();
  });
  it("combines minimum totals and start dates, with a clear empty result", async () => {
    await loaded();
    fireEvent.change(screen.getByLabelText("Total min"), { target: { value: "10" } });
    expect(screen.queryByRole("button", { name: "Beta" })).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Started from"), { target: { value: "2026-10-20" } });
    expect(screen.getByText("No rows match the current filters.")).toBeInTheDocument();
  });
  it("navigates between tabs using the URL", async () => {
    await loaded();
    fireEvent.click(screen.getByRole("button", { name: "Past" }));
    expect(navigation.replace).toHaveBeenLastCalledWith("/metering?tab=past");
    fireEvent.click(screen.getByRole("button", { name: "Invoices" }));
    expect(navigation.replace).toHaveBeenLastCalledWith("/metering?tab=invoices");
    fireEvent.click(screen.getByRole("button", { name: "Active" }));
    expect(navigation.replace).toHaveBeenLastCalledWith("/metering");
  });
});

describe("past billing", () => {
  it("loads and caches a project invoice breakdown", async () => {
    await loaded("past");
    fireEvent.click(screen.getAllByRole("button", { name: "Expand breakdown" })[0]);
    expect(await screen.findByText("vCPU")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Collapse breakdown" }));
    fireEvent.click(screen.getAllByRole("button", { name: "Expand breakdown" })[0]);
    expect(vi.mocked(fetch).mock.calls.filter(([url]) => url === "/api/v1/metering/invoices/i1")).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Alpha" }));
    expect(navigation.push).toHaveBeenCalledWith("/metering/i1");
  });
  it.each(["Project", "Owner", "Total", "Started", "Finalized"])("sorts historical invoices by %s", async (column) => {
    await loaded("past");
    const header = screen.getByRole("columnheader", { name: new RegExp(`^${column}`) });
    fireEvent.click(within(header).getByRole("button"));
    expect(header.getAttribute("aria-sort")).not.toBe("none");
  });
  it("filters finalization dates and excludes records with missing dates", async () => {
    await loaded("past");
    fireEvent.change(screen.getByLabelText("Finalized from"), { target: { value: "2026-10-01" } });
    expect(screen.queryByRole("button", { name: "Beta" })).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Finalized to"), { target: { value: "2026-10-10" } });
    expect(screen.getByText("No rows match the current filters.")).toBeInTheDocument();
  });
  it("shows a failed detail request without losing the invoice list", async () => {
    routes["/api/v1/metering/invoices/i1"] = response({}, 500);
    await loaded("past");
    fireEvent.click(screen.getAllByRole("button", { name: "Expand breakdown" })[0]);
    expect(await screen.findByText("Failed to load invoice")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Alpha" })).toBeInTheDocument();
  });
});

describe("monthly invoices", () => {
  it("expands per-project usage and caches the result", async () => {
    await loaded("invoices");
    fireEvent.click(screen.getAllByRole("button", { name: "Expand breakdown" })[0]);
    expect(await screen.findByText("vCPU")).toBeInTheDocument();
    expect(screen.getByText('Alpha, "demo"')).toBeInTheDocument();
    expect(screen.getByText("No usage yet.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Collapse breakdown" }));
    fireEvent.click(screen.getAllByRole("button", { name: "Expand breakdown" })[0]);
    expect(vi.mocked(fetch).mock.calls.filter(([url]) => url === "/api/v1/metering/statements/s1")).toHaveLength(1);
  });
  it.each(["Month", "Owner", "Projects", "Total", "Finalized"])("sorts monthly invoices by %s", async (column) => {
    await loaded("invoices");
    const header = screen.getByRole("columnheader", { name: new RegExp(`^${column}`) });
    fireEvent.click(within(header).getByRole("button"));
    expect(header.getAttribute("aria-sort")).not.toBe("none");
  });
  it("loads project names for filtering and exports the matching invoice", async () => {
    await loaded("invoices");
    fireEvent.change(screen.getByLabelText("Project name"), { target: { value: "alpha" } });
    await waitFor(() => expect(screen.getAllByRole("button", { name: "Expand breakdown" })).toHaveLength(1));
    fireEvent.click(screen.getByRole("button", { name: "Export CSV" }));
    await waitFor(() => expect(createUrl).toHaveBeenCalledOnce());
    expect(createUrl.mock.calls[0][0]).toBeInstanceOf(Blob);
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:test");
  });
  it("exports uncached details and reports a failed export", async () => {
    routes["/api/v1/metering/statements/s1"] = response({}, 500);
    await loaded("invoices");
    fireEvent.click(screen.getByRole("button", { name: "Export CSV" }));
    expect(await screen.findByText("Failed to export invoices")).toBeInTheDocument();
    expect(createUrl).not.toHaveBeenCalled();
  });
  it("exports all invoices including empty project lists", async () => {
    await loaded("invoices");
    fireEvent.click(screen.getByRole("button", { name: "Export CSV" }));
    await waitFor(() => expect(createUrl).toHaveBeenCalledOnce());
    expect(fetch).toHaveBeenCalledWith("/api/v1/metering/statements/s2");
  });
  it("shows empty and failed statement details", async () => {
    routes["/api/v1/metering/statements/s1"] = response({}, 500);
    await loaded("invoices");
    fireEvent.click(screen.getAllByRole("button", { name: "Expand breakdown" })[0]);
    expect(await screen.findByText("Failed to load statement")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Expand breakdown" }));
    expect(await screen.findByText("No usage in this month.")).toBeInTheDocument();
  });
});

describe("metering availability", () => {
  it("explains when metering is disabled", async () => {
    routes["/api/v1/metering/statements"] = response({}, 404);
    render(<MeteringPage />);
    expect(await screen.findByText("Metering is disabled on this instance")).toBeInTheDocument();
  });
  it.each([
    ["/api/v1/metering/statements", "Failed to load invoices"],
    ["/api/v1/projects/", "Failed to load live spend"],
  ])("reports request failures at %s", async (url, message) => {
    routes[url] = new Error("offline");
    render(<MeteringPage />);
    expect(await screen.findByText(message)).toBeInTheDocument();
  });
  it("hides the administrator user filter for ordinary users", async () => {
    routes["/api/v1/auth/me"] = response({ role: "user" });
    render(<MeteringPage />);
    await screen.findByRole("button", { name: "Alpha" });
    expect(screen.queryByLabelText("User")).not.toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: /Owner/ })).not.toBeInTheDocument();
  });
});
