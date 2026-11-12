import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import NativeStorageUsage from "./NativeStorageUsage";
import type { NativeStorageUsageData } from "@/lib/kubevirtStorage";
import { nativeUsage } from "./test-fixtures";
import replay from "@/lib/__fixtures__/native-storage-replay.json";
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });
function respond(patch: Partial<NativeStorageUsageData> = {}) {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ ...nativeUsage, ...patch }))));
}
it("shows complete zero, separate capacity, source UTC time, and report-only semantics", async () => {
  respond({ allocated_bytes: 0, known_allocated_bytes: 0 });
  render(<NativeStorageUsage scope="project" id="p" />);
  expect(await screen.findByTitle("0 bytes")).toHaveTextContent("0 GiB");
  expect(screen.getByText(/Provisioned PVC capacity/)).toHaveTextContent("2 GiB");
  expect(screen.getByText(/2026-11-11 12:00:00 UTC/)).toBeInTheDocument();
  expect(screen.getByText(/Reporting only; native quota enforcement unavailable/)).toBeInTheDocument();
  expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
});
it.each(["partial", "stale", "unavailable"] as const)("keeps nullable %s totals distinct from measured subsets", async status => {
  respond({ status, allocated_bytes: null, reason_codes: ["unsupported"] });
  render(<NativeStorageUsage scope="project" id="p" />);
  await screen.findByText(status === "stale" ? "Last measured" : "Measured subset");
  expect(screen.getByText("Scope total unavailable")).toBeInTheDocument();
  expect(screen.getByTitle("1073741824 bytes")).toHaveTextContent("1 GiB");
  expect(screen.getByText("Coverage details")).toBeInTheDocument();
});
it("does not show no-native-projects as measured zero", async () => {
  respond({ status: "unavailable", allocated_bytes: null, known_allocated_bytes: 0, covered_projects: 0, expected_projects: 0, observed_at: null, reason_codes: ["no_native_projects"] });
  render(<NativeStorageUsage scope="user" id="u" />);
  await screen.findByText("No native projects in this scope");
  expect(screen.queryByTitle("0 bytes")).not.toBeInTheDocument();
  expect(screen.queryByText(/Unlimited/)).not.toBeInTheDocument();
});
it("missing measurement does not present the zero known accumulator as measured bytes", async () => {
  respond({ status: "unavailable", allocated_bytes: null, known_allocated_bytes: 0, provisioned_bytes: null, observed_at: null, covered_projects: 0, reason_codes: ["report_unavailable"] });
  render(<NativeStorageUsage scope="project" id="p" />);
  await screen.findByText("Scope total unavailable");
  expect(screen.queryByTitle("0 bytes")).not.toBeInTheDocument();
  expect(screen.getByText("Source observation time unavailable")).toBeInTheDocument();
});
it("keeps platform diagnostics separate from the attributed scope amount", async () => {
  respond({ platform_bytes: 5368709120, unattributed_retained_projects: 1, unattributed_retained_known_bytes: 3221225472 });
  render(<NativeStorageUsage scope="project" id="p" />);
  expect(await screen.findByTitle("1073741824 bytes")).toHaveTextContent("1 GiB");
  expect(screen.getByText(/Admin diagnostic — platform/)).toHaveTextContent("5 GiB");
  expect(screen.getByText(/Admin diagnostic — unresolved/)).toHaveTextContent("3 GiB");
  expect(screen.queryByTitle("9663676416 bytes")).not.toBeInTheDocument();
});
it("uses exact byte detail with a short rounded GiB value", async () => {
  respond({ allocated_bytes: "9007199254740993", known_allocated_bytes: "9007199254740993" });
  render(<NativeStorageUsage scope="project" id="p" />);
  expect(await screen.findByTitle("9007199254740993 bytes")).toHaveTextContent("8388608 GiB");
});
it("renders the real SQLite importer→API replay fixture", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(replay))));
  render(<NativeStorageUsage scope="project" id={replay.scope_id} />);
  expect(await screen.findByTitle("30 bytes")).toHaveTextContent("<0.01 GiB");
  expect(screen.getByText(/Source observed/)).toHaveTextContent("2026-01-01 00:00:00 UTC");
  expect(screen.getByText(/Native project coverage/)).toHaveTextContent("1 of 1");
});
it("scope changes clear prior data and ignore old responses", async () => {
  let late: (value: Response) => void = () => {};
  vi.stubGlobal("fetch", vi.fn(async (url: string) => url.includes("/p/") ? new Promise<Response>(resolve => { late = resolve; }) : new Response(JSON.stringify({ ...nativeUsage, allocated_bytes: 0 }))));
  const view = render(<NativeStorageUsage scope="project" id="p" />);
  view.rerender(<NativeStorageUsage scope="project" id="new" />);
  await screen.findByTitle("0 bytes");
  await act(async () => late(new Response(JSON.stringify(nativeUsage))));
  expect(screen.queryByTitle("1073741824 bytes")).not.toBeInTheDocument();
});
it("an older denied response revokes even after a newer successful poll", async () => {
  let late: (value: Response) => void = () => {};
  let poll: () => void = () => {};
  let calls = 0;
  vi.spyOn(globalThis, "setInterval").mockImplementation((callback, delay) => { if (delay === 15000) poll = callback as () => void; return 1 as unknown as ReturnType<typeof setInterval>; });
  vi.stubGlobal("fetch", vi.fn(async () => ++calls === 1 ? new Promise<Response>(resolve => { late = resolve; }) : new Response(JSON.stringify(nativeUsage))));
  const onDenied = vi.fn();
  render(<NativeStorageUsage scope="project" id="p" onDenied={onDenied} />);
  act(() => poll());
  await screen.findByTitle("1073741824 bytes");
  await act(async () => late(new Response('{"detail":"Revoked"}', { status: 404 })));
  await waitFor(() => expect(onDenied).toHaveBeenCalledOnce());
  expect(screen.queryByTitle("1073741824 bytes")).not.toBeInTheDocument();
});
it("unmount prevents a late denied request from notifying a replacement view", async () => {
  let late: (value: Response) => void = () => {};
  vi.stubGlobal("fetch", vi.fn(async () => new Promise<Response>(resolve => { late = resolve; })));
  const onDenied = vi.fn();
  const view = render(<NativeStorageUsage scope="project" id="p" onDenied={onDenied} />);
  view.unmount();
  await act(async () => late(new Response('{"detail":"Revoked"}', { status: 403 })));
  expect(onDenied).not.toHaveBeenCalled();
});
