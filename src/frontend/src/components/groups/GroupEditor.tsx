"use client";
import { useState } from "react";
import { Alert, Button, Card, CardBody } from "@patternfly/react-core";
import { api, ApiError, GroupDetail, jsonBody, Person } from "@/lib/group-billing";
import ManagerControls from "./ManagerControls";
import UserPicker from "./UserPicker";
import { appConfirm } from "@/lib/confirm";

export default function GroupEditor({ initial, users, onDeleted, onUpdated, onDenied }: { initial: GroupDetail; users: Person[]; onDeleted: () => void; onUpdated: () => void; onDenied: () => void }) {
  const [group, setGroup] = useState(initial);
  const [name, setName] = useState(initial.name);
  const [description, setDescription] = useState(initial.description || "");
  const [member, setMember] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function mutate(path: string, options: RequestInit) {
    setBusy(true); setError("");
    try {
      const result = await api<GroupDetail | undefined>(`/groups/${group.id}${path}`, options);
      if (result) { setGroup(result); setMember(""); onUpdated(); } else onDeleted();
    } catch (e) {
      if (e instanceof ApiError && [403, 404].includes(e.status)) onDenied();
      setError((e as Error).message);
    } finally { setBusy(false); }
  }
  return <Card><CardBody>
    {error && <Alert variant="danger" isInline title={error} />}
    <form onSubmit={e => { e.preventDefault(); void mutate("", jsonBody("PATCH", { name, description })); }}>
      <label>Name <input aria-label="Group name" required value={name} onChange={e => setName(e.target.value)} /></label>{" "}
      <label>Description <input aria-label="Group description" value={description} onChange={e => setDescription(e.target.value)} /></label>{" "}
      <Button type="submit" isDisabled={busy}>Save group</Button>{" "}
      <Button variant="danger" isDisabled={busy} onClick={async () => {
        if (await appConfirm({ message: `Delete ${group.name}? Users and billing records are preserved.`, variant: "danger" })) await mutate("", { method: "DELETE" });
      }}>Delete group</Button>
    </form>
    <h2>Members</h2>
    <ul>{group.members.map(person => <li key={person.id}>{person.email} <Button variant="link" isDisabled={busy} onClick={async () => {
      if (await appConfirm({ message: `Remove ${person.email} from ${group.name}?`, variant: "danger" })) await mutate(`/members/${person.id}`, { method: "DELETE" });
    }}>Remove member {person.email}</Button></li>)}</ul>
    <UserPicker label="Member to add" users={users.filter(user => !group.members.some(p => p.id === user.id))} value={member} onChange={person => setMember(person?.id || "")} disabled={busy} />{" "}
    <Button isDisabled={busy || !member} onClick={() => void mutate(`/members/${member}`, { method: "PUT" })}>Add member</Button>
    <ManagerControls group={group} users={users} onChange={updated => { setGroup(updated); onUpdated(); }} onDenied={onDenied} />
  </CardBody></Card>;
}
