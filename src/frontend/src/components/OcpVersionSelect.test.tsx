import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import OcpVersionSelect from "./OcpVersionSelect";

const options = [
  { value: "5.1.0-ec.1", label: "5.1.0-ec.1 — Dev Preview" },
  { value: "5.0.0", label: "5.0.0 — General Availability" },
  { value: "5.0.0-ec.6", label: "5.0.0-ec.6 — Dev Preview" },
  { value: "4.22.10", label: "4.22.10 — Full Support" },
];

describe("OCP version search", () => {
  it("matches the start of a version, not a patch number inside another minor", () => {
    render(<OcpVersionSelect value="" onChange={vi.fn()} options={[
      { value: "4.14.12", label: "4.14.12 — Extended Support" },
      { value: "4.12.102", label: "4.12.102 — Extended Support" },
      { value: "4.12.101", label: "4.12.101 — Extended Support" },
    ]} />);
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "4.12" } });
    expect(screen.queryByRole("option", { name: /4\.14\.12/ })).not.toBeInTheDocument();
    expect(screen.getAllByRole("option")).toHaveLength(2);
  });

  it("filters by typed version and saves only the chosen complete release", () => {
    const onChange = vi.fn();
    render(<OcpVersionSelect value="4.22.10" options={options} onChange={onChange} />);
    const input = screen.getByRole("combobox", { name: "OCP Version" });
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: "5.0" } });
    expect(screen.getAllByRole("option")).toHaveLength(2);
    expect(screen.queryByRole("option", { name: /5.1/ })).not.toBeInTheDocument();
    expect(onChange).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("option", { name: "5.0.0 — General Availability" }));
    expect(onChange).toHaveBeenCalledWith("5.0.0");
  });

  it("supports keyboard selection and shows a no-match result", () => {
    const onChange = vi.fn();
    render(<OcpVersionSelect value="" options={options} onChange={onChange} />);
    const input = screen.getByRole("combobox");
    fireEvent.change(input, { target: { value: "5.0" } });
    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onChange).toHaveBeenCalledWith("5.0.0-ec.6");
    fireEvent.change(input, { target: { value: "99.0" } });
    expect(screen.getByText("No matching releases")).toBeInTheDocument();
  });

  it("preserves locked deployed versions even when absent from the catalog", () => {
    render(<OcpVersionSelect value="5.0" options={options} onChange={vi.fn()} disabled />);
    expect(screen.getByRole("combobox")).toHaveValue("5.0");
    expect(screen.getByRole("combobox")).toBeDisabled();
  });
});
