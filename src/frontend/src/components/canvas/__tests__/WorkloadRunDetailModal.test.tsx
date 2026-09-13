import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import WorkloadRunDetailModal from "@/components/canvas/WorkloadRunDetailModal";

describe("WorkloadRunDetailModal", () => {
  beforeEach(() => vi.unstubAllGlobals());
  afterEach(() => vi.unstubAllGlobals());

  it("fetches and renders the run log + status", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve({
          ok: true,
          status: 200,
          json: () =>
            Promise.resolve({ id: "run-1", status: "succeeded", log: "PLAY RECAP\nok=5" }),
        } as Response),
      ),
    );
    render(<WorkloadRunDetailModal runId="run-1" onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText(/PLAY RECAP/)).toBeInTheDocument());
    expect(screen.getByText(/succeeded/i)).toBeInTheDocument();
  });
});
