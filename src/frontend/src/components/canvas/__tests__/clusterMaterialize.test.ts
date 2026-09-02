import { describe, it, expect } from "vitest";
import { reconcileClusterVms, applyClusterSizing } from "@/components/canvas/clusterMaterialize";

const cluster = {
  id: "prod",
  nodeId: "cluster-prod",
  type: "standard",
  controlPlane: 3,
  workers: 2,
  controlPlaneCpu: 8,
  controlPlaneMemory: 16384,
  controlPlaneDisk: 120,
  workerCpu: 4,
  workerMemory: 8192,
  workerDisk: 100,
} as any;

describe("reconcileClusterVms", () => {
  it("adds missing cp/workers with rhcos + parentId + sizing", () => {
    const out = reconcileClusterVms(cluster, []);
    const cps = out.filter((n) => n.data.clusterRole === "control-plane");
    const wks = out.filter((n) => n.data.clusterRole === "worker");
    expect(cps).toHaveLength(3);
    expect(wks).toHaveLength(2);
    expect(cps[0].parentId).toBe("cluster-prod");
    expect(cps[0].data.os).toBe("rhcos");
    expect(cps[0].data.clusterId).toBe("prod");
    expect(cps[0].data.vcpus ?? cps[0].data.cpu).toBe(8);
    expect(cps[0].data.firmware).toBe("uefi");
  });
  it("does not remove a user-customized member when count shrinks", () => {
    const custom = {
      id: "prod-cp-2",
      type: "vmNode",
      position: { x: 0, y: 0 },
      parentId: "cluster-prod",
      data: {
        os: "rhcos",
        clusterId: "prod",
        clusterRole: "control-plane",
        generated: false,
        vcpus: 32,
      },
    };
    const out = reconcileClusterVms({ ...cluster, controlPlane: 1 } as any, [
      custom as any,
    ]);
    expect(out.find((n) => n.id === "prod-cp-2")).toBeTruthy(); // preserved
  });
  it("is idempotent", () => {
    const once = reconcileClusterVms(cluster, []);
    const twice = reconcileClusterVms(cluster, once);
    expect(
      twice.filter((n) => n.data.clusterRole === "control-plane"),
    ).toHaveLength(3);
  });

  it("marks generated members and auto-removes only generated surplus", () => {
    const once = reconcileClusterVms(cluster, []);
    expect(once.every((n) => n.data.generated === true)).toBe(true);
    // shrink workers 2 -> 0: both generated workers removed
    const shrunk = reconcileClusterVms(
      { ...cluster, workers: 0 } as any,
      once,
    );
    expect(shrunk.filter((n) => n.data.clusterRole === "worker")).toHaveLength(
      0,
    );
    expect(
      shrunk.filter((n) => n.data.clusterRole === "control-plane"),
    ).toHaveLength(3);
  });

  it("applyClusterSizing pushes new sizing onto existing generated members", () => {
    const once = reconcileClusterVms(cluster, []);
    const sized = applyClusterSizing({ ...cluster, controlPlaneCpu: 16 } as any, once);
    const cps = sized.filter((n) => n.data.clusterRole === "control-plane");
    expect(cps).toHaveLength(3);
    expect(cps.every((n) => n.data.vcpus === 16)).toBe(true);
    // workers untouched (still worker default 4)
    expect(
      sized.filter((n) => n.data.clusterRole === "worker").every((n) => n.data.vcpus === 4),
    ).toBe(true);
  });

  it("applyClusterSizing leaves user-customized (generated:false) members alone", () => {
    const custom = {
      id: "prod-cp-0",
      type: "vmNode",
      position: { x: 0, y: 0 },
      parentId: "cluster-prod",
      data: { os: "rhcos", clusterId: "prod", clusterRole: "control-plane", generated: false, vcpus: 32 },
    };
    const sized = applyClusterSizing({ ...cluster, controlPlaneCpu: 16 } as any, [custom as any]);
    expect(sized[0].data.vcpus).toBe(32);
  });

  it("maps rhcos membership + tags on generated members", () => {
    const out = reconcileClusterVms(cluster, []);
    const cp = out.find((n) => n.data.clusterRole === "control-plane")!;
    const wk = out.find((n) => n.data.clusterRole === "worker")!;
    expect(cp.id).toBe("prod-cp-0");
    expect((cp.data.tags as Record<string, string>).AnsibleGroup).toBe(
      "controllers",
    );
    expect((wk.data.tags as Record<string, string>).AnsibleGroup).toBe(
      "workers",
    );
    expect(wk.id).toBe("prod-worker-0");
  });
});
