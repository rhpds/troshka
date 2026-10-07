import { describe, it, expect } from "vitest";
import { vmStatusLabel } from "@/lib/vmStatus";

describe("vmStatusLabel", () => {
  it("labels paused distinctly from stopped", () => {
    expect(vmStatusLabel("paused")).toBe("Paused");
    expect(vmStatusLabel("stopped")).toBe("Stopped");
    expect(vmStatusLabel("paused")).not.toBe(vmStatusLabel("stopped"));
  });

  it("labels hibernated distinctly from stopped", () => {
    expect(vmStatusLabel("hibernated")).toBe("Hibernated");
    expect(vmStatusLabel("hibernated")).not.toBe(vmStatusLabel("stopped"));
  });

  it("falls back to Stopped for unknown/empty status", () => {
    expect(vmStatusLabel(undefined)).toBe("Stopped");
    expect(vmStatusLabel(null)).toBe("Stopped");
    expect(vmStatusLabel("not_found")).toBe("Not Found");
  });

  it("labels running", () => {
    expect(vmStatusLabel("running")).toBe("Running");
  });
});
