import { describe, it, expect } from "vitest";
import type { Node, Edge } from "@xyflow/react";
import {
  computeTopologyDiff,
  computeTopologyDirty,
  stableStringify,
  stableNodeData,
  stableClusterKey,
  type ClusterConfig,
  type TopologyDiffState,
} from "@/stores/canvasStore";

// Build a deployed baseline string for a node's data (mirrors buildDeployedBaseline).
const baseline = (data: Record<string, unknown>) => stableStringify(stableNodeData(data));

const vmNode = (data: Record<string, unknown>): Node => ({
  id: "vm1",
  type: "vmNode",
  position: { x: 0, y: 0 },
  data,
});

const emptyBaseline: Partial<TopologyDiffState> = {
  edges: [],
  deployedEdgeKey: "",
  deployedNodeMeta: {},
  deployedEdges: [],
  deployedExternalIps: "[]",
  deployedClusters: "[]",
  externalIps: [],
  clusters: [],
};

describe("computeTopologyDiff", () => {
  it("returns [] and dirty=false when there is no deployed baseline (draft)", () => {
    const state: TopologyDiffState = {
      nodes: [vmNode({ name: "bastion" })],
      edges: [],
      deployedNodeData: {},
      deployedEdgeKey: "",
    };
    expect(computeTopologyDiff(state)).toEqual([]);
    expect(computeTopologyDirty(state)).toBe(false);
  });

  it("returns [] when the canvas matches the deployed baseline", () => {
    const data = { name: "bastion", memory: 8192, cpuCount: 4 };
    const state: TopologyDiffState = {
      ...emptyBaseline,
      nodes: [vmNode(data)],
      deployedNodeData: { vm1: baseline(data) },
    } as TopologyDiffState;
    expect(computeTopologyDiff(state)).toEqual([]);
    expect(computeTopologyDirty(state)).toBe(false);
  });

  it("reports a modified VM with human-readable field diffs (memory in MB)", () => {
    const state: TopologyDiffState = {
      ...emptyBaseline,
      nodes: [vmNode({ name: "bastion", memory: 16384, cpuCount: 8 })],
      deployedNodeData: { vm1: baseline({ name: "bastion", memory: 8192, cpuCount: 4 }) },
    } as TopologyDiffState;
    const diff = computeTopologyDiff(state);
    expect(diff).toHaveLength(1);
    expect(diff[0]).toMatchObject({ kind: "modified", resourceType: "VM", name: "bastion" });
    const byKey = Object.fromEntries(diff[0].fields.map((f) => [f.key, f]));
    expect(byKey.memory).toMatchObject({ label: "Memory", from: "8192 MB", to: "16384 MB" });
    expect(byKey.cpuCount).toMatchObject({ label: "Cpu Count", from: "4", to: "8" });
  });

  it("ignores runtime-only fields (status) — no false modified", () => {
    const state: TopologyDiffState = {
      ...emptyBaseline,
      nodes: [vmNode({ name: "bastion", memory: 8192, status: "running" })],
      deployedNodeData: { vm1: baseline({ name: "bastion", memory: 8192, status: "stopped" }) },
    } as TopologyDiffState;
    expect(computeTopologyDiff(state)).toEqual([]);
  });

  it("reports an added node", () => {
    const state: TopologyDiffState = {
      ...emptyBaseline,
      nodes: [vmNode({ name: "new-vm" })],
      deployedNodeData: { other: baseline({ name: "other" }) },
      deployedNodeMeta: { other: { type: "vmNode", name: "other" } },
    } as TopologyDiffState;
    const diff = computeTopologyDiff(state);
    // vm1 added, "other" removed
    expect(diff.find((e) => e.kind === "added")).toMatchObject({ resourceType: "VM", name: "new-vm" });
    expect(diff.find((e) => e.kind === "removed")).toMatchObject({ resourceType: "VM", name: "other" });
  });

  it("reports a modified cluster (sizing change) by id", () => {
    const canvas: ClusterConfig = {
      id: "prod", name: "prod", nodeId: "cluster-prod", type: "standard",
      controlPlane: 3, workers: 2, workerCpu: 16,
    };
    const deployed: ClusterConfig = { ...canvas, workerCpu: 4 };
    const state: TopologyDiffState = {
      ...emptyBaseline,
      nodes: [],
      deployedNodeData: { "cluster-prod": baseline({ name: "prod" }) },
      clusters: [canvas],
      deployedClusters: stableClusterKey([deployed]),
    } as TopologyDiffState;
    // add a matching node so the node-set doesn't itself read dirty
    state.nodes = [{ id: "cluster-prod", type: "clusterNode", position: { x: 0, y: 0 }, data: { name: "prod" } } as Node];
    const diff = computeTopologyDiff(state);
    const cluster = diff.find((e) => e.resourceType === "Cluster");
    expect(cluster).toMatchObject({ kind: "modified", name: "prod" });
    expect(cluster!.fields.map((f) => f.key)).toContain("workerCpu");
  });

  it("reports an added external IP", () => {
    const state: TopologyDiffState = {
      ...emptyBaseline,
      nodes: [vmNode({ name: "bastion" })],
      deployedNodeData: { vm1: baseline({ name: "bastion" }) },
      externalIps: [{ id: "e1", name: "web", ip: "1.2.3.4" }],
      deployedExternalIps: "[]",
    } as TopologyDiffState;
    const diff = computeTopologyDiff(state);
    expect(diff.find((e) => e.resourceType === "External IP")).toMatchObject({ kind: "added", name: "web" });
  });

  it("ignores deploy-regenerated showroom init containers (pattern deploy)", () => {
    const backendNginxCommand =
      "mkdir -p /showroom/nginx /showroom/repo /showroom/www && chown 1001 /showroom/www && " +
      'echo "$NGINX_B64" | base64 -d > /showroom/nginx/nginx.conf';
    const frontendNginxCommand =
      'mkdir -p /showroom/nginx /showroom/repo && echo "$NGINX_B64" | base64 -d > /showroom/nginx/nginx.conf';
    const deployedShowroom = {
      name: "showroom",
      isShowroom: true,
      isPod: true,
      buildContent: false,
      initContainers: [
        {
          name: "git-cloner",
          image: "quay.io/rhpds/git-cloner:v1.1.4",
          mounts: [{ diskNodeId: "disk-1", mountPath: "/showroom" }],
        },
        {
          name: "nginx-config",
          image: "docker.io/library/busybox:1.36",
          envVars: [{ key: "NGINX_B64", value: "abc" }],
          command: backendNginxCommand,
          mounts: [{ diskNodeId: "disk-1", mountPath: "/showroom" }],
        },
        {
          name: "antora-builder",
          image: "quay.io/rhpds/antora:v1.2.2",
          mounts: [{ diskNodeId: "disk-1", mountPath: "/showroom" }],
        },
      ],
    };
    const canvasShowroom = {
      ...deployedShowroom,
      initContainers: [
        { name: "nginx-config", image: "docker.io/library/busybox:1.36", command: frontendNginxCommand },
      ],
    };
    const showroom: Node = {
      id: "showroom-1",
      type: "containerNode",
      position: { x: 0, y: 0 },
      data: canvasShowroom,
    };
    const state: TopologyDiffState = {
      ...emptyBaseline,
      nodes: [showroom],
      deployedNodeData: { "showroom-1": baseline(deployedShowroom) },
    } as TopologyDiffState;
    expect(computeTopologyDiff(state)).toEqual([]);
    expect(computeTopologyDirty(state)).toBe(false);
  });

  it("ignores effectiveDnsIp display stamp (provider-derived)", () => {
    const net: Node = {
      id: "net1",
      type: "networkNode",
      position: { x: 0, y: 0 },
      data: { name: "cluster", subtype: "network", cidr: "10.0.0.0/24", effectiveDnsIp: "10.0.0.2" },
    };
    const state: TopologyDiffState = {
      ...emptyBaseline,
      nodes: [net],
      deployedNodeData: {
        net1: baseline({ name: "cluster", subtype: "network", cidr: "10.0.0.0/24", effectiveDnsIp: "10.0.0.1" }),
      },
    } as TopologyDiffState;
    expect(computeTopologyDiff(state)).toEqual([]);
  });

  it("reports an added connection with resolved endpoint names", () => {
    const net: Node = { id: "net1", type: "networkNode", position: { x: 0, y: 0 }, data: { name: "lab-net", subtype: "network" } };
    const vm = vmNode({ name: "bastion" });
    const edge: Edge = { id: "edge1", source: "net1", target: "vm1", targetHandle: "nic-0" };
    const state: TopologyDiffState = {
      ...emptyBaseline,
      nodes: [net, vm],
      edges: [edge],
      deployedNodeData: { net1: baseline(net.data as Record<string, unknown>), vm1: baseline(vm.data as Record<string, unknown>) },
      deployedEdgeKey: "x", // non-empty baseline so the guard passes
      deployedEdges: [],
    } as TopologyDiffState;
    const diff = computeTopologyDiff(state);
    const conn = diff.find((e) => e.resourceType === "Connection");
    expect(conn).toMatchObject({ kind: "added", name: "lab-net → bastion" });
  });
});
