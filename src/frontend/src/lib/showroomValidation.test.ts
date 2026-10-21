import { describe, expect, it } from "vitest";
import type { Edge, Node } from "@xyflow/react";
import {
  buildShowroomGatewayEdge, ensureShowroomGatewayEdge, getShowroomReadiness,
  isCephStorageNetworkNode, isDnsEnabledLabNetwork, isGatewayNode,
  isLabNetworkNode, isShowroomContainer, isShowroomGatewayEdge,
  isShowroomLabNetworkEdge, isShowroomReady, sanitizeShowroomTopology,
} from "./showroomValidation";

const node = (id: string, type: string, data: Record<string, unknown> = {}): Node => ({
  id, type, data, position: { x: 0, y: 0 },
});
const showroom = (data: Record<string, unknown> = {}) =>
  node("showroom", "containerNode", { isShowroom: true, contentRepo: "https://example.com/content.git", ...data });
const gateway = (data: Record<string, unknown> = {}) => node("gateway", "networkNode", { subtype: "gateway", ...data });
const network = (data: Record<string, unknown> = {}) =>
  node("lab", "networkNode", { name: "Lab", dns: true, cidr: "192.168.10.0/24", ...data });
const edge = (source: string, target: string, handles: Partial<Edge> = {}): Edge => ({
  id: `${source}-${target}`, source, target, ...handles,
});

describe("showroom network classification", () => {
  it("recognizes the legacy name and current showroom flag", () => {
    expect(isShowroomContainer(showroom())).toBe(true);
    expect(isShowroomContainer(node("legacy", "containerNode", { name: "showroom" }))).toBe(true);
    expect(isShowroomContainer(node("ordinary", "containerNode"))).toBe(false);
    expect(isShowroomContainer(node("wrong", "vmNode", { isShowroom: true }))).toBe(false);
    expect(isShowroomContainer(undefined)).toBe(false);
  });
  it.each([undefined, "network", "dns", "dhcp"])("accepts lab subtype %s", (subtype) => {
    expect(isLabNetworkNode(network({ subtype }))).toBe(true);
    expect(isCephStorageNetworkNode(network({ subtype }))).toBe(true);
  });
  it.each(["gateway", "router", "loadbalancer"])("excludes infrastructure subtype %s", (subtype) => {
    expect(isLabNetworkNode(network({ subtype }))).toBe(false);
    expect(isCephStorageNetworkNode(network({ subtype }))).toBe(false);
    expect(isDnsEnabledLabNetwork(network({ subtype }))).toBe(false);
  });
  it("excludes BMC networks from Ceph and non-networks from DNS eligibility", () => {
    expect(isCephStorageNetworkNode(network({ networkType: "bmc" }))).toBe(false);
    expect(isLabNetworkNode(undefined)).toBe(false);
    expect(isDnsEnabledLabNetwork(undefined)).toBe(false);
    expect(isDnsEnabledLabNetwork(showroom())).toBe(false);
    expect(isGatewayNode(undefined)).toBe(false);
    expect(isGatewayNode(gateway())).toBe(true);
    expect(isDnsEnabledLabNetwork(network({ dns: false }))).toBe(false);
    expect(isDnsEnabledLabNetwork(network({ subtype: "dns", dns: false }))).toBe(true);
  });
});

describe("showroom topology normalization", () => {
  it.each([false, true])("removes direct lab-network edges in either direction (reverse=%s)", (reverse) => {
    const nodes = [showroom(), network(), gateway()];
    const labEdge = reverse ? edge("lab", "showroom") : edge("showroom", "lab");
    const gatewayEdge = buildShowroomGatewayEdge("showroom", "gateway");
    expect(isShowroomLabNetworkEdge(labEdge, nodes)).toBe(true);
    const result = sanitizeShowroomTopology(nodes, [labEdge, gatewayEdge]);
    expect(result.edges).toEqual([gatewayEdge]);
    expect(result.nodes[0].data).toMatchObject({ nics: [], infraNetworking: true });
    expect(nodes[0].data).not.toHaveProperty("infraNetworking");
  });
  it.each([false, true])("strips invalid mount handles in either direction (reverse=%s)", (reverse) => {
    const nodes = [showroom({ nics: [{ id: "old" }] }), node("disk", "storageNode")];
    const mount = reverse
      ? edge("disk", "showroom", { targetHandle: "mnt-disk-right" })
      : edge("showroom", "disk", { sourceHandle: "mnt-disk-right" });
    expect(sanitizeShowroomTopology(nodes, [mount]).edges).toEqual([]);
  });
  it("retains valid mounts and unrelated or dangling edges", () => {
    const nodes = [showroom({ infraNetworking: true, nics: [] }), node("disk", "storageNode"), network()];
    const edges = [edge("showroom", "disk", { sourceHandle: "mnt-disk-left" }),
      edge("missing", "lab"), edge("disk", "lab")];
    const result = sanitizeShowroomTopology(nodes, edges);
    expect(result.nodes).toBe(nodes);
    expect(result.edges).toBe(edges);
  });
  it("preserves references when no showroom exists", () => {
    const nodes = [network()];
    const edges = [edge("missing", "lab")];
    expect(sanitizeShowroomTopology(nodes, edges)).toEqual({ nodes, edges });
    expect(ensureShowroomGatewayEdge(nodes, edges)).toBe(edges);
    expect(ensureShowroomGatewayEdge([showroom()], edges)).toBe(edges);
  });
  it("creates and then normalizes a single cosmetic gateway connection", () => {
    const nodes = [showroom(), gateway()];
    const canonical = buildShowroomGatewayEdge("showroom", "gateway");
    expect(ensureShowroomGatewayEdge(nodes, [])).toEqual([canonical]);
    expect(ensureShowroomGatewayEdge(nodes, [canonical])).toEqual([canonical]);
    expect(isShowroomGatewayEdge(canonical)).toBe(true);
    expect(isShowroomGatewayEdge({ ...canonical, sourceHandle: canonical.targetHandle, targetHandle: canonical.sourceHandle })).toBe(true);
    expect(isShowroomGatewayEdge(edge("a", "b"))).toBe(false);
    const legacy = edge("gateway", "showroom");
    const unrelated = edge("a", "b");
    expect(ensureShowroomGatewayEdge(nodes, [legacy, unrelated]))
      .toEqual([{ ...canonical, id: legacy.id }, unrelated]);
  });
});

describe("showroom deployment readiness", () => {
  it("requires a showroom", () => {
    expect(getShowroomReadiness([], [])).toEqual({ hasContentRepo: false, hasGateway: false, issues: ["Add a showroom to the canvas"] });
  });
  it("reports missing content, gateway, and DNS together", () => {
    expect(getShowroomReadiness([showroom({ contentRepo: " " })], []).issues).toEqual([
      "Content repo URL is required when build at deploy is enabled",
      "Add a gateway to the project for external showroom access",
      "Enable DNS on at least one network connected to the gateway",
    ]);
  });
  it("allows an existing content image without a repository", () => {
    const nodes = [showroom({ contentRepo: "", buildContent: false }), gateway(), network()];
    expect(isShowroomReady(nodes, [edge("gateway", "lab")])).toBe(true);
  });
  it("rejects a gateway that blocks DNS upstream", () => {
    const nodes = [showroom({ dnsNetwork: "Lab" }), gateway({ outboundPolicy: "restrict", outboundPorts: "443" }), network()];
    expect(getShowroomReadiness(nodes, [edge("gateway", "lab")]).issues)
      .toEqual(["Gateway outbound must allow DNS (port 53) so showroom can resolve external names"]);
  });
  it("reports an explicit network that no longer exists", () => {
    expect(getShowroomReadiness([showroom({ dnsNetwork: "gone" }), gateway()], []).issues)
      .toEqual(["DNS network 'gone' was not found on the canvas"]);
  });
  it.each([
    [{ dns: false }, "must have DNS enabled"],
    [{ cidr: "" }, "must have a CIDR"],
    [{}, "must be connected to the gateway"],
  ])("validates explicit DNS network configuration: %s", (data, message) => {
    expect(getShowroomReadiness([showroom({ dnsNetwork: "Lab" }), gateway(), network(data)], []).issues)
      .toEqual([`DNS network 'Lab' ${message}`]);
  });
  it("resolves a network by label and accepts an explicit DNS egress rule", () => {
    const nodes = [showroom({ dnsNetwork: "Lab" }), gateway({ outboundPolicy: "restrict", outboundPorts: "53,443" }),
      network({ name: undefined, label: "Lab" })];
    expect(isShowroomReady(nodes, [edge("lab", "gateway")])).toBe(true);
  });
});
