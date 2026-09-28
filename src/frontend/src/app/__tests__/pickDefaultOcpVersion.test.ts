import { describe, it, expect } from "vitest";
import {
  formatTemplateVersionLabel,
  pickDefaultOcpVersion,
} from "@/lib/ocpVersion";

describe("pickDefaultOcpVersion", () => {
  it("prefers latest 4.x when 5.x is also listed", () => {
    expect(pickDefaultOcpVersion(["4.19", "4.20", "4.21", "4.22", "5.0"])).toBe(
      "4.22",
    );
  });

  it("returns 5.x only when no stable 4.x exists", () => {
    expect(pickDefaultOcpVersion(["5.0"])).toBe("5.0");
  });

  it("handles empty input", () => {
    expect(pickDefaultOcpVersion([])).toBe("");
  });
});

describe("formatTemplateVersionLabel", () => {
  it("marks OCP 5 as dev preview", () => {
    expect(formatTemplateVersionLabel("5.0", false)).toBe("5.0 (dev preview)");
  });

  it("keeps stable OCP labels with latest", () => {
    expect(formatTemplateVersionLabel("4.22", false, "4.22.1")).toBe(
      "4.22 (latest: 4.22.1)",
    );
  });
});
