import { render, screen, cleanup, within } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import QuotaUsage from "./QuotaUsage";
import { exampleUsage } from "./test-fixtures";
afterEach(cleanup);
it("separates allocated storage, provisioned billing and storage shutdown causes", () => {
  render(<QuotaUsage usage={{ ...exampleUsage, enforcement_enabled: true, storage_accounting_status: "ready",
    storage_state: "storage_exceeded_shutdown_pending", action_state: "failed",
    provisioned_storage_bytes: 100 * 1073741824, platform_storage_bytes: 1073741824,
    used: { ...exampleUsage.used, storage_bytes: 1073741824 } }} />);
  expect(screen.getByRole("row", { name: /Physical storage \(GiB\) 1/ })).toBeVisible();
  expect(screen.getByText(/Provisioned disk capacity: 100 GiB/)).toBeVisible();
  expect(screen.getByText("Storage quota shutdown failed; retry pending")).toBeVisible();
  expect(screen.queryByText("Budget shutdown failed; retry pending")).not.toBeInTheDocument();
});
it("shows used plus reserved, zero versus unlimited, monthly UTC and missing cost center", () => {
  render(<QuotaUsage usage={exampleUsage} />);
  const cpu = screen.getByRole("row", { name: "CPU 2 1 0" });
  expect(within(cpu).getAllByRole("cell").map(cell => cell.textContent)).toEqual(["2", "1", "0"]);
  expect(screen.getAllByText(/Unlimited/).length).toBeGreaterThan(0);
  expect(screen.getByText("Cost center not assigned")).toBeVisible();
  expect(screen.getByText(/Monthly spending \(UTC\)/)).toBeVisible();
  expect(screen.getByText(/Enforcement disabled/)).toBeVisible();
});
it("shows lifetime unavailable spend without inventing zero and pending shutdown", () => {
  render(<QuotaUsage usage={{ ...exampleUsage, scope: "project", reset_at: null, spent_usd: null, accounting_status: "unavailable", as_of: null, enforcement_enabled: true, spending_state: "budget_exceeded_shutdown_pending" }} />);
  expect(screen.getByText(/Lifetime spending/)).toBeVisible();
  expect(screen.getByText(/Accounting unavailable/)).toBeVisible();
  expect(screen.getByText("Budget exceeded—shutdown pending")).toBeVisible();
  expect(screen.queryByText(/\$0.00/)).not.toBeInTheDocument();
});
it("does not imply a shutdown is pending when quota enforcement is disabled", () => {
  render(<QuotaUsage usage={{ ...exampleUsage, spending_state: "budget_exceeded_shutdown_pending", action_counts: { pending: 1 } }} />);
  expect(screen.queryByText("Budget exceeded—shutdown pending")).not.toBeInTheDocument();
});
it.each(["80", "90", "100"])("shows %s percent spending warning and failed shutdown", amount => {
  render(<QuotaUsage usage={{ ...exampleUsage, limits: { ...exampleUsage.limits, spending_usd: "100" }, spent_usd: amount, action_state: "failed", enforcement_enabled: true }} />);
  expect(screen.getByText(amount === "100" ? "Spending allowance exhausted" : `${amount}% of spending allowance reached`)).toBeVisible();
  expect(screen.getByText("Budget shutdown failed; retry pending")).toBeVisible();
});
it("distinguishes confirmed shutdown completion from pending enforcement", () => {
  render(<QuotaUsage usage={{ ...exampleUsage, enforcement_enabled: true, spending_state: "budget_exceeded_shutdown_pending", action_state: "completed" }} />);
  expect(screen.getByText(/Budget shutdown completed/)).toBeVisible();
  expect(screen.queryByText("Budget exceeded—shutdown pending")).not.toBeInTheDocument();
});
