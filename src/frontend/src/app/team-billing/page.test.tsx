import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import TeamBillingPage from "./page";
import { exampleUsage } from "@/components/quotas/test-fixtures";

vi.mock("@/lib/confirm", () => ({ appConfirm: vi.fn(async () => true) }));
const person = { id: "u", email: "Member@example.com", display_name: "Member" };
const manager = { id: "m", email: "Manager@example.com", display_name: "Manager" };
const group = (id = "a") => ({ id, name: id === "a" ? "Alpha" : "Beta", description: "Group", members: [person], managers: [manager] });
const report = (id = "a") => ({ group: group(id), period_start: "2026-11-01T00:00:00Z", period_end: "2026-12-01T00:00:00Z", as_of: "2026-11-03T00:00:00Z", currency: "USD", billing_view: "organization_attributed_usage", capacity_state: "unverified", accounting_started_at: "2026-11-01T00:00:00Z", accounting_status: "partial", reason: "Full monetary source coverage has not been verified", known_subtotal_usd: "3.005", total_usd: null, members: [{ owner_id: "u", owner_email: person.email, display_name: "Member", current_member: true, status: "provisional", accounting_status: "partial", known_subtotal_usd: "3.005", total_usd: null, by_project: [{ project_id: "p", project_name: `${id}-project`, subtotal: "3.005", total_usd: null, known_subtotal_usd: "3.005", accounting_status: "partial", by_kind: [{ kind: "vcpu", subtotal: "3.005" }] }] }], active_projects: [] });
const doc = { id: "i", project_name: "Invoice project", owner_id: "u", owner_email: person.email, period_start: "2026-11-01T00:00:00Z", period_end: "2026-11-02T00:00:00Z", total_usd: 3, currency: "USD", finalized_at: "2026-11-02T00:00:00Z" };
let overrides: Record<string, unknown>;
let statuses: Record<string, number>;
const response = (data: unknown, status = 200) => ({ ok: status < 400, status, json: async () => data }) as Response;
let download: ReturnType<typeof vi.fn<(obj: Blob | MediaSource) => string>>;
beforeEach(() => {
  overrides = {}; statuses = {};
  vi.stubGlobal("fetch", vi.fn(async (url: string, options?: RequestInit) => {
    if (url.includes("/quotas/")) return new Response(JSON.stringify(overrides[url] || []), { status: statuses[url] || 200 });
    if (url in overrides) return response(overrides[url], statuses[url]);
    if (url === "/api/v1/groups") return response([group(), group("b")]);
    if (/\/groups\/[ab]$/.test(url)) return response(group(url.endsWith("a") ? "a" : "b"));
    if (url.includes("/report?")) return response(report(url.includes("/groups/b/") ? "b" : "a"));
    if (url.includes("/invoices")) return response(url.endsWith("/i") ? { ...doc, line_items: { by_kind: [{ kind: "vcpu", qty_hours: 6, unit_rate: .5, subtotal: 3 }] } } : [doc]);
    if (url.includes("/statements")) return response([]);
    if (url.includes("/managers")) return response(group());
    throw new Error(`Unexpected request: ${url} ${options?.method}`);
  }));
  download = vi.fn(() => "blob:test");
  vi.stubGlobal("URL", class extends URL { static createObjectURL = download; static revokeObjectURL = vi.fn(); });
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
});
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });
async function loaded() {
  render(<TeamBillingPage />);
  await screen.findByLabelText("Billing month (UTC)");
  fireEvent.change(screen.getByLabelText("Billing month (UTC)"), { target: { value: "2026-11" } });
  await screen.findByRole("button", { name: "Download monthly CSV" });
}
describe("team billing", () => {
  it("separates group ceilings and user limits from the usage month", async () => {
    overrides["/api/v1/quotas/organizations"] = [{ id: "a", name: "Alpha organization", cost_center_id: null }];
    overrides["/api/v1/quotas/organizations/a/members"] = [{ id: "quota-user", email: "Assigned@example.com", display_name: null, organization_id: "a" }];
    overrides["/api/v1/quotas/organization/a/usage"] = { ...exampleUsage, scope: "organization", scope_id: "a", can_edit: false };
    overrides["/api/v1/quotas/user/quota-user/usage"] = { ...exampleUsage, scope_id: "quota-user" };
    await loaded();
    fireEvent.change(await screen.findByLabelText("Quota group"), { target: { value: "a" } });
    expect(await screen.findByText("Cost center not assigned")).toBeVisible();
    expect(screen.getByLabelText("CPU limit")).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Organization user limits"), { target: { value: "quota-user" } });
    await waitFor(() => expect(screen.getAllByLabelText("CPU limit")).toHaveLength(2));
    expect(screen.getAllByLabelText("CPU limit")[1]).toBeEnabled();
    fireEvent.change(screen.getByLabelText("Billing month (UTC)"), { target: { value: "2026-10" } });
    expect(screen.getByLabelText("Organization user limits")).toHaveValue("quota-user");
    expect(vi.mocked(fetch).mock.calls.some(([url]) => String(url).includes("/users/"))).toBe(false);
  });
  it("starts with compact usage and separates invoices and statements", async () => {
    await loaded();
    expect(screen.getByRole("tab", { name: "Usage", selected: true })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "View invoice i" })).not.toBeInTheDocument();
    expect(screen.getByText("Group administrators (1)").closest("details")).not.toHaveAttribute("open");
    fireEvent.click(screen.getByRole("tab", { name: "Statements" }));
    expect(screen.getByText("No statements for this selection.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Download monthly CSV" })).not.toBeInTheDocument();
  });
  it("switches groups, clears details/exports immediately, and never loads the user directory", async () => {
    await loaded();
    fireEvent.click(screen.getByRole("tab", { name: "Invoices" }));
    fireEvent.click(screen.getByRole("button", { name: "View invoice i" }));
    await screen.findByRole("button", { name: "Download document CSV" });
    fireEvent.change(screen.getByLabelText("Group"), { target: { value: "b" } });
    expect(screen.queryByRole("button", { name: "Download document CSV" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Download monthly CSV" })).not.toBeInTheDocument();
    await screen.findByText(/b-project/);
    expect(vi.mocked(fetch).mock.calls.some(([url]) => String(url).includes("/users"))).toBe(false);
  });
  it("filters owners/months in scoped requests and downloads summary and document CSV", async () => {
    await loaded();
    fireEvent.change(screen.getByLabelText("Usage member"), { target: { value: "u" } });
    await waitFor(() => expect(fetch).toHaveBeenCalledWith("/api/v1/metering/groups/a/report?year=2026&month=11&owner_id=u", undefined));
    fireEvent.click(await screen.findByRole("button", { name: "Download monthly CSV" }));
    expect(download).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole("tab", { name: "Invoices" }));
    fireEvent.click(screen.getByRole("button", { name: "View invoice i" }));
    fireEvent.click(await screen.findByRole("button", { name: "Download document CSV" }));
    expect(download).toHaveBeenCalledTimes(2);
    fireEvent.change(screen.getByLabelText("Billing month (UTC)"), { target: { value: "2026-10" } });
    await screen.findByText("No invoices for this selection.");
  });
  it("ignores a late response from the previous group", async () => {
    const original = vi.mocked(fetch).getMockImplementation()!;
    let complete: ((value: Response) => void) | undefined;
    vi.mocked(fetch).mockImplementation((input, options) => {
      if (String(input).includes("/groups/a/report?")) return new Promise(resolve => { complete = resolve; });
      return original(input, options);
    });
    render(<TeamBillingPage />);
    await screen.findByText("Loading billing…");
    fireEvent.change(screen.getByLabelText("Group"), { target: { value: "b" } });
    await screen.findByText(/b-project/);
    complete!(response(report("a")));
    await waitFor(() => expect(screen.queryByText(/a-project/)).not.toBeInTheDocument());
  });
  it("labels unavailable member totals as incomplete", async () => {
    const unavailable = report(); unavailable.members[0].status = "unavailable";
    overrides["/api/v1/metering/groups/a/report?year=2026&month=11"] = { ...unavailable, members: [{ ...unavailable.members[0], total_usd: null }] };
    await loaded();
    expect(screen.getByText(/Incomplete monthly total/)).toBeInTheDocument();
    expect(screen.getByText(/Full monetary source coverage/)).toBeInTheDocument();
  });
  it("supports empty discovery and API errors", async () => {
    overrides["/api/v1/groups"] = [];
    render(<TeamBillingPage />);
    expect(await screen.findByText(/You do not manage any groups/)).toBeInTheDocument();
  });
  it("discards all group data on document access denial", async () => {
    overrides["/api/v1/metering/invoices/i"] = { detail: "Access revoked" }; statuses["/api/v1/metering/invoices/i"] = 403;
    await loaded(); fireEvent.click(screen.getByRole("tab", { name: "Invoices" })); fireEvent.click(screen.getByRole("button", { name: "View invoice i" }));
    await screen.findByText("Access revoked");
    expect(screen.queryByText("Active project lifetime spend")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Download monthly CSV" })).not.toBeInTheDocument();
  });
  it("shows report failure without leaving an export", async () => {
    overrides["/api/v1/metering/groups/a/report?year=2026&month=11"] = { detail: "Metering is disabled" }; statuses["/api/v1/metering/groups/a/report?year=2026&month=11"] = 404;
    render(<TeamBillingPage />);
    expect(await screen.findByText("Metering is disabled")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Download monthly CSV" })).not.toBeInTheDocument();
  });
  it("shows administrators read-only for group administrators", async () => {
    await loaded();
    fireEvent.click(screen.getByText("Group administrators (1)"));
    expect(screen.getByText(manager.email)).toBeVisible();
    expect(screen.queryByRole("button", { name: /Add administrator|Remove administrator/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Download monthly CSV" })).toBeInTheDocument();
  });
  it("filters former-member usage without requesting former-member owner documents", async () => {
    const data = report();
    overrides["/api/v1/metering/groups/a/report?year=2026&month=11"] = { ...data, members: [...data.members, { ...data.members[0], owner_id: "former", owner_email: null, display_name: "Immutable former name", current_member: false }] };
    await loaded();
    fireEvent.change(screen.getByLabelText("Usage member"), { target: { value: "former" } });
    await waitFor(() => expect(fetch).toHaveBeenCalledWith("/api/v1/metering/groups/a/report?year=2026&month=11&owner_id=former", undefined));
    expect(vi.mocked(fetch).mock.calls.some(([url]) => /\/(invoices|statements)\?owner_id=former/.test(String(url)))).toBe(false);
    fireEvent.click(screen.getByRole("tab", { name: "Invoices" }));
    expect(screen.getByLabelText("Owner documents")).not.toHaveTextContent("Immutable former name");
    expect(screen.getByText(/Owner billing history/)).toBeVisible();
  });
  it("shows known decimal costs separately from unknown totals and a fresh accounting boundary", async () => {
    await loaded();
    expect(screen.getByText("Known subtotal: $3.01")).toBeVisible();
    expect(screen.getByText(/Incomplete monthly total: Unavailable/)).toBeVisible();
    expect(screen.getByText(/Group accounting started/)).toBeVisible();
    fireEvent.click(screen.getByText(/Member@example.com —/));
    fireEvent.click(screen.getByText(/a-project —/));
    expect(screen.queryByText("NaN")).not.toBeInTheDocument();
    expect(screen.getAllByText("Unavailable").length).toBeGreaterThan(1);
  });
  it("clears cached usage immediately when refreshing revoked group access", async () => {
    await loaded();
    overrides["/api/v1/groups/a"] = { detail: "Group not found" }; statuses["/api/v1/groups/a"] = 404;
    fireEvent.click(screen.getByRole("button", { name: "Refresh group and billing" }));
    expect(screen.queryByRole("button", { name: "Download monthly CSV" })).not.toBeInTheDocument();
    await screen.findByText("Group not found");
  });
});
