/** Helpers for the project-page workload status chip. */

export interface WorkloadRunSummary {
  id: string;
  role_fqcn: string | null;
  status: string;
  created_at: string;
}

const INFLIGHT = new Set(["pending", "queued", "running"]);

export function shortRoleLabel(roleFqcn: string | null | undefined): string {
  if (!roleFqcn) return "";
  const parts = roleFqcn.split(".");
  return parts[parts.length - 1] || roleFqcn;
}

/** Newest inflight run, or null. */
export function findInflightRun(
  runs: WorkloadRunSummary[],
): WorkloadRunSummary | null {
  const inflight = runs.filter((r) => INFLIGHT.has(r.status));
  if (!inflight.length) return null;
  return inflight.reduce((a, b) =>
    (a.created_at || "") >= (b.created_at || "") ? a : b,
  );
}

export function formatWorkloadChipLabel(opts: {
  roleFqcn: string | null | undefined;
  chainRoles: string[] | null | undefined;
  succeededRoles: Set<string>;
}): string {
  const short = shortRoleLabel(opts.roleFqcn) || "workload";
  const chain = opts.chainRoles?.filter(Boolean) ?? [];
  const idx = opts.roleFqcn ? chain.indexOf(opts.roleFqcn) : -1;
  if (idx >= 0 && chain.length > 0) {
    return `Workload ${idx + 1}/${chain.length} · ${short}`;
  }
  return `Workload · ${short}`;
}

/** Succeeded FQCNs from a run list (for chain progress). */
export function succeededRoleSet(runs: WorkloadRunSummary[]): Set<string> {
  const out = new Set<string>();
  for (const r of runs) {
    if (r.status === "succeeded" && r.role_fqcn) out.add(r.role_fqcn);
  }
  return out;
}
