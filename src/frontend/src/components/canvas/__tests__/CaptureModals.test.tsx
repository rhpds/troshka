import React from "react";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import SavePatternModal from "../SavePatternModal";
import SnapshotVMModal from "../SnapshotVMModal";

const response = (data: unknown, ok = true) => ({ ok, json: async () => data }) as Response;
const saved = vi.fn();
const close = vi.fn();
const fetchMock = vi.fn<typeof fetch>();

beforeEach(() => {
  vi.useFakeTimers();
  vi.clearAllMocks();
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockResolvedValue(response([]));
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

async function submit(label: string) {
  fireEvent.click(screen.getByRole("button", { name: label }));
  await act(async () => { await vi.runAllTimersAsync(); });
}

describe("capture dialogs", () => {
  it.each(["pattern", "snapshot"])("rejects blank %s names without sending requests", async (kind) => {
    render(kind === "pattern"
      ? <SavePatternModal projectId="p" projectName="Lab" hasRunningVMs onSaved={saved} onClose={close} />
      : <SnapshotVMModal projectId="p" vmId="vm" vmName="VM" isRunning onSaved={saved} onClose={close} />);
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "  " } });
    await submit(kind === "pattern" ? "Save Pattern" : "Save Snapshot");
    expect(screen.getByText("Name is required")).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(close).toHaveBeenCalledOnce();
  });

  it.each(["pattern", "snapshot"])("checks duplicate %s names before stopping machines", async (kind) => {
    render(kind === "pattern"
      ? <SavePatternModal projectId="p" projectName="Lab" hasRunningVMs onSaved={saved} onClose={close} />
      : <SnapshotVMModal projectId="p" vmId="vm" vmName="VM" isRunning onSaved={saved} onClose={close} />);
    if (kind === "pattern") fireEvent.click(screen.getByLabelText("Stop VMs before capture instead"));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "duplicate" } });
    fetchMock.mockResolvedValue(response([{ name: "duplicate" }]));
    await submit(kind === "pattern" ? "Save Pattern" : "Save Snapshot");
    expect(screen.getByText(`You already have a ${kind} named "duplicate"`)).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(saved).not.toHaveBeenCalled();
  });

  it("saves SNO recert settings and reports ongoing pattern capture", async () => {
    render(<SavePatternModal projectId="p" projectName="Lab" hasRunningVMs isSno onSaved={saved} onClose={close} />);
    fireEvent.change(screen.getByLabelText("Description"), { target: { value: "training" } });
    fireEvent.click(screen.getByLabelText("Capture clock target for backdating"));
    fetchMock.mockImplementation(async (_, options) => response(options?.method === "POST" ? { id: "pattern", state: "capturing" } : []));
    await submit("Save Pattern");
    expect(saved).toHaveBeenCalledWith("pattern", true);
    const body = JSON.parse(String(fetchMock.mock.calls[1][1]?.body));
    expect(body).toMatchObject({ description: "training", quiesce_cluster: false, capture_clock_target: true, recert: true });
  });

  it("force-stops a project when graceful shutdown expires and captures after confirmation", async () => {
    render(<SavePatternModal projectId="p" projectName="Lab" hasRunningVMs onSaved={saved} onClose={close} />);
    fireEvent.click(screen.getByLabelText("Stop VMs before capture instead"));
    let forced = false;
    fetchMock.mockImplementation(async (url, options) => {
      if (String(url).endsWith("force-stop")) forced = true;
      if (String(url) === "/api/v1/projects/p" && !options?.method) return response({ state: forced ? "stopped" : "active" });
      if (String(url) === "/api/v1/patterns/" && options?.method === "POST") return response({ id: "pattern", state: "available" });
      return response([]);
    });
    await submit("Save Pattern");
    expect(forced).toBe(true);
    expect(saved).toHaveBeenCalledWith("pattern", false);
  });

  it.each(["pattern", "snapshot"])("does not begin %s capture when force-stop never completes", async (kind) => {
    render(kind === "pattern"
      ? <SavePatternModal projectId="p" projectName="Lab" hasRunningVMs onSaved={saved} onClose={close} />
      : <SnapshotVMModal projectId="p" vmId="vm" vmName="VM" isRunning onSaved={saved} onClose={close} />);
    if (kind === "pattern") fireEvent.click(screen.getByLabelText("Stop VMs before capture instead"));
    fetchMock.mockImplementation(async (url) => response(String(url).includes("?q=") || String(url).includes("&q=") ? [] : { state: "running" }));
    await submit(kind === "pattern" ? "Save Pattern" : "Save Snapshot");
    expect(saved).not.toHaveBeenCalled();
    expect(screen.getByText(/Timed out waiting for .* to stop/)).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([url, options]) => options?.method === "POST" && (String(url).endsWith("/snapshot") || String(url) === "/api/v1/patterns/"))).toBe(false);
  });

  it.each(["ready", "error", "capturing"])("handles snapshot capture state %s before restarting the VM", async (state) => {
    render(<SnapshotVMModal projectId="p" vmId="vm" vmName="VM" isRunning onSaved={saved} onClose={close} />);
    fetchMock.mockImplementation(async (url) => {
      if (String(url).includes("?")) return response([]);
      if (String(url).endsWith("/status")) return response({ state: "shut_off" });
      if (String(url).endsWith("/snapshot")) return response({ id: "snap" });
      if (String(url) === "/api/v1/library/snap") return response({ state });
      return response({});
    });
    await submit("Save Snapshot");
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/projects/p/vms/vm/start", { method: "POST" });
    if (state === "ready") expect(saved).toHaveBeenCalledOnce();
    else {
      expect(saved).not.toHaveBeenCalled();
      expect(screen.getByText(state === "error" ? "Disk capture failed" : "Timed out waiting for disk capture")).toBeInTheDocument();
    }
  });

  it.each(["pattern", "snapshot"])("reports %s API errors and restarts only the stopped machines", async (kind) => {
    render(kind === "pattern"
      ? <SavePatternModal projectId="p" projectName="Lab" hasRunningVMs onSaved={saved} onClose={close} />
      : <SnapshotVMModal projectId="p" vmId="vm" vmName="VM" isRunning onSaved={saved} onClose={close} />);
    if (kind === "pattern") fireEvent.click(screen.getByLabelText("Stop VMs before capture instead"));
    fetchMock.mockImplementation(async (url, options) => {
      if (String(url).includes("?")) return response([], false);
      if (String(url).endsWith("/status")) return response({ state: "shut off" });
      if (String(url) === "/api/v1/projects/p") return response({ state: "stopped" });
      if (options?.method === "POST" && (String(url).endsWith("/snapshot") || String(url) === "/api/v1/patterns/")) return response({ detail: "Storage unavailable" }, false);
      return response({});
    });
    await submit(kind === "pattern" ? "Save Pattern" : "Save Snapshot");
    expect(screen.getByText("Storage unavailable")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(kind === "pattern" ? "/api/v1/projects/p/start" : "/api/v1/projects/p/vms/vm/start", { method: "POST" });
    expect(saved).not.toHaveBeenCalled();
  });

  it("captures an already stopped VM without any lifecycle requests", async () => {
    render(<SnapshotVMModal projectId="p" vmId="vm" vmName="VM" isRunning={false} onSaved={saved} onClose={close} />);
    fetchMock.mockImplementation(async (url) => {
      if (String(url).includes("?")) return response([]);
      return response(String(url).endsWith("/snapshot") ? { id: "snap" } : { state: "ready" });
    });
    await submit("Save Snapshot");
    expect(saved).toHaveBeenCalledOnce();
    expect(fetchMock.mock.calls.some(([url]) => /\/(start|stop|status)$/.test(String(url)))).toBe(false);
  });
});
