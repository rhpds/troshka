"use client";
import { useCallback, useEffect, useState } from "react";
import { Alert } from "@patternfly/react-core";
import { Organization, quotaFetch } from "@/lib/quotas";
import OrganizationQuotas from "./OrganizationQuotas";
import styles from "./quotas.module.css";

export default function OrganizationDirectory() {
  const [organizations, setOrganizations] = useState<Organization[]>([]), [selected, setSelected] = useState("");
  const [error, setError] = useState("");
  const denied = useCallback(() => { setOrganizations([]); setSelected(""); setError("Organization quota access is no longer available. Refresh to reauthorize."); }, []);
  useEffect(() => {
    let live = true;
    quotaFetch<Organization[]>("/quotas/organizations").then(data => { if (!Array.isArray(data)) throw new Error("Invalid organization list"); if (live) setOrganizations(data); })
      .catch(e => { if (live) { setOrganizations([]); setSelected(""); setError(e.message); } });
    return () => { live = false; };
  }, []);
  return <section className={styles.directory} aria-label="Organization quota administration">
    {error && <Alert isInline variant="warning" title={error} />}
    {!!organizations.length && <><label className={`${styles.field} ${styles.selector}`}>Quota group <select aria-label="Quota group" value={selected} onChange={e => setSelected(e.target.value)}><option value="">Select a group</option>{organizations.map(org => <option key={org.id} value={org.id}>{org.name}</option>)}</select></label>
    {selected && <OrganizationQuotas key={selected} id={selected} onDenied={denied} />}</>}
    {!organizations.length && !error && <p>No groups available for delegated quota administration.</p>}
  </section>;
}
