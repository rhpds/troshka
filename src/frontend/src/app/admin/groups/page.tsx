"use client";
import { useEffect, useState } from "react";
import { Alert, Button, Card, CardBody, PageSection, Title } from "@patternfly/react-core";
import { api, Group, GroupDetail, jsonBody, Person } from "@/lib/group-billing";
import GroupEditor from "@/components/groups/GroupEditor";

function SelectedGroup({ id, users, onUpdated, onDeleted }: { id: string; users: Person[]; onUpdated: () => void; onDeleted: () => void }) {
  const [group, setGroup] = useState<GroupDetail | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let live = true;
    api<GroupDetail>(`/groups/${id}`).then(data => { if (live) setGroup(data); }).catch(e => { if (live) setError(e.message); });
    return () => { live = false; };
  }, [id]);
  if (error) return <Alert isInline variant="danger" title={error} />;
  return group ? <GroupEditor initial={group} users={users} onUpdated={onUpdated} onDeleted={onDeleted} onDenied={() => { setGroup(null); setError("Group access is no longer available. Refresh the group list."); }} /> : <p>Loading group…</p>;
}
export default function GroupsPage() {
  const [allowed, setAllowed] = useState(false);
  const [groups, setGroups] = useState<Group[]>([]);
  const [users, setUsers] = useState<Person[]>([]);
  const [selected, setSelected] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function refresh() { try { setGroups(await api<Group[]>("/groups")); } catch (e) { setError((e as Error).message); setGroups([]); setSelected(""); } }
  useEffect(() => {
    let live = true;
    async function load() {
      try {
        const user = await api<{ role: string }>("/auth/me");
        if (user.role !== "admin") throw new Error("Administrator access required");
        const [list, people] = await Promise.all([api<Group[]>("/groups"), api<Person[]>("/users/")]);
        if (live) { setAllowed(true); setGroups(list); setUsers(people); }
      } catch (e) { if (live) setError((e as Error).message); }
    }
    void load(); return () => { live = false; };
  }, []);
  async function create() {
    setBusy(true); setError("");
    try { const group = await api<GroupDetail>("/groups", jsonBody("POST", { name, description })); await refresh(); setSelected(group.id); setName(""); setDescription(""); }
    catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  return <PageSection><Title headingLevel="h1">Groups</Title>
    {error && <Alert isInline variant="danger" title={error} />}
    {allowed && <><p>Create managed groups and assign members independently of managers.</p><Card><CardBody><h2>Create group</h2>
      <form onSubmit={e => { e.preventDefault(); void create(); }}>
        <label>Name <input aria-label="New group name" required value={name} onChange={e => setName(e.target.value)} /></label>{" "}
        <label>Description <input aria-label="New group description" value={description} onChange={e => setDescription(e.target.value)} /></label>{" "}
        <Button type="submit" isDisabled={busy || !name.trim()}>Create group</Button>
      </form></CardBody></Card>
      <label>Group <select aria-label="Group" value={selected} onChange={e => setSelected(e.target.value)}><option value="">Select group</option>{groups.map(group => <option key={group.id} value={group.id}>{group.name} ({group.member_count} members, {group.manager_count} managers)</option>)}</select></label>
      {!groups.length && <p>No groups created yet.</p>}
      {selected && <SelectedGroup key={selected} id={selected} users={users} onUpdated={() => void refresh()} onDeleted={() => { setSelected(""); void refresh(); }} />}
    </>}
  </PageSection>;
}
