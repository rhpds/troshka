import { describe, expect, it } from "vitest";
import { csvCell, documentCsv, inMonth, money, reportCsv, TeamReport } from "./group-billing";
describe("group billing CSV", () => {
  it("formats exact decimal strings without losing precision or inventing missing money", () => {
    expect(money("9007199254740993.995")).toBe("$9007199254740994.00");
    expect(money("1.005")).toBe("$1.01");
    expect(money(null)).toBe("Unavailable");
    expect(money(undefined)).toBe("Unavailable");
  });
  it("exports known money and accounting status while missing quantity/rate and complete totals stay blank", () => {
    const report = { group: { id: "g", name: "g" }, accounting_status: "partial", known_subtotal_usd: "1.234", total_usd: null, members: [{ owner_id: "old", owner_email: null, display_name: "Former name", current_member: false, status: "provisional", accounting_status: "partial", known_subtotal_usd: "1.234", total_usd: null, by_project: [{ project_id: "p", project_name: "P", by_kind: [{ kind: "vcpu", subtotal: "1.234" }] }] }] } as unknown as TeamReport;
    const rows = reportCsv(report).split("\r\n").map(row => row.split(","));
    expect(rows[1][18]).toBe("");
    expect(rows[1]).toContain("1.234");
    expect(rows[1]).toContain("partial");
    expect(rows[2][14]).toBe(""); expect(rows[2][15]).toBe("");
    expect(rows[2][16]).toBe("1.234");
  });
  it.each([['=SUM(A1)', "'=SUM(A1)"], ['a,b', '"a,b"'], [' \t=cmd', "' \t=cmd"], ['\u0000+cmd', "'\u0000+cmd"], ['@cmd', "'@cmd"], ['a"b', '"a""b"']])("escapes %s", (input, expected) => expect(csvCell(input)).toBe(expected));
  it("separates document totals from resource costs", () => {
    const output = documentCsv({ id: "g", name: "=group", description: "" }, { id: "d", owner_id: "u", owner_email: "u@example.com", period_start: "2026-11-01", period_end: null, finalized_at: null, total_usd: 5, currency: "USD", line_items: { by_kind: [{ kind: "vcpu", qty_hours: 10, unit_rate: .5, subtotal: 5 }] } });
    const rows = output.split("\r\n").map(row => row.split(","));
    expect(rows[1][17]).toBe("5"); expect(rows[1][16]).toBe("");
    expect(rows[2][16]).toBe("5"); expect(rows[2][17]).toBe("");
    expect(rows[2][13]).toBe("vCPU-hours"); expect(rows[1][2]).toBe("'=group");
    expect(rows[1][23]).toBe("owner_billing_history"); expect(rows[2][23]).toBe("owner_billing_history");
  });
  it("exports unavailable members with a blank amount", () => {
    const report = { group: { id: "g", name: "g" }, members: [{ owner_id: "u", owner_email: "u", status: "unavailable", total_usd: null, by_project: [] }] } as unknown as TeamReport;
    expect(reportCsv(report).split("\r\n")[1].split(",")[18]).toBe("");
  });
  it("filters statements by UTC month and invoices by period overlap", () => {
    const doc = { period_start: "2026-10-20T00:00:00Z", period_end: "2026-11-02T00:00:00Z" } as Parameters<typeof inMonth>[0];
    expect(inMonth(doc, "2026-11")).toBe(false); expect(inMonth(doc, "2026-11", true)).toBe(true); expect(inMonth(doc, "2026-12", true)).toBe(false);
  });
  it("excludes invoices ending exactly at the UTC month boundary", () => {
    const doc = { period_start: "2026-10-20T00:00:00Z", period_end: "2026-11-01T00:00:00Z" } as Parameters<typeof inMonth>[0];
    expect(inMonth(doc, "2026-11", true)).toBe(false);
    expect(inMonth({ ...doc, period_end: "2026-11-01T00:00:00.001Z" }, "2026-11", true)).toBe(true);
  });
});
