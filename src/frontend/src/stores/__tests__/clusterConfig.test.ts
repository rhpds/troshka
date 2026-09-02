import { describe, it, expect } from "vitest";
import { makeCluster } from "@/components/canvas/clusterFactory";

describe("ClusterConfig defaults", () => {
  it("gives CP and worker two disks (IBI-ready) and empty networkIds", () => {
    const { cluster } = makeCluster("ocp", { x: 0, y: 0 });
    expect(cluster.controlPlaneDisks).toEqual([
      { sizeGb: 120, bootable: true },
      { sizeGb: 100 },
    ]);
    expect(cluster.workerDisks).toEqual([
      { sizeGb: 120, bootable: true },
      { sizeGb: 100 },
    ]);
    expect(cluster.networkIds).toEqual([]);
  });
});
