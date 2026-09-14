import { describe, it, expect } from "vitest";
import type { ClusterConfig } from "@/stores/canvasStore";
import {
  clusterHasDeferredWorkers,
  deriveStages,
  parseWorkerReady,
  stagesFor,
} from "@/components/canvas/ClusterInstallLogModal";

const snoWithWorkers: ClusterConfig = {
  id: "source",
  nodeId: "cluster-source",
  name: "source",
  type: "sno",
  controlPlane: 1,
  workers: 2,
};

describe("ClusterInstallLogModal install stages", () => {
  it("includes worker join stages for SNO + workers from cluster config", () => {
    const labels = stagesFor("", snoWithWorkers).map((s) => s.label);
    expect(labels).toContain("Joining worker nodes");
    expect(labels).toContain("Worker nodes ready");
    expect(labels[labels.length - 1]).toBe("Worker nodes ready");
  });

  it("does not include worker join stages for plain SNO", () => {
    const labels = stagesFor(
      "",
      { ...snoWithWorkers, workers: 0 },
    ).map((s) => s.label);
    expect(labels).not.toContain("Joining worker nodes");
    expect(labels[labels.length - 1]).toBe("Install complete");
  });

  it("progresses through worker join after SNO install complete", () => {
    const log = [
      "[source] install complete",
      "[source] control-plane-usable",
      "[source] joining 2 deferred worker(s)",
      "[source] node-image create for source-worker-0",
      "Node ISO URL: http://10.0.0.5:8181/node.iso",
      "[source] worker nodes Ready: 1/2",
    ].join("\n");

    const stages = deriveStages(log, snoWithWorkers);
    const byLabel = Object.fromEntries(stages.map((s) => [s.label, s.state]));

    expect(byLabel["Install complete"]).toBe("done");
    expect(byLabel["Joining worker nodes"]).toBe("done");
    expect(byLabel["Building worker ISO"]).toBe("done");
    expect(byLabel["Booting worker nodes"]).toBe("done");
    expect(byLabel["Worker nodes ready"]).toBe("active");
  });

  it("marks complete only after deferred workers joined", () => {
    const log = [
      "[source] install complete",
      "[source] joining 2 deferred worker(s)",
      "[source] deferred workers joined",
    ].join("\n");

    const stages = deriveStages(log, snoWithWorkers);
    expect(stages[stages.length - 1]).toEqual({
      label: "Worker nodes ready",
      state: "done",
    });
  });

  it("parses worker Ready count from log breadcrumbs", () => {
    const log = "[source] worker nodes Ready: 1/2\n[source] worker nodes Ready: 2/2";
    expect(parseWorkerReady(log)).toEqual({ ready: 2, expected: 2 });
  });

  it("detects deferred workers from cluster shape", () => {
    expect(clusterHasDeferredWorkers(snoWithWorkers)).toBe(true);
    expect(clusterHasDeferredWorkers({ ...snoWithWorkers, workers: 0 })).toBe(false);
    expect(clusterHasDeferredWorkers({ ...snoWithWorkers, type: "standard" })).toBe(false);
  });
});
