import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import QuotaScope from "./QuotaScope";
import { exampleUsage } from "./test-fixtures";
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });
it("clears cached usage/editor immediately on revocation and ignores an older successful response", async () => {
  let late: (response: Response) => void = () => {};
  const pending = new Promise<Response>(resolve => { late = resolve; });
  let calls = 0;
  let poll: () => void = () => {};
  vi.spyOn(globalThis, "setInterval").mockImplementation((callback, delay) => { if (delay === 15000) poll = callback as () => void; return 1 as unknown as ReturnType<typeof setInterval>; });
  vi.stubGlobal("fetch", vi.fn(async () => {
    calls++;
    if (calls === 2) return pending;
    return new Response(JSON.stringify(calls >= 3 ? { detail: "Revoked" } : exampleUsage), { status: calls >= 3 ? 403 : 200 });
  }));
  render(<QuotaScope scope="user" id="u" />);
  await screen.findByText("Cost center not assigned");
  act(() => poll());
  await waitFor(() => expect(calls).toBe(2));
  act(() => poll());
  await screen.findByText(/Quota usage unavailable: Revoked/);
  expect(screen.queryByLabelText("CPU limit")).not.toBeInTheDocument();
  await act(async () => { late(new Response(JSON.stringify(exampleUsage))); });
  expect(screen.queryByText("Cost center not assigned")).not.toBeInTheDocument();
});
it("scope switch discards usage immediately and ignores the previous scope response", async () => {
  let late: (response: Response) => void = () => {};
  vi.stubGlobal("fetch", vi.fn(async (url: string) => url.includes("/u/") ? new Promise<Response>(resolve => { late = resolve; }) : new Response(JSON.stringify({ ...exampleUsage, scope_id: "v", cost_center_id: "V" }))));
  const view = render(<QuotaScope key="u" scope="user" id="u" />);
  view.rerender(<QuotaScope key="v" scope="user" id="v" />);
  await screen.findByText("Cost center: V");
  await act(async () => { late(new Response(JSON.stringify(exampleUsage))); });
  expect(screen.queryByText("Cost center not assigned")).not.toBeInTheDocument();
});
