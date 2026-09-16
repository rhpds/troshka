import { describe, expect, it } from "vitest";
import type { Node } from "@xyflow/react";
import {
  cephClusterPosition,
  layoutCephBetweenClusters,
  repositionCephClearOfClusters,
} from "../cephClusterLayout";

function clusterNode(id: string, x: number, y = 250): Node {
  return {
    id,
    type: "clusterNode",
    position: { x, y },
    data: {},
    style: { width: 520, height: 320 },
  };
}

function cephNode(x: number, y: number): Node {
  return {
    id: "ceph-1",
    type: "cephClusterNode",
    position: { x, y },
    data: {},
  };
}

describe("cephClusterLayout", () => {
  it("places ceph between two cluster boxes", () => {
    const nodes = [clusterNode("cluster-destination", 100), clusterNode("cluster-source", 1000)];
    const pos = cephClusterPosition(nodes);
    expect(pos.x).toBeGreaterThan(620);
    expect(pos.x).toBeLessThan(1000);
  });

  it("repositions overlapping ceph away from clusters", () => {
    const nodes = [
      clusterNode("cluster-destination", 100),
      clusterNode("cluster-source", 1000),
      cephNode(150, 280),
    ];
    const out = repositionCephClearOfClusters(nodes);
    const moved = out.find((n) => n.id === "ceph-1");
    expect(moved?.position?.x).toBeGreaterThan(620);
  });

  it("spreads tight clusters and places ceph between them", () => {
    const nodes = [
      clusterNode("cluster-destination", 40),
      clusterNode("cluster-source", 40 + 520 + 60),
      cephNode(200, 360),
    ];
    const out = layoutCephBetweenClusters(nodes);
    const source = out.find((n) => n.id === "cluster-source");
    const ceph = out.find((n) => n.id === "ceph-1");
    expect(source?.position?.x).toBeGreaterThan(40 + 520 + 60);
    expect(ceph?.position?.x).toBeGreaterThan(40 + 520);
    expect(ceph?.position?.x).toBeLessThan(source?.position?.x ?? 0);
    expect(ceph?.position?.y).toBe(250 + (320 - 80) / 2);
  });
});
