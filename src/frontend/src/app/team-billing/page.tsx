"use client";
import { useCallback, useEffect, useState } from "react";
import { Alert, Button, PageSection, Title } from "@patternfly/react-core";
import { api, ApiError, BillingDocument, Group, GroupDetail, inMonth, TeamReport, utcMonth } from "@/lib/group-billing";
import ManagerControls from "@/components/groups/ManagerControls";
import TeamUsage from "@/components/billing/TeamUsage";
import TeamDocuments from "@/components/billing/TeamDocuments";

function BillingScope({ group, month, owner, onDenied }: { group: GroupDetail; month: string; owner: string; onDenied: (message: string) => void }) {
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
  return <><TeamUsage report={data.report} /><p>Project invoices and monthly statements overlap. Do not add their totals together or to active lifetime spend.</p>
    <TeamDocuments docs={data.invoices.filter(doc => inMonth(doc, month, true))} group={group} kind="invoices" onDenied={onDenied} />
    <TeamDocuments docs={data.statements.filter(doc => inMonth(doc, month))} group={group} kind="statements" onDenied={onDenied} />
  </>;
}
function SelectedTeam({ id }: { id: string }) {
  const [group, setGroup] = useState<GroupDetail | null>(null);
  const [error, setError] = useState("");
  const [month, setMonth] = useState(utcMonth);
  const [owner, setOwner] = useState("");
  const [revision, setRevision] = useState(0);
  const denied = useCallback((message = "Group access is no longer available.") => { setGroup(null); setError(message); }, []);
  useEffect(() => {
    let live = true;
    api<GroupDetail>(`/groups/${id}`).then(data => { if (live) setGroup(data); }).catch(e => { if (live) denied(e.message); });
    return () => { live = false; };
  }, [id, denied]);
  if (error) return <Alert isInline variant="danger" title={error} />;
  if (!group) return <p>Loading group…</p>;
  return <><p>{group.description}</p><p><strong>Current members:</strong> This report includes their available billing history, including usage before joining. It is not historical departmental chargeback. Removing an assignment revokes access on subsequent requests; downloaded files cannot be revoked.</p>
    <label>Billing month (UTC) <input aria-label="Billing month (UTC)" type="month" value={month} onChange={e => { if (/^\d{4}-\d{2}$/.test(e.target.value)) setMonth(e.target.value); }} /></label>{" "}
    <label>Member <select aria-label="Member" value={owner} onChange={e => setOwner(e.target.value)}><option value="">All current members</option>{group.members.map(person => <option key={person.id} value={person.id}>{person.email}</option>)}</select></label>{" "}
    <Button variant="secondary" onClick={() => { setGroup(null); void api<GroupDetail>(`/groups/${id}`).then(updated => { setGroup(updated); setOwner(""); setRevision(value => value + 1); }).catch(e => denied(e.message)); }}>Refresh group and billing</Button>
    <BillingScope key={`${month}:${owner}:${revision}`} group={group} month={month} owner={owner} onDenied={denied} />
    <ManagerControls group={group} onChange={updated => { setGroup(updated); setRevision(value => value + 1); }} onDenied={() => denied()} />
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
  return <PageSection><Title headingLevel="h1">Team Usage &amp; Billing</Title>
    {error && <Alert isInline variant="danger" title={error} />}
    {!groups && !error && <p>Loading managed groups…</p>}
    {groups?.length === 0 && <p>You do not manage any groups. Ask an administrator or a current group manager for access. Membership alone does not grant team billing access.</p>}
    {!!groups?.length && <><label>Group <select aria-label="Group" value={selected} onChange={e => setSelected(e.target.value)}>{groups.map(group => <option key={group.id} value={group.id}>{group.name}</option>)}</select></label><SelectedTeam key={selected} id={selected} /></>}
  </PageSection>;
}
