"use client";
import { useEffect, useRef, useState } from "react";
import { Alert, Button } from "@patternfly/react-core";
import { Organization, quotaFetch } from "@/lib/quotas";

export default function OrganizationAssignment({ user, organizations, onChanged }: { user: { id: string; email: string; organization_id?: string | null }; organizations: Organization[]; onChanged: (message: string) => void }) {
  const [selected, setSelected] = useState(user.organization_id || ""), [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const live = useRef(true);
  useEffect(() => { live.current = true; return () => { live.current = false; }; }, []);
  async function assign() {
    if (busy || selected === (user.organization_id || "")) return;
    setBusy(true); setError("");
    try {
      const result = await quotaFetch<{ spending_state: string }>(`/users/${encodeURIComponent(user.id)}/organization`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ organization_id: selected || null }) });
      if (live.current) {
        const notice = result.spending_state === "budget_exceeded_shutdown_pending" ? "Budget exceeded—shutdown pending" : result.spending_state === "accounting_unavailable" ? "Organization saved. Accounting unavailable; readiness still requires verification." : "Organization assignment saved. Historical charges retain their original organization.";
        onChanged(notice);
      }
    } catch (e) { if (live.current) { setSelected(user.organization_id || ""); setError((e as Error).message); } }
    finally { if (live.current) setBusy(false); }
  }
  return <div><p>Organization: {organizations.find(org => org.id === user.organization_id)?.name || (user.organization_id ? user.organization_id : "Unassigned")}</p>
    <label>Organization <select aria-label={`Organization for ${user.email}`} disabled={busy} value={selected} onChange={e => setSelected(e.target.value)}><option value="">Unassigned</option>{organizations.map(org => <option key={org.id} value={org.id}>{org.name}</option>)}</select></label>{" "}
    <Button variant="secondary" aria-label={`${user.organization_id ? "Transfer" : "Assign"} organization for ${user.email}`} isDisabled={busy || selected === (user.organization_id || "")} onClick={() => void assign()}>{user.organization_id ? "Transfer organization" : "Assign organization"}</Button>
    {user.organization_id && <p>Transfers move live allocations and future charges, preserve user/project spending, and check destination capacity. Unassignment requires no retained infrastructure.</p>}
    {error && <Alert isInline variant="danger" title={error} />}
  </div>;
}
