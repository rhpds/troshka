import type { Node } from "@xyflow/react";
import { describe, expect, it } from "vitest";
import {
  assignCephOsdIps,
  cephIpConflict,
  collectUsedIps,
  pickHighFreeIps,
} from "./dhcpIpAssignment";

function cephNodes(
  osdIps?: string[],
  osdCount = 3,
  extra: Node[] = [],
): Node[] {
  const cephData: Record<string, unknown> = {
    networkRef: "net1",
    labIp: "10.0.0.4",
    osdCount,
  };
  if (osdIps) cephData.osdIps = osdIps;
  return [
    { id: "net1", type: "networkNode", position: { x: 0, y: 0 }, data: { id: "net1", cidr: "10.0.0.0/24" } },
    { id: "cp0", type: "vmNode", position: { x: 0, y: 0 }, data: { nics: [{ ip: "10.0.0.10" }] } },
    { id: "w0", type: "vmNode", position: { x: 0, y: 0 }, data: { nics: [{ ip: "10.0.0.20" }] } },
    { id: "ceph1", type: "cephClusterNode", position: { x: 0, y: 0 }, data: cephData },
    ...extra,
  ] as Node[];
}

function osdIpsOf(nodes: Node[]): string[] {
  const ceph = nodes.find((n) => n.type === "cephClusterNode");
  return ((ceph?.data as Record<string, unknown>)?.osdIps as string[]) || [];
}

describe("pickHighFreeIps", () => {
  it("returns top-down free IPs, skipping used and gateway", () => {
    expect(
      pickHighFreeIps("10.0.0.0/24", 3, new Set(["10.0.0.254", "10.0.0.20"])),
    ).toEqual(["10.0.0.253", "10.0.0.252", "10.0.0.251"]);
  });

  it("excludes the gateway and stops when the range is exhausted", () => {
    expect(pickHighFreeIps("10.9.9.0/30", 5, new Set())).toEqual(["10.9.9.2"]);
  });
});

describe("assignCephOsdIps", () => {
  it("allocates top-down, avoiding node NICs and the mon", () => {
    expect(osdIpsOf(assignCephOsdIps(cephNodes(undefined, 3)))).toEqual([
      "10.0.0.254",
      "10.0.0.253",
      "10.0.0.252",
    ]);
  });

  it("preserves existing IPs and grows to osdCount", () => {
    expect(
      osdIpsOf(assignCephOsdIps(cephNodes(["10.0.0.254", "10.0.0.253"], 3))),
    ).toEqual(["10.0.0.254", "10.0.0.253", "10.0.0.252"]);
  });

  it("trims when osdCount is lowered", () => {
    expect(
      osdIpsOf(
        assignCephOsdIps(cephNodes(["10.0.0.254", "10.0.0.253", "10.0.0.252"], 2)),
      ),
    ).toEqual(["10.0.0.254", "10.0.0.253"]);
  });

  it("returns the same array reference when nothing changes", () => {
    const nodes = cephNodes(["10.0.0.254", "10.0.0.253", "10.0.0.252"], 3);
    expect(assignCephOsdIps(nodes)).toBe(nodes);
  });
});

describe("collectUsedIps", () => {
  it("counts allocated OSD IPs and the mon labIp", () => {
    const used = collectUsedIps(cephNodes(["10.0.0.254"], 1));
    expect(used.has("10.0.0.254")).toBe(true);
    expect(used.has("10.0.0.4")).toBe(true);
  });
});

describe("cephIpConflict", () => {
  it("labels OSD and mon IPs and ignores everything else", () => {
    const nodes = cephNodes(["10.0.0.254", "10.0.0.253"], 2);
    expect(cephIpConflict(nodes, "10.0.0.254")).toBe("Ceph OSD");
    expect(cephIpConflict(nodes, "10.0.0.4")).toBe("Ceph mon");
    expect(cephIpConflict(nodes, "10.0.0.99")).toBeNull();
    expect(cephIpConflict(nodes, "")).toBeNull();
  });
});
