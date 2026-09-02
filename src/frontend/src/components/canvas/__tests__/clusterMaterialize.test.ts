import { describe, it, expect } from "vitest";
import { reconcileClusterVms, applyClusterSizing, memberRole, assignmentDataPatch, materializeClusterInto } from "@/components/canvas/clusterMaterialize";
import { makeCluster } from "@/components/canvas/clusterFactory";

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

// Backend (from-template / migration) members carry clusterId +
// tags.AnsibleGroup + generated:true but NO clusterRole. Membership/role must
// resolve via AnsibleGroup fallback so counts match and no duplicates are added.
function backendMembers() {
  const mk = (id: string, group: string) => ({
    id,
    type: "vmNode",
    position: { x: 0, y: 0 },
    parentId: "cluster-prod",
    data: {
      os: "rhcos",
      clusterId: "prod",
      generated: true,
      vcpus: group === "controllers" ? 8 : 4,
      ram: group === "controllers" ? 16 : 8,
      disk: group === "controllers" ? 120 : 100,
      tags: { AnsibleGroup: group },
    },
  });
  return [
    mk("prod-cp-0", "controllers"),
    mk("prod-cp-1", "controllers"),
    mk("prod-cp-2", "controllers"),
    mk("prod-worker-0", "workers"),
    mk("prod-worker-1", "workers"),
  ] as any[];
}

describe("memberRole", () => {
  const wrap = (data: Record<string, unknown>) => ({ id: "x", type: "vmNode", data } as any);
  it("prefers explicit clusterRole", () => {
    expect(memberRole(wrap({ clusterRole: "control-plane", tags: { AnsibleGroup: "workers" } }))).toBe("control-plane");
    expect(memberRole(wrap({ clusterRole: "worker" }))).toBe("worker");
  });
  it("falls back to AnsibleGroup tag", () => {
    expect(memberRole(wrap({ tags: { AnsibleGroup: "controllers" } }))).toBe("control-plane");
    expect(memberRole(wrap({ tags: { AnsibleGroup: "workers" } }))).toBe("worker");
  });
  it("returns null when neither present", () => {
    expect(memberRole(wrap({}))).toBeNull();
    expect(memberRole(wrap({ tags: { AnsibleGroup: "other" } }))).toBeNull();
  });
});

describe("backend-created cluster members (no clusterRole)", () => {
  it("reconcileClusterVms with matching counts is a no-op (no duplicates)", () => {
    const before = backendMembers();
    const out = reconcileClusterVms(cluster, before);
    expect(out).toHaveLength(5);
    expect(out.filter((n) => memberRole(n) === "control-plane")).toHaveLength(3);
    expect(out.filter((n) => memberRole(n) === "worker")).toHaveLength(2);
  });

  it("applyClusterSizing updates backend-created members via AnsibleGroup", () => {
    const before = backendMembers();
    const sized = applyClusterSizing({ ...cluster, controlPlaneCpu: 16, workerCpu: 12 } as any, before);
    const cps = sized.filter((n) => memberRole(n) === "control-plane");
    const wks = sized.filter((n) => memberRole(n) === "worker");
    expect(cps.every((n) => n.data.vcpus === 16)).toBe(true);
    expect(wks.every((n) => n.data.vcpus === 12)).toBe(true);
  });
});

describe("materializeClusterInto", () => {
  it("materializeClusterInto creates 3 CP members for a fresh standard cluster", () => {
    const { node, cluster } = makeCluster("ocp", { x: 0, y: 0 });
    const nodes = materializeClusterInto(cluster, [node]);
    const cps = nodes.filter(
      (n) => n.type === "vmNode" && n.data.clusterId === cluster.id && n.data.clusterRole === "control-plane",
    );
    expect(cps).toHaveLength(3);
    expect(cps.every((n) => n.parentId === cluster.nodeId)).toBe(true);
  });
});

describe("assignmentDataPatch (drag-in default role)", () => {
  const vm = (data: Record<string, unknown> = {}) => ({ id: "vm1", type: "vmNode", data } as any);

  it("defaults a newly-assigned roleless VM to worker + AnsibleGroup workers", () => {
    const patch = assignmentDataPatch(vm({ tags: { Foo: "bar" } }), "prod", null);
    expect(patch.clusterId).toBe("prod");
    expect(patch.clusterRole).toBe("worker");
    expect(patch.tags).toEqual({ Foo: "bar", AnsibleGroup: "workers" });
  });

  it("does not override an existing role on assignment", () => {
    const patch = assignmentDataPatch(vm({ clusterRole: "control-plane" }), "prod", null);
    expect(patch.clusterId).toBe("prod");
    expect(patch.clusterRole).toBeUndefined();
    expect(patch.tags).toBeUndefined();
  });

  it("does not add a role on re-assignment between clusters", () => {
    const patch = assignmentDataPatch(vm({ tags: { AnsibleGroup: "workers" } }), "prod2", "prod1");
    expect(patch.clusterId).toBe("prod2");
    expect(patch.clusterRole).toBeUndefined();
  });

  it("does not add a role when leaving a cluster (unassignment)", () => {
    const patch = assignmentDataPatch(vm({ clusterRole: "worker" }), null, "prod");
    expect(patch.clusterId).toBeNull();
    expect(patch.clusterRole).toBeUndefined();
  });
});
