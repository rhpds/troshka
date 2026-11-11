"use client";
import { useEffect, useState } from "react";
import { Alert, Button, Card, CardBody, PageSection, Title } from "@patternfly/react-core";
import { api, Group, GroupDetail, jsonBody, Person } from "@/lib/group-billing";
import GroupEditor from "@/components/groups/GroupEditor";
import styles from "@/components/groups/management.module.css";

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
  const [creating, setCreating] = useState(false);
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
    try { const group = await api<GroupDetail>("/groups", jsonBody("POST", { name, description })); await refresh(); setSelected(group.id); setCreating(false); setName(""); setDescription(""); }
    catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  return <PageSection><div className={styles.page}><Title headingLevel="h1">Groups</Title>
    {error && <Alert isInline variant="danger" title={error} />}
    {allowed && <>{selected ? <>
      <div><Button variant="link" isInline onClick={() => setSelected("")}>← Back to groups</Button></div>
      <SelectedGroup key={selected} id={selected} users={users} onUpdated={() => void refresh()} onDeleted={() => { setSelected(""); void refresh(); }} />
    </> : <>
      <div className={styles.header}><p className={styles.muted}>Select a group to manage its members and managers.</p><Button onClick={() => setCreating(true)}>New group</Button></div>
      {creating && <Card><CardBody><Title headingLevel="h2">Create group</Title>
      <form className={styles.toolbar} onSubmit={e => { e.preventDefault(); void create(); }}>
        <label className={styles.field}>Name <input aria-label="New group name" required value={name} onChange={e => setName(e.target.value)} /></label>
        <label className={styles.field}>Description <input aria-label="New group description" value={description} onChange={e => setDescription(e.target.value)} /></label>
        <Button type="submit" isDisabled={busy || !name.trim()}>Create group</Button>
        <Button variant="link" isDisabled={busy} onClick={() => setCreating(false)}>Cancel</Button>
      </form></CardBody></Card>}
      {!groups.length && <p>No groups created yet.</p>}
      {!!groups.length && <Card><CardBody><div className={styles.table} role="region" aria-label="Groups" tabIndex={0}><table aria-label="Groups" className={styles.dataTable}><thead><tr><th scope="col">Group</th><th scope="col">Type</th><th scope="col">Description</th><th scope="col" className={styles.numeric}>Members</th><th scope="col" className={styles.numeric}>Managers</th></tr></thead><tbody>{groups.map(group => <tr key={group.id}>
        <td className={styles.identity}><Button variant="link" isInline onClick={() => setSelected(group.id)}>{group.name}</Button></td><td className={styles.nowrap}>{group.is_organization ? "Quota organization" : "Reporting group"}</td><td className={styles.description}>{group.description || "—"}</td><td className={styles.numeric}>{group.member_count}</td><td className={styles.numeric}>{group.manager_count}</td>
      </tr>)}</tbody></table></div></CardBody></Card>}
    </>}</>}
  </div></PageSection>;
}
