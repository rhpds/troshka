"use client";
import { useState } from "react";
import { Alert, Button } from "@patternfly/react-core";
import { api, ApiError, GroupDetail, jsonBody, Person } from "@/lib/group-billing";
import { appConfirm } from "@/lib/confirm";
import UserPicker from "./UserPicker";
import { TrashIcon } from "@patternfly/react-icons";
import styles from "./management.module.css";

export default function ManagerControls({ group, users, platformAdmin = false, onChange, onDenied }: { group: GroupDetail; users?: Person[]; platformAdmin?: boolean; onChange: (group: GroupDetail) => void; onDenied: () => void }) {
  const [email, setEmail] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function mutate(path: string, options: RequestInit) {
    setBusy(true); setError("");
    try {
      const updated = await api<GroupDetail>(`/groups/${group.id}/managers${path}`, options);
      // Reauthorize after self-removal before displaying or exporting any cached data.
      await api<GroupDetail>(`/groups/${group.id}`);
      onChange(updated); setEmail("");
    } catch (e) {
      if (e instanceof ApiError && e.status === 403) onDenied();
      if (e instanceof ApiError && e.status === 404) {
        // Unknown exact emails are also 404: distinguish them from revoked group access.
        try { await api<GroupDetail>(`/groups/${group.id}`); }
        catch { onDenied(); }
      }
      setError((e as Error).message);
    } finally { setBusy(false); }
  }
  return <section aria-label="Group administrators">
    <p className={styles.muted}>Only platform administrators manage group administrators and membership. Group administrators view attributed usage and current members’ owner billing history, and manage their users’ delegated limits. This grants no project access.</p>
    {error && <Alert variant="danger" isInline title={error} />}
    <ul className={styles.people}>{group.managers.map(person => <li className={styles.person} key={person.id}><span>{person.email}</span>{platformAdmin && <Button variant="plain" aria-label={`Remove administrator ${person.email}`} title={`Remove administrator ${person.email}`} icon={<TrashIcon />} isDisabled={busy} onClick={async () => {
      if (await appConfirm({ title: "Remove administrator", message: `Remove ${person.email} as a group administrator?`, variant: "danger" })) await mutate(`/${person.id}`, { method: "DELETE" });
    }} />}</li>)}</ul>
    {platformAdmin && <>
    <form className={styles.toolbar} onSubmit={e => { e.preventDefault(); if (!busy && email.trim()) void mutate("", jsonBody("POST", { email: email.trim() })); }}>
      {users ? <UserPicker label="Administrator to add" users={users.filter(person => !group.managers.some(manager => manager.id === person.id))}
        value={users.find(person => person.email === email)?.id || ""} onChange={person => setEmail(person?.email || "")} disabled={busy} />
        : <label className={styles.field}>Existing user’s exact email <input aria-label="Administrator email" type="email" required value={email} onChange={e => setEmail(e.target.value)} /></label>}{" "}
      <Button type="submit" isDisabled={busy || !email.trim()}>Add administrator</Button>
    </form></>}
  </section>;
}
