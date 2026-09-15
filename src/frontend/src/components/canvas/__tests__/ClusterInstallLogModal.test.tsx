import { describe, it, expect } from "vitest";
import type { ClusterConfig } from "@/stores/canvasStore";
import {
  clusterHasDeferredWorkers,
  deriveStages,
  installLogIndicatesFailure,
  installLogIndicatesStuck,
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
    expect(labels).toContain("Worker nodes joined");
    expect(labels).toContain("Worker nodes converged");
    expect(labels[labels.length - 1]).toBe("Worker nodes converged");
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
      "[source] deferred workers Ready: 1/2",
    ].join("\n");

    const stages = deriveStages(log, snoWithWorkers);
    const byLabel = Object.fromEntries(stages.map((s) => [s.label, s.state]));

    expect(byLabel["Install complete"]).toBe("done");
    expect(byLabel["Joining worker nodes"]).toBe("done");
    expect(byLabel["Building worker ISO"]).toBe("done");
    expect(byLabel["Booting worker nodes"]).toBe("done");
    expect(byLabel["Worker nodes joined"]).toBe("active");
    expect(byLabel["Worker nodes converged"]).toBe("pending");
  });

  it("marks joined done and converged active after workers join", () => {
    const log = [
      "[source] install complete",
      "[source] joining 2 deferred worker(s)",
      "[source] deferred workers joined",
    ].join("\n");

    const stages = deriveStages(log, snoWithWorkers);
    const byLabel = Object.fromEntries(stages.map((s) => [s.label, s.state]));
    expect(byLabel["Worker nodes joined"]).toBe("done");
    expect(byLabel["Worker nodes converged"]).toBe("active");
  });

  it("marks converged done when cluster status is ready (pre-convergence installs)", () => {
    const log = [
      "[source] install complete",
      "[source] joining 2 deferred worker(s)",
      "[source] deferred workers joined",
    ].join("\n");

    const stages = deriveStages(log, snoWithWorkers, false, "ready");
    const byLabel = Object.fromEntries(stages.map((s) => [s.label, s.state]));
    expect(byLabel["Worker nodes joined"]).toBe("done");
    expect(byLabel["Worker nodes converged"]).toBe("done");
  });

  it("marks complete only after deferred workers converged", () => {
    const log = [
      "[source] install complete",
      "[source] joining 2 deferred worker(s)",
      "[source] deferred workers joined",
      "[source] deferred workers converged",
    ].join("\n");

    const stages = deriveStages(log, snoWithWorkers);
    expect(stages[stages.length - 1]).toEqual({
      label: "Worker nodes converged",
      state: "done",
    });
  });

  it("parses worker Ready count from log breadcrumbs", () => {
    const log = "[source] deferred workers Ready: 1/2\n[source] deferred workers Ready: 2/2";
    expect(parseWorkerReady(log)).toEqual({ ready: 2, expected: 2 });
  });

  it("detects deferred workers from cluster shape", () => {
    expect(clusterHasDeferredWorkers(snoWithWorkers)).toBe(true);
    expect(clusterHasDeferredWorkers({ ...snoWithWorkers, workers: 0 })).toBe(false);
    expect(clusterHasDeferredWorkers({ ...snoWithWorkers, type: "standard" })).toBe(false);
  });

  it("detects worker join pod errors as failure", () => {
    const log = [
      "[source] install complete",
      "[source] node-image create for source-worker-0",
      'error: cannot create pod: pods "node-joiner-" is forbidden',
      "[source] node-image create failed for source-worker-0 (attempt 1), retrying in 60s...",
    ].join("\n");

    expect(installLogIndicatesFailure(log)).toBe(true);
    const stages = deriveStages(log, snoWithWorkers, true);
    expect(stages.find((s) => s.label === "Building worker ISO")?.state).toBe("failed");
    expect(stages.find((s) => s.label === "Install complete")?.state).toBe("done");
  });

  it("detects failed restart breadcrumb", () => {
    const log =
      "[source] post-boot restart: wiping boot disks\n[source] restart failed: Boot disk wipe failed";
    expect(installLogIndicatesFailure(log)).toBe(true);
  });

  it("treats worker join retry breadcrumbs as still in progress", () => {
    const log =
      "[source] node-image create failed for source-worker-0 (attempt 1), retrying in 60s...";
    expect(installLogIndicatesFailure(log)).toBe(false);
  });

  it("does not mark stuck during create-image or early node boot", () => {
    const createImage = [
      "[source] starting agent-based install",
      "Reusing previously-fetched Kubeadmin Password",
      "Generated ISO at agent.x86_64.iso",
    ].join("\n");
    expect(installLogIndicatesStuck(createImage)).toBe(false);

    const earlyWaitFor = [
      "Waiting for cluster installation to complete...",
      ...Array.from(
        { length: 8 },
        () => "Waiting for cluster install to initialize. Sleeping for 30 seconds",
      ),
    ].join("\n");
    expect(installLogIndicatesStuck(earlyWaitFor)).toBe(false);
  });

  it("marks stuck only after prolonged wait-for init loop", () => {
    const log = [
      "Waiting for cluster installation to complete...",
      ...Array.from(
        { length: 15 },
        () => "Waiting for cluster install to initialize. Sleeping for 30 seconds",
      ),
    ].join("\n");
    expect(installLogIndicatesStuck(log)).toBe(true);
  });

  it("marks the in-progress stage failed instead of active on error", () => {
    const log = [
      "Fetching image from OCP release",
      "Generated ISO",
      "Serving via HTTP on port 8080",
      "Waiting for cluster install to initialize",
    ].join("\n");

    const active = deriveStages(log, snoWithWorkers);
    expect(active.find((s) => s.label === "Net booting node")?.state).toBe("active");

    const errored = deriveStages(log, snoWithWorkers, true);
    expect(errored.find((s) => s.label === "Net booting node")?.state).toBe("failed");
    expect(errored.find((s) => s.label === "Agent ISO ready")?.state).toBe("done");
  });
});
