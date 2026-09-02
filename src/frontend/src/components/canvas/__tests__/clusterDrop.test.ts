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
    expect(cluster.baseDomain).toBe("ocp.local");
  });
});
