import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import WorkloadRunDetailModal, {
  openWorkloadRunLogWindow,
} from "@/components/canvas/WorkloadRunDetailModal";

describe("WorkloadRunDetailModal", () => {
  beforeEach(() => vi.unstubAllGlobals());
  afterEach(() => vi.unstubAllGlobals());

  it("fetches and renders the run name, status, time, and log", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve({
          ok: true,
          status: 200,
          json: () =>
            Promise.resolve({
              id: "run-1",
              status: "succeeded",
              log: "PLAY RECAP\nok=5",
              role_fqcn: "rhpds.demo_workloads.troshka_workload_cclm_operators",
              catalog_item: null,
              kind: "ad_hoc",
              created_at: "2026-09-24T14:30:12.735376+00:00",
              started_at: "2026-09-24T14:30:13.523238+00:00",
              ended_at: "2026-09-24T14:54:21.648925+00:00",
            }),
        } as Response),
      ),
    );
    render(<WorkloadRunDetailModal runId="run-1" onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText(/PLAY RECAP/)).toBeInTheDocument());
    expect(screen.getByRole("heading", { name: "troshka_workload_cclm_operators" })).toBeInTheDocument();
    expect(screen.getByTestId("workload-run-fqcn")).toHaveTextContent(
      "rhpds.demo_workloads.troshka_workload_cclm_operators",
    );
    expect(screen.getByText(/succeeded/i)).toBeInTheDocument();
    expect(screen.getByText(/2026-09-24 14:30:13/)).toBeInTheDocument();
    expect(screen.getByText(/2026-09-24 14:54:21/)).toBeInTheDocument();
  });

  it("pops out into a console window and closes the modal", async () => {
    const open = vi.fn();
    vi.stubGlobal("open", open);
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve({
          ok: true,
          status: 200,
          json: () =>
            Promise.resolve({
              id: "run-1",
              status: "running",
              log: "TASK [x]",
              role_fqcn: "demo.role",
            }),
        } as Response),
      ),
    );
    const onClose = vi.fn();
    render(<WorkloadRunDetailModal runId="run-abc" onClose={onClose} />);
    await waitFor(() => expect(screen.getByText("Pop out")).toBeInTheDocument());
    fireEvent.click(screen.getByText("Pop out"));
    expect(open).toHaveBeenCalledWith(
      expect.stringContaining("/console/workload-log?run=run-abc"),
      "workloadlog_runabc",
      expect.stringContaining("width=1100"),
    );
    expect(onClose).toHaveBeenCalled();
  });

  it("openWorkloadRunLogWindow encodes run id", () => {
    const open = vi.fn();
    vi.stubGlobal("open", open);
    openWorkloadRunLogWindow("rid-1", "my-role");
    expect(open).toHaveBeenCalledWith(
      "/console/workload-log?run=rid-1&name=my-role",
      "workloadlog_rid1",
      expect.any(String),
    );
  });
});
