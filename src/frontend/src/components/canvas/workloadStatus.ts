/** Helpers for the project-page workload status chip. */

export interface WorkloadRunSummary {
  id: string;
  role_fqcn: string | null;
  status: string;
  created_at: string;
  error?: string | null;
}

const INFLIGHT = new Set(["pending", "queued", "running"]);
const FAILED = new Set(["error", "timeout"]);

export function shortRoleLabel(roleFqcn: string | null | undefined): string {
  if (!roleFqcn) return "";
  const parts = roleFqcn.split(".");
  return parts[parts.length - 1] || roleFqcn;
}

/** Normalize template workload entries (string or {role|name}) to FQCN list. */
export function normalizeWorkloadRoles(workloads: unknown[] | null | undefined): string[] {
  const out: string[] = [];
  for (const entry of workloads || []) {
    if (typeof entry === "string" && entry.trim()) {
      out.push(entry.trim());
      continue;
    }
    if (entry && typeof entry === "object") {
      const rec = entry as Record<string, unknown>;
      const name = rec.name || rec.role || rec.role_fqcn;
      if (typeof name === "string" && name.trim()) out.push(name.trim());
    }
  }
  return out;
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

/**
 * Newest failed/timeout run for the next unfinished chain role.
 * Used to show Workload N/M with Retry after an interrupt/failure.
 */
export function findFailedChainRun(
  runs: WorkloadRunSummary[],
  chainRoles: string[],
): WorkloadRunSummary | null {
  const succeeded = succeededRoleSet(runs);
  const nextRole = chainRoles.find((r) => !succeeded.has(r));
  if (!nextRole) return null;
  const failed = runs.filter(
    (r) => r.role_fqcn === nextRole && FAILED.has(r.status),
  );
  if (!failed.length) return null;
  return failed.reduce((a, b) =>
    (a.created_at || "") >= (b.created_at || "") ? a : b,
  );
}

export function formatWorkloadChipLabel(opts: {
  roleFqcn: string | null | undefined;
  chainRoles: string[] | null | undefined;
  succeededRoles: Set<string>;
}): { headline: string; detail: string } {
  const detail = shortRoleLabel(opts.roleFqcn) || "workload";
  const chain = opts.chainRoles?.filter(Boolean) ?? [];
  const idx = opts.roleFqcn ? chain.indexOf(opts.roleFqcn) : -1;
  if (idx >= 0 && chain.length > 0) {
    return { headline: `Workload ${idx + 1}/${chain.length}`, detail };
  }
  return { headline: "Workload", detail };
}

/** Succeeded FQCNs from a run list (for chain progress). */
export function succeededRoleSet(runs: WorkloadRunSummary[]): Set<string> {
  const out = new Set<string>();
  for (const r of runs) {
    if (r.status === "succeeded" && r.role_fqcn) out.add(r.role_fqcn);
  }
  return out;
}
