"use client";
import { useEffect, useState } from "react";
import { Alert, Button, Card, CardBody } from "@patternfly/react-core";
import { api, ApiError, BillingDocument, documentCsv, downloadCsv, Group, money } from "@/lib/group-billing";
import { ProjectUsage, ResourceUsage } from "./TeamUsage";
import BillingTime from "./BillingTime";
import styles from "@/components/groups/management.module.css";

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
  return <div className={styles.cardContent}><p>Document total: {money(doc.total_usd)} {doc.currency}. Finalized: {doc.finalized_at ? <><BillingTime value={doc.finalized_at} /> UTC</> : "Not finalized"}.</p>
    <Button variant="secondary" onClick={() => downloadCsv(`team-${kind}-${doc.id}.csv`, documentCsv(group, { ...doc, ...detail }))}>Download document CSV</Button>
    {kind === "statements" ? <ProjectUsage projects={detail.line_items?.by_project || []} /> : <ResourceUsage lines={detail.line_items?.by_kind || []} />}
  </div>;
}
export default function TeamDocuments({ docs, group, kind, onDenied }: { docs: BillingDocument[]; group: Group; kind: "invoices" | "statements"; onDenied: (message: string) => void }) {
  const [expanded, setExpanded] = useState<string | null>(null);
  return <Card><CardBody className={styles.cardContent}><h2>{kind === "invoices" ? "Project invoices" : "Monthly statements"}</h2>
    {kind === "invoices" && <p>Invoices whose lifetime billing periods overlap the selected month. Invoice totals cover their full periods.</p>}
    {!docs.length ? <p>No {kind} for this selection.</p> : <div className={styles.table} role="region" aria-label={kind === "invoices" ? "Project invoices" : "Monthly statements"} tabIndex={0}>
      <table className={`${styles.dataTable} ${styles.documentTable}`} aria-label={kind === "invoices" ? "Project invoices" : "Monthly statements"}>
        <thead><tr><th scope="col">Owner</th><th scope="col">Document / project</th><th scope="col">Period (UTC)</th><th scope="col" className={styles.numeric}>Total</th><th scope="col">Status</th><th scope="col">Details / export</th></tr></thead>
        <tbody>{docs.map(doc => <DocumentRow key={doc.id} doc={doc} group={group} kind={kind} expanded={expanded === doc.id} toggle={() => setExpanded(expanded === doc.id ? null : doc.id)} onDenied={onDenied} />)}</tbody>
      </table>
    </div>}
  </CardBody></Card>;
}
function DocumentRow({ doc, group, kind, expanded, toggle, onDenied }: { doc: BillingDocument; group: Group; kind: string; expanded: boolean; toggle: () => void; onDenied: (message: string) => void }) {
  const label = `${expanded ? "Hide" : "View"} ${kind === "invoices" ? "invoice" : "statement"}`;
  return <><tr>
    <td className={styles.identity}>{doc.owner_email}</td>
    <td className={styles.identity}>{doc.project_name || (kind === "invoices" ? "Project invoice" : "Monthly statement")}<span className={styles.secondary}>{doc.id}</span></td>
    <td className={styles.period}><BillingTime value={doc.period_start} /><span className={styles.secondary}>to <BillingTime value={doc.period_end} /></span></td>
    <td className={styles.numeric}>{money(doc.total_usd)} <span className={styles.secondary}>{doc.currency}</span></td>
    <td className={styles.nowrap}>{doc.status || "final"}</td>
    <td className={styles.nowrap}><Button variant="link" isInline aria-label={`${label} ${doc.id}`} aria-expanded={expanded} onClick={toggle}>{label}</Button></td>
  </tr>
    {expanded && <tr><td colSpan={6}><div className={styles.expandedContent}><DocumentDetail doc={doc} group={group} kind={kind} onDenied={onDenied} /></div></td></tr>}</>;
}
