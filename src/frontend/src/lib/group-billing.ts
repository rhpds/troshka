export interface Person { id: string; email: string; display_name: string | null }
export interface Group { id: string; name: string; description: string; member_count?: number; manager_count?: number }
export interface GroupDetail extends Group { members: Person[]; managers: Person[] }
export interface UsageLine { kind: string; qty_hours: number; unit_rate?: number; subtotal: number }
export interface ProjectLine { project_id: string; project_name: string; subtotal: number; by_kind: UsageLine[] }
export interface MemberUsage { owner_id: string; owner_email: string; display_name: string | null; status: string; total_usd: number | null; by_project: ProjectLine[] }
export interface TeamReport { group: Group; as_of: string; period_start: string; period_end: string; currency: string; total_usd: number; members: MemberUsage[]; active_projects: { id: string; name: string; state: string; owner_id: string; owner_email: string; total_usd: number | null; line_items: { by_kind: UsageLine[] } }[] }
export interface BillingDocument { id: string; project_id?: string; project_name?: string; owner_id: string; owner_email: string; period_start: string | null; period_end: string | null; total_usd: number; currency: string; status?: string; finalized_at: string | null; line_items?: { by_kind?: UsageLine[]; by_project?: ProjectLine[] } }

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}
export async function api<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1${url}`, options);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const detail = typeof body.detail === "string" ? body.detail : `Request failed (${response.status})`;
    throw new ApiError(response.status, detail);
  }
  return response.status === 204 ? undefined as T : response.json();
}
export const jsonBody = (method: string, body: unknown): RequestInit => ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
export const money = (value: number | null) => value === null ? "Unavailable" : `$${value.toFixed(2)}`;
export const utcMonth = () => new Date().toISOString().slice(0, 7);
export const unit = (kind: string) => ({ vcpu: "vCPU-hours", ram: "GiB-hours", disk: "GiB-hours", ceph: "GiB-hours", eip: "IP-hours" }[kind] || `${kind}-hours`);

export function csvCell(value: unknown): string {
  let text = value == null ? "" : String(value);
  if (/^[\s\u0000-\u001f\u007f]*[=+@-]/.test(text)) text = `'${text}`;
  return /[",\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}
const headers = ["row_type", "group_id", "group", "owner_id", "owner", "period_start_utc", "period_end_utc", "currency", "status", "document_id", "project_id", "project", "resource", "unit", "qty_hours", "unit_rate", "line_cost", "document_total", "member_total", "as_of_utc"];
export const csv = (rows: unknown[][]) => [headers, ...rows].map(row => headers.map((_, index) => csvCell(row[index])).join(",")).join("\r\n");
export function reportCsv(report: TeamReport): string {
  const rows: unknown[][] = [];
  for (const member of report.members) {
    const base = [report.group.id, report.group.name, member.owner_id, member.owner_email, report.period_start, report.period_end, report.currency, member.status];
    rows.push(["member_total", ...base, "", "", "", "", "", "", "", "", "", member.total_usd, report.as_of]);
    for (const project of member.by_project) for (const line of project.by_kind) {
      rows.push(["usage_line", ...base, "", project.project_id, project.project_name, line.kind, unit(line.kind), line.qty_hours, line.unit_rate, line.subtotal, "", "", report.as_of]);
    }
  }
  return csv(rows);
}
export function documentCsv(group: Group, doc: BillingDocument): string {
  const base = [group.id, group.name, doc.owner_id, doc.owner_email, doc.period_start, doc.period_end, doc.currency, doc.status || "final"];
  const rows: unknown[][] = [["document_total", ...base, doc.id, "", "", "", "", "", "", "", doc.total_usd]];
  const projects = doc.line_items?.by_project || [{ project_id: doc.project_id || "", project_name: doc.project_name || "", by_kind: doc.line_items?.by_kind || [] }];
  for (const project of projects) for (const line of project.by_kind) {
    rows.push(["usage_line", ...base, doc.id, project.project_id, project.project_name, line.kind, unit(line.kind), line.qty_hours, line.unit_rate, line.subtotal]);
  }
  return csv(rows);
}
export function inMonth(doc: BillingDocument, month: string, invoice = false): boolean {
  if (!doc.period_start) return false;
  if (!invoice) return doc.period_start.slice(0, 7) === month;
  const start = new Date(`${month}-01T00:00:00Z`);
  const end = new Date(start); end.setUTCMonth(end.getUTCMonth() + 1);
  return new Date(doc.period_start) < end && new Date(doc.period_end || doc.finalized_at || doc.period_start) >= start;
}
export function downloadCsv(name: string, content: string) {
  const url = URL.createObjectURL(new Blob([content], { type: "text/csv;charset=utf-8" }));
  const anchor = document.createElement("a"); anchor.href = url; anchor.download = name; anchor.click();
  URL.revokeObjectURL(url);
}
