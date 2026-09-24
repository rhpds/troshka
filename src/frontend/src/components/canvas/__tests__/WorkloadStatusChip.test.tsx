import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import WorkloadStatusChip from "@/components/canvas/WorkloadStatusChip";

describe("WorkloadStatusChip", () => {
  it("renders headline and detail on one line and calls onClick", () => {
    const onClick = vi.fn();
    render(
      <WorkloadStatusChip
        headline="Workload 2/5"
        detail="troshka_workload_cclm_operators"
        onClick={onClick}
      />,
    );
    expect(screen.getByText(/Workload 2\/5/)).toBeInTheDocument();
    expect(screen.getByText(/troshka_workload_cclm_operators/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /workload 2\/5/i }));
    expect(onClick).toHaveBeenCalledTimes(1);
  });
});
