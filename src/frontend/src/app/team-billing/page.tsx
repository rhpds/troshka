"use client";
import { useCallback, useEffect, useState } from "react";
import { Alert, Button, PageSection, Title, Tabs, Tab, TabTitleText } from "@patternfly/react-core";
import styles from "@/components/groups/management.module.css";
import { api, ApiError, BillingDocument, Group, GroupDetail, inMonth, TeamReport, utcMonth } from "@/lib/group-billing";
import ManagerControls from "@/components/groups/ManagerControls";
import TeamUsage from "@/components/billing/TeamUsage";
import TeamDocuments from "@/components/billing/TeamDocuments";

function BillingScope({ group, month, owner, tab, onTab, onDenied }: { group: GroupDetail; month: string; owner: string; tab: string | number; onTab: (tab: string | number) => void; onDenied: (message: string) => void }) {
  const [data, setData] = useState<{ report: TeamReport; invoices: BillingDocument[]; statements: BillingDocument[] } | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let live = true;
    const [year, number] = month.split("-").map(Number);
    const filter = owner ? `?owner_id=${encodeURIComponent(owner)}` : "";
    const base = `/metering/groups/${group.id}`;
    Promise.all([api<TeamReport>(`${base}/report?year=${year}&month=${number}${owner ? `&owner_id=${encodeURIComponent(owner)}` : ""}`), api<BillingDocument[]>(`${base}/invoices${filter}`), api<BillingDocument[]>(`${base}/statements${filter}`)])
      .then(([report, invoices, statements]) => { if (live) setData({ report, invoices, statements }); })
      .catch(e => { if (live) { setData(null); setError(e.message); if (e instanceof ApiError && [403, 404].includes(e.status)) onDenied(e.message); } });
    return () => { live = false; };
  }, [group.id, month, owner, onDenied]);
  if (error) return <Alert isInline variant="danger" title={`Billing unavailable: ${error}`} />;
  if (!data) return <p>Loading billing…</p>;
  return <Tabs activeKey={tab} onSelect={(_, key) => onTab(key)} mountOnEnter unmountOnExit aria-label="Team billing views">
    <Tab eventKey="usage" title={<TabTitleText>Usage</TabTitleText>}><div className={styles.panel}><TeamUsage report={data.report} /></div></Tab>
    <Tab eventKey="invoices" title={<TabTitleText>Invoices</TabTitleText>}><div className={styles.panel}><p className={styles.muted}>Invoices and statements overlap. Their totals should not be added together.</p>
    <TeamDocuments docs={data.invoices.filter(doc => inMonth(doc, month, true))} group={group} kind="invoices" onDenied={onDenied} />
    </div></Tab>
    <Tab eventKey="statements" title={<TabTitleText>Statements</TabTitleText>}><div className={styles.panel}><p className={styles.muted}>Invoices and statements overlap. Their totals should not be added together.</p>
    <TeamDocuments docs={data.statements.filter(doc => inMonth(doc, month))} group={group} kind="statements" onDenied={onDenied} />
    </div></Tab>
  </Tabs>;
}
function SelectedTeam({ id }: { id: string }) {
  const [group, setGroup] = useState<GroupDetail | null>(null);
  const [error, setError] = useState("");
  const [month, setMonth] = useState(utcMonth);
  const [owner, setOwner] = useState("");
  const [revision, setRevision] = useState(0);
  const [tab, setTab] = useState<string | number>("usage");
  const denied = useCallback((message = "Group access is no longer available.") => { setGroup(null); setError(message); }, []);
  useEffect(() => {
    let live = true;
    api<GroupDetail>(`/groups/${id}`).then(data => { if (live) setGroup(data); }).catch(e => { if (live) denied(e.message); });
    return () => { live = false; };
  }, [id, denied]);
  if (error) return <Alert isInline variant="danger" title={error} />;
  if (!group) return <p>Loading group…</p>;
  return <><p className={styles.muted}>{group.description}</p>
    <div className={styles.toolbar}>
    <label className={styles.field}>Billing month (UTC) <input aria-label="Billing month (UTC)" type="month" value={month} onChange={e => { if (/^\d{4}-\d{2}$/.test(e.target.value)) setMonth(e.target.value); }} /></label>
    <label className={styles.field}>Member <select aria-label="Member" value={owner} onChange={e => setOwner(e.target.value)}><option value="">All current members</option>{group.members.map(person => <option key={person.id} value={person.id}>{person.email}</option>)}</select></label>
    <Button variant="secondary" onClick={() => { setGroup(null); void api<GroupDetail>(`/groups/${id}`).then(updated => { setGroup(updated); setOwner(""); setRevision(value => value + 1); }).catch(e => denied(e.message)); }}>Refresh group and billing</Button>
    </div>
    <BillingScope key={`${month}:${owner}:${revision}`} group={group} month={month} owner={owner} tab={tab} onTab={setTab} onDenied={denied} />
    <details className={styles.expandable}><summary>Manage group managers ({group.managers.length})</summary><div className={styles.panel}>
    <ManagerControls group={group} onChange={updated => { setGroup(updated); setRevision(value => value + 1); }} onDenied={() => denied()} />
    </div></details>
    <details className={styles.expandable}><summary>About this report and access</summary><p className={styles.muted}>This report covers current members’ available billing history, including usage before joining. It is not historical departmental chargeback. Removing an assignment revokes access on subsequent requests; downloaded files cannot be revoked.</p></details>
  </>;
}
export default function TeamBillingPage() {
  const [groups, setGroups] = useState<Group[] | null>(null);
  const [selected, setSelected] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    let live = true;
    api<Group[]>("/groups").then(list => { if (live) { setGroups(list); setSelected(list[0]?.id || ""); } }).catch(e => { if (live) setError(e.message); });
    return () => { live = false; };
  }, []);
  return <PageSection><div className={styles.page}><Title headingLevel="h1">Team Usage &amp; Billing</Title>
    {error && <Alert isInline variant="danger" title={error} />}
    {!groups && !error && <p>Loading managed groups…</p>}
    {groups?.length === 0 && <p>You do not manage any groups. Ask an administrator or a current group manager for access. Membership alone does not grant team billing access.</p>}
    {!!groups?.length && <><div className={styles.toolbar}><label className={styles.field}>Group <select aria-label="Group" value={selected} onChange={e => setSelected(e.target.value)}>{groups.map(group => <option key={group.id} value={group.id}>{group.name}</option>)}</select></label></div><div><SelectedTeam key={selected} id={selected} /></div></>}
  </div></PageSection>;
}
