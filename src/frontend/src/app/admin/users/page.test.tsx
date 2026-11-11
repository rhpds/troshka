import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import AdminUsersPage from "./page";

vi.mock("@/lib/confirm", () => ({ appConfirm: vi.fn(async () => true) }));
const account = (id: string, access_enabled: boolean) => ({ id, email: `${id}@example.com`, role: "user", auth_source: "invited", created_at: "2026-11-11T00:00:00Z", access_enabled });
beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(async (url: string, options?: RequestInit) => {
    const data = url.endsWith("/auth/me") ? { id: "admin", role: "admin" }
      : options?.method === "POST" ? account("invite", true)
      : [account("enabled", true), account("disabled", false)];
    return { ok: true, json: async () => data } as Response;
  }));
});
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

it("shows database access controls without group-sync icons or buttons", async () => {
  render(<AdminUsersPage />);
  await screen.findByText("enabled@example.com");
  expect(screen.getByText("Access disabled")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Disable access" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Enable access" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Sync group/ })).not.toBeInTheDocument();
  expect(screen.queryByRole("img")).not.toBeInTheDocument();
});

it("creates using email and role without a display name field", async () => {
  render(<AdminUsersPage />);
  fireEvent.click(screen.getByRole("button", { name: "Add User" }));
  expect(screen.queryByLabelText(/Display Name/)).not.toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("Email"), { target: { value: "invite@example.com" } });
  fireEvent.click(screen.getByRole("button", { name: "Create" }));
  await waitFor(() => expect(fetch).toHaveBeenCalledWith("/api/v1/users/", expect.objectContaining({ method: "POST", body: JSON.stringify({ email: "invite@example.com", role: "user" }) })));
  expect(await screen.findByText(/Account created/)).toBeInTheDocument();
});

it("disables access through the user update endpoint", async () => {
  render(<AdminUsersPage />);
  fireEvent.click(await screen.findByRole("button", { name: "Disable access" }));
  await waitFor(() => expect(fetch).toHaveBeenCalledWith("/api/v1/users/enabled", expect.objectContaining({ method: "PATCH", body: JSON.stringify({ access_enabled: false }) })));
});
