import type { Node } from "@xyflow/react";
import { describe, expect, it } from "vitest";
import {
  isRouteManagedForward,
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
    const nodes = [
      {
        id: "gw",
        type: "networkNode",
        position: { x: 0, y: 0 },
        data: {
          subtype: "gateway",
          gatewayMode: "nat-portforward",
          portForwards: [
            { extPort: "6443", intIp: "10.0.0.10", intPort: "6443", proto: "tcp" },
            { extPort: "443", intIp: "10.0.0.10", intPort: "443", proto: "tcp" },
            { extPort: "80", intIp: "10.0.0.10", intPort: "80", proto: "tcp" },
          ],
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
    // showroom 443 remains (managed)
    expect(pfs.some((p) => p.extPort === "443" && p.managedByShowroom)).toBe(true);
  });
});
