import { describe, expect, it } from "vitest";
import { templateImportError } from "../templateImport";

describe("template import sections", () => {
  it("accepts OCP-owned nodes and static NICs without top-level vms", () => {
    expect(templateImportError({ networks: { cluster: { cidr: "10.0.0.0/24" } }, ocp: [{ name: "ocp", type: "sno", nodes: {
      "cp-0": { role: "control-plane", nics: [{ network: "cluster", ip: "10.0.0.10" }] },
    } }] })).toBeNull();
  });

  it("accepts legacy standalone VMs", () => {
    expect(templateImportError({ networks: {}, vms: {} })).toBeNull();
  });

  it("rejects non-mappings and missing topology sections", () => {
    expect(templateImportError([])).toBe("Template must be a YAML mapping");
    expect(templateImportError(null)).toBe("Template must be a YAML mapping");
    expect(templateImportError({ networks: {} })).toBe("Template must contain 'networks' and either 'vms' or 'ocp' sections");
    expect(templateImportError({ ocp: [{}] })).not.toBeNull();
  });
});
