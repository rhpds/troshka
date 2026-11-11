import { afterEach, expect, it, vi } from "vitest";
import { bytesFromGiB, currency, decimalCompare, fetchUsage, formatQuotaError, integerText, quotaFetch, quotaBody, saveLimits } from "./quotas";
import numericMoney from "./__fixtures__/quota-numeric-money.json";
import { exampleUsage } from "@/components/quotas/test-fixtures";
afterEach(() => vi.unstubAllGlobals());
it("preserves exact decimals and int64 byte quantities across transport", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response('{"spending_usd":"9007199254740993.123456789012","ram_bytes":9223372036854775807}')));
  const result = await quotaFetch<{ spending_usd: string; ram_bytes: string }>("/test");
  expect(result.spending_usd).toBe("9007199254740993.123456789012");
  expect(result.ram_bytes).toBe("9223372036854775807");
  expect(quotaBody({ ram_bytes: result.ram_bytes, spending_usd: "0.123456789012" })).toBe('{"ram_bytes":9223372036854775807,"spending_usd":"0.123456789012"}');
});
it("formats binding constraints and distinct unavailable/unsupported errors", () => {
  expect(formatQuotaError({ detail: { scope: "organization", dimension: "cpu", used: 4, reserved: 2, requested: 3, limit: 8, reason: "quota_exceeded" } })).toContain("organization CPU");
  expect(formatQuotaError({ detail: { reason: "missing_organization" } })).toMatch(/administrator.*organization/);
  expect(formatQuotaError({ detail: { reason: "accounting_unavailable" } })).toMatch(/Accounting unavailable/);
  expect(formatQuotaError({ detail: { reason: "unsupported_quota_operation" } })).toMatch(/not supported/);
});
it("retains structured failures and falls back for malformed errors", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response('{"detail":{"reason":"missing_organization"}}', { status: 409 })));
  await expect(quotaFetch("/test")).rejects.toMatchObject({ status: 409, detail: { reason: "missing_organization" } });
  vi.stubGlobal("fetch", vi.fn(async () => new Response("<html>bad gateway", { status: 502 })));
  await expect(quotaFetch("/test")).rejects.toThrow("Request failed (502)");
});
it("converts GiB exactly and compares decimals without binary arithmetic", () => {
  expect(bytesFromGiB("0.5")).toBe(536870912);
  expect(() => bytesFromGiB("0.0000000001")).toThrow(/whole bytes/);
  expect(decimalCompare("9007199254740993.01", "9007199254740993.00")).toBe(1);
});
it.each(Object.values(numericMoney))("accepts persisted Numeric(38,12) policy/spend without monetary Number conversion: %j", async money => {
  const exact = money.limits.spending_usd;
  const fetcher = vi.fn(async (...request: [string, RequestInit?]) => new Response(JSON.stringify(request[1]?.method === "PATCH" ? { ...exampleUsage.limits, ...money.limits } : { ...exampleUsage, ...money, limits: { ...exampleUsage.limits, ...money.limits } })));
  vi.stubGlobal("fetch", fetcher);
  const result = await fetchUsage("user", "u");
  expect(result.spent_usd).toBe(money.spent_usd);
  expect(result.limits.spending_usd).toBe(exact);
  expect(decimalCompare(exact, exact === "0E-12" ? "0" : "0.000000000001")).toBe(0);
  expect(currency(exact)).toBe("$0.00");
  await saveLimits("user", "u", { spending_usd: exact });
  expect(fetcher.mock.calls[1][1]?.body).toBe(JSON.stringify({ spending_usd: exact }));
});
it("validates int64 maximum exactly and rejects maximum plus one", () => {
  expect(integerText("9223372036854775807")).toBe("9223372036854775807");
  expect(() => integerText("9223372036854775808")).toThrow(/supported limit/);
});
it.each(["1E+100000000000", "1E-100000000000", "1E-13", "1E+26"])("rejects out-of-contract money before expansion: %s", amount => {
  expect(() => decimalCompare(amount, "0")).toThrow();
});
