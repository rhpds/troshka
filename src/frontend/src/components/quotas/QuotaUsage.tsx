"use client";
import { Alert } from "@patternfly/react-core";
import { currency, giB, QuotaUsageData, spendingThreshold } from "@/lib/quotas";
import BillingTime from "@/components/billing/BillingTime";
import styles from "./quotas.module.css";

const labels = { cpu: "CPU", ram_bytes: "RAM (GiB)", storage_bytes: "Physical storage (GiB)", projects: "Deployed projects" };
export default function QuotaUsage({ usage }: { usage: QuotaUsageData }) {
  const threshold = usage.spent_usd === null ? 0 : spendingThreshold(usage.spent_usd, usage.limits.spending_usd);
  const pendingAction = ["pending", "issued", "failed"].includes(usage.action_state || "") || (usage.action_counts.pending || usage.action_counts.issued || usage.action_counts.failed || 0) > 0;
  const awaitingEvaluation = usage.spending_state === "budget_exceeded_shutdown_pending" && usage.action_state === null && Object.keys(usage.action_counts).length === 0;
  const storageExhausted = usage.storage_state === "storage_exceeded_shutdown_pending";
  const actionLabel = storageExhausted || usage.action_reasons?.some(reason => reason.startsWith("physical_")) ? "Storage quota" : "Budget";
  return <section className={styles.usage} aria-label={`${usage.scope} quota usage`}>
    <div className={styles.heading}><div className={styles.status}>
      <span className={styles.badge}>{usage.enforcement_enabled ? `Quota enforcement enabled (${usage.enforcement_status})` : "Enforcement disabled"}</span>
      <span className={styles.badge}>Baseline: {usage.baseline_ready ? "ready" : "not ready"}</span>
    </div><span className={styles.muted}>{usage.cost_center_id ? `Cost center: ${usage.cost_center_id}` : "Cost center not assigned"}</span></div>
    <div className={styles.overview}><div className={styles.resourcePanel} role="region" aria-label="Resource quotas" tabIndex={0}>
    <h3>Resource usage</h3>
    <table className={styles.table} aria-label="Resource quota usage"><thead><tr><th scope="col">Resource</th><th scope="col">Used</th><th scope="col">Reserved</th><th scope="col">Limit</th></tr></thead><tbody>
      {(Object.keys(labels) as (keyof typeof labels)[]).map(key => {
        const display = (value: number | string) => key.endsWith("_bytes") ? giB(value) : String(value);
        return <tr key={key}><th scope="row">{labels[key]}</th><td>{key === "storage_bytes" && usage.storage_accounting_status !== "ready" ? "Unavailable" : display(usage.used[key])}</td><td>{display(usage.reserved[key])}</td><td>{usage.limits[key] === null ? "Unlimited" : display(usage.limits[key])}</td></tr>;
      })}
    </tbody></table>
    <p className={styles.muted}>Provisioned disk capacity: {usage.provisioned_storage_bytes == null ? "Unavailable" : `${giB(usage.provisioned_storage_bytes)} GiB`}. Billing continues to use provisioned disk capacity.</p>
    <p className={styles.muted}>Shared bases on measured pools (platform storage, counted once per pool): {usage.platform_storage_bytes == null ? "Unavailable" : `${giB(usage.platform_storage_bytes)} GiB`}.</p>
    {usage.storage_as_of && <p className={styles.muted}>Storage observed <BillingTime value={usage.storage_as_of} /> UTC</p>}
    </div>
    <div className={styles.spending}><h3>{usage.scope === "project" ? "Lifetime spending" : "Monthly spending (UTC)"}</h3>
      <p className={styles.amount}>{usage.spent_usd === null ? "Unavailable" : currency(usage.spent_usd)}</p>
      <p className={styles.muted}>Spending limit: {usage.limits.spending_usd === null ? "Unlimited" : currency(usage.limits.spending_usd)}</p>
      {usage.reset_at && <p className={styles.muted}>Resets <BillingTime value={usage.reset_at} /> UTC</p>}
      <p className={styles.muted}>{usage.as_of ? <>Accounting as of <BillingTime value={usage.as_of} /> UTC</> : "Accounting as of: unavailable"}</p>
    </div></div>
    {usage.accounting_status === "unavailable" && <Alert isInline variant={usage.enforcement_enabled ? "warning" : "info"} title={usage.enforcement_enabled ? "Accounting unavailable. New growth requires reliable accounting." : "Accounting unavailable. Quota enforcement is disabled."} />}
    {threshold > 0 && <Alert isInline variant={threshold === 100 ? "danger" : "warning"} title={threshold === 100 ? "Spending allowance exhausted" : `${threshold}% of spending allowance reached`} />}
    {usage.storage_accounting_status !== "ready" && <Alert isInline variant="info" title="Physical storage observation unavailable. Missing evidence is never counted as zero." />}
    {storageExhausted && <Alert isInline variant="danger" title="Physical storage allowance or pool headroom exhausted" />}
    {usage.enforcement_enabled && (pendingAction || awaitingEvaluation) && <Alert isInline variant="warning" title={`${actionLabel} exceeded—shutdown pending`} />}
    {(usage.action_state === "failed" || (usage.action_counts.failed || 0) > 0) && <Alert isInline variant="danger" title={`${actionLabel} shutdown failed; retry pending`} />}
    {(usage.action_state === "completed" || (usage.action_counts.completed || 0) > 0) && <p>{actionLabel} shutdown completed. Compute never restarts automatically.</p>}
    <details className={styles.details}><summary>Enforcement and supported operations</summary><div className={styles.explanation}>
      {!usage.enforcement_enabled && <p>Saved hierarchy policies apply after enablement; existing project spending guardrails remain in place.</p>}
      <p>Current enforcement supports basic single-host nested VM whole-project operations on an existing shared billing host. Native providers, BMC, containers, OpenShift, snapshots and live growth are not supported while enforcement is enabled. Stop and cleanup remain available.</p>
      <p>Physical storage counts allocated filesystem blocks for project disks and retained files, even when stopped. Only local filesystem qcow2 storage with prewarmed shared bases is supported. Shared filesystem pools require a separate adapter. Guest disk growth is checked periodically; shutdown is best effort and writes may overshoot between polls. This is not a filesystem hard cap. Exhaustion preserves stored data and never restarts compute automatically.</p>
    </div></details>
  </section>;
}
