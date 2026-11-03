import { describe, expect, it } from "vitest";
import { csvCell, documentCsv, inMonth, reportCsv, TeamReport } from "./group-billing";
describe("group billing CSV", () => {
  it.each([['=SUM(A1)', "'=SUM(A1)"], ['a,b', '"a,b"'], [' \t=cmd', "' \t=cmd"], ['\u0000+cmd', "'\u0000+cmd"], ['@cmd', "'@cmd"], ['a"b', '"a""b"']])("escapes %s", (input, expected) => expect(csvCell(input)).toBe(expected));
  it("separates document totals from resource costs", () => {
    const output = documentCsv({ id: "g", name: "=group", description: "" }, { id: "d", owner_id: "u", owner_email: "u@example.com", period_start: "2026-11-01", period_end: null, finalized_at: null, total_usd: 5, currency: "USD", line_items: { by_kind: [{ kind: "vcpu", qty_hours: 10, unit_rate: .5, subtotal: 5 }] } });
    const rows = output.split("\r\n").map(row => row.split(","));
    expect(rows[1][17]).toBe("5"); expect(rows[1][16]).toBe("");
    expect(rows[2][16]).toBe("5"); expect(rows[2][17]).toBe("");
    expect(rows[2][13]).toBe("vCPU-hours"); expect(rows[1][2]).toBe("'=group");
  });
  it("exports unavailable members with a blank amount", () => {
    const report = { group: { id: "g", name: "g" }, members: [{ owner_id: "u", owner_email: "u", status: "unavailable", total_usd: null, by_project: [] }] } as unknown as TeamReport;
    expect(reportCsv(report).split("\r\n")[1].split(",")[18]).toBe("");
  });
  it("filters statements by UTC month and invoices by period overlap", () => {
    const doc = { period_start: "2026-10-20T00:00:00Z", period_end: "2026-11-02T00:00:00Z" } as Parameters<typeof inMonth>[0];
    expect(inMonth(doc, "2026-11")).toBe(false); expect(inMonth(doc, "2026-11", true)).toBe(true); expect(inMonth(doc, "2026-12", true)).toBe(false);
  });
});
