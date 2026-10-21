import { describe, expect, it } from "vitest";
import type { Edge, Node } from "@xyflow/react";
import type { ClusterConfig } from "@/stores/canvasStore";
import { collectIpsOnNetwork, defaultCephLabIp, validateCephLabIp } from "./cephLabIpValidation";

const node = (id: string, type: string, data: Record<string, unknown> = {}): Node => ({
  id, type, data, position: { x: 0, y: 0 },
});
const network = (data: Record<string, unknown> = {}) =>
  node("lab", "networkNode", { name: "Lab", cidr: "192.168.10.0/24", ...data });
const edge = (source: string, target: string, handles: Partial<Edge> = {}): Edge => ({
  id: `${source}-${target}`, source, target, ...handles,
});

describe("Ceph lab address defaults", () => {
  it.each(["", "192.168.10.0", "192.168/24"])("rejects incomplete network %s", (cidr) => {
    expect(defaultCephLabIp(cidr)).toBe("");
  });
  it("places the monitor at the reserved fourth address", () => {
    expect(defaultCephLabIp("192.168.10.0/24")).toBe("192.168.10.4");
  });
});

describe("network address ownership", () => {
  it("reserves infrastructure, DNS, and load-balancer addresses", () => {
    const claimed = collectIpsOnNetwork([network({
      dnsRecords: [{ name: "api", ip: " 192.168.10.5 " }, { ip: "192.168.10.6" }, {}],
      lbIp: " 192.168.10.7 ",
    })], [], [], "lab");
    expect(Object.fromEntries(claimed)).toEqual({
      "192.168.10.1": "gateway", "192.168.10.2": "dnsmasq",
      "192.168.10.5": "DNS record 'api'", "192.168.10.6": "DNS record 'record'",
      "192.168.10.7": "load balancer",
    });
  });
  it("collects NIC addresses in both edge directions without reserving disconnected NICs", () => {
    const nodes = [network(), node("other", "networkNode"),
      node("vm", "vmNode", { name: "master", nics: [
        { id: "a", ip: "192.168.10.11" }, { id: "b", ip: "192.168.10.12" },
      ] }),
      node("container", "containerNode", { label: "service", nics: [{ id: "c", ip: "192.168.10.13" }] }),
      node("empty", "vmNode"),
    ];
    const edges = [edge("vm", "lab", { sourceHandle: "nic-a-top" }),
      edge("other", "vm", { targetHandle: "nic-b-bottom" }),
      edge("lab", "container", { targetHandle: "nic-c-bottom" })];
    const claimed = collectIpsOnNetwork(nodes, edges, [], "lab");
    expect(claimed.get("192.168.10.11")).toBe("VM/container 'master'");
    expect(claimed.get("192.168.10.13")).toBe("VM/container 'service'");
    expect(claimed.has("192.168.10.12")).toBe(false);
  });
  it("ignores absent networks and non-network edge targets", () => {
    expect(collectIpsOnNetwork([], [], [], "lab").size).toBe(0);
    const vm = node("vm", "vmNode", { nics: [{ id: "a", ip: "192.168.10.9" }] });
    const edges = [edge("vm", "missing", { sourceHandle: "nic-a-bottom" }),
      edge("vm", "storage", { sourceHandle: "nic-a-top" }), edge("vm", "lab")];
    expect(collectIpsOnNetwork([network(), vm, node("storage", "storageNode")], edges, [], "lab")
      .has("192.168.10.9")).toBe(false);
  });
  it("reserves cluster VIPs while excluding cluster configs on other networks", () => {
    const clusters = [
      { networkIds: ["lab"], name: "ocp", apiVip: "192.168.10.20", ingressVip: "192.168.10.21" },
      { networkIds: ["other"], apiVip: "192.168.10.22" },
      {}, { networkIds: ["lab"] },
    ] as ClusterConfig[];
    const claimed = collectIpsOnNetwork([network(),
      node("cluster", "clusterNode", { apiVip: "192.168.10.30", ingressVip: "192.168.10.31" }),
      node("blank-cluster", "clusterNode"),
    ], [], clusters, "lab");
    expect(claimed.get("192.168.10.20")).toBe("cluster 'ocp' API VIP");
    expect(claimed.get("192.168.10.21")).toBe("cluster 'ocp' ingress VIP");
    expect(claimed.get("192.168.10.30")).toBe("cluster 'cluster' API VIP");
    expect(claimed.get("192.168.10.31")).toBe("cluster 'cluster' ingress VIP");
    expect(claimed.has("192.168.10.22")).toBe(false);
  });
});

describe("Ceph lab address validation", () => {
  it.each([{}, { networkRef: "missing" }])("requires an existing selected network", (data) => {
    expect(validateCephLabIp([network()], [], [], data)).toEqual(["Select a lab network"]);
  });
  it("requires an address when the network has no usable default", () => {
    expect(validateCephLabIp([network({ cidr: "" })], [], [], { networkRef: "lab" }))
      .toEqual(["Lab IP is required"]);
  });
  it.each(["192.168.11.4", "invalid", "999.0.0.1"])("rejects address outside the CIDR: %s", (labIp) => {
    expect(validateCephLabIp([network()], [], [], { networkRef: "lab", labIp })[0])
      .toContain("outside network 'Lab'");
  });
  it("accepts the default monitor address outside the default DHCP range", () => {
    expect(validateCephLabIp([network({ dhcp: true })], [], [], { networkRef: "lab" })).toEqual([]);
  });
  it.each(["192.168.10.10", "192.168.10.254"])("rejects DHCP range boundary %s", (labIp) => {
    expect(validateCephLabIp([network({ dhcp: true })], [], [], { networkRef: "lab", labIp })[0])
      .toContain("falls in the DHCP pool");
  });
  it("prefers an ownership conflict over the DHCP warning", () => {
    const nodes = [network({ dhcp: true, dnsRecords: [{ name: "api", ip: "192.168.10.10" }] })];
    expect(validateCephLabIp(nodes, [], [], { networkRef: "lab", labIp: "192.168.10.10" }))
      .toEqual(["Lab IP 192.168.10.10 conflicts with DNS record 'api' on 'Lab'"]);
  });
  it("allows static addresses when DHCP is disabled or has no usable range", () => {
    for (const data of [{ dhcp: false }, { cidr: "", dhcp: true }]) {
      expect(validateCephLabIp([network(data)], [], [], { networkRef: "lab", labIp: "192.168.10.15" }))
        .toEqual([]);
    }
  });
  it("uses the selected network ID when it has no display name", () => {
    expect(validateCephLabIp([network({ name: "" })], [], [], { networkRef: "lab", labIp: "192.168.10.1" }))
      .toEqual(["Lab IP 192.168.10.1 conflicts with gateway on 'lab'"]);
  });
});
