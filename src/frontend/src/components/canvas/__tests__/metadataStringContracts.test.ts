import { describe, expect, it, vi } from "vitest";
import type { Node } from "@xyflow/react";
import { nextFreeBmcIp } from "../clusterBmc";
import { reconcileManagedClusterDns } from "../clusterNetworkBackfill";
import { healClusterMembership } from "../clusterTopologyHeal";
import { buildPaletteOcpInfo } from "../paletteOcpInfo";
import { makeCluster } from "../clusterFactory";
import { _saveTopologyToApi, computeTopologyDiff, stableNodeData } from "@/stores/canvasStore";

const node = (id: string, type: string, data: Record<string, unknown>): Node => ({ id, type, data, position: { x: 0, y: 0 } });

describe("metadata string contracts", () => {
  it.each([{}, ["10.20.30.0/24"], 123, false, null])("defaults malformed BMC CIDR %j without stringifying it", (cidr) => {
    expect(nextFreeBmcIp([], node("bmc", "networkNode", { cidr }))).toBe("192.168.100.11");
  });
  it("preserves configured BMC addressing and skips occupied hosts", () => {
    const bmc = node("bmc", "networkNode", { cidr: "10.20.30.0/24" });
    expect(nextFreeBmcIp([], bmc, new Set(["10.20.30.11"]))).toBe("10.20.30.12");
  });
  it("tags only string DNS names that match managed cluster records", () => {
    const { cluster } = makeCluster("prod", { x: 0, y: 0 });
    cluster.baseDomain = "local";
    const invalid = { name: ["api.prod.local"], ip: "10.0.0.3" };
    const nodes = [node("net", "networkNode", { dnsRecords: [invalid, { name: "api.prod.local", ip: "10.0.0.4" }] })];
    const result = reconcileManagedClusterDns(nodes, [cluster]);
    expect(result[0].data.dnsRecords).toEqual([invalid, { name: "api.prod.local", ip: "10.0.0.4", managed: true, clusterId: cluster.id }]);
  });
  it("does not infer legacy SNO membership from an array-valued name", () => {
    const { cluster } = makeCluster("prod", { x: 0, y: 0 });
    cluster.type = "sno";
    const malformed = node("unrelated", "vmNode", { clusterId: "ocp", name: ["cp-0"] });
    expect(healClusterMembership([malformed], [cluster])[0]).toBe(malformed);
    const valid = node("unrelated", "vmNode", { clusterId: "ocp", name: "cp-0" });
    expect(healClusterMembership([valid], [cluster])[0].data.clusterId).toBe(cluster.id);
  });
  it("does not attach credentials to an array-valued cluster identifier", () => {
    const nodes = [node("cluster-prod", "clusterNode", { clusterId: "prod" }), node("bad", "vmNode", { clusterId: ["prod"], ocpKubeadminPassword: "wrong", ocpKubeconfig: "wrong" }), node("good", "vmNode", { clusterId: "prod", ocpKubeadminPassword: "right", ocpKubeconfig: "config", label: "control-plane" })];
    const result = buildPaletteOcpInfo(nodes, [{ id: "prod", name: "prod" }]);
    expect(result.clusters[0]).toMatchObject({ kubeadminPassword: "right", kubeconfig: "config", kubeconfigVmName: "control-plane" });
  });
  it("preserves numeric ports while rejecting structured values in forward comparison", () => {
    expect(stableNodeData({ portForwards: [{ extPort: 8443, intIp: {}, intPort: [443], proto: {}, extIpId: {} }] }).portForwards).toEqual([{ extPort: "8443", intIp: "", intPort: "", proto: "" }]);
  });
  it("uses the valid label when a node display name is structured data", () => {
    const diff = computeTopologyDiff({ nodes: [node("new", "vmNode", { name: {}, label: "worker" })], edges: [], deployedNodeData: { existing: "{}" }, deployedEdgeKey: "", deployedNodeMeta: { existing: { name: "existing", type: "vmNode" } } });
    expect(diff.find((entry) => entry.id === "new")?.name).toBe("worker");
  });
  it("saves valid showroom defaults when imported string fields contain objects", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue({ ok: true, json: async () => ({}) } as Response);
    try {
      await _saveTopologyToApi("project", { nodes: [node("showroom", "containerNode", { isShowroom: true, name: "showroom", contentRepo: {}, contentRef: ["branch"], dnsNetwork: {} })], edges: [], hiddenNodeIds: [], startOrder: [], externalIps: [], showroom: null });
      const body = JSON.parse(fetchSpy.mock.calls[0][1]?.body as string);
      expect(body.topology.showroom).toMatchObject({ enabled: true, content_repo: "", content_ref: "main" });
      expect(body.topology.showroom.dns_network).toBeUndefined();
    } finally {
      fetchSpy.mockRestore();
    }
  });
});
