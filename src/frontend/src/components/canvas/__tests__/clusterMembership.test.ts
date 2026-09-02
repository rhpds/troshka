import { describe, it, expect } from "vitest";
import { resolveMembership, orderChildAfterParent } from "@/components/canvas/clusterMembership";

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

describe("orderChildAfterParent", () => {
  const vm = { id: "n1", type: "vmNode" } as any;
  const cluster = { id: "cluster-prod", type: "clusterNode" } as any;

  it("moves a child that precedes its parent to immediately after it", () => {
    const ordered = orderChildAfterParent([vm, cluster], "n1", "cluster-prod");
    const childIdx = ordered.findIndex((n) => n.id === "n1");
    const parentIdx = ordered.findIndex((n) => n.id === "cluster-prod");
    expect(childIdx).toBeGreaterThan(parentIdx);
    expect(parentIdx).toBe(0);
    expect(childIdx).toBe(1);
  });

  it("is a no-op when the child is already after the parent", () => {
    const input = [cluster, vm];
    const ordered = orderChildAfterParent(input, "n1", "cluster-prod");
    expect(ordered).toEqual(input);
    expect(ordered.findIndex((n) => n.id === "n1")).toBeGreaterThan(
      ordered.findIndex((n) => n.id === "cluster-prod"),
    );
  });

  it("is a no-op when the parent is missing", () => {
    const input = [vm];
    expect(orderChildAfterParent(input, "n1", "cluster-missing")).toEqual(input);
  });
});
