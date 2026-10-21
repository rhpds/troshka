import React from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { WorkloadRunLogPanel } from "../WorkloadRunDetailModal";

const response = (data: unknown, ok = true) => ({ ok, json: async () => data }) as Response;
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("workload run actions", () => {
  it("cancels a running workload and notifies the parent", async () => {
    const changed = vi.fn();
    const request = vi.fn(async (url: string) => response(url.endsWith("cancel") ? {} : { status: "running", log: "Task started", role_fqcn: "a.b.demo" }));
    vi.stubGlobal("fetch", request);
    render(<WorkloadRunLogPanel runId="run" onRunIdChange={changed} />);
    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(changed).toHaveBeenCalledWith("run"));
    expect(request).toHaveBeenCalledWith("/api/v1/workloads/run/cancel", { method: "POST" });
    expect(screen.getByTestId("workload-run-status")).toHaveTextContent("Cancelled");
    expect(screen.getByTestId("workload-run-error")).toHaveTextContent("Cancelled by user");
    expect(screen.getByRole("button", { name: "Retry" })).toBeEnabled();
  });

  it.each(["Cancel", "Retry"])("keeps %s available after a server rejection", async (action) => {
    const request = vi.fn(async (url: string) => {
      if (url.endsWith("log")) return response({ status: action === "Retry" ? "timeout" : "running", log: "kept" });
      return response({ detail: "Denied" }, false);
    });
    vi.stubGlobal("fetch", request);
    render(<WorkloadRunLogPanel runId="run" />);
    fireEvent.click(await screen.findByRole("button", { name: action }));
    expect(await screen.findByText("Denied")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: action })).toBeEnabled();
    expect(screen.getByText("kept")).toBeInTheDocument();
  });

  it.each(["Cancel", "Retry"])("reports transport failures for %s", async (action) => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (url.endsWith("log")) return response({ status: action === "Retry" ? "error" : "running", started_at: "2026-01-01", error: "failure" });
      throw new Error("offline");
    }));
    render(<WorkloadRunLogPanel runId="run" />);
    fireEvent.click(await screen.findByRole("button", { name: action }));
    expect(await screen.findByText(`${action} failed`)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: action })).toBeEnabled();
  });

  it("sets a standalone title and copies the complete log with temporary feedback", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    vi.stubGlobal("fetch", vi.fn(async () => response({ status: "succeeded", catalog_item: "demo.catalog", log: "one\ntwo" })));
    render(<WorkloadRunLogPanel runId="run" standalone />);
    const copy = await screen.findByRole("button", { name: "Copy All" });
    expect(document.title).toBe("catalog — succeeded");
    vi.useFakeTimers();
    fireEvent.click(copy);
    expect(writeText).toHaveBeenCalledWith("one\ntwo");
    expect(copy).toHaveTextContent("Copied!");
    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
    expect(copy).toHaveTextContent("Copy All");
  });
});
