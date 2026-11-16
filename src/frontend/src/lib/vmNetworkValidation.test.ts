import { describe, expect, it } from "vitest";
import type { Edge, Node } from "@xyflow/react";
import { vmNetworkIssues } from "./vmNetworkValidation";

const nodes: Node[] = [
  { id: "vm", type: "vmNode", position: { x: 0, y: 0 }, data: {
    name: "bastion", nics: [{ id: "nic-one", name: "eth0" }, { id: "nic-two", name: "eth1" }],
  } },
  { id: "net", type: "networkNode", position: { x: 0, y: 0 }, data: {} },
];
const edge: Edge = { id: "edge", source: "net", target: "vm", targetHandle: "nic-nic-one-top" };

describe("VM network validation", () => {
  it("identifies the unused NIC even when another NIC is connected", () => {
    expect(vmNetworkIssues(nodes, [edge])).toEqual([
      expect.objectContaining({ vmId: "vm", nicId: "nic-two", message: expect.stringContaining("eth1") }),
    ]);
  });
  it("accepts reverse edges and bottom handles", () => {
    expect(vmNetworkIssues(nodes, [edge, { id: "other", source: "vm", target: "net", sourceHandle: "nic-nic-two-bottom" }])).toEqual([]);
  });
  it("rejects stale edges and edges on another VM", () => {
    expect(vmNetworkIssues(nodes, [{ ...edge, source: "deleted" }])).toHaveLength(2);
    expect(vmNetworkIssues(nodes, [{ ...edge, target: "other-vm" }])).toHaveLength(2);
  });
});
