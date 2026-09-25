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
    fireEvent.click(screen.getByTestId("workload-status-chip"));
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("shows Retry for failed variant and calls onRetry", () => {
    const onRetry = vi.fn();
    render(
      <WorkloadStatusChip
        headline="Workload 2/5"
        detail="troshka_workload_cclm_network"
        variant="failed"
        onClick={() => {}}
        onRetry={onRetry}
      />,
    );
    expect(screen.getByText(/Interrupted/)).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("workload-chain-retry"));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });
});
