import React from "react";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  params: new URLSearchParams("project=p&vm=v&name=TestVM"),
  state: { deleted: false, vmStates: {} as Record<string, string> },
  confirm: vi.fn(),
}));
vi.mock("next/navigation", () => ({ useSearchParams: () => mocks.params }));
vi.mock("@/hooks/useVmStateSocket", () => ({ useVmStateSocket: () => mocks.state }));
vi.mock("@/lib/confirm", () => ({ appConfirm: mocks.confirm }));

class Rfb {
  static instances: Rfb[] = [];
  events: Record<string, () => void> = {};
  scaleViewport = false;
  _rfbConnectionState = "connected";
  _canvas = document.createElement("canvas");
  sendKey = vi.fn();
  focus = vi.fn();
  disconnect = vi.fn(() => { this._rfbConnectionState = "disconnected"; this.events.disconnect?.(); });
  constructor(target: HTMLElement, readonly url: string) { Rfb.instances.push(this); target.append(this._canvas); }
  addEventListener(event: string, callback: () => void) { this.events[event] = callback; }
}
class PreflightSocket {
  static instances: PreflightSocket[] = [];
  onclose: ((event: { code: number }) => void) | null = null;
  onerror: (() => void) | null = null;
  close = vi.fn();
  constructor(readonly url: string) { PreflightSocket.instances.push(this); }
}
const request = vi.fn<typeof fetch>();
const response = (data: unknown, status = 200) => ({ ok: status < 400, status, json: async () => data }) as Response;
const flush = async () => { await act(async () => { await Promise.resolve(); }); };
async function loadPage() { return (await import("../page")).default; }

beforeEach(() => {
  vi.resetModules();
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-01-01"));
  mocks.params = new URLSearchParams("project=p&vm=v&name=TestVM");
  mocks.state = { deleted: false, vmStates: {} };
  mocks.confirm.mockReset().mockResolvedValue(true);
  Rfb.instances = []; PreflightSocket.instances = [];
  request.mockReset().mockImplementation(async (url) => response(String(url).endsWith("/console") ? { ws_url: "ws://host/ws/token", host_type: "libvirt" } : { name: "Lab", topology: { nodes: [] } }));
  vi.stubGlobal("fetch", request);
  vi.stubGlobal("WebSocket", PreflightSocket);
  vi.doMock("@novnc/novnc", () => ({ default: Rfb }));
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("console connection lifecycle", () => {
  it("waits for the noVNC module before connecting an already fetched URL", async () => {
    let finishModule!: (value: { default: typeof Rfb }) => void;
    vi.doMock("@novnc/novnc", () => new Promise((resolve) => { finishModule = resolve; }));
    const Page = await loadPage(); render(<Page />); await flush();
    expect(Rfb.instances).toHaveLength(0);
    await act(async () => { finishModule({ default: Rfb }); });
    expect(Rfb.instances).toHaveLength(1);
  });

  it("disconnects a session when the page unmounts", async () => {
    const Page = await loadPage(); const { unmount } = render(<Page />); await flush();
    expect(Rfb.instances).toHaveLength(1);
    const rfb = Rfb.instances[0];
    act(() => rfb.events.connect());
    unmount();
    expect(rfb.disconnect).toHaveBeenCalledOnce();
    await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
    expect(Rfb.instances).toHaveLength(1);
  });

  it("polls unavailable ports and reconnects after a disconnect", async () => {
    let attempts = 0;
    request.mockImplementation(async (url) => {
      if (!String(url).endsWith("console")) return response({});
      attempts += 1;
      return response(attempts === 1 ? {} : { ws_url: "ws://host/ws/token" });
    });
    const Page = await loadPage(); render(<Page />); await flush();
    expect(Rfb.instances).toHaveLength(0);
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    expect(Rfb.instances).toHaveLength(1);
    act(() => Rfb.instances[0].events.connect());
    expect(screen.getByText("Connected")).toBeInTheDocument();
    act(() => Rfb.instances[0].events.disconnect());
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    expect(Rfb.instances.length).toBeGreaterThan(1);
  });

  it("checks KubeVirt session ownership and allows an explicit forced connection", async () => {
    request.mockResolvedValue(response({ ws_url: "ws://host/ws/token", host_type: "kubevirt-cluster" }));
    const Page = await loadPage(); render(<Page />); await flush();
    expect(PreflightSocket.instances[0].url).toBe("ws://host/check/token");
    await act(async () => { PreflightSocket.instances[0].onclose?.({ code: 4010 }); });
    expect(Rfb.instances).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "Force Connect" }));
    await flush();
    expect(Rfb.instances[0].url).toBe("ws://host/ws/force/token");
  });

  it.each(["timeout", "available", "error"])("handles a %s preflight check", async (outcome) => {
    request.mockResolvedValue(response({ ws_url: "ws://host/ws/token", host_type: "kubevirt-cluster" }));
    const Page = await loadPage(); render(<Page />); await flush();
    if (outcome === "timeout") await act(async () => { await vi.advanceTimersByTimeAsync(800); });
    else await act(async () => { PreflightSocket.instances[0].onclose?.({ code: outcome === "available" ? 4000 : 1006 }); });
    expect(Rfb.instances).toHaveLength(outcome === "error" ? 0 : 1);
  });

  it.each(["headless", "deleted"])("stops polling when the VM is %s", async (mode) => {
    request.mockResolvedValue(response({ headless: mode === "headless" }, mode === "deleted" ? 404 : 200));
    const Page = await loadPage(); render(<Page />); await flush();
    expect(screen.getAllByText(mode === "headless" ? "Headless Mode" : "Project deleted").length).toBeGreaterThan(0);
    const calls = request.mock.calls.length;
    await act(async () => { await vi.advanceTimersByTimeAsync(9000); });
    expect(request).toHaveBeenCalledTimes(calls);
    expect(Rfb.instances).toHaveLength(0);
  });

  it("shows a missing project error and does not open a session", async () => {
    mocks.params = new URLSearchParams("vm=v");
    const Page = await loadPage(); render(<Page />); await flush();
    expect(screen.getByText("Missing project parameter.")).toBeInTheDocument();
    expect(Rfb.instances).toHaveLength(0);
  });

  it("reports a noVNC loading failure", async () => {
    vi.doMock("@novnc/novnc", () => { throw new Error("module unavailable"); });
    const Page = await loadPage(); render(<Page />); await flush();
    expect(screen.getAllByText(/Failed to load noVNC/).length).toBeGreaterThan(0);
    expect(Rfb.instances).toHaveLength(0);
  });

  it("ignores console URL responses that arrive after unmount", async () => {
    let resolveConsole!: (value: Response) => void;
    request.mockImplementation(async (url) => {
      if (String(url).endsWith("console")) return new Promise((resolve) => { resolveConsole = resolve; });
      return response({});
    });
    const Page = await loadPage(); const { unmount } = render(<Page />); await flush();
    unmount();
    await act(async () => { resolveConsole(response({ ws_url: "ws://late" })); });
    expect(Rfb.instances).toHaveLength(0);
  });
});

describe("console controls", () => {
  it("starts a stopped VM, shows progress, and handles network failure", async () => {
    mocks.state.vmStates.v = "stopped";
    request.mockImplementation(async (_, options) => {
      if (options?.method === "POST") throw new Error("offline");
      return response({});
    });
    const Page = await loadPage(); render(<Page />); await flush();
    expect(screen.getAllByText("Powered Off").length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: "Power ▾" }));
    fireEvent.click(screen.getByRole("button", { name: "Start" }));
    await flush();
    expect(mocks.confirm).not.toHaveBeenCalled();
    expect(screen.getByText("Failed to connect to server")).toBeInTheDocument();
    expect(screen.getAllByText("Starting...").length).toBeGreaterThan(0);
  });

  it("cancels a power confirmation without sending a POST", async () => {
    mocks.state.vmStates.v = "running";
    mocks.confirm.mockResolvedValue(false);
    const Page = await loadPage(); render(<Page />); await flush();
    fireEvent.click(screen.getByRole("button", { name: "Power ▾" }));
    fireEvent.click(screen.getByRole("button", { name: "Force Off" }));
    await flush();
    expect(request.mock.calls.some(([, options]) => options?.method === "POST")).toBe(false);
  });

  it("updates focus and state indicators and supports the Windows menu", async () => {
    mocks.state.vmStates.v = "starting";
    const Page = await loadPage(); const { rerender } = render(<Page />); await flush();
    expect(screen.getAllByText("Starting...").length).toBeGreaterThan(0);
    mocks.state = { deleted: false, vmStates: { v: "running" } };
    rerender(<Page />);
    const rfb = Rfb.instances[0];
    act(() => rfb.events.connect());
    fireEvent.focusIn(rfb._canvas);
    expect(screen.getByText("Focused")).toBeInTheDocument();
    fireEvent.focusOut(rfb._canvas);
    expect(screen.getByText("Unfocused")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Windows ▾" }));
    fireEvent.click(screen.getByRole("button", { name: "Win Start Menu" }));
    expect(rfb.sendKey).toHaveBeenCalledWith(0xffeb, null, true);
  });

  it("types a configured password and prevents duplicate rapid sends", async () => {
    request.mockImplementation(async (url) => response(String(url).endsWith("console")
      ? { ws_url: "ws://host/ws/token" }
      : { name: "Lab", topology: { nodes: [{ id: "v", type: "vmNode", data: { ciCloudUserPassword: "A!é", ciRootPassword: "root" } }] } }));
    const Page = await loadPage(); render(<Page />); await flush();
    const rfb = Rfb.instances[0];
    act(() => rfb.events.connect());
    const button = screen.getByRole("button", { name: "🔑 cloud-user" });
    fireEvent.click(button);
    expect(screen.getByText("cloud-user password sent")).toBeInTheDocument();
    expect(rfb.sendKey).toHaveBeenCalledWith(0xff0d, "", false);
    const calls = rfb.sendKey.mock.calls.length;
    fireEvent.click(button);
    expect(rfb.sendKey).toHaveBeenCalledTimes(calls);
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(screen.queryByText("cloud-user password sent")).not.toBeInTheDocument();
    expect(document.title).toBe("Lab — TestVM");
  });

  it("reuses the keyboard popup and accepts key combinations only from its own origin", async () => {
    const popup = { closed: false, focus: vi.fn(), close: vi.fn() };
    const open = vi.spyOn(window, "open").mockReturnValue(popup as unknown as Window);
    const Page = await loadPage(); const { unmount } = render(<Page />); await flush();
    fireEvent.click(screen.getByTitle("Virtual Keyboard"));
    fireEvent.click(screen.getByTitle("Virtual Keyboard"));
    expect(open).toHaveBeenCalledTimes(1);
    expect(popup.focus).toHaveBeenCalledOnce();
    const rfb = Rfb.instances[0];
    act(() => window.dispatchEvent(new MessageEvent("message", { origin: "https://other.test", data: { type: "vkb-combo", keysyms: [65] } })));
    expect(rfb.sendKey).not.toHaveBeenCalled();
    act(() => window.dispatchEvent(new MessageEvent("message", { origin: window.location.origin, data: { type: "vkb-combo", keysyms: [65] } })));
    expect(rfb.sendKey).toHaveBeenCalledWith(65, null, true);
    unmount();
    expect(popup.close).toHaveBeenCalledOnce();
  });

  it.each([true, false])("copies a screenshot or falls back to download (clipboard=%s)", async (clipboardOk) => {
    const write = vi.fn().mockImplementation(async () => { if (!clipboardOk) throw new Error("denied"); });
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { write } });
    vi.stubGlobal("ClipboardItem", class { constructor(readonly data: unknown) {} });
    vi.spyOn(HTMLCanvasElement.prototype, "toBlob").mockImplementation((callback) => callback(new Blob(["image"], { type: "image/png" })));
    vi.spyOn(HTMLCanvasElement.prototype, "toDataURL").mockReturnValue("data:image/png;base64,AA==");
    const download = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    const Page = await loadPage(); render(<Page />); await flush();
    fireEvent.click(screen.getByRole("button", { name: "Screenshot" }));
    await flush();
    expect(screen.getByText(clipboardOk ? "Screenshot copied" : "Screenshot saved")).toBeInTheDocument();
    expect(write).toHaveBeenCalledOnce();
    expect(download).toHaveBeenCalledTimes(clipboardOk ? 0 : 1);
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(screen.queryByText(/Screenshot (copied|saved)/)).not.toBeInTheDocument();
  });

  it("uses the paste prompt when clipboard access is denied and sends unicode keys", async () => {
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { readText: vi.fn().mockRejectedValue(new Error("denied")) } });
    vi.spyOn(window, "prompt").mockReturnValue("λ😀");
    const Page = await loadPage(); render(<Page />); await flush();
    fireEvent.click(screen.getByRole("button", { name: "Paste" }));
    await flush();
    expect(Rfb.instances[0].sendKey).toHaveBeenCalledWith(0x010003bb, "", true);
    expect(Rfb.instances[0].sendKey).toHaveBeenCalledWith(0x0101f600, "", true);
  });

  it("closes and disconnects the console when the project is deleted", async () => {
    const close = vi.spyOn(window, "close").mockImplementation(() => {});
    const Page = await loadPage(); const { rerender } = render(<Page />); await flush();
    const rfb = Rfb.instances[0];
    mocks.state = { deleted: true, vmStates: {} };
    rerender(<Page />);
    expect(close).toHaveBeenCalledOnce();
    expect(rfb.disconnect).toHaveBeenCalledOnce();
  });

  it("replaces the session when navigating to another VM", async () => {
    const Page = await loadPage(); const { rerender } = render(<Page />); await flush();
    const old = Rfb.instances[0];
    act(() => old.events.connect());
    mocks.params = new URLSearchParams("project=p&vm=other&name=OtherVM");
    rerender(<Page />); await flush();
    expect(old.disconnect).toHaveBeenCalledOnce();
    expect(Rfb.instances).toHaveLength(2);
    expect(request).toHaveBeenCalledWith("/api/v1/projects/p/vms/other/console");
  });

  it("changes scaling, sends macros and pastes text as keyboard events", async () => {
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { readText: vi.fn().mockResolvedValue("A!\n") } });
    const Page = await loadPage(); render(<Page />); await flush();
    const rfb = Rfb.instances[0];
    act(() => rfb.events.connect());
    fireEvent.click(screen.getByRole("button", { name: "Scaled" }));
    expect(rfb.scaleViewport).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Linux ▾" }));
    fireEvent.click(screen.getByRole("button", { name: "Ctrl+C Interrupt" }));
    expect(rfb.sendKey.mock.calls).toEqual([[0xffe3, null, true], [99, null, true], [99, null, false], [0xffe3, null, false]]);
    rfb.sendKey.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "Paste" }));
    await flush();
    expect(rfb.sendKey).toHaveBeenCalledWith(0xffe1, "", true);
    expect(rfb.sendKey).toHaveBeenCalledWith(0xff0d, "", false);
    expect(rfb.focus).toHaveBeenCalled();
  });

  it("confirms power actions and surfaces API failures", async () => {
    mocks.state.vmStates.v = "running";
    request.mockImplementation(async (url, options) => {
      if (options?.method === "POST") return response({ detail: "Denied" }, 403);
      return response(String(url).endsWith("console") ? { ws_url: "ws://host/ws/token" } : {});
    });
    const Page = await loadPage(); render(<Page />); await flush();
    fireEvent.click(screen.getByRole("button", { name: "Power ▾" }));
    fireEvent.click(screen.getByRole("button", { name: "Shutdown" }));
    await flush();
    expect(mocks.confirm).toHaveBeenCalledWith({ message: 'Shut down "TestVM"?', confirmLabel: "Shutdown" });
    expect(screen.getByText("Denied")).toBeInTheDocument();
  });
});
