import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import GroupsPage from "./page";
import { appConfirm } from "@/lib/confirm";
import { exampleUsage } from "@/components/quotas/test-fixtures";
vi.mock("@/lib/confirm", () => ({ appConfirm: vi.fn(async () => true) }));
const person = { id: "u", email: "User@example.com", display_name: "User" };
const other = { id: "v", email: "Alice@example.com", display_name: "Alice Smith" };
const initial = { id: "g", name: "Engineering", description: "Group", members: [], managers: [person] };
let current: { id: string; name: string; description: string; members: typeof person[]; managers: typeof person[] };
let role: string;
let fail: boolean;
const response = (data: unknown, status = 200) => ({ ok: status < 400, status, json: async () => data }) as Response;
beforeEach(() => {
  role = "admin"; fail = false; current = { ...initial };
  vi.mocked(appConfirm).mockResolvedValue(true);
  vi.stubGlobal("fetch", vi.fn(async (url: string, options?: RequestInit) => {
    if (url.endsWith("/auth/me")) return response({ role });
    if (url.endsWith("/users/")) return response([person, other]);
    if (url === "/api/v1/groups" && options?.method === "POST") { if (fail) return response({ detail: "Group name already exists" }, 409); current = { ...current, ...JSON.parse(options.body as string) }; return response(current, 201); }
    if (url === "/api/v1/groups") return response([current]);
    if (url.endsWith("/members/u")) { if (fail) return response({ detail: "Transfer failed; membership unchanged" }, 409); current = { ...current, members: options?.method === "PUT" ? [person] : [] }; }
    if (url.endsWith("/groups/g") && options?.method === "PATCH") current = { ...current, ...JSON.parse(options.body as string) };
    if (url.endsWith("/groups/g") && options?.method === "DELETE") return response(null, 204);
    return response(current);
  }));
});
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });
async function selected() {
  render(<GroupsPage />);
  fireEvent.click(await screen.findByRole("button", { name: "Engineering" }));
  await screen.findByRole("tab", { name: "Members (0)" });
}
describe("group administration", () => {
  it("offers members, administrators, quotas and settings for every group without a type choice", async () => {
    const previous = vi.mocked(fetch).getMockImplementation()!;
    vi.mocked(fetch).mockImplementation(async (url, options) => {
      if (String(url).includes("/quotas/organizations/g/members")) return new Response(JSON.stringify([{ ...person, organization_id: "g" }]));
      if (String(url).includes("/quotas/organization/g/usage")) return new Response(JSON.stringify({ ...exampleUsage, scope: "organization", scope_id: "g" }));
      return previous(url, options);
    });
    await selected();
    fireEvent.click(screen.getByRole("tab", { name: "Settings" }));
    expect(screen.queryByLabelText("Quota organization")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Cost center (optional)"), { target: { value: "CC-42" } });
    fireEvent.click(screen.getByRole("button", { name: "Save group" }));
    await waitFor(() => expect(current).toMatchObject({ cost_center_id: "CC-42" }));
    fireEvent.click(screen.getByRole("tab", { name: "Quotas" }));
    expect(await screen.findByLabelText("Organization user limits")).toHaveTextContent(person.email);
    fireEvent.click(screen.getByRole("tab", { name: "Members (0)" }));
    expect(screen.getByRole("button", { name: "Add member" })).toBeVisible();
  });
  it("starts with the group list and opens focused management tabs", async () => {
    render(<GroupsPage />);
    expect(await screen.findByRole("table", { name: "Groups" })).toBeInTheDocument();
    expect(screen.queryByLabelText("New group name")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Engineering" }));
    expect(await screen.findByRole("tab", { name: "Members (0)", selected: true })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save group" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Administrators (1)" }));
    const remove = screen.getByRole("button", { name: "Remove administrator User@example.com" });
    expect(remove.textContent).toBe("");
    fireEvent.click(screen.getByRole("button", { name: /Back to groups/ }));
    expect(screen.getByRole("table", { name: "Groups" })).toBeInTheDocument();
  });
  it("creates and edits a group, assigns and removes existing members", async () => {
    render(<GroupsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "New group" }));
    expect(screen.queryByLabelText("Quota organization")).not.toBeInTheDocument();
    fireEvent.change(await screen.findByLabelText("New group name"), { target: { value: "New group" } });
    fireEvent.click(screen.getByRole("button", { name: "Create group" }));
    fireEvent.click(await screen.findByRole("tab", { name: "Settings" }));
    fireEvent.change(screen.getByLabelText("Group name"), { target: { value: "Renamed" } });
    fireEvent.click(screen.getByRole("button", { name: "Save group" }));
    await waitFor(() => expect(current.name).toBe("Renamed"));
    fireEvent.click(screen.getByRole("tab", { name: "Members (0)" }));
    fireEvent.change(screen.getByLabelText("Member to add"), { target: { value: "user@" } });
    fireEvent.click(screen.getByRole("option", { name: "User — User@example.com" }));
    fireEvent.click(screen.getByRole("button", { name: "Add member" }));
    expect(await screen.findByRole("tab", { name: "Members (1)" })).toBeVisible();
    expect(fetch).toHaveBeenCalledWith("/api/v1/groups/g/members/u", { method: "PUT" });
    fireEvent.click(await screen.findByRole("button", { name: "Remove member User@example.com" }));
    await waitFor(() => expect(current.members).toEqual([]));
    expect(appConfirm).toHaveBeenCalled();
  });
  it("confirms deletion and clears the editor", async () => {
    await selected(); fireEvent.click(screen.getByRole("tab", { name: "Settings" })); fireEvent.click(screen.getByRole("button", { name: "Delete group" }));
    await waitFor(() => expect(fetch).toHaveBeenCalledWith("/api/v1/groups/g", { method: "DELETE" }));
    expect(screen.queryByRole("button", { name: "Save group" })).not.toBeInTheDocument();
  });
  it("finds managers by display name and submits their exact stored email", async () => {
    await selected();
    fireEvent.click(screen.getByRole("tab", { name: "Administrators (1)" }));
    fireEvent.change(screen.getByLabelText("Administrator to add"), { target: { value: "SMITH" } });
    expect(screen.getByRole("button", { name: "Add administrator" })).toBeDisabled();
    fireEvent.click(screen.getByRole("option", { name: "Alice Smith — Alice@example.com" }));
    fireEvent.click(screen.getByRole("button", { name: "Add administrator" }));
    await waitFor(() => expect(fetch).toHaveBeenCalledWith("/api/v1/groups/g/managers", expect.objectContaining({
      method: "POST", body: JSON.stringify({ email: "Alice@example.com" }),
    })));
  });
  it("supports keyboard selection and clears selection when the search changes", async () => {
    await selected();
    const input = screen.getByLabelText("Member to add");
    fireEvent.change(input, { target: { value: "smith" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(screen.getByRole("button", { name: "Add member" })).toBeEnabled();
    fireEvent.change(input, { target: { value: "nobody-matches" } });
    expect(screen.getByRole("button", { name: "Add member" })).toBeDisabled();
    expect(screen.getByText("No matching users")).toBeInTheDocument();
  });
  it("respects a canceled deletion", async () => {
    vi.mocked(appConfirm).mockResolvedValue(false);
    await selected(); fireEvent.click(screen.getByRole("tab", { name: "Settings" })); fireEvent.click(screen.getByRole("button", { name: "Delete group" }));
    await waitFor(() => expect(appConfirm).toHaveBeenCalled());
    expect(fetch).not.toHaveBeenCalledWith("/api/v1/groups/g", { method: "DELETE" });
  });
  it("shows server mutation errors", async () => {
    fail = true; render(<GroupsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "New group" }));
    fireEvent.change(await screen.findByLabelText("New group name"), { target: { value: "Duplicate" } });
    fireEvent.click(screen.getByRole("button", { name: "Create group" }));
    await screen.findByText("Group name already exists");
  });
  it("keeps the prior roster after a failed transfer", async () => {
    current = { ...initial, members: [other] };
    render(<GroupsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Engineering" }));
    fireEvent.change(await screen.findByLabelText("Member to add"), { target: { value: "user@" } });
    fireEvent.click(screen.getByRole("option", { name: "User — User@example.com" }));
    fail = true;
    fireEvent.click(screen.getByRole("button", { name: "Add member" }));
    expect(await screen.findByText("Transfer failed; membership unchanged")).toBeVisible();
    expect(screen.getByRole("tab", { name: "Members (1)" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Remove member Alice@example.com" })).toBeVisible();
  });
  it("denies ordinary managers without requesting users", async () => {
    role = "user"; render(<GroupsPage />);
    await screen.findByText("Administrator access required");
    expect(fetch).not.toHaveBeenCalledWith("/api/v1/users/", undefined);
  });
});
