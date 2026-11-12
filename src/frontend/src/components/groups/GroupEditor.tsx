"use client";
import { useCallback, useState } from "react";
import { Alert, Button, Card, CardBody, Popover, Tabs, Tab, TabTitleText, Title } from "@patternfly/react-core";
import { TrashIcon } from "@patternfly/react-icons";
import styles from "./management.module.css";
import { api, ApiError, GroupDetail, jsonBody, Person } from "@/lib/group-billing";
import ManagerControls from "./ManagerControls";
import UserPicker from "./UserPicker";
import { appConfirm } from "@/lib/confirm";
import OrganizationQuotas from "@/components/quotas/OrganizationQuotas";
import { InfoCircleIcon } from "@patternfly/react-icons";
import { transferHelp } from "@/components/quotas/OrganizationAssignment";

export default function GroupEditor({ initial, users, onDeleted, onUpdated, onDenied }: { initial: GroupDetail; users: Person[]; onDeleted: () => void; onUpdated: () => void; onDenied: () => void }) {
  const [group, setGroup] = useState(initial);
  const [name, setName] = useState(initial.name);
  const [description, setDescription] = useState(initial.description || "");
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
    {group.description && <p className={styles.muted}>{group.description}</p>}
    {error && <Alert variant="danger" isInline title={error} />}
    <Tabs activeKey={tab} onSelect={(_, key) => setTab(key)} mountOnEnter unmountOnExit aria-label="Group management">
    <Tab eventKey="members" title={<TabTitleText>Members ({group.members.length})</TabTitleText>}>
    <section className={styles.panel} aria-label="Group members">
    <div className={styles.muted}>Each user belongs to one group. Adding a member moves them from their current group and preserves their projects, images, patterns and ownership. <Popover headerContent="Group transfers" bodyContent={transferHelp}><Button variant="plain" aria-label="About group transfers" icon={<InfoCircleIcon />} /></Popover></div>
    <div className={styles.toolbar}>
    <UserPicker label="Member to add" users={users.filter(user => !group.members.some(p => p.id === user.id))} value={member} onChange={person => setMember(person?.id || "")} disabled={busy} />
    <Button isDisabled={busy || !member} onClick={() => void mutate(`/members/${member}`, { method: "PUT" })}>Add member</Button>
    </div>
    {!group.members.length && <p>No members yet. Search for a user to add them.</p>}
    <ul className={styles.people}>{group.members.map(person => <li className={styles.person} key={person.id}><span>{person.email}</span><Button variant="plain" aria-label={`Remove member ${person.email}`} title={`Remove member ${person.email}`} icon={<TrashIcon />} isDisabled={busy} onClick={async () => {
      if (await appConfirm({ message: `Remove ${person.email} from ${group.name}?`, variant: "danger" })) await mutate(`/members/${person.id}`, { method: "DELETE" });
    }} /></li>)}</ul>
    </section></Tab>
    <Tab eventKey="managers" title={<TabTitleText>Administrators ({group.managers.length})</TabTitleText>}>
    <div className={styles.panel}><ManagerControls group={group} platformAdmin users={users} onChange={updated => { setGroup(updated); onUpdated(); }} onDenied={onDenied} /></div>
    </Tab>
    <Tab eventKey="quotas" title={<TabTitleText>Quotas</TabTitleText>}><div className={styles.panel}><OrganizationQuotas key={group.id} id={group.id} onDenied={quotaDenied} /></div></Tab>
    <Tab eventKey="settings" title={<TabTitleText>Settings</TabTitleText>}>
    <form className={styles.panel} onSubmit={e => { e.preventDefault(); void mutate("", jsonBody("PATCH", { name, description, cost_center_id: costCenter.trim() || null })); }}>
      <label className={styles.field}>Name <input aria-label="Group name" required value={name} onChange={e => setName(e.target.value)} /></label>
      <label className={styles.field}>Description <input aria-label="Group description" value={description} onChange={e => setDescription(e.target.value)} /></label>
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
