import type { Node } from "@xyflow/react";
import { describe, expect, it } from "vitest";
import {
  injectShowroomPortForwards,
  isRouteManagedForward,
  isShowroomInfraForward,
  syncShowroomGatewayAccess,
  type PortForward,
} from "./showroomPortForwards";

const pf = (extPort: string, extIpId?: string): PortForward => ({
  extPort,
  intIp: "10.0.0.10",
  intPort: extPort,
  proto: "tcp",
  extIpId,
});

function showroomNodes(
  portForwards: PortForward[],
): Node[] {
  return [
    {
      id: "gw",
      type: "networkNode",
      position: { x: 0, y: 0 },
      data: {
        subtype: "gateway",
        gatewayMode: "nat-portforward",
        portForwards,
      },
    },
    {
      id: "showroom",
      type: "containerNode",
      position: { x: 0, y: 0 },
      data: { isShowroom: true, name: "showroom", nics: [] },
    },
    {
      id: "net-1",
      type: "networkNode",
      position: { x: 0, y: 0 },
      data: { subtype: "network", cidr: "10.0.0.0/24" },
    },
  ] as unknown as Node[];
}

describe("isShowroomInfraForward", () => {
  it("recognizes cloud terminator .2:443 and route-provider .3:80", () => {
    expect(
      isShowroomInfraForward({
        extPort: "443",
        intIp: "172.30.103.2",
        intPort: "443",
        proto: "tcp",
      }),
    ).toBe(true);
    expect(
      isShowroomInfraForward({
        extPort: "443",
        intIp: "172.30.5.3",
        intPort: "80",
        proto: "tcp",
      }),
    ).toBe(true);
  });

  it("rejects non-showroom 443 forwards", () => {
    expect(
      isShowroomInfraForward({
        extPort: "443",
        intIp: "10.0.0.10",
        intPort: "443",
        proto: "tcp",
      }),
    ).toBe(false);
  });
});

describe("injectShowroomPortForwards", () => {
  it("targets gateway TLS terminator .2:443 on cloud providers", () => {
    const out = injectShowroomPortForwards([], 5, false);
    expect(out).toEqual([
      {
        extPort: "443",
        intIp: "172.30.5.2",
        intPort: "443",
        proto: "tcp",
        extIpId: "",
        managedByShowroom: true,
      },
    ]);
  });

  it("targets showroom container .3:80 on route providers", () => {
    const out = injectShowroomPortForwards([], 5, true);
    expect(out).toEqual([
      {
        extPort: "443",
        intIp: "172.30.5.3",
        intPort: "80",
        proto: "tcp",
        extIpId: "",
        managedByShowroom: true,
      },
    ]);
  });

  it("replaces all existing 443 forwards with a single managed one", () => {
    const existing: PortForward[] = [
      {
        extPort: "6443",
        intIp: "10.0.0.10",
        intPort: "6443",
        proto: "tcp",
      },
      {
        extPort: "443",
        intIp: "172.30.103.2",
        intPort: "443",
        proto: "tcp",
        managedByShowroom: true,
      },
      {
        extPort: "443",
        intIp: "172.30.103.3",
        intPort: "80",
        proto: "tcp",
        managedByShowroom: true,
      },
    ];
    const out = injectShowroomPortForwards(existing, 2151, false);
    expect(out.filter((p) => p.extPort === "443")).toHaveLength(1);
    expect(out).toContainEqual({
      extPort: "443",
      intIp: "172.30.103.2",
      intPort: "443",
      proto: "tcp",
      extIpId: "",
      managedByShowroom: true,
    });
    expect(out.some((p) => p.extPort === "6443")).toBe(true);
  });
});

describe("isRouteManagedForward", () => {
  it("treats 80/443/6443 and secondary API listen keys as route-managed", () => {
    // On kubevirt/ocpvirt, ingress (80/443) AND the API (intPort 6443, any
    // gateway listen key including 6444+) are served by OpenShift Routes.
    expect(isRouteManagedForward(pf("80"), "ocpvirt")).toBe(true);
    expect(isRouteManagedForward(pf("443"), "kubevirt")).toBe(true);
    expect(isRouteManagedForward(pf("6443"), "kubevirt")).toBe(true);
    expect(
      isRouteManagedForward(
        { extPort: "6444", intIp: "10.0.0.10", intPort: "6443", proto: "tcp" },
        "kubevirt",
      ),
    ).toBe(true);
  });

  it("does not route-manage arbitrary ports (e.g. 8080)", () => {
    expect(isRouteManagedForward(pf("8080"), "kubevirt")).toBe(false);
  });

  it("does not route-manage web ports on non-route providers", () => {
    // Cloud providers have no ingress — 443/80 stay EIP-bound, so a missing
    // external IP there IS a real error and must still warn.
    expect(isRouteManagedForward(pf("443"), "aws")).toBe(false);
    expect(isRouteManagedForward(pf("443"), null)).toBe(false);
  });
});

describe("syncShowroomGatewayAccess — showroom owns 443/80", () => {
  it("drops direct cluster-ingress 443/80 forwards when a showroom is present", () => {
    const nodes = showroomNodes([
      { extPort: "6443", intIp: "10.0.0.10", intPort: "6443", proto: "tcp" },
      { extPort: "443", intIp: "10.0.0.10", intPort: "443", proto: "tcp" },
      { extPort: "80", intIp: "10.0.0.10", intPort: "80", proto: "tcp" },
    ]);

    const { nodes: out } = syncShowroomGatewayAccess(
      nodes,
      [],
      [],
      { "net-1": 1000 },
      "kubevirt",
    );
    const gw = out.find((n) => n.id === "gw")!;
    const pfs = (gw.data as { portForwards: PortForward[] }).portForwards;
    // API (distinct port) kept
    expect(pfs.some((p) => p.extPort === "6443")).toBe(true);
    // direct cluster-ingress 443/80 (intIp 10.0.0.10) dropped
    expect(pfs.some((p) => (p.extPort === "443" || p.extPort === "80") && p.intIp === "10.0.0.10")).toBe(false);
    // showroom 443 remains (managed) at route-provider target .3:80
    const showroomPf = pfs.find((p) => p.extPort === "443" && p.managedByShowroom)!;
    expect(showroomPf.intIp).toBe("172.30.232.3"); // 1000 & 0xff = 232
    expect(showroomPf.intPort).toBe("80");
    expect(pfs.filter((p) => p.extPort === "443")).toHaveLength(1);
  });

  it("does not duplicate showroom 443 when topology already has cloud .2:443", () => {
    const nodes = showroomNodes([
      { extPort: "6443", intIp: "10.0.0.10", intPort: "6443", proto: "tcp" },
      {
        extPort: "443",
        intIp: "172.30.103.2",
        intPort: "443",
        proto: "tcp",
        managedByShowroom: true,
      },
    ]);

    const { nodes: out } = syncShowroomGatewayAccess(
      nodes,
      [],
      [{ id: "eip-1", name: "IP-1", ip: "184.194.236.17" }],
      { "net-1": 2151 },
      "aws",
    );
    const gw = out.find((n) => n.id === "gw")!;
    const pfs = (gw.data as { portForwards: PortForward[] }).portForwards;
    expect(pfs.filter((p) => p.extPort === "443")).toHaveLength(1);
    expect(pfs.find((p) => p.extPort === "443")).toMatchObject({
      intIp: "172.30.103.2",
      intPort: "443",
      managedByShowroom: true,
    });
  });
});
