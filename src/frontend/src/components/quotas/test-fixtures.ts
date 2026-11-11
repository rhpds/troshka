import type { QuotaUsageData } from "@/lib/quotas";
export const exampleUsage: QuotaUsageData = {
  scope: "user", scope_id: "u", limits: { cpu: 0, ram_bytes: null, storage_bytes: null, projects: null, spending_usd: "100.123456789012" },
  used: { cpu: 2, ram_bytes: 1073741824, storage_bytes: 0, projects: 1 },
  reserved: { cpu: 1, ram_bytes: 0, storage_bytes: 0, projects: 0 },
  spent_usd: "80.123456789012", as_of: "2026-11-11T12:00:00Z", reset_at: "2026-12-01T00:00:00Z",
  accounting_status: "ready", enforcement_status: "disabled", enforcement_enabled: false, baseline_ready: false,
  can_edit: true, cost_center_id: null, policy_revision: 1, spending_state: "within_budget", action_state: null, action_counts: {}, cause_ids: [],
};
