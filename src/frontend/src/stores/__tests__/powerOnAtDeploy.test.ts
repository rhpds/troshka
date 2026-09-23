import { describe, it, expect, beforeEach } from "vitest";
import {
  resolvePowerOnAtDeploy,
  setVmPowerOnAtDeploy,
  useCanvasStore,
} from "@/stores/canvasStore";

describe("resolvePowerOnAtDeploy", () => {
  it("defaults true when unset", () => {
    expect(resolvePowerOnAtDeploy({})).toBe(true);
    expect(resolvePowerOnAtDeploy(undefined)).toBe(true);
  });

  it("defaults false for deferred OCP install members", () => {
    expect(resolvePowerOnAtDeploy({ deferOcpInstall: true })).toBe(false);
  });

  it("honors explicit powerOnAtDeploy", () => {
    expect(resolvePowerOnAtDeploy({ powerOnAtDeploy: false })).toBe(false);
    expect(
      resolvePowerOnAtDeploy({ deferOcpInstall: true, powerOnAtDeploy: true }),
    ).toBe(true);
  });
});

describe("setVmPowerOnAtDeploy", () => {
  beforeEach(() => {
    useCanvasStore.setState({
      nodes: [
        {
          id: "vm-1",
          type: "vmNode",
          position: { x: 0, y: 0 },
          data: { name: "worker-0", powerOnAtDeploy: false },
        },
      ],
      startOrder: [],
      deployedNodeData: {
        "vm-1": JSON.stringify({ name: "worker-0", powerOnAtDeploy: false }),
      },
      deployedEdgeKey: "x",
      topologyDirty: false,
    });
  });

  it("writes powerOnAtDeploy and marks topology dirty", () => {
    setVmPowerOnAtDeploy("vm-1", true);
    const state = useCanvasStore.getState();
    expect((state.nodes[0].data as { powerOnAtDeploy: boolean }).powerOnAtDeploy).toBe(
      true,
    );
    expect(state.topologyDirty).toBe(true);
    expect(state.startOrder.find((e) => e.vmId === "vm-1")?.autoStart).toBeUndefined();
  });

  it("keeps startOrder.autoStart aligned when disabling", () => {
    setVmPowerOnAtDeploy("vm-1", false);
    const entry = useCanvasStore.getState().startOrder.find((e) => e.vmId === "vm-1");
    expect(entry?.autoStart).toBe(false);
  });
});
