import { describe, it, expect } from "vitest";
import {
  reconcileClusterVms,
  applyClusterSizing,
  memberRole,
  assignmentDataPatch,
  materializeClusterInto,
  clusterNetworkIdsFromEdges,
  applyClusterNetworks,
  applyClusterDisks,
  suggestClusterVips,
  vipCollision,
  clusterBoxSize,
  buildClusterDnsRecords,
  applyClusterDns,
  effectiveDnsNetworkId,
  clusterPrereqIssues,
} from "@/components/canvas/clusterMaterialize";
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
    const { nodes: out } = reconcileClusterVms(cluster, []);
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
    const { nodes: out } = reconcileClusterVms({ ...cluster, controlPlane: 1 } as any, [
      custom as any,
    ]);
    expect(out.find((n) => n.id === "prod-cp-2")).toBeTruthy(); // preserved
  });
  it("is idempotent", () => {
    const { nodes: once } = reconcileClusterVms(cluster, []);
    const { nodes: twice } = reconcileClusterVms(cluster, once);
    expect(
      twice.filter((n) => n.data.clusterRole === "control-plane"),
    ).toHaveLength(3);
  });

  it("marks generated members and auto-removes only generated surplus", () => {
    const { nodes: once } = reconcileClusterVms(cluster, []);
    expect(once.filter((n) => n.type === "vmNode").every((n) => n.data.generated === true)).toBe(true);
    // shrink workers 2 -> 0: both generated workers removed
    const { nodes: shrunk } = reconcileClusterVms(
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
    const { nodes: once } = reconcileClusterVms(cluster, []);
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
    const { nodes: out } = reconcileClusterVms(cluster, []);
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
    const { nodes: out } = reconcileClusterVms(cluster, before);
    // VM nodes only (not disk nodes): should be 5
    expect(out.filter((n) => n.type === "vmNode")).toHaveLength(5);
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
    const { nodes } = materializeClusterInto(cluster, [node]);
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

describe("cluster member disks (storageNodes + edges + bootDevices)", () => {
  it("materializes two disks per CP member with correct edge + bootDevices", () => {
    const { node, cluster } = makeCluster("ocp", { x: 0, y: 0 });
    const { nodes, edges } = reconcileClusterVms(cluster, [node]);
    const cp = nodes.find((n) => n.data.clusterRole === "control-plane")!;
    const disks = nodes.filter(
      (n) =>
        n.type === "storageNode" &&
        edges.some((e) => e.source === n.id && e.target === cp.id && e.sourceHandle === "right"),
    );
    expect(disks).toHaveLength(2);
    const diskControllers = cp.data as Record<string, unknown>;
    const dcId = (diskControllers.diskControllers as any[])[0].id;
    const e0 = edges.find((e) => e.target === cp.id && e.targetHandle === `dp-${dcId}-left`);
    expect(e0).toBeTruthy();
    const bootDevices = diskControllers.bootDevices as string[];
    expect(bootDevices).toContain(disks.find((d) => (d.data as Record<string, unknown>).bootable)?.id ?? disks[0].id);
  });
});

describe("cluster member NICs (wired to cluster networks)", () => {
  it("gives each member one NIC per cluster network, wired to the network node", () => {
    const net = { id: "net1", type: "networkNode", data: { subtype: "network", cidr: "10.0.0.0/24" } } as any;
    const { node, cluster } = makeCluster("ocp", { x: 0, y: 0 });
    cluster.networkIds = ["net1"];
    const { nodes, edges } = reconcileClusterVms(cluster, [node, net]);
    const cp = nodes.find((n) => n.data.clusterRole === "control-plane")!;
    const nics = (cp.data as Record<string, unknown>).nics as any[];
    expect(nics).toHaveLength(1);
    const nicId = nics[0].id;
    const e = edges.find((x) => x.source === "net1" && x.target === cp.id && x.targetHandle === `nic-${nicId}-top`);
    expect(e).toBeTruthy();
    expect(e!.sourceHandle).toBe("bottom");
  });
});

describe("clusterNetworkIdsFromEdges", () => {
  it("extracts distinct network ids from member NIC edges in member order", () => {
    const memberIds = ["prod-cp-0", "prod-cp-1", "prod-worker-0"];
    const edges = [
      {
        id: "e1",
        source: "net1",
        target: "prod-cp-0",
        targetHandle: "nic-nic1-top",
      },
      {
        id: "e2",
        source: "net2",
        target: "prod-cp-1",
        targetHandle: "nic-nic2-top",
      },
      {
        id: "e3",
        source: "net1",
        target: "prod-worker-0",
        targetHandle: "nic-nic3-top",
      },
      {
        id: "e4",
        source: "net3",
        target: "prod-worker-0",
        targetHandle: "nic-nic4-bottom",
      },
    ] as any[];
    const result = clusterNetworkIdsFromEdges("cluster-prod", memberIds, edges);
    expect(result).toEqual(["net1", "net2", "net3"]);
  });

  it("returns empty array when no NIC edges found", () => {
    const edges = [
      { id: "e1", source: "disk1", target: "prod-cp-0", targetHandle: "dp-dc1-left" },
    ] as any[];
    const result = clusterNetworkIdsFromEdges("cluster-prod", ["prod-cp-0"], edges);
    expect(result).toEqual([]);
  });
});

describe("applyClusterNetworks", () => {
  it("rebuilds NICs for all members to match cluster.networkIds", () => {
    const { node, cluster } = makeCluster("ocp", { x: 0, y: 0 });
    cluster.networkIds = ["net1"];
    const { nodes: materialized, edges: materializedEdges } = reconcileClusterVms(cluster, [node]);

    // Now change to 2 networks and apply
    const net2 = { id: "net2", type: "networkNode", data: { subtype: "network" } } as any;
    const updatedCluster = { ...cluster, networkIds: ["net1", "net2"] };
    const allNodes = [...materialized, net2];

    const { nodes: result, edges: resultEdges } = applyClusterNetworks(
      updatedCluster,
      allNodes,
      materializedEdges,
    );

    // All members should now have 2 NICs
    const cp = result.find((n) => n.data.clusterRole === "control-plane")!;
    const nics = (cp.data as Record<string, unknown>).nics as any[];
    expect(nics).toHaveLength(2);

    // Should have exactly 2 NIC edges per member (1 for each network)
    const cpNicEdges = resultEdges.filter(
      (e) => e.target === cp.id && (e.targetHandle?.startsWith("nic-") ?? false),
    );
    expect(cpNicEdges).toHaveLength(2);
  });

  it("is idempotent: re-applying same networkIds yields identical nic ids/macs/edge ids", () => {
    const { node, cluster } = makeCluster("ocp", { x: 0, y: 0 });
    cluster.networkIds = ["net1", "net2"];
    const net1 = { id: "net1", type: "networkNode", data: { subtype: "network" } } as any;
    const net2 = { id: "net2", type: "networkNode", data: { subtype: "network" } } as any;

    // First materialize with members, then apply networks
    const { nodes: materialized, edges: materializedEdges } = reconcileClusterVms(
      cluster,
      [node, net1, net2],
    );
    const { nodes: n1, edges: e1 } = applyClusterNetworks(
      cluster,
      materialized,
      materializedEdges,
    );
    const { nodes: n2, edges: e2 } = applyClusterNetworks(cluster, n1, e1);

    // NIC ids and MACs must be identical across re-apply (MAC stability critical for BMH boot)
    const cp1 = n1.find((n) => n.data.clusterRole === "control-plane")!;
    const cp2 = n2.find((n) => n.data.clusterRole === "control-plane")!;
    const nics1 = (cp1.data as Record<string, unknown>).nics as any[];
    const nics2 = (cp2.data as Record<string, unknown>).nics as any[];
    expect(nics2).toHaveLength(nics1.length);
    for (let i = 0; i < nics1.length; i += 1) {
      expect(nics2[i].id).toBe(nics1[i].id); // Same NIC id
      expect(nics2[i].mac).toBe(nics1[i].mac); // Same MAC (OCP BMH boot depends on this)
    }

    // Edge ids must be identical
    const nicEdges1 = e1.filter((e) => e.targetHandle?.startsWith("nic-") ?? false);
    const nicEdges2 = e2.filter((e) => e.targetHandle?.startsWith("nic-") ?? false);
    expect(nicEdges2.map((e) => e.id).sort()).toEqual(nicEdges1.map((e) => e.id).sort());
  });

  it("preserves NICs when transitioning: ['net1'] → ['net1','net2'] keeps net1's id+mac", () => {
    const { node, cluster } = makeCluster("ocp", { x: 0, y: 0 });
    cluster.networkIds = ["net1"];
    const net1 = { id: "net1", type: "networkNode", data: { subtype: "network" } } as any;
    const net2 = { id: "net2", type: "networkNode", data: { subtype: "network" } } as any;

    // Initial: materialize with net1, then apply
    const { nodes: materialized, edges: materializedEdges } = reconcileClusterVms(
      cluster,
      [node, net1, net2],
    );
    const { nodes: n1, edges: e1 } = applyClusterNetworks(
      cluster,
      materialized,
      materializedEdges,
    );

    const cp1 = n1.find((n) => n.data.clusterRole === "control-plane")!;
    const nics1 = (cp1.data as Record<string, unknown>).nics as any[];
    const originalNic1 = nics1[0];

    // Add net2
    cluster.networkIds = ["net1", "net2"];
    const { nodes: n2 } = applyClusterNetworks(cluster, n1, e1);

    const cp2 = n2.find((n) => n.data.clusterRole === "control-plane")!;
    const nics2 = (cp2.data as Record<string, unknown>).nics as any[];

    // net1's NIC should be preserved (same id, mac)
    expect(nics2[0].id).toBe(originalNic1.id);
    expect(nics2[0].mac).toBe(originalNic1.mac);
    // net2's NIC should be new
    expect(nics2[1].id).not.toBe(originalNic1.id);
    expect(nics2[1].mac).not.toBe(originalNic1.mac);
  });

  it("removes stale NICs when transitioning: ['net1','net2'] → ['net1'] keeps net1", () => {
    const { node, cluster } = makeCluster("ocp", { x: 0, y: 0 });
    cluster.networkIds = ["net1", "net2"];
    const net1 = { id: "net1", type: "networkNode", data: { subtype: "network" } } as any;
    const net2 = { id: "net2", type: "networkNode", data: { subtype: "network" } } as any;

    // First materialize the cluster with 2 networks
    const { nodes: materialized, edges: materializedEdges } = reconcileClusterVms(
      cluster,
      [node, net1, net2],
    );
    const { nodes: initial, edges: initialEdges } = applyClusterNetworks(
      cluster,
      materialized,
      materializedEdges,
    );

    const cp1 = initial.find((n) => n.data.clusterRole === "control-plane")!;
    const nics1 = (cp1.data as Record<string, unknown>).nics as any[];
    const originalNic1 = nics1[0];

    // Remove net2
    const shrunk = { ...cluster, networkIds: ["net1"] };
    const { nodes: result, edges: resultEdges } = applyClusterNetworks(
      shrunk,
      initial,
      initialEdges,
    );

    // All members should now have 1 NIC
    const cp = result.find((n) => n.data.clusterRole === "control-plane")!;
    const nics = (cp.data as Record<string, unknown>).nics as any[];

    // Should have only 1 NIC (net1, preserved)
    expect(nics).toHaveLength(1);
    expect(nics[0].id).toBe(originalNic1.id);
    expect(nics[0].mac).toBe(originalNic1.mac);

    // Check edge count: should have only 1 NIC edge for this member
    const memberNicEdges = resultEdges.filter(
      (e) => e.target === cp.id && (e.targetHandle?.startsWith("nic-") ?? false),
    );
    expect(memberNicEdges).toHaveLength(1);
  });
});

describe("applyClusterDisks", () => {
  it("adds an extra storageNode + edge + diskController when disk is added to controlPlaneDisks", () => {
    const { node, cluster } = makeCluster("ocp", { x: 0, y: 0 });
    cluster.controlPlaneDisks = [{ sizeGb: 120, bootable: true }];
    const { nodes: initial, edges: initialEdges } = reconcileClusterVms(cluster, [node]);

    const cp = initial.find((n) => n.data.clusterRole === "control-plane")!;
    const disksBefore = initial.filter(
      (n) =>
        n.type === "storageNode" &&
        initialEdges.some((e) => e.source === n.id && e.target === cp.id),
    );
    expect(disksBefore).toHaveLength(1);

    // Add a second disk
    const updated = { ...cluster, controlPlaneDisks: [{ sizeGb: 120, bootable: true }, { sizeGb: 100 }] };
    const { nodes: result, edges: resultEdges } = applyClusterDisks(updated, initial, initialEdges);

    const cpAfter = result.find((n) => n.id === cp.id)!;
    const disksAfter = result.filter(
      (n) =>
        n.type === "storageNode" &&
        resultEdges.some((e) => e.source === n.id && e.target === cpAfter.id),
    );
    expect(disksAfter).toHaveLength(2);

    // Check diskControllers count
    const dcAfter = (cpAfter.data as Record<string, unknown>).diskControllers as any[];
    expect(dcAfter).toHaveLength(2);
  });

  it("removes stale disk nodes when disk count decreases", () => {
    const { node, cluster } = makeCluster("ocp", { x: 0, y: 0 });
    cluster.controlPlaneDisks = [{ sizeGb: 120, bootable: true }, { sizeGb: 100 }];
    const { nodes: initial, edges: initialEdges } = reconcileClusterVms(cluster, [node]);

    const cp = initial.find((n) => n.data.clusterRole === "control-plane")!;
    const disksBefore = initial.filter(
      (n) =>
        n.type === "storageNode" &&
        initialEdges.some((e) => e.source === n.id && e.target === cp.id),
    );
    expect(disksBefore).toHaveLength(2);

    // Remove the second disk
    const updated = { ...cluster, controlPlaneDisks: [{ sizeGb: 120, bootable: true }] };
    const { nodes: result, edges: resultEdges } = applyClusterDisks(updated, initial, initialEdges);

    const cpAfter = result.find((n) => n.id === cp.id)!;
    const disksAfter = result.filter(
      (n) =>
        n.type === "storageNode" &&
        resultEdges.some((e) => e.source === n.id && e.target === cpAfter.id),
    );
    expect(disksAfter).toHaveLength(1);
  });

  it("is idempotent: re-applying same disk specs yields identical nodes/edges", () => {
    const { node, cluster } = makeCluster("ocp", { x: 0, y: 0 });
    cluster.controlPlaneDisks = [{ sizeGb: 120, bootable: true }, { sizeGb: 100 }];
    const { nodes: initial, edges: initialEdges } = reconcileClusterVms(cluster, [node]);

    const { nodes: n1, edges: e1 } = applyClusterDisks(cluster, initial, initialEdges);
    const { nodes: n2, edges: e2 } = applyClusterDisks(cluster, n1, e1);

    // Node count and structure should be identical
    expect(n2.length).toBe(n1.length);
    expect(e2.length).toBe(e1.length);

    // Storage node IDs should be identical
    const storageIds1 = n1.filter((n) => n.type === "storageNode").map((n) => n.id).sort();
    const storageIds2 = n2.filter((n) => n.type === "storageNode").map((n) => n.id).sort();
    expect(storageIds2).toEqual(storageIds1);
  });

  it("preserves disk node IDs when disk index still exists", () => {
    const { node, cluster } = makeCluster("ocp", { x: 0, y: 0 });
    cluster.workers = 1; // Ensure we have a worker
    cluster.workerDisks = [{ sizeGb: 100, bootable: true }, { sizeGb: 50 }];
    const { nodes: initial, edges: initialEdges } = reconcileClusterVms(cluster, [node]);

    const worker = initial.find((n) => n.data.clusterRole === "worker")!;
    const disk0Id = initial.find(
      (n) => n.type === "storageNode" && n.id.startsWith(`${worker.id}-disk-0`),
    )?.id;
    expect(disk0Id).toBeTruthy();

    // Now add a third disk
    const updated = { ...cluster, workerDisks: [{ sizeGb: 100, bootable: true }, { sizeGb: 50 }, { sizeGb: 30 }] };
    const { nodes: result } = applyClusterDisks(updated, initial, initialEdges);

    // The first disk should still have the same ID
    const disk0After = result.find((n) => n.type === "storageNode" && n.id === disk0Id);
    expect(disk0After).toBeTruthy();
  });
});

describe("suggestClusterVips", () => {
  it("returns high unused IPs for multi-node cluster with CIDR", () => {
    const { node, cluster } = makeCluster("ocp", { x: 0, y: 0 });
    const net = { id: "net1", type: "networkNode", data: { subtype: "network", cidr: "10.0.0.0/24" } } as any;
    cluster.networkIds = ["net1"];

    // Materialize the cluster
    const { nodes: materialized } = reconcileClusterVms(cluster, [node, net]);

    const suggestion = suggestClusterVips(cluster, materialized);

    // Should suggest two high unused IPs in the range 10.0.0.2-254
    expect(suggestion.apiVip).toBeTruthy();
    expect(suggestion.ingressVip).toBeTruthy();
    expect(suggestion.apiVip).not.toBe(suggestion.ingressVip);

    // Should be in the CIDR
    const apiNum = suggestion.apiVip ? parseInt(suggestion.apiVip.split(".")[3], 10) : 0;
    const ingressNum = suggestion.ingressVip ? parseInt(suggestion.ingressVip.split(".")[3], 10) : 0;
    expect(apiNum).toBeGreaterThan(1);
    expect(apiNum).toBeLessThan(255);
    expect(ingressNum).toBeGreaterThan(1);
    expect(ingressNum).toBeLessThan(255);
  });

  it("returns null for SNO (single-node cluster)", () => {
    const { node, cluster } = makeCluster("ocp", { x: 0, y: 0 });
    cluster.type = "sno";
    cluster.controlPlane = 1;
    cluster.workers = 0;
    const net = { id: "net1", type: "networkNode", data: { subtype: "network", cidr: "10.0.0.0/24" } } as any;
    cluster.networkIds = ["net1"];

    const suggestion = suggestClusterVips(cluster, [node, net]);

    expect(suggestion.apiVip).toBeNull();
    expect(suggestion.ingressVip).toBeNull();
  });

  it("returns null when no machine network defined", () => {
    const { node, cluster } = makeCluster("ocp", { x: 0, y: 0 });
    cluster.networkIds = [];

    const suggestion = suggestClusterVips(cluster, [node]);

    expect(suggestion.apiVip).toBeNull();
    expect(suggestion.ingressVip).toBeNull();
  });

  it("avoids IPs used by cluster members", () => {
    const { node, cluster } = makeCluster("ocp", { x: 0, y: 0 });
    const net = { id: "net1", type: "networkNode", data: { subtype: "network", cidr: "10.0.0.0/28" } } as any;
    cluster.networkIds = ["net1"];

    const { nodes: materialized, edges } = reconcileClusterVms(cluster, [node, net]);

    // Assign IPs to member NICs
    const updated = materialized.map((n) => {
      if (n.type === "vmNode") {
        const nics = ((n.data as Record<string, unknown>).nics || []) as any[];
        return {
          ...n,
          data: {
            ...n.data,
            nics: nics.map((nic, i) => ({ ...nic, ip: `10.0.0.${i + 2}` })),
          },
        };
      }
      return n;
    });

    const suggestion = suggestClusterVips(cluster, updated);

    // Should not suggest IPs that are already used
    expect(suggestion.apiVip).not.toBe("10.0.0.2");
    expect(suggestion.apiVip).not.toBe("10.0.0.3");
    expect(suggestion.ingressVip).not.toBe("10.0.0.2");
    expect(suggestion.ingressVip).not.toBe("10.0.0.3");
  });
});

describe("vipCollision", () => {
  it("detects collision with member IP", () => {
    const { node, cluster } = makeCluster("ocp", { x: 0, y: 0 });
    const net = { id: "net1", type: "networkNode", data: { subtype: "network", cidr: "10.0.0.0/24" } } as any;
    cluster.networkIds = ["net1"];

    const { nodes: materialized } = reconcileClusterVms(cluster, [node, net]);

    // Assign an IP to a member NIC
    const updated = materialized.map((n) => {
      if (n.type === "vmNode") {
        const nics = ((n.data as Record<string, unknown>).nics || []) as any[];
        return {
          ...n,
          data: {
            ...n.data,
            nics: nics.map((nic) => ({ ...nic, ip: "10.0.0.5" })),
          },
        };
      }
      return n;
    });

    expect(vipCollision("10.0.0.5", cluster, updated)).toBe(true);
  });

  it("detects collision with gateway IP", () => {
    const { node, cluster } = makeCluster("ocp", { x: 0, y: 0 });
    const net = { id: "net1", type: "networkNode", data: { subtype: "network", cidr: "10.0.0.0/24" } } as any;
    cluster.networkIds = ["net1"];

    const { nodes: materialized } = reconcileClusterVms(cluster, [node, net]);

    // Gateway is first host (10.0.0.1)
    expect(vipCollision("10.0.0.1", cluster, materialized)).toBe(true);
  });

  it("detects collision with another cluster's VIP", () => {
    const { node: node1, cluster: cluster1 } = makeCluster("ocp", { x: 0, y: 0 });
    const { node: node2, cluster: cluster2 } = makeCluster("ocp", { x: 200, y: 0 });
    const net = { id: "net1", type: "networkNode", data: { subtype: "network", cidr: "10.0.0.0/24" } } as any;

    cluster1.networkIds = ["net1"];
    cluster2.networkIds = ["net1"];
    cluster2.apiVip = "10.0.0.250";

    // Update node2's data to reflect the cluster2 VIP
    const node2Updated = {
      ...node2,
      data: { ...node2.data, clusterId: cluster2.id, apiVip: "10.0.0.250" },
    };

    const nodes = [node1, node2Updated, net];

    expect(vipCollision("10.0.0.250", cluster1, nodes)).toBe(true);
  });

  it("returns false for available IP", () => {
    const { node, cluster } = makeCluster("ocp", { x: 0, y: 0 });
    const net = { id: "net1", type: "networkNode", data: { subtype: "network", cidr: "10.0.0.0/24" } } as any;
    cluster.networkIds = ["net1"];

    const { nodes: materialized } = reconcileClusterVms(cluster, [node, net]);

    expect(vipCollision("10.0.0.250", cluster, materialized)).toBe(false);
  });
});

describe("clusterBoxSize", () => {
  it("returns dimensions that grow with member count", () => {
    const size1 = clusterBoxSize(1);
    const size6 = clusterBoxSize(6);
    expect(size6.width).toBeGreaterThan(size1.width);
    expect(size6.height).toBeGreaterThan(size1.height);
  });

  it("returns height > 320 (old fixed height) for 6 members", () => {
    const size = clusterBoxSize(6);
    expect(size.height).toBeGreaterThan(320);
  });

  it("caps columns at 4 per row", () => {
    const size4 = clusterBoxSize(4);
    const size5 = clusterBoxSize(5);
    const size8 = clusterBoxSize(8);

    // 4 members: 1 row of 4
    // 5 members: 2 rows (4 + 1)
    // 8 members: 2 rows of 4
    expect(size4.height).toBeLessThan(size5.height); // 5 adds a row
    expect(size8.height).toBe(size5.height); // 8 is still 2 rows
  });

  it("calculates width based on column count", () => {
    const size1 = clusterBoxSize(1);
    const size4 = clusterBoxSize(4);
    const size5 = clusterBoxSize(5);

    // 1 col: 60 + 130 = 190
    // 4 cols: 60 + 130*4 = 580
    // 5 cols capped at 4: 60 + 130*4 = 580
    expect(size1.width).toBeLessThan(size4.width);
    expect(size4.width).toBe(size5.width); // Both cap at 4 cols
  });
});

describe("cluster boundary auto-sizing + member grid reflow", () => {
  it("sets cluster boundary node style.width/height based on content bbox after reconcileClusterVms", () => {
    const { node, cluster } = makeCluster("ocp", { x: 0, y: 0 });
    cluster.controlPlane = 2;
    cluster.workers = 4;
    const { nodes: materialized } = reconcileClusterVms(cluster, [node]);

    const boundaryNode = materialized.find((n) => n.id === cluster.nodeId)!;
    const members = materialized.filter((n) => n.type === "vmNode");

    // Boundary size is content-based, not count-based
    expect(boundaryNode.style?.width).toBeGreaterThan(0);
    expect(boundaryNode.style?.height).toBeGreaterThan(0);

    // All members must fit within the boundary
    for (const member of members) {
      expect(member.position.x + 180).toBeLessThanOrEqual(boundaryNode.style?.width as number);
      expect(member.position.y + 260).toBeLessThanOrEqual(boundaryNode.style?.height as number);
    }
  });

  it("member VM cards positioned on grid within boundary (no overflow)", () => {
    const { node, cluster } = makeCluster("ocp", { x: 0, y: 0 });
    cluster.controlPlane = 6; // Will wrap to 2 rows
    cluster.workers = 0;
    const { nodes: materialized } = reconcileClusterVms(cluster, [node]);

    const boundaryNode = materialized.find((n) => n.id === cluster.nodeId)!;
    const boundaryHeight = (boundaryNode.style?.height as number) || 0;
    const members = materialized.filter((n) => n.type === "vmNode");

    for (const member of members) {
      const memberMaxY = member.position.y + 130; // CELL_H = 130
      expect(memberMaxY).toBeLessThanOrEqual(boundaryHeight);
    }
  });

  it("member vmNodes have extent: 'parent' to constrain drag within cluster boundary", () => {
    const { node, cluster } = makeCluster("ocp", { x: 0, y: 0 });
    cluster.controlPlane = 3;
    cluster.workers = 0;
    const { nodes: materialized } = reconcileClusterVms(cluster, [node]);

    const members = materialized.filter((n) => n.type === "vmNode");
    expect(members.length).toBeGreaterThan(0);
    for (const member of members) {
      expect((member as any).extent).toBe("parent");
    }
  });

  it("auto-encompasses content bbox: boundary width ≥ max(member.x + CARD_W) + PAD", () => {
    const { node, cluster } = makeCluster("ocp", { x: 0, y: 0 });
    cluster.controlPlane = 3;
    cluster.workers = 2;
    const { nodes: materialized } = reconcileClusterVms(cluster, [node]);

    const boundaryNode = materialized.find((n) => n.id === cluster.nodeId)!;
    const boundaryWidth = (boundaryNode.style?.width as number) || 0;
    const boundaryHeight = (boundaryNode.style?.height as number) || 0;

    const members = materialized.filter((n) => n.type === "vmNode");
    for (const member of members) {
      const maxX = member.position.x + 180; // CARD_W = 180
      const maxY = member.position.y + 260; // CARD_H = 260
      expect(maxX).toBeLessThanOrEqual(boundaryWidth);
      expect(maxY).toBeLessThanOrEqual(boundaryHeight);
    }
  });

  it("auto-fits to content both ways: re-reconcile shrinks an over-large box back to the content bbox", () => {
    const { node, cluster } = makeCluster("ocp", { x: 0, y: 0 });
    cluster.controlPlane = 1;
    cluster.workers = 0;

    // First reconcile establishes the content-fitted baseline size.
    const { nodes: initial } = reconcileClusterVms(cluster, [node]);
    const initialBoundary = initial.find((n) => n.id === cluster.nodeId)!;
    const fitW = (initialBoundary.style?.width as number) || 0;
    const fitH = (initialBoundary.style?.height as number) || 0;

    // Force an over-large size, then re-reconcile: the box auto-fits back to content.
    const enlarged = initial.map((n) =>
      n.id === cluster.nodeId
        ? { ...n, style: { ...n.style, width: fitW + 400, height: fitH + 400 } }
        : n,
    );
    const { nodes: result } = reconcileClusterVms(cluster, enlarged);
    const resultBoundary = result.find((n) => n.id === cluster.nodeId)!;
    expect(resultBoundary.style?.width).toBe(fitW);
    expect(resultBoundary.style?.height).toBe(fitH);
  });

  it("shrinks the box when members are removed (workers 3 -> 0)", () => {
    const { node, cluster } = makeCluster("ocp", { x: 0, y: 0 });
    cluster.controlPlane = 3;
    cluster.workers = 3;
    const { nodes: withWorkers } = reconcileClusterVms(cluster, [node]);
    const tallH = ((withWorkers.find((n) => n.id === cluster.nodeId)!).style
      ?.height as number) || 0;

    cluster.workers = 0;
    const { nodes: noWorkers } = reconcileClusterVms(cluster, withWorkers);
    const shortH = ((noWorkers.find((n) => n.id === cluster.nodeId)!).style
      ?.height as number) || 0;

    expect(shortH).toBeLessThan(tallH); // removing the worker row shrinks the box
  });

  it("sets data.minWidth/minHeight to the content bbox for NodeResizer constraints", () => {
    const { node, cluster } = makeCluster("ocp", { x: 0, y: 0 });
    cluster.controlPlane = 2;
    cluster.workers = 1;
    const { nodes: materialized } = reconcileClusterVms(cluster, [node]);

    const boundaryNode = materialized.find((n) => n.id === cluster.nodeId)!;
    const minWidth = (boundaryNode.data as Record<string, unknown>).minWidth as number;
    const minHeight = (boundaryNode.data as Record<string, unknown>).minHeight as number;

    expect(minWidth).toBeGreaterThan(0);
    expect(minHeight).toBeGreaterThan(0);

    // Min should be ≤ actual size (can't set min larger than current)
    const actualWidth = (boundaryNode.style?.width as number) || 0;
    const actualHeight = (boundaryNode.style?.height as number) || 0;
    expect(minWidth).toBeLessThanOrEqual(actualWidth);
    expect(minHeight).toBeLessThanOrEqual(actualHeight);
  });

  it("members do not overlap on grid layout (180×260 card rects)", () => {
    const { node, cluster } = makeCluster("ocp", { x: 0, y: 0 });
    cluster.controlPlane = 4;
    cluster.workers = 4;
    const { nodes: materialized } = reconcileClusterVms(cluster, [node]);

    const members = materialized.filter((n) => n.type === "vmNode");
    expect(members.length).toBe(8);

    // Check pairwise: no two 180×260 rects overlap
    for (let i = 0; i < members.length; i++) {
      for (let j = i + 1; j < members.length; j++) {
        const m1 = members[i];
        const m2 = members[j];

        const m1Right = m1.position.x + 180;
        const m1Bottom = m1.position.y + 260;
        const m2Right = m2.position.x + 180;
        const m2Bottom = m2.position.y + 260;

        // Rects don't overlap if one is completely to the right, left, above, or below the other
        const noOverlap =
          m1Right <= m2.position.x ||
          m2Right <= m1.position.x ||
          m1Bottom <= m2.position.y ||
          m2Bottom <= m1.position.y;

        expect(noOverlap).toBe(true);
      }
    }
  });
});

describe("member disk storageNodes hidden flag", () => {
  it("disk storageNodes have hidden: true but remain in nodes array", () => {
    const { node, cluster } = makeCluster("ocp", { x: 0, y: 0 });
    cluster.controlPlane = 1;
    cluster.workers = 0;
    const { nodes: materialized } = reconcileClusterVms(cluster, [node]);

    const diskNodes = materialized.filter((n) => n.type === "storageNode");
    expect(diskNodes.length).toBeGreaterThan(0);
    expect(diskNodes.every((d) => (d as any).hidden === true)).toBe(true);
  });

  it("disk node count unchanged after applyClusterDisks (hidden flag only)", () => {
    const { node, cluster } = makeCluster("ocp", { x: 0, y: 0 });
    cluster.controlPlaneDisks = [{ sizeGb: 120, bootable: true }];
    const { nodes: initial, edges: initialEdges } = reconcileClusterVms(cluster, [node]);

    const diskCountBefore = initial.filter((n) => n.type === "storageNode").length;

    // Apply again (idempotent)
    const { nodes: result } = applyClusterDisks(cluster, initial, initialEdges);
    const diskCountAfter = result.filter((n) => n.type === "storageNode").length;

    expect(diskCountAfter).toBe(diskCountBefore);
    // All disk nodes should still be hidden
    const diskNodesAfter = result.filter((n) => n.type === "storageNode");
    expect(diskNodesAfter.every((d) => (d as any).hidden === true)).toBe(true);
  });

  it("VM card disk summary still renders with hidden disk nodes", () => {
    const { node, cluster } = makeCluster("ocp", { x: 0, y: 0 });
    cluster.controlPlane = 1;
    cluster.workers = 0;
    const { nodes: materialized, edges } = reconcileClusterVms(cluster, [node]);

    // Simulate the VM card's lookup logic from VMNode.tsx lines 140-143
    const cp = materialized.find((n) => n.data.clusterRole === "control-plane")!;
    const connectedStorageIds = edges
      .filter((e) => e.source === cp.id || e.target === cp.id)
      .map((e) => e.source === cp.id ? e.target : e.source)
      .filter((nid) => materialized.some((n) => n.id === nid && n.type === "storageNode"));

    // Should find disks even though they are hidden
    expect(connectedStorageIds.length).toBeGreaterThan(0);
  });

  it("deploy-facing topology still contains disk nodes + edges", () => {
    const { node, cluster } = makeCluster("ocp", { x: 0, y: 0 });
    cluster.controlPlane = 2;
    cluster.workers = 0;
    const { nodes: materialized, edges } = reconcileClusterVms(cluster, [node]);

    const diskNodes = materialized.filter((n) => n.type === "storageNode");
    const diskEdges = edges.filter((e) => e.sourceHandle === "right" && e.targetHandle?.startsWith("dp-"));

    expect(diskNodes.length).toBeGreaterThan(0);
    expect(diskEdges.length).toBeGreaterThan(0);

    // All members should have disk edges
    const memberIds = materialized.filter((n) => n.type === "vmNode").map((n) => n.id);
    for (const memberId of memberIds) {
      const memberDiskEdges = diskEdges.filter((e) => e.target === memberId);
      expect(memberDiskEdges.length).toBeGreaterThan(0);
    }
  });
});

describe("Cluster Network Anchors", () => {
  it("applyClusterNetworks produces hidden per-member NIC edges", () => {
    const { nodes: members, edges: allEdges } = reconcileClusterVms(cluster, []);
    const clusterWithNets = { ...cluster, networkIds: ["net-1", "net-2"] } as any;
    const { nodes, edges } = applyClusterNetworks(clusterWithNets, members, []);

    // Find per-member NIC edges (not anchor edges)
    const memberNicEdges = edges.filter(
      (e) => e.id.includes("-nic") && !e.id.includes("-to-cluster")
    );
    // All per-member NIC edges should be hidden
    expect(memberNicEdges.every((e) => e.hidden === true)).toBe(true);
    // Confirm there are some NIC edges
    expect(memberNicEdges.length).toBeGreaterThan(0);
  });

  it("applyClusterNetworks preserves member nics in data even when edges are hidden", () => {
    const { nodes: members } = reconcileClusterVms(cluster, []);
    const clusterWithNets = { ...cluster, networkIds: ["net-1", "net-2"] } as any;
    const { nodes } = applyClusterNetworks(clusterWithNets, members, []);

    // All members should have nics array with 2 NICs (one per network)
    const memberNodes = nodes.filter((n) => n.type === "vmNode" && (n.data as any).clusterId === "prod");
    for (const member of memberNodes) {
      const nics = (member.data as any).nics || [];
      expect(nics.length).toBe(2);
      expect(nics[0].name).toBe("eth0");
      expect(nics[1].name).toBe("eth1");
    }
  });

  it("applyClusterNetworks with different network counts updates member NICs", () => {
    const { nodes: members } = reconcileClusterVms(cluster, []);
    const clusterWith3Nets = { ...cluster, networkIds: ["net-1", "net-2", "net-3"] } as any;
    const { nodes } = applyClusterNetworks(clusterWith3Nets, members, []);

    const memberNodes = nodes.filter((n) => n.type === "vmNode" && (n.data as any).clusterId === "prod");
    for (const member of memberNodes) {
      const nics = (member.data as any).nics || [];
      expect(nics.length).toBe(3);
    }
  });

  it("applyClusterNetworks preserves existing NIC MACs when networks remain", () => {
    const { nodes: members } = reconcileClusterVms(cluster, []);
    const clusterWith2Nets = { ...cluster, networkIds: ["net-1", "net-2"] } as any;
    const { nodes: nodes1, edges: edges1 } = applyClusterNetworks(clusterWith2Nets, members, []);

    // Extract the first member's NIC MACs
    const member1 = nodes1.find((n) => n.type === "vmNode" && n.id.startsWith("prod-cp-"));
    const originalMacs = ((member1?.data as any).nics || []).map((nic: any) => nic.mac);

    // Apply same networks again (should be idempotent)
    const { nodes: nodes2 } = applyClusterNetworks(clusterWith2Nets, nodes1, edges1);
    const member2 = nodes2.find((n) => n.id === member1?.id);
    const newMacs = ((member2?.data as any).nics || []).map((nic: any) => nic.mac);

    // MACs should be identical (idempotent)
    expect(newMacs).toEqual(originalMacs);
  });

  it("clusterNetworkIdsFromEdges extracts distinct network ids from NIC edges", () => {
    const { nodes: members, edges: memberEdges } = reconcileClusterVms(cluster, []);
    const clusterWithNets = { ...cluster, networkIds: ["net-1", "net-2"] } as any;
    const { edges } = applyClusterNetworks(clusterWithNets, members, memberEdges);

    // Get member IDs
    const memberIds = members
      .filter((n) => n.type === "vmNode" && (n.data as any).clusterId === "prod")
      .map((n) => n.id);

    // Extract network IDs from edges
    const extractedIds = clusterNetworkIdsFromEdges("cluster-prod", memberIds, edges);

    // Should return the networks in order (distinct)
    expect(extractedIds).toEqual(["net-1", "net-2"]);
  });
});

describe("buildClusterDnsRecords", () => {
  it("returns api/api-int/apps records tagged with the cluster id", () => {
    const c = {
      id: "ocp",
      name: "ocp",
      baseDomain: "ocp.local",
      apiVip: "10.0.0.5",
      ingressVip: "10.0.0.6",
    } as any;
    const recs = buildClusterDnsRecords(c);
    expect(recs).toEqual([
      { name: "api.ocp.ocp.local", ip: "10.0.0.5", type: "A", clusterId: "ocp", managed: true },
      { name: "api-int.ocp.ocp.local", ip: "10.0.0.5", type: "A", clusterId: "ocp", managed: true },
      { name: ".apps.ocp.ocp.local", ip: "10.0.0.6", type: "A", clusterId: "ocp", managed: true },
    ]);
  });

  it("returns [] until name and base domain are set", () => {
    expect(buildClusterDnsRecords({ id: "x", name: "", baseDomain: "ocp.local" } as any)).toEqual([]);
    expect(buildClusterDnsRecords({ id: "x", name: "ocp", baseDomain: "" } as any)).toEqual([]);
  });

  it("skips records whose VIP is blank", () => {
    const recs = buildClusterDnsRecords({ id: "x", name: "ocp", baseDomain: "ocp.local", apiVip: "10.0.0.5" } as any);
    expect(recs.map((r) => r.name)).toEqual(["api.ocp.ocp.local", "api-int.ocp.ocp.local"]);
  });
});

describe("applyClusterDns", () => {
  const baseCluster = {
    id: "ocp",
    name: "ocp",
    baseDomain: "ocp.local",
    apiVip: "10.0.0.5",
    ingressVip: "10.0.0.6",
    networkIds: ["net1"],
  } as any;

  it("writes records + enables DNS on the target network, leaving others untouched", () => {
    const net1 = { id: "net1", type: "networkNode", data: { subtype: "network" } } as any;
    const net2 = { id: "net2", type: "networkNode", data: { subtype: "network" } } as any;
    const out = applyClusterDns(baseCluster, [net1, net2]);
    const d1 = out[0].data as any;
    const d2 = out[1].data as any;
    expect(d1.dns).toBe(true);
    expect(d1.dnsDomain).toBe("ocp.local");
    expect((d1.dnsRecords as any[]).map((r) => r.name)).toEqual([
      "api.ocp.ocp.local",
      "api-int.ocp.ocp.local",
      ".apps.ocp.ocp.local",
    ]);
    expect(d2.dnsRecords).toBeUndefined();
  });

  it("preserves user-authored records and dedups by name on the target", () => {
    const net1 = {
      id: "net1",
      type: "networkNode",
      data: {
        subtype: "network",
        dnsRecords: [
          { name: "myapp.lab.local", ip: "10.0.0.9" },
          { name: "api.ocp.ocp.local", ip: "1.1.1.1" }, // stale/backend-written, no clusterId
        ],
      },
    } as any;
    const out = applyClusterDns(baseCluster, [net1]);
    const recs = (out[0].data as any).dnsRecords as any[];
    expect(recs.find((r) => r.name === "myapp.lab.local")).toBeTruthy();
    // the un-tagged api record is replaced by the managed one
    const api = recs.filter((r) => r.name === "api.ocp.ocp.local");
    expect(api).toHaveLength(1);
    expect(api[0]).toMatchObject({ ip: "10.0.0.5", managed: true });
  });

  it("removes this cluster's records from a detached network", () => {
    const net1 = {
      id: "net1",
      type: "networkNode",
      data: {
        subtype: "network",
        dnsRecords: [
          { name: "api.ocp.ocp.local", ip: "10.0.0.5", clusterId: "ocp", managed: true },
          { name: "keep.lab.local", ip: "10.0.0.9" },
        ],
      },
    } as any;
    // cluster no longer targets net1
    const out = applyClusterDns({ ...baseCluster, networkIds: [] }, [net1]);
    const recs = (out[0].data as any).dnsRecords as any[];
    expect(recs.map((r) => r.name)).toEqual(["keep.lab.local"]);
  });

  it("is idempotent — returns the same nodes ref when nothing changes", () => {
    const net1 = { id: "net1", type: "networkNode", data: { subtype: "network" } } as any;
    const first = applyClusterDns(baseCluster, [net1]);
    const second = applyClusterDns(baseCluster, first);
    expect(second).toBe(first);
  });

  it("updates records in place when a VIP changes", () => {
    const net1 = { id: "net1", type: "networkNode", data: { subtype: "network" } } as any;
    const first = applyClusterDns(baseCluster, [net1]);
    const second = applyClusterDns({ ...baseCluster, ingressVip: "10.0.0.99" }, first);
    const apps = ((second[0].data as any).dnsRecords as any[]).find((r) => r.name === ".apps.ocp.ocp.local");
    expect(apps.ip).toBe("10.0.0.99");
    expect(((second[0].data as any).dnsRecords as any[]).filter((r) => r.name === ".apps.ocp.ocp.local")).toHaveLength(1);
  });
});

describe("clusterPrereqIssues", () => {
  const dnsNet = { id: "net1", type: "networkNode", data: { subtype: "network", dns: true } } as any;
  const plainNet = { id: "net2", type: "networkNode", data: { subtype: "network" } } as any;
  const openGw = { id: "gw", type: "networkNode", data: { subtype: "gateway", outboundPolicy: "allow-all" } } as any;
  const restrictedGwOk = {
    id: "gw",
    type: "networkNode",
    data: { subtype: "gateway", outboundPolicy: "restrict", outboundPorts: "80/tcp,443/tcp,123/udp" },
  } as any;
  const restrictedGwBad = {
    id: "gw",
    type: "networkNode",
    data: { subtype: "gateway", outboundPolicy: "restrict", outboundPorts: "80/tcp,443/tcp" },
  } as any;

  it("errors when no member network has DNS enabled", () => {
    const issues = clusterPrereqIssues({ id: "c", networkIds: ["net2"] } as any, [plainNet, openGw]);
    const err = issues.find((i) => i.level === "error");
    expect(err).toBeTruthy();
    expect(err!.message).toMatch(/DNS/);
  });

  it("no error when a member network has DNS", () => {
    const issues = clusterPrereqIssues({ id: "c", networkIds: ["net1"] } as any, [dnsNet, openGw]);
    expect(issues.some((i) => i.level === "error")).toBe(false);
  });

  it("warns (not errors) when the gateway blocks outbound http/https/ntp", () => {
    const issues = clusterPrereqIssues({ id: "c", networkIds: ["net1"] } as any, [dnsNet, restrictedGwBad]);
    expect(issues.some((i) => i.level === "error")).toBe(false);
    const warn = issues.find((i) => i.level === "warning");
    expect(warn).toBeTruthy();
    expect(warn!.message).toMatch(/outbound/i);
  });

  it("no outbound warning when gateway is allow-all or restrict-lists all three", () => {
    expect(clusterPrereqIssues({ id: "c", networkIds: ["net1"] } as any, [dnsNet, openGw]).some((i) => i.level === "warning")).toBe(false);
    expect(clusterPrereqIssues({ id: "c", networkIds: ["net1"] } as any, [dnsNet, restrictedGwOk]).some((i) => i.level === "warning")).toBe(false);
  });

  it("warns when there is no gateway at all", () => {
    const issues = clusterPrereqIssues({ id: "c", networkIds: ["net1"] } as any, [dnsNet]);
    expect(issues.some((i) => i.level === "warning")).toBe(true);
  });
});

describe("effectiveDnsNetworkId", () => {
  it("prefers an explicit dnsNetworkId when it is still a member", () => {
    expect(effectiveDnsNetworkId({ networkIds: ["a", "b"], dnsNetworkId: "b" } as any)).toBe("b");
  });
  it("falls back to the first member network when dnsNetworkId is unset/stale", () => {
    expect(effectiveDnsNetworkId({ networkIds: ["a", "b"] } as any)).toBe("a");
    expect(effectiveDnsNetworkId({ networkIds: ["a", "b"], dnsNetworkId: "z" } as any)).toBe("a");
  });
  it("returns undefined with no member networks", () => {
    expect(effectiveDnsNetworkId({ networkIds: [] } as any)).toBeUndefined();
  });
});

describe("applyClusterDns single DNS network", () => {
  it("writes records to only the chosen DNS network, not every member", () => {
    const net1 = { id: "net1", type: "networkNode", data: { subtype: "network" } } as any;
    const net2 = { id: "net2", type: "networkNode", data: { subtype: "network" } } as any;
    const cluster = {
      id: "ocp",
      name: "ocp",
      baseDomain: "local",
      apiVip: "10.0.0.5",
      ingressVip: "10.0.0.6",
      networkIds: ["net1", "net2"],
      dnsNetworkId: "net2",
    } as any;
    const out = applyClusterDns(cluster, [net1, net2]);
    expect((out[0].data as any).dnsRecords).toBeUndefined(); // net1 untouched
    expect(((out[1].data as any).dnsRecords as any[]).map((r) => r.name)).toEqual([
      "api.ocp.local",
      "api-int.ocp.local",
      ".apps.ocp.local",
    ]);
    expect((out[1].data as any).dns).toBe(true);
  });

  it("moves records when the DNS network changes (strips the old one)", () => {
    const net1 = { id: "net1", type: "networkNode", data: { subtype: "network" } } as any;
    const net2 = { id: "net2", type: "networkNode", data: { subtype: "network" } } as any;
    const base = { id: "ocp", name: "ocp", baseDomain: "local", apiVip: "10.0.0.5", ingressVip: "10.0.0.6", networkIds: ["net1", "net2"] } as any;
    const first = applyClusterDns({ ...base, dnsNetworkId: "net1" }, [net1, net2]);
    expect(((first[0].data as any).dnsRecords as any[]).length).toBe(3);
    const second = applyClusterDns({ ...base, dnsNetworkId: "net2" }, first);
    expect((second[0].data as any).dnsRecords).toEqual([]); // net1 stripped
    expect(((second[1].data as any).dnsRecords as any[]).length).toBe(3); // net2 now hosts
  });
});
