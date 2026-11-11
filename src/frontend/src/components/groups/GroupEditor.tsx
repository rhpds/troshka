"use client";
import { useCallback, useState } from "react";
import { Alert, Button, Card, CardBody, Tabs, Tab, TabTitleText, Title } from "@patternfly/react-core";
import { TrashIcon } from "@patternfly/react-icons";
import styles from "./management.module.css";
import { api, ApiError, GroupDetail, jsonBody, Person } from "@/lib/group-billing";
import ManagerControls from "./ManagerControls";
import UserPicker from "./UserPicker";
import { appConfirm } from "@/lib/confirm";
import OrganizationQuotas from "@/components/quotas/OrganizationQuotas";

export default function GroupEditor({ initial, users, onDeleted, onUpdated, onDenied }: { initial: GroupDetail; users: Person[]; onDeleted: () => void; onUpdated: () => void; onDenied: () => void }) {
  const [group, setGroup] = useState(initial);
  const [name, setName] = useState(initial.name);
  const [description, setDescription] = useState(initial.description || "");
  const [organization, setOrganization] = useState(initial.is_organization || false);
  const [costCenter, setCostCenter] = useState(initial.cost_center_id || "");
  const quotaDenied = useCallback(() => onDenied(), [onDenied]);
  const [member, setMember] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [tab, setTab] = useState<string | number>("members");
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
    <Title headingLevel="h2">{group.name}</Title>
    <p>{group.is_organization ? "Quota organization" : "Reporting group"}</p>
    {group.description && <p className={styles.muted}>{group.description}</p>}
    {error && <Alert variant="danger" isInline title={error} />}
    <Tabs activeKey={tab} onSelect={(_, key) => setTab(key)} aria-label="Group management">
    <Tab eventKey="members" title={<TabTitleText>Members ({group.members.length})</TabTitleText>}>
    <section className={styles.panel} aria-label="Group members">
    <p className={styles.muted}>{group.is_organization ? "Authoritative organization users. Assign and transfer users from Users administration." : "Members’ usage and billing are visible to this group’s managers."}</p>
    {!group.is_organization && <>
    <div className={styles.toolbar}>
    <UserPicker label="Member to add" users={users.filter(user => !group.members.some(p => p.id === user.id))} value={member} onChange={person => setMember(person?.id || "")} disabled={busy} />
    <Button isDisabled={busy || !member} onClick={() => void mutate(`/members/${member}`, { method: "PUT" })}>Add member</Button>
    </div></>}
    {!group.members.length && <p>No members yet. Search for a user to add them.</p>}
    <ul className={styles.people}>{group.members.map(person => <li className={styles.person} key={person.id}><span>{person.email}</span>{!group.is_organization && <Button variant="plain" aria-label={`Remove member ${person.email}`} title={`Remove member ${person.email}`} icon={<TrashIcon />} isDisabled={busy} onClick={async () => {
      if (await appConfirm({ message: `Remove ${person.email} from ${group.name}?`, variant: "danger" })) await mutate(`/members/${person.id}`, { method: "DELETE" });
    }} />}</li>)}</ul>
    </section></Tab>
    <Tab eventKey="managers" title={<TabTitleText>{group.is_organization ? "Organization administrators" : "Managers"} ({group.managers.length})</TabTitleText>}>
    <div className={styles.panel}><ManagerControls group={group} platformAdmin users={users} onChange={updated => { setGroup(updated); onUpdated(); }} onDenied={onDenied} /></div>
    </Tab>
    {group.is_organization && <Tab eventKey="quotas" title={<TabTitleText>Organization quotas</TabTitleText>}><div className={styles.panel}><OrganizationQuotas key={group.id} id={group.id} onDenied={quotaDenied} /></div></Tab>}
    <Tab eventKey="settings" title={<TabTitleText>Settings</TabTitleText>}>
    <form className={styles.panel} onSubmit={e => { e.preventDefault(); void mutate("", jsonBody("PATCH", { name, description, is_organization: organization, cost_center_id: costCenter.trim() || null })); }}>
      <label className={styles.field}>Name <input aria-label="Group name" required value={name} onChange={e => setName(e.target.value)} /></label>
      <label className={styles.field}>Description <input aria-label="Group description" value={description} onChange={e => setDescription(e.target.value)} /></label>
      <label className={styles.field}><input aria-label="Quota organization" type="checkbox" checked={organization} onChange={e => setOrganization(e.target.checked)} /> Quota organization</label>
      <label className={styles.field}>Cost center (optional) <input aria-label="Cost center (optional)" value={costCenter} onChange={e => setCostCenter(e.target.value)} /></label>
      <div className={styles.toolbar}>
      <Button type="submit" isDisabled={busy}>Save group</Button>{" "}
      <Button variant="danger" isDisabled={busy} onClick={async () => {
        if (await appConfirm({ message: `Delete ${group.name}? Users and billing records are preserved.`, variant: "danger" })) await mutate("", { method: "DELETE" });
      }}>Delete group</Button>
      </div>
    </form>
    </Tab></Tabs>
  </CardBody></Card>;
}
