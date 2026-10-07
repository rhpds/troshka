import { describe, expect, it } from "vitest";

import { formatApiDetail } from "./apiError";

describe("formatApiDetail", () => {
  it("returns strings as-is", () => {
    expect(formatApiDetail("boom")).toBe("boom");
  });

  it("formats schema validation detail with paths", () => {
    const text = formatApiDetail({
      message: "Template schema validation failed: /name: bad (and 1 more)",
      errors: [
        { path: "/name", message: "does not match pattern" },
        { path: "/networks/bmc", message: "unknown keys: bmc_password" },
      ],
    });
    expect(text).toContain("Template schema validation failed");
    expect(text).toContain("/name: does not match pattern");
    expect(text).toContain("/networks/bmc: unknown keys: bmc_password");
  });

  it("formats fastapi validation arrays", () => {
    const text = formatApiDetail([
      { loc: ["body", "template_yaml"], msg: "field required", type: "value_error" },
    ]);
    expect(text).toContain("body.template_yaml: field required");
  });

  it("uses fallback for empty values", () => {
    expect(formatApiDetail(null, "Import failed")).toBe("Import failed");
  });
});
