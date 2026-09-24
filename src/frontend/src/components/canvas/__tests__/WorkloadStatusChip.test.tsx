import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import WorkloadStatusChip from "@/components/canvas/WorkloadStatusChip";

describe("WorkloadStatusChip", () => {
  it("renders label and calls onClick", () => {
    const onClick = vi.fn();
    render(<WorkloadStatusChip label="Workload 2/5 · operators" onClick={onClick} />);
    expect(screen.getByText("Workload 2/5 · operators")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /workload 2\/5/i }));
    expect(onClick).toHaveBeenCalledTimes(1);
  });
});
