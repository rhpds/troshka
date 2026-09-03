import { describe, it, expect } from "vitest";
import { makeCluster } from "@/components/canvas/clusterFactory";

describe("makeCluster", () => {
  it("creates a clusterNode + matching ClusterConfig with defaults", () => {
    const { node, cluster } = makeCluster("prod", { x: 10, y: 20 });
    expect(node.type).toBe("clusterNode");
    expect(node.id).toBe(cluster.nodeId);
    expect(cluster.type).toBe("standard");
    expect(cluster.controlPlane).toBe(3);
    expect(cluster.workers).toBe(0);
    expect(cluster.controlPlaneCpu).toBe(8);
    expect(cluster.workerCpu).toBe(4);
    expect(cluster.baseDomain).toBe("local");
    expect(cluster.recert).toBe(false);
    expect(cluster.monitorHealth).toBe(true);
    expect(cluster.configureBastionBrowser).toBe(false);
    expect((node.data as Record<string, unknown>).clusterId).toBe(cluster.id);
  });

  it("produces unique ids across calls", () => {
    const a = makeCluster("ocp", { x: 0, y: 0 });
    const b = makeCluster("ocp", { x: 0, y: 0 });
    expect(a.node.id).not.toBe(b.node.id);
    expect(a.cluster.id).not.toBe(b.cluster.id);
    expect(a.node.id).toBe(a.cluster.nodeId);
    expect(b.node.id).toBe(b.cluster.nodeId);
  });
});
