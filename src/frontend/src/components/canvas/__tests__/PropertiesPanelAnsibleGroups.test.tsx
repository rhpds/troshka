import { describe, it, expect, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { useCanvasStore } from "@/stores/canvasStore";
import PropertiesPanel from "@/components/canvas/PropertiesPanel";

function seedStore(nodeData: Record<string, unknown>) {
  useCanvasStore.setState({
    nodes: [{ id: "vm-1", type: "vmNode", position: { x: 0, y: 0 }, data: nodeData }],
    edges: [],
    selectedNodeId: "vm-1",
    projectState: "draft",
    clusters: [],
    deployedNodeData: {},
    deployedEdgeKey: "",
    deployedExternalIps: "[]",
    deployedClusters: "[]",
    externalIps: [],
  } as never);
}

describe("PropertiesPanel Ansible Groups field", () => {
  beforeEach(() => seedStore({ name: "vm-1", os: "rhel9" }));

  it("writes AnsibleGroup into node data.tags", () => {
    render(<PropertiesPanel />);
    const input = screen.getByPlaceholderText("e.g. bastions, webservers") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "bastions, web" } });
    const node = useCanvasStore.getState().nodes.find((n) => n.id === "vm-1")!;
    expect(((node.data as Record<string, any>).tags || {}).AnsibleGroup).toBe("bastions, web");
  });

  it("shows existing AnsibleGroup value", () => {
    seedStore({ name: "vm-1", os: "rhel9", tags: { AnsibleGroup: "db" } });
    render(<PropertiesPanel />);
    const input = screen.getByPlaceholderText("e.g. bastions, webservers") as HTMLInputElement;
    expect(input.value).toBe("db");
  });
});
