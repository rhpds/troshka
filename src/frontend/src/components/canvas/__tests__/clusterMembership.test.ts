import { describe, it, expect } from "vitest";
import { resolveMembership } from "@/components/canvas/clusterMembership";

const boundary = { id: "cluster-prod", type: "clusterNode", position: { x: 0, y: 0 }, width: 500, height: 300, data: { clusterId: "prod" } } as any;

describe("resolveMembership", () => {
  it("assigns when the node center is inside a boundary", () => {
    const vm = { id: "n1", type: "vmNode", position: { x: 100, y: 100 }, data: { os: "rhcos" } } as any;
    expect(resolveMembership(vm, [boundary])).toEqual({ parentId: "cluster-prod", clusterId: "prod" });
  });
  it("clears when the node is outside all boundaries", () => {
    const vm = { id: "n1", type: "vmNode", position: { x: 900, y: 900 }, data: { os: "rhcos" } } as any;
    expect(resolveMembership(vm, [boundary])).toEqual({ parentId: null, clusterId: null });
  });
});
