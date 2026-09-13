import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import WorkloadRunsModal from "@/components/canvas/WorkloadRunsModal";

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
});
