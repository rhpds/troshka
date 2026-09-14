import { describe, it, expect } from "vitest";
import {
  applyClusterBmc,
  collectUsedBmcIps,
  nextFreeBmcIp,
  findBmcNetwork,
} from "@/components/canvas/clusterBmc";
import { makeCluster } from "@/components/canvas/clusterFactory";
import { materializeClusterInto } from "@/components/canvas/clusterMaterialize";

const cluster = {
  id: "prod",
  nodeId: "cluster-prod",
  type: "compact",
  controlPlane: 1,
  workers: 0,
  controlPlaneCpu: 8,
  controlPlaneMemory: 16384,
  controlPlaneDisk: 120,
  workerCpu: 4,
  workerMemory: 8192,
  workerDisk: 100,
} as const;

describe("applyClusterBmc", () => {
  it("creates BMC network, enables members, allocates unique IPs, and wires edges", () => {
    const { node: clusterNode, cluster: cfg } = makeCluster("ocp", { x: 100, y: 200 });
    const { nodes, edges } = materializeClusterInto(cfg, [clusterNode]);

    const bmcNet = findBmcNetwork(nodes);
    expect(bmcNet).toBeDefined();
    expect((bmcNet!.data as Record<string, unknown>).bmcUsername).toBe("admin");
    expect((bmcNet!.data as Record<string, unknown>).bmcPassword).toBeTruthy();

    const members = nodes.filter(
      (n) => n.type === "vmNode" && (n.data as Record<string, unknown>).clusterId === cfg.id,
    );
    expect(members.length).toBeGreaterThan(0);
    const ips = members.map((m) => (m.data as Record<string, unknown>).bmcIp);
    expect(ips.every((ip) => typeof ip === "string" && String(ip).startsWith("192.168.100."))).toBe(true);
    expect(new Set(ips).size).toBe(ips.length);
    expect(members.every((m) => (m.data as Record<string, unknown>).bmcEnabled === true)).toBe(true);
    const netCreds = {
      username: (bmcNet!.data as Record<string, unknown>).bmcUsername,
      password: (bmcNet!.data as Record<string, unknown>).bmcPassword,
    };
    expect(members.every((m) => {
      const d = m.data as Record<string, unknown>;
      return d.bmcUsername === netCreds.username && d.bmcPassword === netCreds.password;
    })).toBe(true);

    for (const m of members) {
      expect(
        edges.some((e) => e.source === bmcNet!.id && e.target === m.id),
      ).toBe(true);
    }
  });

  it("reuses existing BMC network and skips already-assigned IPs", () => {
    const existingBmc = {
      id: "bmc-net-1",
      type: "networkNode",
      position: { x: 0, y: 0 },
      data: {
        subtype: "network",
        networkType: "bmc",
        cidr: "10.50.0.0/24",
        bmcUsername: "admin",
        bmcPassword: "keep-me",
      },
    };
    const member = {
      id: "cp-0",
      type: "vmNode",
      parentId: "cluster-prod",
      position: { x: 30, y: 48 },
      data: {
        clusterId: "prod",
        clusterRole: "control-plane",
        bmcEnabled: true,
        bmcIp: "10.50.0.42",
      },
    };
    const clusterNode = {
      id: "cluster-prod",
      type: "clusterNode",
      position: { x: 0, y: 0 },
      data: { label: "ocp" },
    };

    const { nodes, edges } = applyClusterBmc(cluster as any, [clusterNode, existingBmc, member] as any, []);
    expect(findBmcNetwork(nodes)?.id).toBe("bmc-net-1");
    expect((findBmcNetwork(nodes)!.data as Record<string, unknown>).bmcPassword).toBe("keep-me");
    const cp = nodes.find((n) => n.id === "cp-0");
    expect((cp!.data as Record<string, unknown>).bmcIp).toBe("10.50.0.42");
    expect((cp!.data as Record<string, unknown>).bmcUsername).toBe("admin");
    expect((cp!.data as Record<string, unknown>).bmcPassword).toBe("keep-me");
    expect(edges).toHaveLength(1);
    expect(edges[0].source).toBe("bmc-net-1");
  });

  it("nextFreeBmcIp skips used addresses on the BMC CIDR", () => {
    const bmcNet = {
      id: "bmc",
      type: "networkNode",
      data: { networkType: "bmc", cidr: "192.168.100.0/24" },
    };
    const nodes = [
      bmcNet,
      { id: "v1", type: "vmNode", data: { bmcIp: "192.168.100.11" } },
      { id: "v2", type: "vmNode", data: { bmcIp: "192.168.100.12" } },
    ] as any[];
    expect(collectUsedBmcIps(nodes).size).toBe(2);
    expect(nextFreeBmcIp(nodes, bmcNet as any)).toBe("192.168.100.13");
  });
});
