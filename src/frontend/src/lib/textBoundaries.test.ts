import { describe, expect, it } from "vitest";
import { numericNameSuffix, trimEdgeCharacter } from "./textBoundaries";

describe("delimiter trimming", () => {
  it.each([
    ["", "-", ""], ["---", "-", ""], ["--a--b---", "-", "a--b"],
    ["///path/to///", "/", "path/to"], ["plain", "/", "plain"],
    ["-a", "-", "a"], ["a-", "-", "a"],
  ])("trims %j without altering interior delimiters", (value, delimiter, expected) => {
    expect(trimEdgeCharacter(value, delimiter)).toBe(expected);
  });
  it("handles long delimiter runs", () => {
    expect(trimEdgeCharacter("/".repeat(100_000) + "x" + "/".repeat(100_000), "/")).toBe("x");
  });
});

describe("numbered names", () => {
  it.each([
    ["vm-001", "vm", "001"], ["vm_12", "vm", "12"], ["vm12", "vm", "12"],
    ["123", "", "123"], ["vm--1", "vm-", "1"], ["vm2a3", "vm2a", "3"],
  ])("splits %s", (name, prefix, digits) => {
    expect(numericNameSuffix(name)).toEqual({ prefix, digits });
  });
  it.each(["", "vm", "vm-", "vm123x"])("rejects unnumbered %j", name => {
    expect(numericNameSuffix(name)).toBeNull();
  });
  it("handles long digit runs without repeated matching", () => {
    const digits = "1".repeat(100_000);
    expect(numericNameSuffix("vm-" + digits)).toEqual({ prefix: "vm", digits });
  });
});
