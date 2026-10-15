import { beforeEach, describe, expect, it } from "vitest";
import { makeCluster } from "../clusterFactory";
import { applyClusterDisks, applyClusterNetworks, applyClusterSizing, reconcileClusterVms } from "../clusterMaterialize";
import { useCanvasStore, stableClusterKey } from "@/stores/canvasStore";
import { seedClustersFromDeployed } from "../clusterNetworkBackfill";
import { reconcileCanvasClusters } from "../clusterTopologyHeal";

describe("cluster container-storage disks", () => {
  beforeEach(() => useCanvasStore.setState({ deployedVmIds: new Set() }));

  it("does not share mutable default disk specs between clusters", () => {
    const first = makeCluster("first", { x: 0, y: 0 });
    first.cluster.controlPlaneDisks![0].sizeGb = 900;
    expect(makeCluster("second", { x: 0, y: 0 }).cluster.controlPlaneDisks![0].sizeGb).toBe(120);
    expect(first.node.data.controlPlaneDisks).not.toBe(first.cluster.controlPlaneDisks);
  });

  it("keeps the worker legacy disk size aligned with its default boot disk", () => {
    const { cluster } = makeCluster("test", { x: 0, y: 0 });
    expect(cluster.workerDisk).toBe(cluster.workerDisks![0].sizeGb);
  });

  it("preserves explicit template member disks", () => {
    const { cluster } = makeCluster("test", { x: 0, y: 0 });
    const first = reconcileClusterVms(cluster, []);
    const nodes = first.nodes.map(n => n.type === "vmNode" ? { ...n, data: { ...n.data, generated: false } } : n);
    const updated = { ...cluster, controlPlaneDisks: [{ sizeGb: 700 }] };
    expect(applyClusterDisks(updated, nodes, first.edges)).toEqual({ nodes, edges: first.edges });
  });

  it("uses role sizing, bus and boot choice for newly added members", () => {
    const { cluster } = makeCluster("test", { x: 0, y: 0 });
    const first = reconcileClusterVms(cluster, []);
    const updated = { ...cluster, workers: 1, workerCpu: 11, workerMemory: 24576, workerDisks: [
      { sizeGb: 55, bus: "sata" as const, bootable: false },
      { sizeGb: 250, bus: "scsi" as const, bootable: true, ocpMount: "/var/lib/containers" },
    ] };
    const next = reconcileClusterVms(updated, first.nodes);
    const worker = next.nodes.find(n => n.data.clusterRole === "worker")!;
    expect(worker.data).toMatchObject({ vcpus: 11, ram: 24, bootDevices: [`${worker.id}-disk-1`] });
    expect((worker.data.diskControllers as Array<{ bus: string }>).map(dc => dc.bus)).toEqual(["sata", "scsi", "sata"]);
    expect(next.nodes.find(n => n.id === `${worker.id}-disk-1`)?.data).toMatchObject({ size: 250, ocpMount: "/var/lib/containers" });
    expect(next.nodes.find(n => n.id === first.nodes.find(m => m.type === "vmNode")!.id)?.data).toEqual(first.nodes.find(m => m.type === "vmNode")?.data);
  });
  it("uses legacy role sizes for actual storage nodes", () => {
    const { cluster } = makeCluster("legacy", { x: 0, y: 0 });
    cluster.controlPlaneDisks = undefined;
    cluster.controlPlaneDisk = 275;
    const first = reconcileClusterVms(cluster, []);
    expect(first.nodes.find(n => n.type === "storageNode")?.data.size).toBe(275);
  });

  it("retains disk definitions through seed/heal and compares secondary disk edits", () => {
    const { cluster } = makeCluster("test", { x: 0, y: 0 });
    expect(seedClustersFromDeployed([], [cluster])[0].controlPlaneDisks).toEqual(cluster.controlPlaneDisks);
    expect(reconcileCanvasClusters([], [cluster], [])[0].workerDisks).toEqual(cluster.workerDisks);
    const updated = { ...cluster, workerDisks: [{ sizeGb: 120, bootable: true }, { sizeGb: 300 }] };
    expect(stableClusterKey([updated])).not.toBe(stableClusterKey([cluster]));
  });

  it("preserves VM defaults through heal/seed and tracks changes", () => {
    const { cluster } = makeCluster("test", { x: 0, y: 0 });
    const configured = { ...cluster, vmDefaults: { nested_virt: false, firmware: "bios", power_on: false } };
    expect(seedClustersFromDeployed([], [configured])[0]).toMatchObject({ vmDefaults: configured.vmDefaults });
    expect(reconcileCanvasClusters([], [configured], [])[0]).toMatchObject({ vmDefaults: configured.vmDefaults });
    expect(stableClusterKey([configured])).not.toBe(stableClusterKey([cluster]));
  });

  it("inherits VM behavior defaults including false flags on newly generated members", () => {
    const { cluster } = makeCluster("test", { x: 0, y: 0 });
    const configured = { ...cluster, workers: 1, vmDefaults: {
      bmc: false, nested_virt: false, firmware: "bios", secure_boot: false, power_on: false,
      machine_type: "pc", legacy_root_bus: false, serial_only: true, serial_exec: "SSH",
      cloud_init: true, login_user: "core", tags: { custom: "yes" },
    } };
    const first = reconcileClusterVms(configured, []);
    const members = first.nodes.filter(n => n.type === "vmNode");
    expect(members).toHaveLength(2);
    for (const member of members) {
      expect(member.data).toMatchObject({ bmcEnabled: false, nestedVirt: false, firmware: "bios", secureBoot: false,
        powerOnAtDeploy: false, machineType: "pc", legacyRootBus: false, headless: true, serialExecType: "ssh",
        cloudInit: true, ciLoginUser: "core", tags: { custom: "yes" },
      });
    }
    expect(first.nodes.some(n => n.data.networkType === "bmc")).toBe(false);
    expect(new Set(members.flatMap(n => (n.data.diskControllers as Array<{ id: string }>).map(dc => dc.id))).size).toBe(6);
  });

  it.each([[true, "linux"], [false, ""], ["EOS", "eos"]])(
    "normalizes serial_exec %s to profile %s for generated RHCOS members", (serialExec, profile) => {
      const { cluster } = makeCluster("test", { x: 0, y: 0 });
      const next = reconcileClusterVms({ ...cluster, vmDefaults: { serial_exec: serialExec } }, []);
      expect(next.nodes.find(n => n.type === "vmNode")?.data.serialExecType).toBe(profile);
    },
  );

  it("preserves fractional GiB for both initial members and role sizing updates", () => {
    const { cluster } = makeCluster("test", { x: 0, y: 0 });
    const configured = { ...cluster, workers: 1, controlPlaneMemory: 512, workerMemory: 12800 };
    const first = reconcileClusterVms(configured, []);
    expect(first.nodes.find(n => n.data.clusterRole === "control-plane")?.data.ram).toBe(0.5);
    expect(first.nodes.find(n => n.data.clusterRole === "worker")?.data.ram).toBe(12.5);
    const next = applyClusterSizing({ ...configured, controlPlaneMemory: 12800, workerMemory: 512 }, first.nodes);
    expect(next.find(n => n.data.clusterRole === "control-plane")?.data.ram).toBe(12.5);
    expect(next.find(n => n.data.clusterRole === "worker")?.data.ram).toBe(0.5);
  });

  it("leaves existing VM behavior intact when defaults change before a count increase", () => {
    const { cluster } = makeCluster("test", { x: 0, y: 0 });
    const first = reconcileClusterVms(cluster, []);
    const vm = first.nodes.find(n => n.type === "vmNode")!;
    useCanvasStore.setState({ deployedVmIds: new Set([vm.id]) });
    const next = reconcileClusterVms({ ...cluster, workers: 1, vmDefaults: { nestedVirt: false, machineType: "pc", legacyRootBus: false, headless: false, power_on: false } }, first.nodes);
    expect(next.nodes.find(n => n.id === vm.id)?.data).toEqual(vm.data);
    expect(next.nodes.find(n => n.data.clusterRole === "worker")?.data).toMatchObject({ nestedVirt: false, machineType: "pc", legacyRootBus: false, headless: false, powerOnAtDeploy: false });
  });

  it("preserves explicit CPU overrides while inheriting other role sizing", () => {
    const { cluster } = makeCluster("test", { x: 0, y: 0 });
    const first = reconcileClusterVms(cluster, []);
    const vm = first.nodes.find(n => n.type === "vmNode")!;
    useCanvasStore.setState({ nodes: first.nodes, edges: first.edges, deployedVmIds: new Set() });
    useCanvasStore.getState().updateNodeData(vm.id, { vcpus: 23 });
    const next = applyClusterSizing({ ...cluster, controlPlaneCpu: 12, controlPlaneMemory: 65536 }, useCanvasStore.getState().nodes);
    const member = next.find(n => n.id === vm.id)!;
    expect(member.data.vcpus).toBe(23);
    expect(member.data.ram).toBe(64);
  });

  it("keeps individually edited storage when role disk defaults change", () => {
    const { cluster } = makeCluster("test", { x: 0, y: 0 });
    const first = reconcileClusterVms(cluster, []);
    const disk = first.nodes.find(n => n.type === "storageNode")!;
    useCanvasStore.setState({ nodes: first.nodes, edges: first.edges });
    useCanvasStore.getState().updateNodeData(disk.id, { size: 333, ocpMount: "/custom" });
    const edited = useCanvasStore.getState();
    const next = applyClusterDisks({ ...cluster, controlPlaneDisks: [{ sizeGb: 700 }] }, edited.nodes, edited.edges);
    expect(next.nodes.find(n => n.id === disk.id)?.data).toMatchObject({ size: 333, ocpMount: "/custom" });
  });

  it("keeps individually customized and deployed members when a count shrinks", () => {
    const { cluster } = makeCluster("test", { x: 0, y: 0 });
    cluster.workers = 2;
    const first = reconcileClusterVms(cluster, []);
    const workers = first.nodes.filter(n => n.data.clusterRole === "worker");
    useCanvasStore.setState({ nodes: first.nodes, edges: first.edges, deployedVmIds: new Set([workers[0].id]) });
    useCanvasStore.getState().updateNodeData(workers[1].id, { ram: 128 });
    const next = reconcileClusterVms({ ...cluster, workers: 0 }, useCanvasStore.getState().nodes);
    expect(next.nodes.filter(n => n.data.clusterRole === "worker")).toHaveLength(2);
  });

  it("preserves deployed, explicit and individually customized NICs on network edits", () => {
    const { cluster } = makeCluster("test", { x: 0, y: 0 });
    cluster.workers = 2;
    cluster.networkIds = ["cluster-network"];
    const first = reconcileClusterVms(cluster, []);
    const members = first.nodes.filter(n => n.type === "vmNode");
    const nodes = first.nodes.map(n => n.id === members[0].id ? { ...n, data: { ...n.data, generated: false } } : n);
    useCanvasStore.setState({ nodes, edges: first.edges, deployedVmIds: new Set([members[1].id]) });
    const customNics = [{ id: "custom-nic", name: "eth0", mac: "52:54:00:aa:bb:cc", model: "e1000", ip: "10.0.0.55" }];
    useCanvasStore.getState().updateNodeData(members[2].id, { nics: customNics });
    const state = useCanvasStore.getState();
    const next = applyClusterNetworks({ ...cluster, networkIds: [] }, state.nodes, state.edges);
    for (const vm of state.nodes.filter(n => n.type === "vmNode")) {
      expect(next.nodes.find(n => n.id === vm.id)?.data.nics).toEqual(vm.data.nics);
    }
    expect(next.edges).toEqual(first.edges);
  });

  it("does not rewrite provisioned member resources or storage", () => {
    const { cluster } = makeCluster("test", { x: 0, y: 0 });
    const first = reconcileClusterVms(cluster, []);
    const vm = first.nodes.find(n => n.type === "vmNode")!;
    useCanvasStore.setState({ deployedVmIds: new Set([vm.id]) });
    const updated = { ...cluster, controlPlaneCpu: 23, controlPlaneDisks: [{ sizeGb: 700 }] };
    expect(applyClusterSizing(updated, first.nodes)).toEqual(first.nodes);
    expect(applyClusterDisks(updated, first.nodes, first.edges)).toEqual(first);
    useCanvasStore.setState({ deployedVmIds: new Set() });
  });

  it("reuses imported disk/controller IDs and preserves ISO/custom disk metadata", () => {
    const { cluster } = makeCluster("test", { x: 0, y: 0 });
    cluster.controlPlaneDisks = [{ sizeGb: 120, bootable: true, bus: "scsi" }];
    const first = reconcileClusterVms(cluster, []);
    const vm = first.nodes.find(n => n.type === "vmNode")!;
    const disk = first.nodes.find(n => n.type === "storageNode")!;
    const importedId = "imported-disk-uuid";
    const nodes = first.nodes.map(n => n.id === disk.id ? { ...n, id: importedId, data: { ...n.data, format: "raw", custom: true } } : n);
    nodes.push({ id: "installer-iso", type: "storageNode", position: { x: 0, y: 0 }, data: { format: "iso" } });
    const dcs = vm.data.diskControllers as Array<{ id: string; name: string; bus: string }>;
    dcs.push({ id: "extra-cdrom", name: "cdrom1", bus: "sata" });
    const edges = first.edges.map(e => e.source === disk.id ? { ...e, source: importedId } : e);
    const isoEdge = { id: "iso-attachment", source: "installer-iso", target: vm.id, targetHandle: `dp-${dcs.at(-1)!.id}-left` };
    edges.push(isoEdge);
    const next = applyClusterDisks(cluster, nodes, edges);
    expect(next.nodes.find(n => n.id === importedId)?.data).toMatchObject({ size: 120, format: "raw", custom: true });
    expect(next.nodes.find(n => n.id === vm.id)?.data.diskControllers).toEqual(dcs);
    expect(next.edges).toContainEqual(isoEdge);
    expect(applyClusterDisks(cluster, next.nodes, next.edges)).toEqual(next);
  });
  it("marks the second disk of new palette clusters as container storage", () => {
    const { cluster } = makeCluster("test", { x: 0, y: 0 });
    expect(cluster.controlPlaneDisks?.[1].ocpMount).toBe("/var/lib/containers");
    expect(cluster.workerDisks?.[1].ocpMount).toBe("/var/lib/containers");
  });

  it("preserves role mount metadata when materializing and resizing members", () => {
    const { cluster } = makeCluster("test", { x: 0, y: 0 });
    cluster.controlPlaneDisks = [
      { sizeGb: 120, bootable: true },
      { sizeGb: 250, ocpMount: "/var/lib/containers" },
    ];
    const first = reconcileClusterVms(cluster, []);
    expect(first.nodes.find(n => n.id.endsWith("-disk-1"))?.data.ocpMount).toBe("/var/lib/containers");
    cluster.controlPlaneDisks[1].sizeGb = 500;
    const resized = applyClusterDisks(cluster, first.nodes, first.edges);
    const disk = resized.nodes.find(n => n.id.endsWith("-disk-1"));
    expect(disk?.data.size).toBe(500);
    expect(disk?.data.ocpMount).toBe("/var/lib/containers");
  });
});
