import { describe, it, expect } from "vitest";
import { makeCluster } from "@/components/canvas/clusterFactory";
import {
  coerceDiskSpec,
  normalizeClusterDiskFields,
  type ClusterConfig,
  type DiskSpec,
} from "@/stores/canvasStore";

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

describe("normalizeClusterDiskFields", () => {
  it("converts size_gb / ocp_mount and syncs legacy single-disk ints", () => {
    expect(coerceDiskSpec({ size_gb: 250, bootable: true })).toEqual({
      sizeGb: 250,
      bootable: true,
    });
    expect(
      coerceDiskSpec({ size_gb: 250, ocp_mount: "/var/lib/containers" }),
    ).toEqual({ sizeGb: 250, ocpMount: "/var/lib/containers" });

    const snakeDisks = [
      { size_gb: 250, bootable: true },
      { size_gb: 250, ocp_mount: "/var/lib/containers" },
    ] as unknown as DiskSpec[];
    const cluster = normalizeClusterDiskFields({
      id: "c1",
      name: "c1",
      nodeId: "n1",
      type: "sno",
      controlPlane: 1,
      workers: 0,
      controlPlaneDisk: 120,
      controlPlaneDisks: snakeDisks,
    } as ClusterConfig);
    expect(cluster.controlPlaneDisks).toEqual([
      { sizeGb: 250, bootable: true },
      { sizeGb: 250, ocpMount: "/var/lib/containers" },
    ]);
    expect(cluster.controlPlaneDisk).toBe(250);
  });
});
