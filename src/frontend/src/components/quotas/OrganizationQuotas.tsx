"use client";
import { useEffect, useState } from "react";
import { Alert, Title } from "@patternfly/react-core";
import { OrganizationUser, quotaFetch } from "@/lib/quotas";
import QuotaScope from "./QuotaScope";
import styles from "./quotas.module.css";

export default function OrganizationQuotas({ id, onDenied }: { id: string; onDenied: () => void }) {
  const [users, setUsers] = useState<OrganizationUser[] | null>(null), [selected, setSelected] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    let live = true;
    quotaFetch<OrganizationUser[]>(`/quotas/organizations/${encodeURIComponent(id)}/members`).then(data => {
      if (!Array.isArray(data)) throw new Error("Invalid organization members response");
      if (live) setUsers(data);
    }).catch(e => { if (live) { setUsers(null); setSelected(""); setError(e.message); onDenied(); } });
    return () => { live = false; };
  }, [id, onDenied]);
  if (error) return <Alert isInline variant="danger" title={error} />;
  return <section className={styles.organization} aria-label="Organization quotas"><Title headingLevel="h2">Organization quotas</Title>
    <QuotaScope key={id} scope="organization" id={id} onDenied={onDenied} />
    <div className={styles.memberSection}>
    <label className={`${styles.field} ${styles.selector}`}>User limits <select aria-label="Organization user limits" value={selected} onChange={e => setSelected(e.target.value)}><option value="">Select an organization user</option>{users?.map(user => <option key={user.id} value={user.id}>{user.display_name || user.email} — {user.email}</option>)}</select></label>
    {users?.length === 0 && <p>No users assigned to this organization.</p>}
    {selected && <QuotaScope key={`${id}:${selected}`} scope="user" id={selected} onDenied={onDenied} />}
    </div>
    <details className={styles.details}><summary>Organization access and membership</summary><p className={styles.help}>Organization membership is authoritative and distinct from reporting groups. Platform administrators assign and transfer users. Organization administrators manage only their own users’ limits; this grants no project access.</p></details>
  </section>;
}
