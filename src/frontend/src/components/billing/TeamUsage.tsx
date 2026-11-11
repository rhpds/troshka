"use client";
import { Button, Card, CardBody } from "@patternfly/react-core";
import { downloadCsv, money, ProjectLine, reportCsv, TeamReport, unit, UsageLine } from "@/lib/group-billing";
import styles from "@/components/groups/management.module.css";
import BillingTime from "./BillingTime";

const quantity = new Intl.NumberFormat("en-US", { maximumFractionDigits: 3 });
const rate = new Intl.NumberFormat("en-US", { maximumSignificantDigits: 6 });

export function ResourceUsage({ lines }: { lines: UsageLine[] }) {
  if (!lines.length) return <p>No resource usage available.</p>;
  return <div className={styles.table} role="region" aria-label="Resource usage" tabIndex={0}>
    <table className={`${styles.dataTable} ${styles.resourceTable}`} aria-label="Resource usage breakdown">
      <thead><tr><th scope="col">Resource</th><th scope="col" className={styles.numeric}>Quantity-hours</th><th scope="col">Unit</th><th scope="col" className={styles.numeric}>Rate (USD)</th><th scope="col" className={styles.numeric}>Cost (USD)</th></tr></thead>
      <tbody>{lines.map((line, index) => <tr key={`${line.kind}-${index}`}>
        <td>{line.kind}</td>
        <td className={styles.numeric} title={String(line.qty_hours)}>{quantity.format(line.qty_hours)}</td>
        <td className={styles.nowrap}>{unit(line.kind)}</td>
        <td className={styles.numeric} title={line.unit_rate == null ? undefined : String(line.unit_rate)}>{line.unit_rate == null ? "Unavailable" : rate.format(line.unit_rate)}</td>
        <td className={styles.numeric}>{money(line.subtotal)}</td>
      </tr>)}</tbody>
    </table>
  </div>;
}
export function ProjectUsage({ projects }: { projects: ProjectLine[] }) {
  return projects.length ? <>{projects.map(project => <details className={styles.projectDetails} key={project.project_id}><summary>{project.project_name} — {money(project.subtotal)}</summary><ResourceUsage lines={project.by_kind} /></details>)}</> : <p>No project usage in this period.</p>;
}
export default function TeamUsage({ report }: { report: TeamReport }) {
  const incomplete = report.members.some(member => member.status === "unavailable");
  return <>
    <Card><CardBody className={styles.cardContent}><h2>Monthly usage — {report.currency}</h2>
      <p className={styles.summaryTotal}><strong>{incomplete ? "Incomplete monthly total (available data only)" : "Monthly total"}: {money(report.total_usd)}</strong></p>
      {incomplete && <p role="alert" className={styles.notice}>Some members’ usage is unavailable. This total excludes their missing costs and must not be treated as a complete group total.</p>}
      <Button variant="secondary" onClick={() => downloadCsv("team-monthly-usage.csv", reportCsv(report))}>Download monthly CSV</Button>
      <details className={styles.expandable}><summary>Reporting period and update time</summary><p className={styles.muted}>UTC period: <BillingTime value={report.period_start} /> to <BillingTime value={report.period_end} />. As of <BillingTime value={report.as_of} /> UTC.</p></details>
      {!report.members.length && <p>No current members.</p>}
      {report.members.map(member => <details className={styles.expandable} key={member.owner_id}><summary>{member.owner_email} — {money(member.total_usd)}{member.status !== "unavailable" && <span className={styles.muted}> · {member.status}</span>}</summary><ProjectUsage projects={member.by_project} /></details>)}
    </CardBody></Card>
    <details className={styles.expandable}><summary>Active project lifetime spend ({report.active_projects.length})</summary><Card><CardBody className={styles.cardContent}><p>Lifetime values are separate from monthly usage and documents. They are never added to the monthly total.</p>
      {!report.active_projects.length && <p>No active projects.</p>}
      {report.active_projects.map(project => <details className={styles.projectDetails} key={project.id}><summary>{project.name} — {project.owner_email} — {project.state} — {money(project.total_usd)}</summary><ResourceUsage lines={project.line_items?.by_kind || []} /></details>)}
    </CardBody></Card></details>
  </>;
}
