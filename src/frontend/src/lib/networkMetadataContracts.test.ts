import type { Node } from "@xyflow/react";
import { describe, expect, it } from "vitest";
import { collectUsedIps, getDhcpRange, assignCephOsdIps } from "./dhcpIpAssignment";
import { collectIpsOnNetwork } from "./cephLabIpValidation";
import { gatewayAllowsDnsUpstream, gatewayOutboundPorts } from "./gatewayValidation";
import { getShowroomReadiness, isShowroomLabNetworkEdge } from "./showroomValidation";

const node = (id: string, type: string, data: Record<string, unknown>): Node => ({
  id, type, data, position: { x: 0, y: 0 },
});

describe("network metadata validation", () => {
  it("ignores malformed address fields without stringifying objects", () => {
    const nodes = [
      node("net", "networkNode", { cidr: {} }),
      node("cluster", "clusterNode", { apiVip: {}, ingressVip: " 192.0.2.20 " }),
      node("ceph", "cephClusterNode", { labIp: {}, osdIps: ["192.0.2.21"] }),
    ];
    expect([...collectUsedIps(nodes)]).toEqual(["192.0.2.20", "192.0.2.21"]);
    expect(getDhcpRange({ cidr: {}, dhcpRangeStart: {}, dhcpRangeEnd: {} })).toBeNull();
  });

  it("preserves valid Ceph addresses and fills only missing slots", () => {
    const nodes = [
      node("net", "networkNode", { cidr: "192.0.2.0/29" }),
      node("ceph", "cephClusterNode", {
        networkRef: "net", labIp: "192.0.2.4", osdCount: 2,
        osdIps: ["192.0.2.5", "192.0.2.5", "198.51.100.5"],
      }),
    ];
    const updated = assignCephOsdIps(nodes);
    expect(updated[1].data.osdIps).toEqual(["192.0.2.5", "192.0.2.6"]);
    expect(assignCephOsdIps(updated)).toBe(updated);
  });

  it("labels address ownership from string names and keeps conflict precedence", () => {
    const nodes = [
      node("net", "networkNode", { cidr: "192.0.2.0/29", lbIp: "192.0.2.3" }),
      node("vm", "vmNode", { name: {}, label: "valid label", nics: [{ id: "nic", ip: "192.0.2.3" }] }),
    ];
    const edges = [{ id: "edge", source: "net", target: "vm", targetHandle: "nic-nic-top" }];
    const claimed = collectIpsOnNetwork(nodes, edges, [], "net");
    expect(claimed.get("192.0.2.3")).toBe("VM/container 'valid label'");
    expect(claimed.get("192.0.2.1")).toBe("gateway");
  });

  it("does not interpret malformed outbound ports as valid DNS permission", () => {
    expect(gatewayOutboundPorts({ outboundPorts: {} })).toBe("");
    expect(gatewayAllowsDnsUpstream({ outboundPolicy: "restrict", outboundPorts: {} })).toBe(false);
    expect(gatewayAllowsDnsUpstream({ outboundPolicy: "restrict", outboundPorts: "53/udp" })).toBe(true);
  });

  it("requires a string content repository for showroom builds", () => {
    const showroom = node("showroom", "containerNode", { isShowroom: true, contentRepo: {} });
    expect(getShowroomReadiness([showroom], []).hasContentRepo).toBe(false);
  });

  it("recognizes showroom lab connections in both directions", () => {
    const nodes = [node("showroom", "containerNode", { isShowroom: true }), node("net", "networkNode", {})];
    expect(isShowroomLabNetworkEdge({ id: "forward", source: "showroom", target: "net" }, nodes)).toBe(true);
    expect(isShowroomLabNetworkEdge({ id: "reverse", source: "net", target: "showroom" }, nodes)).toBe(true);
    expect(isShowroomLabNetworkEdge({ id: "missing", source: "unknown", target: "showroom" }, nodes)).toBe(false);
  });
});
