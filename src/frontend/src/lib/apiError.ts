/** Format FastAPI `detail` (string | object | array) for UI error display. */

export type SchemaErrorItem = {
  path?: string;
  message?: string;
};

export type SchemaErrorDetail = {
  message?: string;
  errors?: SchemaErrorItem[];
};

function formatValidationItem(item: unknown): string {
  if (typeof item === "string") return item;
  if (!item || typeof item !== "object") return String(item);
  const error = item as { loc?: unknown; msg?: string; message?: string };
  const loc = Array.isArray(error.loc) ? error.loc.join(".") : "";
  const msg = error.msg || error.message || JSON.stringify(item);
  return loc ? `${loc}: ${msg}` : String(msg);
}

function formatSchemaError(obj: SchemaErrorDetail, fallback: string): string {
  const lines: string[] = [];
  if (obj.message) lines.push(obj.message);
  for (const err of obj.errors || []) {
    if (err?.message) lines.push(`${err.path || "/"}: ${err.message}`);
  }
  return lines.join("\n") || fallback;
}

/** Turn API error `detail` into a multi-line string for alerts / modals. */
export function formatApiDetail(detail: unknown, fallback = "Request failed"): string {
  if (detail == null) return fallback;
  if (typeof detail === "string") return detail || fallback;
  if (Array.isArray(detail)) {
    // FastAPI request-validation errors: [{loc, msg, type}, ...]
    const lines = detail.map(formatValidationItem);
    return lines.filter(Boolean).join("\n") || fallback;
  }
  if (typeof detail === "object") {
    const obj = detail as SchemaErrorDetail;
    if (obj.message != null || Array.isArray(obj.errors)) {
      return formatSchemaError(obj, fallback);
    }
    try {
      return JSON.stringify(detail);
    } catch {
      return fallback;
    }
  }
  return String(detail);
}
