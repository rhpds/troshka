/** Format FastAPI `detail` (string | object | array) for UI error display. */

export type SchemaErrorItem = {
  path?: string;
  message?: string;
};

export type SchemaErrorDetail = {
  message?: string;
  errors?: SchemaErrorItem[];
};

/** Turn API error `detail` into a multi-line string for alerts / modals. */
export function formatApiDetail(detail: unknown, fallback = "Request failed"): string {
  if (detail == null) return fallback;
  if (typeof detail === "string") return detail || fallback;
  if (Array.isArray(detail)) {
    // FastAPI request-validation errors: [{loc, msg, type}, ...]
    const lines = detail.map((item) => {
      if (typeof item === "string") return item;
      if (item && typeof item === "object") {
        const loc = Array.isArray((item as { loc?: unknown }).loc)
          ? (item as { loc: unknown[] }).loc.join(".")
          : "";
        const msg =
          (item as { msg?: string }).msg ||
          (item as { message?: string }).message ||
          JSON.stringify(item);
        return loc ? `${loc}: ${msg}` : String(msg);
      }
      return String(item);
    });
    return lines.filter(Boolean).join("\n") || fallback;
  }
  if (typeof detail === "object") {
    const obj = detail as SchemaErrorDetail;
    if (obj.message != null || Array.isArray(obj.errors)) {
      const lines: string[] = [];
      if (obj.message) lines.push(obj.message);
      for (const err of obj.errors || []) {
        if (err?.message) {
          lines.push(`${err.path || "/"}: ${err.message}`);
        }
      }
      return lines.join("\n") || fallback;
    }
    try {
      return JSON.stringify(detail);
    } catch {
      return fallback;
    }
  }
  return String(detail);
}
