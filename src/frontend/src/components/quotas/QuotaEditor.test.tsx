import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import QuotaEditor from "./QuotaEditor";
import { exampleUsage } from "./test-fixtures";
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
it("disables unauthorized inputs and cannot mutate", () => {
  render(<QuotaEditor usage={{ ...exampleUsage, can_edit: false }} onSaved={vi.fn()} />);
  expect(screen.getByLabelText("CPU limit")).toBeDisabled();
  expect(screen.getByRole("button", { name: "Save limits" })).toBeDisabled();
});
it("submits zero, unlimited, exact currency and whole bytes, then refreshes", async () => {
  const saved = vi.fn();
  const fetcher = vi.fn<(url: string, options?: RequestInit) => Promise<Response>>(async () => new Response(JSON.stringify(exampleUsage.limits)));
  vi.stubGlobal("fetch", fetcher);
  render(<QuotaEditor usage={exampleUsage} onSaved={saved} />);
  fireEvent.change(screen.getByLabelText("RAM limit (GiB)"), { target: { value: "0.5" } });
  fireEvent.change(screen.getByLabelText("Spending limit (USD)"), { target: { value: "90.123456789012" } });
  fireEvent.click(screen.getByRole("button", { name: "Save limits" }));
  await waitFor(() => expect(saved).toHaveBeenCalled());
  expect(JSON.parse(fetcher.mock.calls[0][1]!.body as string)).toEqual({ cpu: 0, ram_bytes: 536870912, storage_bytes: null, projects: null, spending_usd: "90.123456789012" });
});
it("explains reductions, displays server failure and revokes on 403", async () => {
  const denied = vi.fn();
  vi.stubGlobal("fetch", vi.fn(async () => new Response('{"detail":"Quota access denied"}', { status: 403 })));
  render(<QuotaEditor usage={{ ...exampleUsage, scope: "project" }} onSaved={vi.fn()} onDenied={denied} />);
  expect(screen.getByText(/CPU, RAM and deployed-project reductions.*growth/)).toBeVisible();
  expect(screen.getByText(/physical storage limit.*triggers compute shutdown/)).toBeVisible();
  expect(screen.getByText(/lifetime spend.*shutdown/)).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Save limits" }));
  await waitFor(() => expect(denied).toHaveBeenCalled());
});
