"use client";
import { useCallback, useEffect, useState } from "react";
import { Alert, Button, PageSection, Title, Tabs, Tab, TabTitleText } from "@patternfly/react-core";
import styles from "@/components/groups/management.module.css";
import { api, ApiError, BillingDocument, Group, GroupDetail, inMonth, MemberUsage, TeamReport, utcMonth } from "@/lib/group-billing";
import ManagerControls from "@/components/groups/ManagerControls";
import TeamUsage from "@/components/billing/TeamUsage";
import TeamDocuments from "@/components/billing/TeamDocuments";
import OrganizationDirectory from "@/components/quotas/OrganizationDirectory";

function BillingScope({ group, month, owner, documentOwner, tab, onTab, onDenied, onMembers }: { group: GroupDetail; month: string; owner: string; documentOwner: string; tab: string | number; onTab: (tab: string | number) => void; onDenied: (message: string) => void; onMembers: (members: MemberUsage[]) => void }) {
  const [data, setData] = useState<{ report: TeamReport; invoices: BillingDocument[]; statements: BillingDocument[] } | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let live = true;
    const [year, number] = month.split("-").map(Number);
    const currentOwner = group.members.some(person => person.id === documentOwner) ? documentOwner : "";
    const filter = currentOwner ? `?owner_id=${encodeURIComponent(currentOwner)}` : "";
    const base = `/metering/groups/${group.id}`;
    Promise.all([api<TeamReport>(`${base}/report?year=${year}&month=${number}${owner ? `&owner_id=${encodeURIComponent(owner)}` : ""}`), api<BillingDocument[]>(`${base}/invoices${filter}`), api<BillingDocument[]>(`${base}/statements${filter}`)])
      .then(([report, invoices, statements]) => { if (live) { setData({ report, invoices, statements }); if (!owner) onMembers(report.members); } })
      .catch(e => { if (live) { setData(null); setError(e.message); if (e instanceof ApiError && [403, 404].includes(e.status)) onDenied(e.message); } });
    return () => { live = false; };
  }, [group.id, group.members, month, owner, documentOwner, onDenied, onMembers]);
  if (error) return <Alert isInline variant="danger" title={`Billing unavailable: ${error}`} />;
  if (!data) return <p>Loading billing…</p>;
  return <Tabs activeKey={tab} onSelect={(_, key) => onTab(key)} mountOnEnter unmountOnExit aria-label="Team billing views">
    <Tab eventKey="usage" title={<TabTitleText>Usage</TabTitleText>}><div className={styles.panel}><TeamUsage report={data.report} /></div></Tab>
    <Tab eventKey="invoices" title={<TabTitleText>Invoices</TabTitleText>}><div className={styles.panel}><p className={styles.muted}>Owner billing history for current members. Whole owner documents may span multiple groups and are separate from group-attributed usage. Invoices and statements overlap; their totals should not be added together.</p>
    <TeamDocuments docs={data.invoices.filter(doc => inMonth(doc, month, true))} group={group} kind="invoices" onDenied={onDenied} />
    </div></Tab>
    <Tab eventKey="statements" title={<TabTitleText>Statements</TabTitleText>}><div className={styles.panel}><p className={styles.muted}>Owner billing history for current members. Whole owner documents may span multiple groups and are separate from group-attributed usage. Invoices and statements overlap; their totals should not be added together.</p>
    <TeamDocuments docs={data.statements.filter(doc => inMonth(doc, month))} group={group} kind="statements" onDenied={onDenied} />
    </div></Tab>
  </Tabs>;
}
function SelectedTeam({ id }: { id: string }) {
  const [group, setGroup] = useState<GroupDetail | null>(null);
  const [error, setError] = useState("");
  const [month, setMonth] = useState(utcMonth);
  const [owner, setOwner] = useState("");
  const [documentOwner, setDocumentOwner] = useState("");
  const [usageMembers, setUsageMembers] = useState<MemberUsage[]>([]);
  const onMembers = useCallback((members: MemberUsage[]) => setUsageMembers(members), []);
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
    {tab === "usage" ? <label className={styles.field}>Usage member <select aria-label="Usage member" value={owner} onChange={e => setOwner(e.target.value)}><option value="">All group-attributed usage</option>{group.members.map(person => <option key={person.id} value={person.id}>{person.email}</option>)}{usageMembers.filter(member => !member.current_member && !group.members.some(person => person.id === member.owner_id)).map(member => <option key={member.owner_id} value={member.owner_id}>{member.display_name || "Unknown member"} (former member)</option>)}</select></label>
      : <label className={styles.field}>Owner documents <select aria-label="Owner documents" value={documentOwner} onChange={e => setDocumentOwner(e.target.value)}><option value="">All current members’ owner documents</option>{group.members.map(person => <option key={person.id} value={person.id}>{person.email}</option>)}</select></label>}
    <Button variant="secondary" onClick={() => { setGroup(null); setUsageMembers([]); void api<GroupDetail>(`/groups/${id}`).then(updated => { setGroup(updated); setOwner(""); setDocumentOwner(""); setRevision(value => value + 1); }).catch(e => denied(e.message)); }}>Refresh group and billing</Button>
    </div>
    <BillingScope key={`${month}:${owner}:${documentOwner}:${revision}`} group={group} month={month} owner={owner} documentOwner={documentOwner} tab={tab} onTab={setTab} onDenied={denied} onMembers={onMembers} />
    <details className={styles.expandable}><summary>Group administrators ({group.managers.length})</summary><div className={styles.panel}>
    <ManagerControls group={group} onChange={updated => { setGroup(updated); setRevision(value => value + 1); }} onDenied={() => denied()} />
    </div></details>
    <details className={styles.expandable}><summary>About this report and access</summary><p className={styles.muted}>Group usage retains charges attributed here, including former members. Member detail includes only charges attributed to this group after group accounting started. Earlier costs are outside group accounting. Owner invoices and statements remain a separate current-member history. Removing an administrator assignment revokes access on subsequent requests; downloaded files cannot be revoked.</p></details>
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
    <OrganizationDirectory />
    {error && <Alert isInline variant="danger" title={error} />}
    {!groups && !error && <p>Loading managed groups…</p>}
    {groups?.length === 0 && <p>You do not manage any groups. Ask a platform administrator for access. Membership alone does not grant team billing access.</p>}
    {!!groups?.length && <><div className={styles.toolbar}><label className={styles.field}>Group <select aria-label="Group" value={selected} onChange={e => setSelected(e.target.value)}>{groups.map(group => <option key={group.id} value={group.id}>{group.name}</option>)}</select></label></div><div><SelectedTeam key={selected} id={selected} /></div></>}
  </div></PageSection>;
}
