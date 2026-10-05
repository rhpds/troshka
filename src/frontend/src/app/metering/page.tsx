"use client";

import React, { Fragment, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import {
  Alert,
  Button,
  Card,
  CardBody,
  PageSection,
  Title,
} from "@patternfly/react-core";
import AngleRightIcon from "@patternfly/react-icons/dist/esm/icons/angle-right-icon";
import AngleDownIcon from "@patternfly/react-icons/dist/esm/icons/angle-down-icon";

interface LineItem {
  kind: string;
  qty_hours: number;
  subtotal: number;
}

interface InvoiceRow {
  id: string;
  project_id: string;
  project_name: string;
  total_usd: number;
  currency: string;
  finalized_at: string | null;
}

interface ProjectRow {
  id: string;
  name: string;
  state: string;
}

interface LiveSpend {
  total_usd: number;
  budget_usd: number | null;
  budget_warned: boolean;
  budget_stopped: boolean;
  line_items?: { by_kind?: LineItem[] };
}

interface ProjectMonthLine {
  project_id: string;
  project_name: string;
  subtotal: number;
  by_kind: LineItem[];
}

interface StatementRow {
  id: string;
  period_start: string | null;
  period_end: string | null;
  total_usd: number;
  currency: string;
  finalized_at: string | null;
  project_count: number;
}

const KIND_LABELS: Record<string, string> = {
  vcpu: "vCPU",
  ram: "RAM",
  disk: "Disk",
  eip: "Elastic IP",
  ceph: "Ceph",
};

function money(n: number): string {
  return `$${n.toFixed(2)}`;
}

function kindLabel(kind: string): string {
  return KIND_LABELS[kind] || kind;
}

function BreakdownTable({ items }: { items: LineItem[] }) {
  if (!items.length) {
    return <div style={{ opacity: 0.65, fontSize: 13 }}>No usage yet.</div>;
  }
  return (
    <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
      <thead>
        <tr style={{ textAlign: "left", opacity: 0.75 }}>
          <th style={{ padding: "4px 8px 4px 0" }}>Kind</th>
          <th style={{ padding: "4px 8px" }}>Qty-hours</th>
          <th style={{ padding: "4px 0 4px 8px" }}>Subtotal</th>
        </tr>
      </thead>
      <tbody>
        {items.map((row) => (
          <tr key={row.kind}>
            <td style={{ padding: "4px 8px 4px 0" }}>{kindLabel(row.kind)}</td>
            <td style={{ padding: "4px 8px" }}>{row.qty_hours.toFixed(4)}</td>
            <td style={{ padding: "4px 0 4px 8px" }}>{money(row.subtotal)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function StatementBreakdown({ projects }: { projects: ProjectMonthLine[] }) {
  if (!projects.length) {
    return <div style={{ opacity: 0.65, fontSize: 13 }}>No usage in this month.</div>;
  }
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {projects.map((proj) => (
        <div key={proj.project_id}>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 4 }}>
            {proj.project_name}{" "}
            <span style={{ fontWeight: 400, opacity: 0.7 }}>{money(proj.subtotal)}</span>
          </div>
          <BreakdownTable items={proj.by_kind || []} />
        </div>
      ))}
    </div>
  );
}

function monthLabel(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString(undefined, { month: "long", year: "numeric", timeZone: "UTC" });
}

export default function MeteringPage() {
  const router = useRouter();
  const [tab, setTab] = useState<"active" | "past" | "invoices">("active");
  const [invoices, setInvoices] = useState<InvoiceRow[]>([]);
  const [statements, setStatements] = useState<StatementRow[]>([]);
  const [active, setActive] = useState<Array<ProjectRow & { spend?: LiveSpend }>>([]);
  const [error, setError] = useState("");
  const [disabled, setDisabled] = useState(false);
  const [expandedActive, setExpandedActive] = useState<Set<string>>(new Set());
  const [expandedInvoices, setExpandedInvoices] = useState<Set<string>>(new Set());
  const [expandedStatements, setExpandedStatements] = useState<Set<string>>(new Set());
  const [invoiceDetails, setInvoiceDetails] = useState<
    Record<string, { by_kind: LineItem[]; loading?: boolean; error?: string }>
  >({});
  const [statementDetails, setStatementDetails] = useState<
    Record<string, { by_project: ProjectMonthLine[]; loading?: boolean; error?: string }>
  >({});

  useEffect(() => {
    fetch("/api/v1/metering/statements")
      .then((r) => {
        if (r.status === 404) {
          setDisabled(true);
          return [];
        }
        return r.ok ? r.json() : [];
      })
      .then((data) => setStatements(Array.isArray(data) ? data : []))
      .catch(() => setError("Failed to load invoices"));
  }, []);

  useEffect(() => {
    if (disabled) return;
    fetch("/api/v1/metering/invoices")
      .then((r) => (r.ok ? r.json() : []))
      .then((data) => setInvoices(Array.isArray(data) ? data : []))
      .catch(() => {});
  }, [disabled]);

  useEffect(() => {
    if (tab !== "active" || disabled) return;
    fetch("/api/v1/projects/")
      .then((r) => (r.ok ? r.json() : []))
      .then(async (projects: ProjectRow[]) => {
        const open = (Array.isArray(projects) ? projects : []).filter(
          (p) => p.state !== "draft"
        );
        const rows = await Promise.all(
          open.map(async (p) => {
            const r = await fetch(`/api/v1/projects/${p.id}/metering`);
            const spend = r.ok ? ((await r.json()) as LiveSpend) : undefined;
            return { ...p, spend };
          })
        );
        setActive(rows);
      })
      .catch(() => setError("Failed to load live spend"));
  }, [tab, disabled]);

  const toggleActive = (id: string) => {
    setExpandedActive((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleInvoice = async (id: string) => {
    setExpandedInvoices((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
        return next;
      }
      next.add(id);
      return next;
    });
    if (invoiceDetails[id]?.by_kind || invoiceDetails[id]?.loading) return;
    setInvoiceDetails((prev) => ({
      ...prev,
      [id]: { by_kind: [], loading: true },
    }));
    try {
      const r = await fetch(`/api/v1/metering/invoices/${id}`);
      if (!r.ok) throw new Error("Failed to load invoice");
      const data = await r.json();
      setInvoiceDetails((prev) => ({
        ...prev,
        [id]: {
          by_kind: Array.isArray(data.line_items?.by_kind)
            ? data.line_items.by_kind
            : [],
        },
      }));
    } catch (e) {
      setInvoiceDetails((prev) => ({
        ...prev,
        [id]: {
          by_kind: [],
          error: e instanceof Error ? e.message : "Failed to load",
        },
      }));
    }
  };

  const toggleStatement = async (id: string) => {
    setExpandedStatements((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
        return next;
      }
      next.add(id);
      return next;
    });
    if (statementDetails[id]?.by_project || statementDetails[id]?.loading) return;
    setStatementDetails((prev) => ({
      ...prev,
      [id]: { by_project: [], loading: true },
    }));
    try {
      const r = await fetch(`/api/v1/metering/statements/${id}`);
      if (!r.ok) throw new Error("Failed to load statement");
      const data = await r.json();
      setStatementDetails((prev) => ({
        ...prev,
        [id]: {
          by_project: Array.isArray(data.line_items?.by_project)
            ? data.line_items.by_project
            : [],
        },
      }));
    } catch (e) {
      setStatementDetails((prev) => ({
        ...prev,
        [id]: {
          by_project: [],
          error: e instanceof Error ? e.message : "Failed to load",
        },
      }));
    }
  };

  if (disabled) {
    return (
      <PageSection>
        <Title headingLevel="h1">Metering</Title>
        <Alert variant="info" title="Metering is disabled on this instance" />
      </PageSection>
    );
  }

  const th = { padding: "8px 4px" };
  const td = { padding: "8px 4px" };
  const expandBtn: React.CSSProperties = {
    background: "none",
    border: "none",
    color: "inherit",
    cursor: "pointer",
    padding: "2px 6px",
    display: "inline-flex",
    alignItems: "center",
  };

  return (
    <PageSection>
      <Title headingLevel="h1">Metering</Title>
      <div style={{ display: "flex", gap: 8, margin: "16px 0" }}>
        <Button
          variant={tab === "active" ? "primary" : "secondary"}
          onClick={() => setTab("active")}
        >
          Active
        </Button>
        <Button
          variant={tab === "past" ? "primary" : "secondary"}
          onClick={() => setTab("past")}
        >
          Past
        </Button>
        <Button
          variant={tab === "invoices" ? "primary" : "secondary"}
          onClick={() => setTab("invoices")}
        >
          Invoices
        </Button>
      </div>
      {error && <Alert variant="danger" title={error} />}
      {tab === "invoices" && (
        <Card>
          <CardBody>
            {statements.length === 0 ? (
              <div style={{ opacity: 0.7 }}>
                No monthly invoices yet. They freeze after each UTC month closes (or when an admin finalizes).
              </div>
            ) : (
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 14 }}>
                <thead>
                  <tr style={{ textAlign: "left" }}>
                    <th style={{ ...th, width: 36 }} />
                    <th style={th}>Month</th>
                    <th style={th}>Projects</th>
                    <th style={th}>Total</th>
                    <th style={th}>Finalized</th>
                  </tr>
                </thead>
                <tbody>
                  {statements.map((row) => {
                    const open = expandedStatements.has(row.id);
                    const detail = statementDetails[row.id];
                    return (
                      <Fragment key={row.id}>
                        <tr
                          style={{
                            borderTop:
                              "1px solid var(--pf-t--global--border--color--default)",
                          }}
                        >
                          <td style={td}>
                            <button
                              type="button"
                              aria-label={open ? "Collapse breakdown" : "Expand breakdown"}
                              style={expandBtn}
                              onClick={() => toggleStatement(row.id)}
                            >
                              {open ? (
                                <AngleDownIcon style={{ width: 12, height: 12 }} />
                              ) : (
                                <AngleRightIcon style={{ width: 12, height: 12 }} />
                              )}
                            </button>
                          </td>
                          <td style={td}>{monthLabel(row.period_start)}</td>
                          <td style={td}>{row.project_count}</td>
                          <td style={td}>{money(row.total_usd)}</td>
                          <td style={td}>
                            {row.finalized_at
                              ? new Date(row.finalized_at).toLocaleString()
                              : "—"}
                          </td>
                        </tr>
                        {open && (
                          <tr>
                            <td colSpan={5} style={{ padding: "4px 8px 12px 40px" }}>
                              {detail?.loading && (
                                <div style={{ opacity: 0.65, fontSize: 13 }}>Loading…</div>
                              )}
                              {detail?.error && (
                                <div style={{ color: "#ef4444", fontSize: 13 }}>
                                  {detail.error}
                                </div>
                              )}
                              {detail && !detail.loading && !detail.error && (
                                <StatementBreakdown projects={detail.by_project} />
                              )}
                            </td>
                          </tr>
                        )}
                      </Fragment>
                    );
                  })}
                </tbody>
              </table>
            )}
          </CardBody>
        </Card>
      )}
      {tab === "active" && (
        <Card>
          <CardBody>
            {active.length === 0 ? (
              <div style={{ opacity: 0.7 }}>No active or stopped projects.</div>
            ) : (
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 14 }}>
                <thead>
                  <tr style={{ textAlign: "left" }}>
                    <th style={{ ...th, width: 36 }} />
                    <th style={th}>Project</th>
                    <th style={th}>State</th>
                    <th style={th}>Spend</th>
                    <th style={th}>Budget</th>
                  </tr>
                </thead>
                <tbody>
                  {active.map((row) => {
                    const open = expandedActive.has(row.id);
                    const items = row.spend?.line_items?.by_kind || [];
                    return (
                      <Fragment key={row.id}>
                        <tr
                          style={{
                            borderTop:
                              "1px solid var(--pf-t--global--border--color--default)",
                          }}
                        >
                          <td style={td}>
                            <button
                              type="button"
                              aria-label={open ? "Collapse breakdown" : "Expand breakdown"}
                              style={expandBtn}
                              onClick={() => toggleActive(row.id)}
                            >
                              {open ? (
                                <AngleDownIcon style={{ width: 12, height: 12 }} />
                              ) : (
                                <AngleRightIcon style={{ width: 12, height: 12 }} />
                              )}
                            </button>
                          </td>
                          <td style={td}>
                            <button
                              type="button"
                              style={{
                                ...expandBtn,
                                padding: 0,
                                textDecoration: "underline",
                                textUnderlineOffset: 2,
                              }}
                              onClick={() => router.push(`/projects/${row.id}`)}
                            >
                              {row.name}
                            </button>
                          </td>
                          <td style={td}>{row.state}</td>
                          <td style={td}>
                            {row.spend ? money(row.spend.total_usd) : "—"}
                          </td>
                          <td style={td}>
                            {row.spend?.budget_usd != null
                              ? money(row.spend.budget_usd)
                              : "—"}
                          </td>
                        </tr>
                        {open && (
                          <tr>
                            <td colSpan={5} style={{ padding: "4px 8px 12px 40px" }}>
                              <BreakdownTable items={items} />
                            </td>
                          </tr>
                        )}
                      </Fragment>
                    );
                  })}
                </tbody>
              </table>
            )}
          </CardBody>
        </Card>
      )}
      {tab === "past" && (
        <Card>
          <CardBody>
            {invoices.length === 0 ? (
              <div style={{ opacity: 0.7 }}>No past projects billed yet.</div>
            ) : (
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 14 }}>
                <thead>
                  <tr style={{ textAlign: "left" }}>
                    <th style={{ ...th, width: 36 }} />
                    <th style={th}>Project</th>
                    <th style={th}>Total</th>
                    <th style={th}>Finalized</th>
                  </tr>
                </thead>
                <tbody>
                  {invoices.map((row) => {
                    const open = expandedInvoices.has(row.id);
                    const detail = invoiceDetails[row.id];
                    return (
                      <Fragment key={row.id}>
                        <tr
                          style={{
                            borderTop:
                              "1px solid var(--pf-t--global--border--color--default)",
                          }}
                        >
                          <td style={td}>
                            <button
                              type="button"
                              aria-label={open ? "Collapse breakdown" : "Expand breakdown"}
                              style={expandBtn}
                              onClick={() => toggleInvoice(row.id)}
                            >
                              {open ? (
                                <AngleDownIcon style={{ width: 12, height: 12 }} />
                              ) : (
                                <AngleRightIcon style={{ width: 12, height: 12 }} />
                              )}
                            </button>
                          </td>
                          <td style={td}>
                            <button
                              type="button"
                              style={{
                                ...expandBtn,
                                padding: 0,
                                textDecoration: "underline",
                                textUnderlineOffset: 2,
                              }}
                              onClick={() => router.push(`/metering/${row.id}`)}
                            >
                              {row.project_name}
                            </button>
                          </td>
                          <td style={td}>{money(row.total_usd)}</td>
                          <td style={td}>
                            {row.finalized_at
                              ? new Date(row.finalized_at).toLocaleString()
                              : "—"}
                          </td>
                        </tr>
                        {open && (
                          <tr>
                            <td colSpan={4} style={{ padding: "4px 8px 12px 40px" }}>
                              {detail?.loading && (
                                <div style={{ opacity: 0.65, fontSize: 13 }}>Loading…</div>
                              )}
                              {detail?.error && (
                                <div style={{ color: "#ef4444", fontSize: 13 }}>
                                  {detail.error}
                                </div>
                              )}
                              {detail && !detail.loading && !detail.error && (
                                <BreakdownTable items={detail.by_kind} />
                              )}
                            </td>
                          </tr>
                        )}
                      </Fragment>
                    );
                  })}
                </tbody>
              </table>
            )}
          </CardBody>
        </Card>
      )}
    </PageSection>
  );
}
