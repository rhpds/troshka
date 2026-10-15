import React from "react";
import { afterEach, describe, it, expect, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import type { ClusterConfig } from "@/stores/canvasStore";
import {
  clusterHasDeferredWorkers,
  deriveStages,
  installLogIndicatesFailure,
  installLogIndicatesStuck,
  parseWorkerReady,
  workerStageLabel,
  stagesFor,
  ClusterInstallLogPanel,
} from "@/components/canvas/ClusterInstallLogModal";

describe("ClusterInstallLogPanel project identity", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    document.title = "";
  });

  function mockProject(project: Record<string, unknown>) {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => ({
      ok: true,
      json: async () => String(input).includes("/ocp/install-log")
        ? { output: "", cluster_status: "ready" }
        : project,
    }));
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  it("shows canonical project and cluster names in standalone header and browser title", async () => {
    const fetchMock = mockProject({ name: "Migration lab 42", topology: { clusters: [{ ...snoWithWorkers, name: "source" }], nodes: [] } });
    render(<ClusterInstallLogPanel projectId="project-42" clusterKey="source" clusterName="Old name hint" standalone />);
    const heading = await screen.findByRole("heading", { name: /^Migration lab 42\s*:\s*source/ });
    expect(heading).toHaveTextContent("Migration lab 42: source");
    expect(heading).not.toHaveTextContent("☸");
    expect(heading).not.toHaveTextContent("Status & Log");
    const project = within(heading).getByText("Migration lab 42");
    const cluster = within(heading).getByText("source");
    expect(project).not.toBe(cluster);
    expect(project.parentElement).toBe(cluster.parentElement);
    await waitFor(() => expect(document.title).toBe("Migration lab 42: source — Status & Log"));
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/projects/project-42");
  });

  it("resolves names for old popout URLs without a cluster-name hint", async () => {
    mockProject({ name: "Tenant B", topology: { clusters: [{ ...snoWithWorkers, id: "cluster-key", name: "destination" }], nodes: [] } });
    render(<ClusterInstallLogPanel projectId="project-b" clusterKey="cluster-key" clusterName="" standalone />);
    expect(await screen.findByRole("heading", { name: /^Tenant B\s*:\s*destination/ })).toBeInTheDocument();
    await waitFor(() => expect(document.title).toBe("Tenant B: destination — Status & Log"));
  });

  it("uses stable ID fallbacks when project or cluster names are missing", async () => {
    const fetchMock = mockProject({ topology: { clusters: [], nodes: [] } });
    render(<ClusterInstallLogPanel projectId="project-c" clusterKey="cluster-c" clusterName="" standalone />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/v1/projects/project-c"));
    expect(screen.getByRole("heading", { name: /^project-c\s*:\s*cluster-c/ })).toBeInTheDocument();
    expect(document.title).toBe("project-c: cluster-c — Status & Log");
  });

  it("adds the project name to modal headers without changing the canvas browser title", async () => {
    document.title = "Project canvas";
    mockProject({ name: "Tenant D" });
    render(<ClusterInstallLogPanel projectId="project-d" clusterKey="source" clusterName="source" cluster={snoWithWorkers} nodes={[]} />);
    expect(await screen.findByRole("heading", { name: /^Tenant D\s*:\s*source/ })).toBeInTheDocument();
    expect(document.title).toBe("Project canvas");
  });
});

const snoWithWorkers: ClusterConfig = {
  id: "source",
  nodeId: "cluster-source",
  name: "source",
  type: "sno",
  controlPlane: 1,
  workers: 2,
};

describe("ClusterInstallLogModal install stages", () => {
  it("keeps cluster initialization active until operator readiness begins", () => {
    const bootstrap = "INFO Bootstrap Kube API Initialized";
    const initializing = `${bootstrap}\nINFO Still waiting for the cluster to initialize: Working towards 4.22.17: 67 of 1023 done (6% complete)`;
    const operators = `${initializing}\nINFO Cluster operators console, monitoring are not available`;
    const complete = `${operators}\nINFO Install complete`;
    const progressLabels = ["Bootstrap Kube API", "Waiting for the cluster to initialize", "Cluster operators", "Install complete"];
    for (const [log, states] of [
      [bootstrap, ["active", "pending", "pending", "pending"]],
      [initializing, ["done", "active", "pending", "pending"]],
      [operators, ["done", "done", "active", "pending"]],
      [complete, ["done", "done", "done", "done"]],
    ] as const) {
      const stages = deriveStages(log);
      expect(stages.filter((s) => progressLabels.includes(s.label))).toEqual(
        progressLabels.map((label, index) => ({ label, state: states[index] })),
      );
    }
  });

  it("does not advance to operators from initialization failure details", () => {
    const log = "INFO Still waiting for the cluster to initialize: Could not update: Cluster operators monitoring are not available";
    const stages = deriveStages(log, snoWithWorkers);
    expect(stages.find((s) => s.label === "Waiting for the cluster to initialize")?.state).toBe("active");
    expect(stages.find((s) => s.label === "Cluster operators")?.state).toBe("pending");
    expect(stages.find((s) => s.label === "Joining worker nodes")?.state).toBe("pending");
  });

  it.each([
    "Working towards 4.20.0: 20 of 100 done (20% complete)",
    "Could not update cluster version",
    "Cluster operators console, monitoring are not available",
  ])("preserves operator progress for older logs: %s", (log) => {
    const stages = deriveStages(log);
    expect(stages.find((s) => s.label === "Waiting for the cluster to initialize")?.state).toBe("done");
    expect(stages.find((s) => s.label === "Cluster operators")?.state).toBe("active");
  });

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

  it("describes convergence as stabilizing until it has finished", () => {
    expect(workerStageLabel("Worker nodes converged", "active")).toBe("Workers stabilizing");
    expect(workerStageLabel("Worker nodes converged", "done")).toBe("Workers ready");
    expect(parseWorkerReady("[source] workers stabilizing: 2/2 currently Ready (stable checks 1/3)"))
      .toEqual({ ready: 2, expected: 2 });
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
