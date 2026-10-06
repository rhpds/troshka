"use client";

import React, { Fragment, Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
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
  hours?: number;
  qty_hours: number;
  unit_rate?: number;
  subtotal: number;
}

interface InvoiceRow {
  id: string;
  project_id: string;
  project_name: string;
  owner_id?: string | null;
  owner_email?: string | null;
  total_usd: number;
  currency: string;
  period_start?: string | null;
  finalized_at: string | null;
}

interface ProjectRow {
  id: string;
  name: string;
  state: string;
  created_at?: string | null;
  owner_id?: string | null;
  owner_email?: string | null;
}

interface MeteringFilters {
  name: string;
  user: string;
  startFrom: string;
  startTo: string;
  finalFrom: string;
  finalTo: string;
  totalMin: string;
  totalMax: string;
}

const EMPTY_FILTERS: MeteringFilters = {
  name: "",
  user: "",
  startFrom: "",
  startTo: "",
  finalFrom: "",
  finalTo: "",
  totalMin: "",
  totalMax: "",
};

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
  owner_id?: string | null;
  owner_email?: string | null;
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

function effectiveRate(row: LineItem): number | null {
  if (row.unit_rate != null && !Number.isNaN(row.unit_rate)) return row.unit_rate;
  if (row.qty_hours > 0) return row.subtotal / row.qty_hours;
  return null;
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
          <th style={{ padding: "4px 8px" }}>Rate</th>
          <th style={{ padding: "4px 8px" }}>Hours</th>
          <th style={{ padding: "4px 0 4px 8px" }}>Subtotal</th>
        </tr>
      </thead>
      <tbody>
        {items.map((row) => {
          const rate = effectiveRate(row);
          return (
            <tr key={row.kind}>
              <td style={{ padding: "4px 8px 4px 0" }}>{kindLabel(row.kind)}</td>
              <td style={{ padding: "4px 8px" }}>
                {rate != null ? `$${rate.toFixed(6)}` : "—"}
              </td>
              <td style={{ padding: "4px 8px" }}>
                {row.hours != null ? row.hours.toFixed(4) : "—"}
              </td>
              <td style={{ padding: "4px 0 4px 8px" }}>{money(row.subtotal)}</td>
            </tr>
          );
        })}
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

function csvCell(value: string | number | null | undefined): string {
  if (value == null || value === "") return "";
  const s = typeof value === "number" ? String(value) : String(value);
  if (/[",\n\r]/.test(s)) return `"${s.replace(/"/g, '""')}"`;
  return s;
}

function downloadCsv(
  filename: string,
  header: string[],
  rows: Array<Array<string | number | null | undefined>>
) {
  const body = [header, ...rows].map((row) => row.map(csvCell).join(",")).join("\r\n");
  const blob = new Blob(["\uFEFF" + body + "\r\n"], {
    type: "text/csv;charset=utf-8",
  });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

function parseStatementProjects(data: {
  line_items?: { by_project?: ProjectMonthLine[] };
}): ProjectMonthLine[] {
  return Array.isArray(data.line_items?.by_project)
    ? data.line_items.by_project
    : [];
}

const INVOICE_CSV_HEADER = [
  "Month",
  "Owner",
  "Period start",
  "Project",
  "Kind",
  "Rate",
  "Hours",
  "Line subtotal",
  "Invoice total",
  "Finalized",
];

function invoiceExportRows(
  stmt: StatementRow,
  projects: ProjectMonthLine[],
  nameQ: string
): Array<Array<string | number | null | undefined>> {
  const month = monthLabel(stmt.period_start);
  const visible = nameQ
    ? projects.filter((p) => p.project_name.toLowerCase().includes(nameQ))
    : projects;
  const rows: Array<Array<string | number | null | undefined>> = [];
  const push = (projectName: string, item: LineItem | null, lineTotal: number) => {
    const rate = item ? effectiveRate(item) : null;
    rows.push([
      month,
      stmt.owner_email || "",
      stmt.period_start || "",
      projectName,
      item ? kindLabel(item.kind) : "",
      rate != null ? rate : "",
      item ? (item.hours ?? item.qty_hours) : "",
      lineTotal,
      stmt.total_usd,
      stmt.finalized_at || "",
    ]);
  };
  if (!visible.length) {
    push("", null, stmt.total_usd);
    return rows;
  }
  for (const proj of visible) {
    const kinds = proj.by_kind || [];
    if (!kinds.length) {
      push(proj.project_name, null, proj.subtotal);
      continue;
    }
    for (const item of kinds) {
      push(proj.project_name, item, item.subtotal);
    }
  }
  return rows;
}

function dayStartMs(ymd: string): number {
  return new Date(`${ymd}T00:00:00`).getTime();
}

function dayEndMs(ymd: string): number {
  return new Date(`${ymd}T23:59:59.999`).getTime();
}

function inDateRange(iso: string | null | undefined, from: string, to: string): boolean {
  if (!from && !to) return true;
  if (!iso) return false;
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return false;
  if (from && t < dayStartMs(from)) return false;
  if (to && t > dayEndMs(to)) return false;
  return true;
}

function inTotalRange(total: number | null | undefined, minStr: string, maxStr: string): boolean {
  if (total == null || Number.isNaN(total)) {
    return !minStr && !maxStr;
  }
  if (minStr !== "") {
    const min = Number(minStr);
    if (!Number.isNaN(min) && total < min) return false;
  }
  if (maxStr !== "") {
    const max = Number(maxStr);
    if (!Number.isNaN(max) && total > max) return false;
  }
  return true;
}

function nameMatches(haystack: string, needle: string): boolean {
  if (!needle.trim()) return true;
  return haystack.toLowerCase().includes(needle.trim().toLowerCase());
}

function filtersActive(f: MeteringFilters): boolean {
  return Object.values(f).some((v) => v !== "");
}

function matchRow(
  f: MeteringFilters,
  opts: {
    name: string;
    user?: string | null;
    total?: number | null;
    start?: string | null;
    final?: string | null;
  }
): boolean {
  return (
    nameMatches(opts.name, f.name) &&
    nameMatches(opts.user || "", f.user) &&
    inTotalRange(opts.total, f.totalMin, f.totalMax) &&
    inDateRange(opts.start, f.startFrom, f.startTo) &&
    inDateRange(opts.final, f.finalFrom, f.finalTo)
  );
}

type MeteringTab = "active" | "past" | "invoices";
type SortDir = "asc" | "desc";

interface TabSort {
  key: string;
  dir: SortDir;
}

const DEFAULT_SORT: Record<MeteringTab, TabSort> = {
  active: { key: "name", dir: "asc" },
  past: { key: "finalized", dir: "desc" },
  invoices: { key: "month", dir: "desc" },
};

function compareSortValues(
  a: string | number | null | undefined,
  b: string | number | null | undefined,
  dir: SortDir
): number {
  const mul = dir === "asc" ? 1 : -1;
  if (a == null && b == null) return 0;
  if (a == null) return 1;
  if (b == null) return -1;
  if (typeof a === "number" && typeof b === "number") return (a - b) * mul;
  return String(a).localeCompare(String(b), undefined, { sensitivity: "base" }) * mul;
}

function SortableTh({
  label,
  col,
  sort,
  onSort,
  style,
}: {
  label: string;
  col: string;
  sort: TabSort;
  onSort: (col: string) => void;
  style?: React.CSSProperties;
}) {
  const active = sort.key === col;
  return (
    <th
      style={{ ...style, cursor: "pointer", userSelect: "none", whiteSpace: "nowrap" }}
      onClick={() => onSort(col)}
      aria-sort={active ? (sort.dir === "asc" ? "ascending" : "descending") : "none"}
    >
      {label}
      {active ? (sort.dir === "asc" ? " ↑" : " ↓") : ""}
    </th>
  );
}

const filterInputStyle: React.CSSProperties = {
  padding: "6px 8px",
  borderRadius: 6,
  border: "1px solid var(--pf-t--global--border--color--default)",
  background: "var(--pf-t--global--background--color--primary--default)",
  color: "var(--pf-t--global--text--color--regular)",
  fontSize: 13,
  minWidth: 0,
};

const filterLabelStyle: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: 4,
  fontSize: 12,
  opacity: 0.85,
};

function MeteringFilterBar({
  filters,
  onChange,
  onClear,
  showUser,
}: {
  filters: MeteringFilters;
  onChange: (next: MeteringFilters) => void;
  onClear: () => void;
  showUser?: boolean;
}) {
  const set = (key: keyof MeteringFilters, value: string) =>
    onChange({ ...filters, [key]: value });
  return (
    <div
      style={{
        display: "flex",
        flexWrap: "wrap",
        gap: 12,
        alignItems: "flex-end",
        marginBottom: 16,
      }}
    >
      <label style={{ ...filterLabelStyle, flex: "1 1 160px" }}>
        Project name
        <input
          style={filterInputStyle}
          value={filters.name}
          onChange={(e) => set("name", e.target.value)}
          placeholder="Contains…"
        />
      </label>
      {showUser && (
        <label style={{ ...filterLabelStyle, flex: "1 1 180px" }}>
          User
          <input
            style={filterInputStyle}
            value={filters.user}
            onChange={(e) => set("user", e.target.value)}
            placeholder="Email contains…"
          />
        </label>
      )}
      <label style={filterLabelStyle}>
        Started from
        <input
          type="date"
          style={filterInputStyle}
          value={filters.startFrom}
          onChange={(e) => set("startFrom", e.target.value)}
        />
      </label>
      <label style={filterLabelStyle}>
        Started to
        <input
          type="date"
          style={filterInputStyle}
          value={filters.startTo}
          onChange={(e) => set("startTo", e.target.value)}
        />
      </label>
      <label style={filterLabelStyle}>
        Finalized from
        <input
          type="date"
          style={filterInputStyle}
          value={filters.finalFrom}
          onChange={(e) => set("finalFrom", e.target.value)}
        />
      </label>
      <label style={filterLabelStyle}>
        Finalized to
        <input
          type="date"
          style={filterInputStyle}
          value={filters.finalTo}
          onChange={(e) => set("finalTo", e.target.value)}
        />
      </label>
      <label style={filterLabelStyle}>
        Total min
        <input
          type="number"
          step="0.01"
          min="0"
          style={{ ...filterInputStyle, width: 96 }}
          value={filters.totalMin}
          onChange={(e) => set("totalMin", e.target.value)}
          placeholder="$"
        />
      </label>
      <label style={filterLabelStyle}>
        Total max
        <input
          type="number"
          step="0.01"
          min="0"
          style={{ ...filterInputStyle, width: 96 }}
          value={filters.totalMax}
          onChange={(e) => set("totalMax", e.target.value)}
          placeholder="$"
        />
      </label>
      {filtersActive(filters) && (
        <Button variant="link" onClick={onClear}>
          Clear filters
        </Button>
      )}
    </div>
  );
}

function parseTab(raw: string | null): MeteringTab {
  if (raw === "past" || raw === "invoices") return raw;
  return "active";
}

export default function MeteringPage() {
  return (
    <Suspense fallback={<PageSection><Title headingLevel="h1">Metering</Title></PageSection>}>
      <MeteringPageInner />
    </Suspense>
  );
}

function MeteringPageInner() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const tab = parseTab(searchParams.get("tab"));
  const setTab = (next: MeteringTab) => {
    router.replace(next === "active" ? "/metering" : `/metering?tab=${next}`);
  };
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
  const [filters, setFilters] = useState<MeteringFilters>(EMPTY_FILTERS);
  const [sortByTab, setSortByTab] = useState(DEFAULT_SORT);
  const [exporting, setExporting] = useState(false);
  const [isAdmin, setIsAdmin] = useState(false);
  const sort = sortByTab[tab];
  const setSortCol = (col: string) => {
    setSortByTab((prev) => {
      const cur = prev[tab];
      if (cur.key === col) {
        return { ...prev, [tab]: { key: col, dir: cur.dir === "asc" ? "desc" : "asc" } };
      }
      const defaultDesc = [
        "spend",
        "total",
        "budget",
        "finalized",
        "started",
        "month",
        "projects",
      ].includes(col);
      return { ...prev, [tab]: { key: col, dir: defaultDesc ? "desc" : "asc" } };
    });
  };

  useEffect(() => {
    fetch("/api/v1/auth/me")
      .then((r) => (r.ok ? r.json() : {}))
      .then((d: { role?: string }) => setIsAdmin(d.role === "admin"))
      .catch(() => setIsAdmin(false));
  }, []);

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

  const loadInvoiceDetail = async (id: string) => {
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

  const loadStatementDetail = async (id: string) => {
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
        [id]: { by_project: parseStatementProjects(data) },
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
    await loadInvoiceDetail(id);
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
    await loadStatementDetail(id);
  };

  // Invoices tab: project-name filter needs per-statement project lines.
  useEffect(() => {
    if (tab !== "invoices" || !filters.name.trim()) return;
    statements.forEach((s) => {
      void loadStatementDetail(s.id);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only when name filter / list changes
  }, [tab, filters.name, statements]);

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
  const ownerTh = isAdmin ? (
    <SortableTh label="Owner" col="owner" sort={sort} onSort={setSortCol} style={th} />
  ) : null;
  const rowColSpan = isAdmin ? 6 : 5;

  // Active rows have created_at as "Started"; finalized does not apply.
  const filteredActive = active
    .filter(
      (row) =>
        nameMatches(row.name, filters.name) &&
        nameMatches(row.owner_email || "", filters.user) &&
        inTotalRange(row.spend?.total_usd, filters.totalMin, filters.totalMax) &&
        inDateRange(row.created_at, filters.startFrom, filters.startTo)
    )
    .slice()
    .sort((a, b) => {
      const key = sortByTab.active.key;
      if (key === "state") return compareSortValues(a.state, b.state, sortByTab.active.dir);
      if (key === "spend") {
        return compareSortValues(a.spend?.total_usd, b.spend?.total_usd, sortByTab.active.dir);
      }
      if (key === "budget") {
        return compareSortValues(a.spend?.budget_usd, b.spend?.budget_usd, sortByTab.active.dir);
      }
      if (key === "owner") {
        return compareSortValues(a.owner_email, b.owner_email, sortByTab.active.dir);
      }
      return compareSortValues(a.name, b.name, sortByTab.active.dir);
    });

  const filteredInvoices = invoices
    .filter((row) =>
      matchRow(filters, {
        name: row.project_name,
        user: row.owner_email,
        total: row.total_usd,
        start: row.period_start,
        final: row.finalized_at,
      })
    )
    .slice()
    .sort((a, b) => {
      const key = sortByTab.past.key;
      if (key === "total") return compareSortValues(a.total_usd, b.total_usd, sortByTab.past.dir);
      if (key === "started") {
        return compareSortValues(a.period_start, b.period_start, sortByTab.past.dir);
      }
      if (key === "finalized") {
        return compareSortValues(a.finalized_at, b.finalized_at, sortByTab.past.dir);
      }
      if (key === "owner") {
        return compareSortValues(a.owner_email, b.owner_email, sortByTab.past.dir);
      }
      return compareSortValues(a.project_name, b.project_name, sortByTab.past.dir);
    });

  const nameQ = filters.name.trim().toLowerCase();
  const userQ = filters.user.trim().toLowerCase();
  const filteredStatements = statements
    .filter((row) => {
      if (!inTotalRange(row.total_usd, filters.totalMin, filters.totalMax)) return false;
      if (!inDateRange(row.period_start, filters.startFrom, filters.startTo)) return false;
      if (!inDateRange(row.finalized_at, filters.finalFrom, filters.finalTo)) return false;
      if (userQ && !(row.owner_email || "").toLowerCase().includes(userQ)) return false;
      if (!nameQ) return true;
      const detail = statementDetails[row.id];
      if (!detail || detail.loading || detail.error) return false;
      return detail.by_project.some((p) =>
        p.project_name.toLowerCase().includes(nameQ)
      );
    })
    .slice()
    .sort((a, b) => {
      const key = sortByTab.invoices.key;
      if (key === "projects") {
        return compareSortValues(a.project_count, b.project_count, sortByTab.invoices.dir);
      }
      if (key === "total") {
        return compareSortValues(a.total_usd, b.total_usd, sortByTab.invoices.dir);
      }
      if (key === "finalized") {
        return compareSortValues(a.finalized_at, b.finalized_at, sortByTab.invoices.dir);
      }
      if (key === "owner") {
        return compareSortValues(a.owner_email, b.owner_email, sortByTab.invoices.dir);
      }
      return compareSortValues(a.period_start, b.period_start, sortByTab.invoices.dir);
    });

  const noFilterMatches = "No rows match the current filters.";
  const statementsNamePending =
    !!nameQ &&
    statements.some((s) => !statementDetails[s.id] || statementDetails[s.id]?.loading);

  const exportInvoicesCsv = async () => {
    if (!filteredStatements.length || exporting) return;
    setExporting(true);
    try {
      const rows: Array<Array<string | number | null | undefined>> = [];
      for (const stmt of filteredStatements) {
        const cached = statementDetails[stmt.id];
        let projects: ProjectMonthLine[];
        if (cached && !cached.loading && !cached.error) {
          projects = cached.by_project;
        } else {
          const r = await fetch(`/api/v1/metering/statements/${stmt.id}`);
          if (!r.ok) throw new Error("Failed to load statement");
          projects = parseStatementProjects(await r.json());
          setStatementDetails((prev) => ({
            ...prev,
            [stmt.id]: { by_project: projects },
          }));
        }
        rows.push(...invoiceExportRows(stmt, projects, nameQ));
      }
      downloadCsv("troshka-invoices.csv", INVOICE_CSV_HEADER, rows);
    } catch {
      setError("Failed to export invoices");
    } finally {
      setExporting(false);
    }
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
      <MeteringFilterBar
        filters={filters}
        onChange={setFilters}
        onClear={() => setFilters(EMPTY_FILTERS)}
        showUser={isAdmin}
      />
      {error && <Alert variant="danger" title={error} />}
      {tab === "invoices" && (
        <Card>
          <CardBody>
            <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 12 }}>
              <Button
                variant="secondary"
                onClick={() => void exportInvoicesCsv()}
                isDisabled={filteredStatements.length === 0 || exporting || statementsNamePending}
              >
                {exporting ? "Exporting…" : "Export CSV"}
              </Button>
            </div>
            {statements.length === 0 ? (
              <div style={{ opacity: 0.7 }}>
                No monthly invoices yet.
              </div>
            ) : statementsNamePending && filteredStatements.length === 0 ? (
              <div style={{ opacity: 0.7 }}>Loading project names…</div>
            ) : filteredStatements.length === 0 ? (
              <div style={{ opacity: 0.7 }}>{noFilterMatches}</div>
            ) : (
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 14 }}>
                <thead>
                  <tr style={{ textAlign: "left" }}>
                    <th style={{ ...th, width: 36 }} />
                    <SortableTh label="Month" col="month" sort={sort} onSort={setSortCol} style={th} />
                    {ownerTh}
                    <SortableTh label="Projects" col="projects" sort={sort} onSort={setSortCol} style={th} />
                    <SortableTh label="Total" col="total" sort={sort} onSort={setSortCol} style={th} />
                    <SortableTh label="Finalized" col="finalized" sort={sort} onSort={setSortCol} style={th} />
                  </tr>
                </thead>
                <tbody>
                  {filteredStatements.map((row) => {
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
                          {isAdmin && <td style={td}>{row.owner_email || "—"}</td>}
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
                            <td colSpan={rowColSpan} style={{ padding: "4px 8px 12px 40px" }}>
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
            ) : filteredActive.length === 0 ? (
              <div style={{ opacity: 0.7 }}>{noFilterMatches}</div>
            ) : (
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 14 }}>
                <thead>
                  <tr style={{ textAlign: "left" }}>
                    <th style={{ ...th, width: 36 }} />
                    <SortableTh label="Project" col="name" sort={sort} onSort={setSortCol} style={th} />
                    {ownerTh}
                    <SortableTh label="State" col="state" sort={sort} onSort={setSortCol} style={th} />
                    <SortableTh label="Spend" col="spend" sort={sort} onSort={setSortCol} style={th} />
                    <SortableTh label="Budget" col="budget" sort={sort} onSort={setSortCol} style={th} />
                  </tr>
                </thead>
                <tbody>
                  {filteredActive.map((row) => {
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
                          {isAdmin && <td style={td}>{row.owner_email || "—"}</td>}
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
                            <td colSpan={rowColSpan} style={{ padding: "4px 8px 12px 40px" }}>
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
            ) : filteredInvoices.length === 0 ? (
              <div style={{ opacity: 0.7 }}>{noFilterMatches}</div>
            ) : (
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 14 }}>
                <thead>
                  <tr style={{ textAlign: "left" }}>
                    <th style={{ ...th, width: 36 }} />
                    <SortableTh label="Project" col="name" sort={sort} onSort={setSortCol} style={th} />
                    {ownerTh}
                    <SortableTh label="Total" col="total" sort={sort} onSort={setSortCol} style={th} />
                    <SortableTh label="Started" col="started" sort={sort} onSort={setSortCol} style={th} />
                    <SortableTh label="Finalized" col="finalized" sort={sort} onSort={setSortCol} style={th} />
                  </tr>
                </thead>
                <tbody>
                  {filteredInvoices.map((row) => {
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
                          {isAdmin && <td style={td}>{row.owner_email || "—"}</td>}
                          <td style={td}>{money(row.total_usd)}</td>
                          <td style={td}>
                            {row.period_start
                              ? new Date(row.period_start).toLocaleString()
                              : "—"}
                          </td>
                          <td style={td}>
                            {row.finalized_at
                              ? new Date(row.finalized_at).toLocaleString()
                              : "—"}
                          </td>
                        </tr>
                        {open && (
                          <tr>
                            <td colSpan={rowColSpan} style={{ padding: "4px 8px 12px 40px" }}>
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
