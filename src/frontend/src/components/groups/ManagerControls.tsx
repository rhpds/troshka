"use client";
import { useState } from "react";
import { Alert, Button } from "@patternfly/react-core";
import { api, ApiError, GroupDetail, jsonBody, Person } from "@/lib/group-billing";
import { appConfirm } from "@/lib/confirm";
import UserPicker from "./UserPicker";

export default function ManagerControls({ group, users, onChange, onDenied }: { group: GroupDetail; users?: Person[]; onChange: (group: GroupDetail) => void; onDenied: () => void }) {
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
  return <section aria-label="Group managers">
    <h2>Managers</h2>
    <p>Managers can view current members’ billing history and delegate management. Managers are not automatically members. The last manager cannot be removed.</p>
    {error && <Alert variant="danger" isInline title={error} />}
    <ul>{group.managers.map(person => <li key={person.id}>{person.email} <Button variant="link" isDisabled={busy} onClick={async () => {
      if (await appConfirm({ title: "Remove manager", message: `Remove ${person.email} as a manager?`, variant: "danger" })) await mutate(`/${person.id}`, { method: "DELETE" });
    }}>Remove manager {person.email}</Button></li>)}</ul>
    <form onSubmit={e => { e.preventDefault(); if (!busy && email.trim()) void mutate("", jsonBody("POST", { email: email.trim() })); }}>
      {users ? <UserPicker label="Manager to add" users={users.filter(person => !group.managers.some(manager => manager.id === person.id))}
        value={users.find(person => person.email === email)?.id || ""} onChange={person => setEmail(person?.email || "")} disabled={busy} />
        : <label>Existing user’s exact email <input aria-label="Manager email" type="email" required value={email} onChange={e => setEmail(e.target.value)} /></label>}{" "}
      <Button type="submit" isDisabled={busy || !email.trim()}>Add manager</Button>
    </form>
  </section>;
}
