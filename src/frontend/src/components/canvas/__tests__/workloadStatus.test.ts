import { describe, it, expect } from "vitest";
import {
  shortRoleLabel,
  findInflightRun,
  formatWorkloadChipLabel,
  type WorkloadRunSummary,
} from "../workloadStatus";

describe("shortRoleLabel", () => {
  it("returns the last FQCN segment", () => {
    expect(shortRoleLabel("rhpds.demo_workloads.troshka_workload_cclm_operators")).toBe(
      "troshka_workload_cclm_operators",
    );
  });

  it("returns the whole string when there is no dot", () => {
    expect(shortRoleLabel("operators")).toBe("operators");
  });

  it("handles empty", () => {
    expect(shortRoleLabel("")).toBe("");
    expect(shortRoleLabel(null)).toBe("");
  });
});

describe("findInflightRun", () => {
  const runs: WorkloadRunSummary[] = [
    {
      id: "r-done",
      role_fqcn: "a.role.one",
      status: "succeeded",
      created_at: "2026-09-24T10:00:00Z",
    },
    {
      id: "r-run",
      role_fqcn: "a.role.two",
      status: "running",
      created_at: "2026-09-24T10:05:00Z",
    },
    {
      id: "r-old",
      role_fqcn: "a.role.zero",
      status: "pending",
      created_at: "2026-09-24T09:00:00Z",
    },
  ];

  it("prefers the newest inflight run", () => {
    expect(findInflightRun(runs)?.id).toBe("r-run");
  });

  it("returns null when none are inflight", () => {
    expect(findInflightRun([{ ...runs[0] }])).toBeNull();
  });

  it("treats queued and pending as inflight", () => {
    expect(
      findInflightRun([
        { id: "q", role_fqcn: "x", status: "queued", created_at: "2026-09-24T11:00:00Z" },
      ])?.id,
    ).toBe("q");
  });
});

describe("formatWorkloadChipLabel", () => {
  const chain = [
    "rhpds.demo_workloads.troshka_workload_cclm_operators",
    "rhpds.demo_workloads.troshka_workload_cclm_network",
    "rhpds.demo_workloads.troshka_workload_cclm_hco",
  ];

  it("shows N/M when role is in the template chain", () => {
    const label = formatWorkloadChipLabel({
      roleFqcn: chain[1],
      chainRoles: chain,
      succeededRoles: new Set([chain[0]]),
    });
    expect(label).toBe("Workload 2/3 · troshka_workload_cclm_network");
  });

  it("omits fraction when role is not in the chain", () => {
    const label = formatWorkloadChipLabel({
      roleFqcn: "some.other.role",
      chainRoles: chain,
      succeededRoles: new Set(),
    });
    expect(label).toBe("Workload · role");
  });

  it("omits fraction when there is no chain", () => {
    const label = formatWorkloadChipLabel({
      roleFqcn: "a.b.operators",
      chainRoles: null,
      succeededRoles: new Set(),
    });
    expect(label).toBe("Workload · operators");
  });
});
