"use client";
import { useEffect, useRef, useState } from "react";
import { Alert, Button } from "@patternfly/react-core";
import { bytesFromGiB, decimalCompare, giB, integerText, isQuotaRevocation, Limits, QuotaUsageData, saveLimits } from "@/lib/quotas";
import styles from "./quotas.module.css";

const labels = { cpu: "CPU limit", ram_bytes: "RAM limit (GiB)", storage_bytes: "Physical storage limit (GiB)", projects: "Deployed-project limit", spending_usd: "Spending limit (USD)" };
function drafts(limits: Limits) {
  return Object.fromEntries(Object.entries(limits).map(([key, value]) => [key, value === null ? "" : key.endsWith("_bytes") ? giB(value) : String(value)])) as Record<keyof Limits, string>;
}
export default function QuotaEditor({ usage, onSaved, onDenied }: { usage: QuotaUsageData; onSaved: () => void | Promise<void>; onDenied?: () => void }) {
  const [draft, setDraft] = useState(() => drafts(usage.limits));
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const live = useRef(true);
  useEffect(() => { live.current = true; return () => { live.current = false; }; }, []);
  async function save() {
    if (!usage.can_edit || busy) return;
    setBusy(true); setError("");
    try {
      const limits = {} as Limits;
      for (const key of Object.keys(labels) as (keyof Limits)[]) {
        const value = draft[key].trim();
        if (key === "spending_usd") { if (value) decimalCompare(value, "0"); limits[key] = value || null; }
        else limits[key] = !value ? null : key.endsWith("_bytes") ? bytesFromGiB(value) : integerText(value);
      }
      await saveLimits(usage.scope, usage.scope_id, limits);
      if (live.current) await onSaved();
    } catch (e) { if (live.current) { setError((e as Error).message); if (isQuotaRevocation(e)) onDenied?.(); } }
    finally { if (live.current) setBusy(false); }
  }
  return <form className={styles.editor} onSubmit={event => { event.preventDefault(); void save(); }} aria-label={`${usage.scope} quota editor`}>
    <p className={styles.help}>Leave a limit blank for unlimited. Set zero to prohibit consumption when enforcement is enabled. Parent limits still apply.</p>
    {error && <Alert isInline variant="danger" title={error} />}
    <div className={styles.fields}>{(Object.keys(labels) as (keyof Limits)[]).map(key => <label key={key} className={styles.field}>{labels[key]}<input aria-label={labels[key]} placeholder="Unlimited" inputMode="decimal" disabled={!usage.can_edit || busy} value={draft[key]} onChange={event => setDraft(current => ({ ...current, [key]: event.target.value }))} /></label>)}</div>
    <div className={styles.policyNote}>
    <p>CPU, RAM and deployed-project reductions below used + reserved block growth and leave existing environments running.</p>
    <p>A physical storage limit at or below measured usage blocks growth and triggers compute shutdown when enforcement is enabled, including when an administrator lowers the limit. Stored data is preserved. Organization or user exhaustion stops the affected scope’s environments; freeing storage or raising the cap never restarts them automatically.</p>
    <p>{usage.scope === "project" ? "A spending limit at or below lifetime spend triggers compute shutdown when enforcement is enabled. Existing positive project spending caps retain their current safeguards even with hierarchy enforcement disabled." : "A spending limit at or below current UTC-month spend triggers compute shutdown when enforcement is enabled."} Retained storage can continue accruing charges.</p>
    {usage.scope === "project" && <p>A deployed-project limit of zero disables deployment; one enables a single environment.</p>}
    </div>
    <Button type="submit" isDisabled={!usage.can_edit || busy} isLoading={busy}>Save limits</Button>
  </form>;
}
