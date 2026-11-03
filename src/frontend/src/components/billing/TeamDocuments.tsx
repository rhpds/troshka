"use client";
import { useEffect, useState } from "react";
import { Alert, Button, Card, CardBody } from "@patternfly/react-core";
import { api, ApiError, BillingDocument, documentCsv, downloadCsv, Group, money } from "@/lib/group-billing";
import { ProjectUsage, ResourceUsage } from "./TeamUsage";

function DocumentDetail({ doc, group, kind, onDenied }: { doc: BillingDocument; group: Group; kind: string; onDenied: (message: string) => void }) {
  const [detail, setDetail] = useState<BillingDocument | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let live = true;
    api<BillingDocument>(`/metering/${kind}/${doc.id}`).then(data => { if (live) setDetail(data); }).catch(e => {
      if (!live) return;
      if (e instanceof ApiError && [403, 404].includes(e.status)) onDenied(e.message);
      else setError(e.message);
    });
    return () => { live = false; };
  }, [doc.id, kind, onDenied]);
  if (error) return <Alert isInline variant="danger" title={error} />;
  if (!detail) return <p>Loading document…</p>;
  return <div><p>Document total: {money(doc.total_usd)} {doc.currency}. Finalized: {doc.finalized_at || "Not finalized"}.</p>
    <Button variant="secondary" onClick={() => downloadCsv(`team-${kind}-${doc.id}.csv`, documentCsv(group, { ...doc, ...detail }))}>Download document CSV</Button>
    {kind === "statements" ? <ProjectUsage projects={detail.line_items?.by_project || []} /> : <ResourceUsage lines={detail.line_items?.by_kind || []} />}
  </div>;
}
export default function TeamDocuments({ docs, group, kind, onDenied }: { docs: BillingDocument[]; group: Group; kind: "invoices" | "statements"; onDenied: (message: string) => void }) {
  const [expanded, setExpanded] = useState<string | null>(null);
  return <Card><CardBody><h2>{kind === "invoices" ? "Project invoices" : "Monthly statements"}</h2>
    {kind === "invoices" && <p>Invoices whose lifetime billing periods overlap the selected month. Invoice totals cover their full periods.</p>}
    {!docs.length ? <p>No {kind} for this selection.</p> : <table className="pf-v6-c-table pf-m-compact"><thead><tr><th>Owner</th><th>Document / project</th><th>Period (UTC)</th><th>Total</th><th>Status</th><th>Details / export</th></tr></thead><tbody>{docs.map(doc => <DocumentRow key={doc.id} doc={doc} group={group} kind={kind} expanded={expanded === doc.id} toggle={() => setExpanded(expanded === doc.id ? null : doc.id)} onDenied={onDenied} />)}</tbody></table>}
  </CardBody></Card>;
}
function DocumentRow({ doc, group, kind, expanded, toggle, onDenied }: { doc: BillingDocument; group: Group; kind: string; expanded: boolean; toggle: () => void; onDenied: (message: string) => void }) {
  return <><tr><td>{doc.owner_email}</td><td>{doc.project_name || doc.id}</td><td>{doc.period_start || "Unavailable"} to {doc.period_end || "Unavailable"}</td><td>{money(doc.total_usd)} {doc.currency}</td><td>{doc.status || "final"}</td><td><Button variant="link" onClick={toggle}>{expanded ? "Hide" : "View"} {kind === "invoices" ? "invoice" : "statement"} {doc.id}</Button></td></tr>
    {expanded && <tr><td colSpan={6}><DocumentDetail doc={doc} group={group} kind={kind} onDenied={onDenied} /></td></tr>}</>;
}
