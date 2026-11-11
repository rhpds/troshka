/** Exact quota transport: currency stays decimal text; int64 quantities never round. */
export type Scope = "organization" | "user" | "project";
export type Quantity = number | string;
export type Resources = Record<"cpu" | "ram_bytes" | "storage_bytes" | "projects", Quantity>;
export type Limits = { [K in keyof Resources]: Quantity | null } & { spending_usd: string | null };
export interface QuotaUsageData {
  scope: Scope; scope_id: string; limits: Limits; used: Resources; reserved: Resources;
  spent_usd: string | null; as_of: string | null; reset_at: string | null;
  accounting_status: "ready" | "unavailable"; enforcement_status: string; enforcement_enabled: boolean;
  baseline_ready: boolean; can_edit: boolean; cost_center_id: string | null; policy_revision: number;
  spending_state: string; action_state: string | null; action_counts: Record<string, number>; cause_ids: string[];
  storage_state?: string; storage_accounting_status?: "ready" | "unavailable";
  action_reasons?: string[];
  provisioned_storage_bytes?: Quantity | null; platform_storage_bytes?: Quantity | null; storage_as_of?: string | null;
}
export interface Organization { id: string; name: string; cost_center_id: string | null }
export interface OrganizationUser { id: string; email: string; display_name: string | null; organization_id: string }
type Denial = { reason?: string; scope?: string; dimension?: string; used?: Quantity; reserved?: Quantity; requested?: Quantity; limit?: Quantity | null; message?: string };
const dimensions: Record<string, string> = { cpu: "CPU", ram_bytes: "RAM bytes", storage_bytes: "physical storage bytes", projects: "deployed projects", spending_usd: "spending USD" };
export function formatQuotaError(body: unknown, fallback = "Request failed"): string {
  if (!body || typeof body !== "object") return fallback;
  const detail = "detail" in body ? body.detail : body;
  if (typeof detail === "string") return detail;
  if (!detail || typeof detail !== "object") return fallback;
  const denial = detail as Denial;
  if (denial.reason === "missing_organization" || denial.reason === "organization_required" || denial.message === "Project requires an assigned owner organization") return "Ask a platform administrator to assign the project owner to an organization before deploying.";
  if (denial.reason === "accounting_unavailable") return "Accounting unavailable. New growth is blocked until accounting is ready. Stop and cleanup remain available.";
  if (denial.reason === "unsupported_quota_operation") return "This operation is not supported with quota enforcement enabled. Only basic single-host nested VM whole-project operations on an existing shared billing host are currently supported.";
  if (denial.scope && denial.dimension) return `${denial.scope} ${dimensions[denial.dimension] || denial.dimension} limit: used ${denial.used ?? "unavailable"} + reserved ${denial.reserved ?? "unavailable"}, requested ${denial.requested ?? "unavailable"}, limit ${denial.limit ?? "unlimited"}. ${denial.reason || "Quota exceeded"}`;
  return denial.message || denial.reason || fallback;
}
export class QuotaApiError extends Error {
  constructor(public status: number, public detail: unknown, message: string) { super(message); }
}
export function isQuotaRevocation(error: unknown) { return error instanceof QuotaApiError && [403, 404].includes(error.status); }
export function parseExactJson(text: string): unknown {
  return JSON.parse(text.replace(/"(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/g, token => {
    if (/^-?\d+$/.test(token) && !Number.isSafeInteger(Number(token))) return JSON.stringify(token);
    return token;
  }));
}
export async function quotaFetch<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1${path}`, options);
  // text() retains numeric tokens beyond Number.MAX_SAFE_INTEGER.
  const body = await response.text().then(text => parseExactJson(text)).catch(() => null);
  if (!response.ok) throw new QuotaApiError(response.status, body && typeof body === "object" && "detail" in body ? body.detail : null, formatQuotaError(body, `Request failed (${response.status})`));
  if (body === null && response.status !== 204) throw new Error("Invalid quota response");
  return body as T;
}
export function quotaBody(limits: Partial<Limits>): string {
  return `{${Object.entries(limits).map(([key, value]) => `${JSON.stringify(key)}:${key === "spending_usd" || value === null ? JSON.stringify(value) : integerText(value)}`).join(",")}}`;
}
export function integerText(value: unknown): string {
  if ((typeof value === "number" && !Number.isSafeInteger(value)) || !/^\d+$/.test(String(value))) throw new Error("Enter a nonnegative whole quantity");
  const integer = BigInt(String(value));
  if (integer > BigInt("9223372036854775807")) throw new Error("Quantity exceeds the supported limit");
  return integer.toString();
}
export function bytesFromGiB(value: string): Quantity {
  if (!/^\d+(\.\d+)?$/.test(value)) throw new Error("Enter a nonnegative GiB quantity");
  const [whole, fraction = ""] = value.split(".");
  const scale = BigInt(10) ** BigInt(fraction.length);
  const numerator = BigInt(whole + fraction) * BigInt(1073741824);
  if (numerator % scale) throw new Error("GiB must convert to whole bytes");
  const bytes = integerText(numerator / scale);
  return Number.isSafeInteger(Number(bytes)) ? Number(bytes) : bytes;
}
export function giB(value: Quantity): string {
  const bytes = BigInt(integerText(value));
  const whole = bytes / BigInt(1073741824), remainder = bytes % BigInt(1073741824);
  return remainder ? `${whole}.${(remainder * BigInt(5) ** BigInt(30)).toString().padStart(30, "0").replace(/0+$/, "")}` : String(whole);
}
function decimalParts(value: string) {
  const match = /^(\d+)(?:\.(\d+))?(?:[eE]([+-]?\d+))?$/.exec(value);
  const invalid = () => new Error("Enter a nonnegative USD amount within Numeric(38,12) precision");
  if (!match) throw invalid();
  const fraction = match[2] || "";
  const coefficient = (match[1] + fraction).replace(/^0+/, "") || "0";
  const exponent = match[3] || "0";
  // Bound text and exponents before constructing powers/BigInts. Coefficients
  // never pass through Number, including backend Decimal strings such as 1E-12.
  if (coefficient.length > 38 || fraction.length > 38 || exponent.replace(/^[+-]?0*/, "").length > 3) throw invalid();
  const shift = BigInt(12) + BigInt(exponent) - BigInt(fraction.length);
  if (shift > BigInt(38) || shift < BigInt(-38)) throw invalid();
  const amount = BigInt(coefficient), scale = BigInt(10) ** (shift < BigInt(0) ? -shift : shift);
  if (shift < BigInt(0) && amount % scale !== BigInt(0)) throw invalid();
  const units = shift < BigInt(0) ? amount / scale : amount * scale;
  if (units > BigInt("99999999999999999999999999999999999999")) throw invalid();
  return units;
}
export function decimalCompare(a: string, b: string) { const first = decimalParts(a), second = decimalParts(b); return first === second ? 0 : first > second ? 1 : -1; }
export function spendingThreshold(spent: string, limit: string | null): number {
  if (limit === null) return 0;
  const amount = decimalParts(spent), cap = decimalParts(limit);
  if (amount >= cap) return 100;
  if (amount * BigInt(100) >= cap * BigInt(90)) return 90;
  return amount * BigInt(100) >= cap * BigInt(80) ? 80 : 0;
}
export function currency(value: string): string {
  const cents = (decimalParts(value) + BigInt(5000000000)) / BigInt(10000000000);
  return `$${cents / BigInt(100)}.${String(cents % BigInt(100)).padStart(2, "0")}`;
}
export function assertUsage(value: QuotaUsageData): QuotaUsageData {
  if (!value || !["organization", "user", "project"].includes(value.scope) || typeof value.can_edit !== "boolean" || typeof value.enforcement_enabled !== "boolean" || !["ready", "unavailable"].includes(value.accounting_status)) throw new Error("Invalid quota usage response");
  for (const key of ["cpu", "ram_bytes", "storage_bytes", "projects"] as const) {
    integerText(value.used[key]); integerText(value.reserved[key]);
    if (value.limits[key] !== null) integerText(value.limits[key]);
  }
  if (value.spent_usd !== null) decimalParts(value.spent_usd);
  if (value.limits.spending_usd !== null) decimalParts(value.limits.spending_usd);
  if (value.accounting_status === "ready" && (value.spent_usd === null || !value.as_of)) throw new Error("Missing quota accounting snapshot");
  return value;
}
export async function fetchUsage(scope: Scope, id: string) { return assertUsage(await quotaFetch<QuotaUsageData>(`/quotas/${scope}/${encodeURIComponent(id)}/usage`)); }
export async function saveLimits(scope: Scope, id: string, limits: Partial<Limits>) { return quotaFetch<Limits>(`/quotas/${scope}/${encodeURIComponent(id)}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: quotaBody(limits) }); }
