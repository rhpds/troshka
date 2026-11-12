import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import QuotaScope from "./QuotaScope";
import { exampleUsage, nativeUsage } from "./test-fixtures";
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });
it("clears cached usage/editor immediately on revocation and ignores an older successful response", async () => {
  let late: (response: Response) => void = () => {};
  const pending = new Promise<Response>(resolve => { late = resolve; });
  let calls = 0;
  let poll: () => void = () => {};
  vi.spyOn(globalThis, "setInterval").mockImplementation((callback, delay) => { if (delay === 15000) poll = callback as () => void; return 1 as unknown as ReturnType<typeof setInterval>; });
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    if (url.endsWith("/native-storage")) return new Response(JSON.stringify(nativeUsage));
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
  vi.stubGlobal("fetch", vi.fn(async (url: string) => url.endsWith("/native-storage") ? new Response(JSON.stringify(nativeUsage)) : url.includes("/u/") ? new Promise<Response>(resolve => { late = resolve; }) : new Response(JSON.stringify({ ...exampleUsage, scope_id: "v", cost_center_id: "V" }))));
  const view = render(<QuotaScope scope="user" id="u" />);
  view.rerender(<QuotaScope scope="user" id="v" />);
  await screen.findByText("Cost center: V");
  await act(async () => { late(new Response(JSON.stringify(exampleUsage))); });
  expect(screen.queryByText("Cost center not assigned")).not.toBeInTheDocument();
});

it("native report loads independently while quota accounting fails", async () => {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => url.endsWith("/native-storage") ? new Response(JSON.stringify(nativeUsage)) : new Response('{"detail":"No baseline"}', { status: 503 })));
  render(<QuotaScope scope="project" id="p" />);
  await screen.findByText(/Quota usage unavailable: No baseline/);
  expect(await screen.findByTitle("1073741824 bytes")).toBeInTheDocument();
});

it("native report loading and success preserve policy drafts and enforcement labels", async () => {
  let finish: (value: Response) => void = () => {};
  vi.stubGlobal("fetch", vi.fn(async (url: string) => url.endsWith("/native-storage") ? new Promise<Response>(resolve => { finish = resolve; }) : new Response(JSON.stringify(exampleUsage))));
  render(<QuotaScope scope="user" id="u" />);
  await screen.findByText("Baseline: not ready");
  const input = screen.getByLabelText("CPU limit");
  const userEvent = (await import("@testing-library/user-event")).default.setup();
  await userEvent.clear(input); await userEvent.type(input, "47");
  await act(async () => finish(new Response(JSON.stringify(nativeUsage))));
  expect(input).toHaveValue("47");
  expect(screen.getByText("Baseline: not ready")).toBeInTheDocument();
  expect(screen.getByText("Enforcement disabled")).toBeInTheDocument();
});

it.each(["native-storage", "usage"])("%s revocation clears both datasets and ignores the other endpoint's late success", async deniedEndpoint => {
  const pending: Record<string, ((value: Response) => void)[]> = { usage: [], "native-storage": [] };
  const polls: (() => void)[] = [];
  vi.spyOn(globalThis, "setInterval").mockImplementation((callback, delay) => { if (delay === 15000) polls.push(callback as () => void); return polls.length as unknown as ReturnType<typeof setInterval>; });
  const calls: Record<string, number> = { usage: 0, "native-storage": 0 };
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    const endpoint = url.endsWith("/usage") ? "usage" : "native-storage";
    if (++calls[endpoint] === 1) return new Response(JSON.stringify(endpoint === "usage" ? exampleUsage : nativeUsage));
    return new Promise<Response>(resolve => pending[endpoint].push(resolve));
  }));
  const onDenied = vi.fn();
  render(<QuotaScope scope="user" id="u" onDenied={onDenied} />);
  await screen.findByText("Cost center not assigned"); await screen.findByTitle("1073741824 bytes");
  act(() => polls.forEach(poll => poll()));
  await act(async () => pending[deniedEndpoint][0](new Response('{"detail":"Revoked"}', { status: 403 })));
  const other = deniedEndpoint === "usage" ? "native-storage" : "usage";
  await act(async () => pending[other][0](new Response(JSON.stringify(other === "usage" ? exampleUsage : nativeUsage))));
  expect(onDenied).toHaveBeenCalledOnce();
  expect(screen.queryByText("Cost center not assigned")).not.toBeInTheDocument();
  expect(screen.queryByTitle("1073741824 bytes")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("CPU limit")).not.toBeInTheDocument();
});

it("an older quota denial revokes a newer successful quota response", async () => {
  let late: (response: Response) => void = () => {};
  let poll: () => void = () => {};
  let calls = 0;
  vi.spyOn(globalThis, "setInterval").mockImplementation((callback, delay) => { if (delay === 15000) poll = callback as () => void; return 1 as unknown as ReturnType<typeof setInterval>; });
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    if (url.endsWith("/native-storage")) return new Response(JSON.stringify(nativeUsage));
    if (++calls === 2) return new Promise<Response>(resolve => { late = resolve; });
    return new Response(JSON.stringify(exampleUsage));
  }));
  const onDenied = vi.fn();
  render(<QuotaScope scope="user" id="u" onDenied={onDenied} />);
  await screen.findByText("Cost center not assigned");
  act(() => poll());
  await waitFor(() => expect(calls).toBe(2));
  act(() => poll());
  await waitFor(() => expect(calls).toBe(3));
  await act(async () => late(new Response('{"detail":"Revoked"}', { status: 403 })));
  expect(onDenied).toHaveBeenCalledOnce();
  expect(screen.queryByTitle("1073741824 bytes")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("CPU limit")).not.toBeInTheDocument();
});

it.each([403, 404])("a pending %s denial survives a same-scope refresh and clears newer quota/native data", async status => {
  let late: (response: Response) => void = () => {};
  let poll: () => void = () => {};
  let calls = 0;
  vi.spyOn(globalThis, "setInterval").mockImplementation((callback, delay) => { if (delay === 15000) poll = callback as () => void; return 1 as unknown as ReturnType<typeof setInterval>; });
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    if (url.endsWith("/native-storage")) return new Response(JSON.stringify(nativeUsage));
    if (++calls === 2) return new Promise<Response>(resolve => { late = resolve; });
    return new Response(JSON.stringify({ ...exampleUsage, cost_center_id: calls >= 3 ? "Refreshed" : null }));
  }));
  const onDenied = vi.fn();
  render(<QuotaScope scope="user" id="u" onDenied={onDenied} />);
  await screen.findByText("Cost center not assigned");
  act(() => poll());
  await waitFor(() => expect(calls).toBe(2));
  fireEvent.click(screen.getByRole("button", { name: "Refresh quotas" }));
  await screen.findByText("Cost center: Refreshed");
  await act(async () => late(new Response('{"detail":"Revoked"}', { status })));
  expect(onDenied).toHaveBeenCalledOnce();
  expect(screen.queryByText("Cost center: Refreshed")).not.toBeInTheDocument();
  expect(screen.queryByTitle("1073741824 bytes")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("CPU limit")).not.toBeInTheDocument();
});

it("a pending previous-scope denial cannot revoke the replacement scope", async () => {
  let late: (response: Response) => void = () => {};
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    if (url.endsWith("/native-storage")) return new Response(JSON.stringify(nativeUsage));
    if (url.includes("/u/")) return new Promise<Response>(resolve => { late = resolve; });
    return new Response(JSON.stringify({ ...exampleUsage, scope_id: "v", cost_center_id: "Replacement" }));
  }));
  const onDenied = vi.fn();
  const view = render(<QuotaScope scope="user" id="u" onDenied={onDenied} />);
  view.rerender(<QuotaScope scope="user" id="v" onDenied={onDenied} />);
  await screen.findByText("Cost center: Replacement");
  await act(async () => late(new Response('{"detail":"Revoked"}', { status: 403 })));
  expect(onDenied).not.toHaveBeenCalled();
  expect(screen.getByText("Cost center: Replacement")).toBeInTheDocument();
});
