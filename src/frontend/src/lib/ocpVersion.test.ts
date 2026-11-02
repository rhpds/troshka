import { describe, expect, it } from "vitest";
import { pickDefaultOcpVersion, templateVersionChoices, formatTemplateVersionLabel } from "./ocpVersion";

describe("available OCP releases", () => {
  it("expands template minors to exact available releases without crossing minors", () => {
    const available = ["5.1.0-ec.1", "5.0.0-ec.6", "4.22.10", "4.22.9", "4.21.1"];
    expect(templateVersionChoices(["4.22", "5.0"], available, false)).toEqual([
      "5.0.0-ec.6", "4.22.10", "4.22.9",
    ]);
  });
  it("does not invent choices when the catalog is unavailable", () => {
    expect(templateVersionChoices(["5.0"], [], false)).toEqual([]);
  });
  it("preserves the separate OKD version choices", () => {
    expect(templateVersionChoices(["4.22", "5.0"], ["5.1.0-ec.1"], true)).toEqual(["4.22", "5.0"]);
  });
  it("defaults to the newest numeric stable patch, not a preview or lexical maximum", () => {
    expect(pickDefaultOcpVersion(["5.1.0-ec.1", "4.22.10", "4.22.9"])).toBe("4.22.10");
    expect(pickDefaultOcpVersion(["5.0.0-ec.10", "5.0.0-ec.9"])).toBe("5.0.0-ec.10");
  });
  it("labels an exact stable release without calling it latest", () => {
    expect(formatTemplateVersionLabel("4.22.10", false, "4.22.10")).toBe("4.22.10");
    expect(formatTemplateVersionLabel("5.0.0", false, "5.0.0")).toBe("5.0.0");
    expect(pickDefaultOcpVersion(["5.1.0-ec.1", "5.0.0", "4.22.10"])).toBe("5.0.0");
  });
});
