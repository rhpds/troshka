"use client";
import { Button, Card, CardBody } from "@patternfly/react-core";
import { downloadCsv, money, ProjectLine, reportCsv, TeamReport, unit, UsageLine } from "@/lib/group-billing";
import styles from "@/components/groups/management.module.css";

export function ResourceUsage({ lines }: { lines: UsageLine[] }) {
  if (!lines.length) return <p>No resource usage available.</p>;
  return <table className="pf-v6-c-table pf-m-compact"><thead><tr><th>Resource</th><th>Quantity-hours</th><th>Unit</th><th>Rate (USD)</th><th>Cost (USD)</th></tr></thead><tbody>{lines.map((line, index) => <tr key={`${line.kind}-${index}`}><td>{line.kind}</td><td>{line.qty_hours}</td><td>{unit(line.kind)}</td><td>{line.unit_rate ?? "Unavailable"}</td><td>{money(line.subtotal)}</td></tr>)}</tbody></table>;
}
export function ProjectUsage({ projects }: { projects: ProjectLine[] }) {
  return projects.length ? <>{projects.map(project => <details key={project.project_id}><summary>{project.project_name} — {money(project.subtotal)}</summary><ResourceUsage lines={project.by_kind} /></details>)}</> : <p>No project usage in this period.</p>;
}
export default function TeamUsage({ report }: { report: TeamReport }) {
  const incomplete = report.members.some(member => member.status === "unavailable");
  return <>
    <Card><CardBody><h2>Monthly usage — {report.currency}</h2>
      <p><strong>{incomplete ? "Incomplete monthly total (available data only)" : "Monthly total"}: {money(report.total_usd)}</strong></p>
      {incomplete && <p role="alert">Some members’ usage is unavailable. This total excludes their missing costs and must not be treated as a complete group total.</p>}
      <Button variant="secondary" onClick={() => downloadCsv("team-monthly-usage.csv", reportCsv(report))}>Download monthly CSV</Button>
      <details className={styles.expandable}><summary>Reporting period and update time</summary><p className={styles.muted}>UTC period: {report.period_start} to {report.period_end}. As of {report.as_of}.</p></details>
      {!report.members.length && <p>No current members.</p>}
      {report.members.map(member => <details className={styles.expandable} key={member.owner_id}><summary>{member.owner_email} — {money(member.total_usd)}{member.status !== "unavailable" && <span className={styles.muted}> · {member.status}</span>}</summary><ProjectUsage projects={member.by_project} /></details>)}
    </CardBody></Card>
    <details className={styles.expandable}><summary>Active project lifetime spend ({report.active_projects.length})</summary><Card><CardBody><p>Lifetime values are separate from monthly usage and documents. They are never added to the monthly total.</p>
      {!report.active_projects.length && <p>No active projects.</p>}
      {report.active_projects.map(project => <details key={project.id}><summary>{project.name} — {project.owner_email} — {project.state} — {money(project.total_usd)}</summary><ResourceUsage lines={project.line_items?.by_kind || []} /></details>)}
    </CardBody></Card></details>
  </>;
}
