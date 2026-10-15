import { describe, expect, it } from "vitest";
import type { Edge, Node } from "@xyflow/react";
import { withStorageFlowAnimation } from "@/lib/storageEdgeStyle";

const node = (id: string, type: string, data = {}): Node => ({ id, type, data, position: { x: 0, y: 0 } });

describe("storage link animation", () => {
  it.each([false, true])("animates existing Showroom mount links in either direction (%s)", (reverse) => {
    const nodes = [node("disk", "storageNode"), node("showroom", "containerNode", { name: "showroom" })];
    const edge: Edge = { id: "mount", source: reverse ? "showroom" : "disk", target: reverse ? "disk" : "showroom", className: "edge-storage-pulse", style: { strokeDasharray: "4 4" } };
    const result = withStorageFlowAnimation(edge, nodes);
    expect(result.animated).toBe(true);
    expect(result.className).toContain("edge-storage-flow");
    expect(result.style).toMatchObject({ ...edge.style, "--troshka-edge-dash-offset": "-8px" });
    expect(edge.animated).toBeUndefined();
  });

  it.each([false, true])("animates Ceph-to-cluster links in either direction (%s)", (reverse) => {
    const nodes = [node("ceph", "cephClusterNode"), node("ocp", "clusterNode")];
    const edge: Edge = { id: "ceph-link", source: reverse ? "ocp" : "ceph", target: reverse ? "ceph" : "ocp" };
    expect(withStorageFlowAnimation(edge, nodes).className).toContain("edge-storage-flow");
  });

  it("leaves other links unchanged", () => {
    const edge: Edge = { id: "other", source: "disk", target: "vm" };
    expect(withStorageFlowAnimation(edge, [node("disk", "storageNode"), node("vm", "vmNode")])).toBe(edge);
    expect(withStorageFlowAnimation(edge, [])).toBe(edge);
  });

  it("uses the gateway link's 14-pixel dash cycle instead of React Flow's 10", () => {
    const edge: Edge = { id: "gateway", source: "showroom", target: "gateway", animated: true, style: { strokeDasharray: "8 6" } };
    const nodes = [node("showroom", "containerNode", { isShowroom: true }), node("gateway", "networkNode", { subtype: "gateway" })];
    const result = withStorageFlowAnimation(edge, nodes);
    expect(result.className).toContain("edge-storage-flow");
    expect(result.style).toMatchObject({ "--troshka-edge-dash-offset": "-14px" });
  });
});
