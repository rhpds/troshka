import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import GroupsPage from "./page";
import { appConfirm } from "@/lib/confirm";
vi.mock("@/lib/confirm", () => ({ appConfirm: vi.fn(async () => true) }));
const person = { id: "u", email: "User@example.com", display_name: "User" };
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
    if (url.endsWith("/users/")) return response([person]);
    if (url === "/api/v1/groups" && options?.method === "POST") { if (fail) return response({ detail: "Group name already exists" }, 409); current = { ...current, ...JSON.parse(options.body as string) }; return response(current, 201); }
    if (url === "/api/v1/groups") return response([current]);
    if (url.endsWith("/members/u")) current = { ...current, members: options?.method === "PUT" ? [person] : [] };
    if (url.endsWith("/groups/g") && options?.method === "PATCH") current = { ...current, ...JSON.parse(options.body as string) };
    if (url.endsWith("/groups/g") && options?.method === "DELETE") return response(null, 204);
    return response(current);
  }));
});
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });
async function selected() {
  render(<GroupsPage />);
  fireEvent.change(await screen.findByLabelText("Group"), { target: { value: "g" } });
  await screen.findByRole("button", { name: "Save group" });
}
describe("group administration", () => {
  it("creates and edits a group, assigns and removes existing members", async () => {
    render(<GroupsPage />);
    fireEvent.change(await screen.findByLabelText("New group name"), { target: { value: "New group" } });
    fireEvent.click(screen.getByRole("button", { name: "Create group" }));
    await screen.findByRole("button", { name: "Save group" });
    fireEvent.change(screen.getByLabelText("Group name"), { target: { value: "Renamed" } });
    fireEvent.click(screen.getByRole("button", { name: "Save group" }));
    await waitFor(() => expect(current.name).toBe("Renamed"));
    fireEvent.change(screen.getByLabelText("Member to add"), { target: { value: "u" } });
    fireEvent.click(screen.getByRole("button", { name: "Add member" }));
    fireEvent.click(await screen.findByRole("button", { name: "Remove member User@example.com" }));
    await waitFor(() => expect(current.members).toEqual([]));
    expect(appConfirm).toHaveBeenCalled();
  });
  it("confirms deletion and clears the editor", async () => {
    await selected(); fireEvent.click(screen.getByRole("button", { name: "Delete group" }));
    await waitFor(() => expect(fetch).toHaveBeenCalledWith("/api/v1/groups/g", { method: "DELETE" }));
    expect(screen.queryByRole("button", { name: "Save group" })).not.toBeInTheDocument();
  });
  it("respects a canceled deletion", async () => {
    vi.mocked(appConfirm).mockResolvedValue(false);
    await selected(); fireEvent.click(screen.getByRole("button", { name: "Delete group" }));
    await waitFor(() => expect(appConfirm).toHaveBeenCalled());
    expect(fetch).not.toHaveBeenCalledWith("/api/v1/groups/g", { method: "DELETE" });
  });
  it("shows server mutation errors", async () => {
    fail = true; render(<GroupsPage />);
    fireEvent.change(await screen.findByLabelText("New group name"), { target: { value: "Duplicate" } });
    fireEvent.click(screen.getByRole("button", { name: "Create group" }));
    await screen.findByText("Group name already exists");
  });
  it("denies ordinary managers without requesting users", async () => {
    role = "user"; render(<GroupsPage />);
    await screen.findByText("Administrator access required");
    expect(fetch).not.toHaveBeenCalledWith("/api/v1/users/", undefined);
  });
});
