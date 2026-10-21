import type { Edge, Node } from "@xyflow/react";
import { afterEach, describe, expect, it } from "vitest";
import { makeCluster } from "@/components/canvas/clusterFactory";
import {
  applyClusterNetworks,
  gatewayAllowsClusterOutbound,
  memberRole,
  suggestClusterVips,
} from "@/components/canvas/clusterMaterialize";
import { useCanvasStore, type VMNic } from "@/stores/canvasStore";

const node = (id: string, type: string, data: Record<string, unknown>): Node => ({
  id, type, data, position: { x: 0, y: 0 },
});

afterEach(() => useCanvasStore.setState({ deployedVmIds: new Set<string>() }));

describe("cluster materialization contracts", () => {
  it("reuses NIC identity by network while dropping detached and malformed edges", () => {
    const { cluster } = makeCluster("test", { x: 0, y: 0 });
    cluster.networkIds = ["net-b", "net-a"];
    const nics: VMNic[] = [
      { id: "nic-a", name: "eth0", mac: "02:00:00:00:00:01", model: "virtio" },
      { id: "nic-b", name: "eth1", mac: "02:00:00:00:00:02", model: "virtio" },
    ];
    const member = node("vm", "vmNode", { clusterId: cluster.id, generated: true, nics });
    const edges: Edge[] = [
      { id: "a", source: "net-a", target: "vm", targetHandle: "nic-nic-a-top" },
      { id: "b", source: "net-b", target: "vm", targetHandle: "nic-nic-b-bottom" },
      { id: "malformed", source: "old", target: "vm", targetHandle: "nic-invalid" },
      { id: "disk", source: "disk", target: "vm", targetHandle: "dp-controller-left" },
    ];
    const result = applyClusterNetworks(cluster, [member], edges);
    expect(result.nodes[0].data.nics).toEqual([
      { ...nics[1], name: "eth0" }, { ...nics[0], name: "eth1" },
    ]);
    expect(result.edges.map((edge) => edge.id)).toEqual([
      "disk", "edge-net-b-to-vm-nic", "edge-net-a-to-vm-nic",
    ]);
    expect(applyClusterNetworks(cluster, result.nodes, result.edges)).toEqual(result);
    expect(member.data.nics).toEqual(nics);
  });

  it("preserves deployed NICs and explicit NIC overrides", () => {
    const { cluster } = makeCluster("test", { x: 0, y: 0 });
    cluster.networkIds = ["new-network"];
    const nics: VMNic[] = [{ id: "keep", name: "eth0", mac: "02:00:00:00:00:01", model: "virtio" }];
    const deployed = node("deployed", "vmNode", { clusterId: cluster.id, generated: true, nics });
    const overridden = node("custom", "vmNode", { clusterId: cluster.id, generated: true, nics, clusterOverrides: ["nics"] });
    useCanvasStore.setState({ deployedVmIds: new Set(["deployed"]) });
    const result = applyClusterNetworks(cluster, [deployed, overridden], []);
    expect(result.nodes).toEqual([deployed, overridden]);
    expect(result.edges).toEqual([]);
  });

  it("returns one available VIP when the gateway consumes the other subnet host", () => {
    const { cluster } = makeCluster("test", { x: 0, y: 0 });
    cluster.controlPlane = 3;
    cluster.networkIds = ["net"];
    const network = node("net", "networkNode", { cidr: "192.0.2.0/30" });
    expect(suggestClusterVips(cluster, [network])).toEqual({ apiVip: "192.0.2.2", ingressVip: null });
    const member = node("vm", "vmNode", { nics: [{ ip: "192.0.2.2" }] });
    expect(suggestClusterVips(cluster, [network, member])).toEqual({ apiVip: null, ingressVip: null });
  });

  it("does not stringify arbitrary node metadata when inferring roles", () => {
    expect(memberRole(node("test-worker-2", "vmNode", { name: {} }))).toBe("worker");
    expect(memberRole(node("test-cp-1", "vmNode", { name: "renamed-worker-0" }))).toBe("worker");
  });

  it("requires all required protocols in restricted gateway rules", () => {
    const gateway = node("gw", "networkNode", {
      subtype: "gateway", outboundPolicy: "restrict", outboundPorts: "80/tcp, 443/tcp, 123/udp, 443/tcp",
    });
    expect(gatewayAllowsClusterOutbound([gateway])).toBe(true);
    gateway.data.outboundPorts = "80/tcp,443/tcp,123/tcp";
    expect(gatewayAllowsClusterOutbound([gateway])).toBe(false);
    gateway.data.outboundPorts = {};
    expect(gatewayAllowsClusterOutbound([gateway])).toBe(false);
  });
});
