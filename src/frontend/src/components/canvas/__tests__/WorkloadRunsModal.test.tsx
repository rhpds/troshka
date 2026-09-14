import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import WorkloadRunsModal from "@/components/canvas/WorkloadRunsModal";
import { useCanvasStore } from "@/stores/canvasStore";

describe("WorkloadRunsModal", () => {
  beforeEach(() =>
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve({
          ok: true,
          status: 200,
          json: () =>
            Promise.resolve([
              {
                id: "run-1",
                kind: "ad_hoc",
                role_fqcn: "some.role",
                catalog_item: null,
                status: "succeeded",
                error: null,
                created_at: "2026-09-13T10:00:00+00:00",
              },
            ]),
        } as Response),
      ),
    ),
  );
  afterEach(() => vi.unstubAllGlobals());

  it("lists runs and opens one on View", async () => {
    const onOpenRun = vi.fn();
    render(<WorkloadRunsModal projectId="p1" onClose={() => {}} onOpenRun={onOpenRun} />);
    await waitFor(() => expect(screen.getByText("some.role")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /view/i }));
    expect(onOpenRun).toHaveBeenCalledWith("run-1");
  });

  it("renders target label for cluster-mode runs", async () => {
    useCanvasStore.setState({
      clusters: [
        { id: "c1", name: "prod", nodeId: "n1", type: "ocp", controlPlane: 1, workers: 0 },
      ],
      nodes: [],
      edges: [],
    } as never);

    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve({
          ok: true,
          status: 200,
          json: () =>
            Promise.resolve([
              {
                id: "run-cluster",
                kind: "ad_hoc",
                role_fqcn: "demo.role",
                catalog_item: null,
                status: "pending",
                error: null,
                created_at: "2026-09-13T11:00:00+00:00",
                target_map: { mode: "cluster", cluster_id: "c1" },
              },
            ]),
        } as Response),
      ),
    );

    render(<WorkloadRunsModal projectId="p1" onClose={() => {}} onOpenRun={() => {}} />);
    await waitFor(() => expect(screen.getByText("prod")).toBeInTheDocument());
  });

  it("renders target label for vms-mode runs", async () => {
    useCanvasStore.setState({
      clusters: [],
      nodes: [],
      edges: [],
    } as never);

    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve({
          ok: true,
          status: 200,
          json: () =>
            Promise.resolve([
              {
                id: "run-vms",
                kind: "ad_hoc",
                role_fqcn: "demo.role",
                catalog_item: null,
                status: "pending",
                error: null,
                created_at: "2026-09-13T12:00:00+00:00",
                target_map: { mode: "vms", vm_names: ["web1", "db1"] },
              },
            ]),
        } as Response),
      ),
    );

    render(<WorkloadRunsModal projectId="p1" onClose={() => {}} onOpenRun={() => {}} />);
    await waitFor(() => expect(screen.getByText("web1, db1")).toBeInTheDocument());
  });
});
