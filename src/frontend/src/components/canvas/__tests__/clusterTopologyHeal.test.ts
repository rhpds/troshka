import { describe, it, expect } from "vitest";
import type { Node } from "@xyflow/react";
import {
  healClusterTopology,
  isLegacyMigrationGhost,
  inferClusterIdFromMemberId,
  LEGACY_GHOST_NODE_ID,
} from "@/components/canvas/clusterTopologyHeal";
import type { ClusterConfig } from "@/stores/canvasStore";

const ghostCluster: ClusterConfig = {
  id: "ocp",
  name: "ocp",
  nodeId: "cluster-ocp",
  type: "compact",
  controlPlane: 3,
  workers: 0,
  baseDomain: "ocp.local",
};

const deployedCluster: ClusterConfig = {
  id: "ocp-68a740",
  name: "ocp",
  nodeId: "68a740b1-8108-4d30-b2bc-1eb77ae63b50",
  type: "sno",
  controlPlane: 1,
  workers: 0,
  baseDomain: "local",
  ocpVersion: "4.22",
};

const ocp2Cluster: ClusterConfig = {
  id: "ocp-2-hbf8w5",
  name: "ocp-2",
  nodeId: "cluster-ocp-2-hbf8w5",
  type: "sno",
  controlPlane: 1,
  workers: 0,
  baseDomain: "local",
};

describe("clusterTopologyHeal", () => {
  it("detects the legacy migration ghost fingerprint", () => {
    expect(isLegacyMigrationGhost(ghostCluster)).toBe(true);
    expect(isLegacyMigrationGhost(deployedCluster)).toBe(false);
    expect(isLegacyMigrationGhost(ocp2Cluster)).toBe(false);
  });

  it("infers cluster id from generated member VM ids", () => {
    expect(inferClusterIdFromMemberId("ocp-2-hbf8w5-cp-0")).toBe("ocp-2-hbf8w5");
    expect(inferClusterIdFromMemberId("cp-0")).toBeNull();
  });

  it("heals ghost-stolen members back into their cluster boxes (kv-pattern scenario)", () => {
    const nodes: Node[] = [
      {
        id: deployedCluster.nodeId,
        type: "clusterNode",
        position: { x: 240, y: 340 },
        data: {
          name: "ocp",
          clusterId: deployedCluster.id,
          type: "sno",
          controlPlane: 1,
          workers: 0,
          baseDomain: "local",
        },
      },
      {
        id: "72eaf5f9-4983-4250-9571-86d4076086ad",
        type: "vmNode",
        position: { x: 30, y: 48 },
        parentId: LEGACY_GHOST_NODE_ID,
        data: { name: "cp-0", os: "rhcos", clusterId: "ocp", clusterRole: "control-plane" },
      },
      {
        id: ocp2Cluster.nodeId,
        type: "clusterNode",
        position: { x: -330, y: 350 },
        data: {
          name: "ocp-2",
          clusterId: ocp2Cluster.id,
          type: "sno",
          controlPlane: 1,
          workers: 0,
          baseDomain: "local",
        },
      },
      {
        id: "ocp-2-hbf8w5-cp-0",
        type: "vmNode",
        position: { x: 30, y: 48 },
        parentId: LEGACY_GHOST_NODE_ID,
        data: {
          name: "ocp-2-hbf8w5-cp-0",
          os: "rhcos",
          clusterId: "ocp",
          clusterRole: "control-plane",
          generated: true,
        },
      },
      {
        id: LEGACY_GHOST_NODE_ID,
        type: "clusterNode",
        position: { x: 100, y: 100 },
        data: { name: "ocp", type: "compact", controlPlane: 3, workers: 0, baseDomain: "ocp.local" },
      },
    ];

    const out = healClusterTopology({
      nodes,
      edges: [],
      clusters: [ghostCluster, ocp2Cluster],
      deployedClusters: [deployedCluster],
    });

    expect(out.clusters.map((c) => c.id).sort()).toEqual(
      [deployedCluster.id, ocp2Cluster.id].sort(),
    );
    expect(out.clusters.some((c) => isLegacyMigrationGhost(c))).toBe(false);
    expect(out.nodes.some((n) => n.id === LEGACY_GHOST_NODE_ID)).toBe(false);

    const cp0 = out.nodes.find((n) => n.id === "72eaf5f9-4983-4250-9571-86d4076086ad")!;
    const cp2 = out.nodes.find((n) => n.id === "ocp-2-hbf8w5-cp-0")!;
    expect(cp0.parentId).toBe(deployedCluster.nodeId);
    expect((cp0.data as Record<string, unknown>).clusterId).toBe(deployedCluster.id);
    expect(cp2.parentId).toBe(ocp2Cluster.nodeId);
    expect((cp2.data as Record<string, unknown>).clusterId).toBe(ocp2Cluster.id);

    const parentIdx = out.nodes.findIndex((n) => n.id === deployedCluster.nodeId);
    const childIdx = out.nodes.findIndex((n) => n.id === cp0.id);
    expect(parentIdx).toBeLessThan(childIdx);
  });

  it("keeps a sole single-cluster OCP (cluster-ocp is the real, only cluster)", () => {
    // A fresh sno/compact/standard template makes cluster id=ocp / nodeId=
    // cluster-ocp with baseDomain "local" (NOT the ghost's "ocp.local"), so it is
    // not a ghost — but its box is still cluster-ocp. With no other cluster, heal
    // must NOT drop the box (that deleted the box + member VM -> empty canvas).
    const soleCluster: ClusterConfig = {
      id: "ocp",
      name: "ocp",
      nodeId: "cluster-ocp",
      type: "sno",
      controlPlane: 1,
      workers: 0,
      baseDomain: "local",
    };
    const nodes: Node[] = [
      {
        id: LEGACY_GHOST_NODE_ID,
        type: "clusterNode",
        position: { x: 100, y: 250 },
        data: {
          name: "ocp",
          clusterId: "ocp",
          type: "sno",
          controlPlane: 1,
          workers: 0,
          baseDomain: "local",
        },
      },
      {
        id: "9a1b2c3d-0000-4000-8000-000000000001",
        type: "vmNode",
        position: { x: 30, y: 78 },
        data: { name: "cp-0", os: "rhcos", clusterId: "ocp", clusterRole: "control-plane" },
      },
    ];

    const out = healClusterTopology({
      nodes,
      edges: [],
      clusters: [soleCluster],
      deployedClusters: [],
    });

    // The sole cluster, its box, and its member VM all survive.
    expect(out.clusters.map((c) => c.id)).toEqual(["ocp"]);
    expect(out.nodes.some((n) => n.id === LEGACY_GHOST_NODE_ID)).toBe(true);
    const cp0 = out.nodes.find((n) => n.type === "vmNode")!;
    expect(cp0).toBeDefined();
    expect(cp0.parentId).toBe("cluster-ocp");
  });
});
