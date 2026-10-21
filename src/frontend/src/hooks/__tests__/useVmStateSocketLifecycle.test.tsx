import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useVmStateSocket } from "../useVmStateSocket";

class Socket {
  static instances: Socket[] = [];
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  closed = false;
  constructor(readonly url: string) { Socket.instances.push(this); }
  close() { this.closed = true; this.onclose?.(); }
  message(value: unknown) { this.onmessage?.({ data: JSON.stringify(value) }); }
}
const response = (token?: string, ok = true) => ({ ok, json: async () => ({ token }) }) as Response;
const request = vi.fn<typeof fetch>();
const flush = async () => { await act(async () => { await Promise.resolve(); }); };

beforeEach(() => {
  vi.useFakeTimers();
  Socket.instances = [];
  request.mockReset().mockResolvedValue(response("token & value"));
  vi.stubGlobal("fetch", request);
  vi.stubGlobal("WebSocket", Socket);
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

describe("VM socket lifecycle", () => {
  it("does not authenticate or connect without a project", async () => {
    const { result } = renderHook(() => useVmStateSocket(null));
    await flush();
    expect(request).not.toHaveBeenCalled();
    expect(result.current.connected).toBe(false);
  });

  it("ignores an authentication response belonging to a previous project", async () => {
    let resolveOld!: (value: Response) => void;
    request.mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve; }));
    const { rerender } = renderHook(({ id }) => useVmStateSocket(id), { initialProps: { id: "old" } });
    rerender({ id: "new" });
    await flush();
    await act(async () => { resolveOld(response("old")); });
    expect(Socket.instances).toHaveLength(1);
    expect(Socket.instances[0].url).toContain("/projects/new/ws?token=token%20%26%20value");
  });

  it("never creates an orphan socket when authentication finishes after unmount", async () => {
    let resolveToken!: (value: Response) => void;
    request.mockImplementationOnce(() => new Promise((resolve) => { resolveToken = resolve; }));
    const { unmount } = renderHook(() => useVmStateSocket("p"));
    unmount();
    await act(async () => { resolveToken(response("late")); });
    expect(Socket.instances).toHaveLength(0);
  });

  it.each(["rejected", "unauthorized", "missing"])("connects in development when token is %s", async (mode) => {
    if (mode === "rejected") request.mockRejectedValue(new Error("offline"));
    else request.mockResolvedValue(response(undefined, mode !== "unauthorized"));
    renderHook(() => useVmStateSocket("p"));
    await flush();
    expect(Socket.instances[0].url).toMatch(/\/projects\/p\/ws$/);
  });

  it("backs off failed connections, caps retries, and resets after connection", async () => {
    const { result, unmount } = renderHook(() => useVmStateSocket("p"));
    await flush();
    for (const delay of [1000, 2000, 4000, 8000, 16000, 30000, 30000]) {
      const count = Socket.instances.length;
      act(() => Socket.instances.at(-1)!.onclose?.());
      await act(async () => { await vi.advanceTimersByTimeAsync(delay - 1); });
      expect(Socket.instances).toHaveLength(count);
      await act(async () => { await vi.advanceTimersByTimeAsync(1); });
      expect(Socket.instances).toHaveLength(count + 1);
    }
    act(() => Socket.instances.at(-1)!.onopen?.());
    expect(result.current.connected).toBe(true);
    act(() => Socket.instances.at(-1)!.onerror?.());
    act(() => Socket.instances.at(-1)!.onclose?.());
    expect(result.current.connected).toBe(false);
    const count = Socket.instances.length;
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(Socket.instances).toHaveLength(count + 1);
    act(() => Socket.instances.at(-1)!.onclose?.());
    unmount();
    await act(async () => { await vi.runAllTimersAsync(); });
    expect(Socket.instances).toHaveLength(count + 1);
  });

  it("closes the prior project socket and rejects its late messages", async () => {
    const { result, rerender } = renderHook(({ id }) => useVmStateSocket(id), { initialProps: { id: "old" } });
    await flush();
    const old = Socket.instances[0];
    const lateMessage = old.onmessage;
    act(() => old.onopen?.());
    rerender({ id: "new" });
    await flush();
    act(() => lateMessage?.({ data: JSON.stringify({ type: "project-deleted" }) }));
    expect(old.closed).toBe(true);
    expect(result.current.deleted).toBe(false);
  });
});

describe("VM socket messages", () => {
  it("merges VM events and preserves states across empty snapshots", async () => {
    const { result } = renderHook(() => useVmStateSocket("p"));
    await flush();
    const ws = Socket.instances[0];
    act(() => ws.message({ type: "snapshot", vm_states: { one: "running" }, vm_progress: { one: { step: "boot", detail: "ready" } }, project_state: "active", deploy_error: "old", deploy_progress: { step: "install", detail: "started" } }));
    act(() => ws.message({ type: "vm-state", states: { two: "stopped" }, progress: { two: { step: "stop", detail: "done" } }, boot_devs: { one: ["disk"] } }));
    expect(result.current.vmStates).toEqual({ one: "running", two: "stopped" });
    expect(result.current.vmBootDevs).toEqual({ one: ["disk"] });
    expect(result.current.vmProgress.two.step).toBe("stop");
    act(() => ws.message({ type: "snapshot", vm_states: {} }));
    expect(result.current.vmStates.one).toBe("running");
    expect(result.current.vmProgress).toEqual({});
    expect(result.current.projectState).toBeNull();
    expect(result.current.deployError).toBeNull();
    expect(result.current.deployProgress?.step).toBe("install");
  });

  it("clears prior deployment progress and applies optional lifecycle deadlines", async () => {
    const { result } = renderHook(() => useVmStateSocket("p"));
    await flush();
    const ws = Socket.instances[0];
    act(() => ws.message({ type: "deploy-progress", progress: { step: "ready", detail: "done", items: ["vm"] } }));
    act(() => ws.message({ type: "project-state", state: "deploying", auto_stop_expires_at: "soon", lifetime_expires_at: "later", auto_stopped: true }));
    expect(result.current.deployProgress).toBeNull();
    expect(result.current.autoStopExpiresAt).toBe("soon");
    expect(result.current.lifetimeExpiresAt).toBe("later");
    expect(result.current.autoStopped).toBe(true);
    act(() => ws.message({ type: "project-state", state: "active" }));
    expect(result.current.autoStopExpiresAt).toBe("soon");
    act(() => ws.message({ type: "project-state", auto_stop_expires_at: null, lifetime_expires_at: null, auto_stopped: false }));
    expect(result.current.autoStopExpiresAt).toBeNull();
    expect(result.current.lifetimeExpiresAt).toBeNull();
    expect(result.current.autoStopped).toBe(false);
    act(() => ws.message({ type: "deploy-progress", detail: "flat payload" }));
    expect(result.current.deployProgress).toMatchObject({ step: "", detail: "flat payload" });
    act(() => ws.message({ type: "deploy-progress" }));
    expect(result.current.deployProgress?.detail).toBe("flat payload");
    act(() => ws.message({ type: "workload-progress", run_id: "r", step: "run" }));
    expect(result.current.workloadProgress).toEqual({ run_id: "r", step: "run", detail: "" });
  });

  it.each([["complete", "ready"], ["failed", "error"], ["cancelled", "error"], ["timeout", "timeout"], ["installing", "ssh"], [undefined, "ssh"]])("maps cluster phase %s to %s", async (overall, phase) => {
    const { result } = renderHook(() => useVmStateSocket("p"));
    await flush();
    act(() => Socket.instances[0].message({ type: "ocp-install-progress", overall, clusters: { c: "installing" }, detail: "stage", items: ["node"] }));
    expect(result.current.ocpHealth).toEqual({ phase, detail: "stage", items: ["node"] });
    expect(result.current.clusterOcpPhases).toEqual({ c: "installing" });
  });

  it("handles topology, address, health, timer and deletion updates while ignoring bad messages", async () => {
    const { result } = renderHook(() => useVmStateSocket("p"));
    await flush();
    const ws = Socket.instances[0];
    act(() => {
      ws.message({ type: "ocp-health", phase: "ready", detail: "ok" });
      ws.message({ type: "topology-update", topology: { nodes: [] }, deployed_topology: { nodes: [1] } });
      ws.message({ type: "external-ips-updated", externalIps: [{ id: "ip", name: "public", ip: "1.2.3.4" }] });
      ws.message({ type: "timer_warning", timer: "stop", expires_at: "soon", minutes_remaining: 5 });
      ws.message({ type: "timer_fired", timer: "stop" });
      ws.message({ type: "project-deleted" });
      ws.onmessage?.({ data: "not json" });
      ws.message(null); ws.message({ type: "ping" }); ws.message({ type: "unknown" });
    });
    expect(result.current.ocpHealth?.phase).toBe("ready");
    expect(result.current.topologyUpdate).toEqual({ topology: { nodes: [] }, deployed_topology: { nodes: [1] } });
    expect(result.current.externalIpsUpdate?.[0].ip).toBe("1.2.3.4");
    expect(result.current.timerWarning?.minutes_remaining).toBe(5);
    expect(result.current.timerFired).toBe("stop");
    expect(result.current.deleted).toBe(true);
    act(() => { ws.message({ type: "topology-update" }); ws.message({ type: "external-ips-updated" }); });
    expect(result.current.topologyUpdate).toBeNull();
    expect(result.current.externalIpsUpdate).toEqual([]);
  });
});
