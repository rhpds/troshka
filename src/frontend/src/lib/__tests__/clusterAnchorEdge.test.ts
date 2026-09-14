import { describe, expect, it } from "vitest";
import { buildClusterAnchorPath } from "@/lib/clusterAnchorEdge";

describe("buildClusterAnchorPath", () => {
  it("draws a straight vertical line when aligned", () => {
    const path = buildClusterAnchorPath(100, 200, 102, 350, "top");
    expect(path).toBe("M 100 200 L 102 350");
  });

  it("routes top anchor with bus stub then horizontal bus", () => {
    const path = buildClusterAnchorPath(400, 220, 200, 350, "top");
    expect(path).toBe("M 400 220 L 400 248 L 200 248 L 200 350");
  });

  it("routes bottom anchor with bus stub up to shared horizontal", () => {
    const path = buildClusterAnchorPath(600, 820, 300, 750, "bottom");
    expect(path).toBe("M 300 750 L 300 792 L 600 792 L 600 820");
  });
});
