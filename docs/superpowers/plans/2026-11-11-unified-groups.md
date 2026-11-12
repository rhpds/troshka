# Unified Groups Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Make Groups the single organization membership for quotas and reporting.

**Architecture:** Preserve User.organization_id as authority and stable group IDs. Establish attribution-aware metering before shared transfer endpoints and a fenced data conversion; reporting uses event-time organization identities without relying on native capacity readiness.

**Tech Stack:** FastAPI, SQLAlchemy/PostgreSQL, Alembic, Python3, Next.js/PatternFly, pytest/Vitest.

**Spec:** `docs/superpowers/specs/2026-11-11-unified-groups-design.md`

## Global Constraints

- `User.organization_id` is the sole current membership authority.
- Every group supports reporting and quotas; no reporting-only group type remains.
- Platform administrators manage membership and administrator assignments.
- Group administrators manage their own users' limits, not the group's allowance.
- Historical charges and frozen invoices/statements are never reassigned or rewritten.
- Transfers preserve explicit user/project limits and lifetime spending.
- Missing accounting or capacity evidence is unknown, never zero.
- `quotas.enabled` remains false throughout this rollout.
- Membership conversion does not establish capacity readiness or enable native quotas.
- No provider workload is redeployed or storage mutated for this change.
- Python commands use `python3`; commits have no Co-Authored-By trailers.

Existing implementation/deployment authorization persists. No duplicate approval
is required for the corrected single-organization model. Production changes wait
for reviewed code and a successful migration rehearsal. Reuse current checkout;
finish native Task3b review/import independently before conversion. Never mix
native report import with membership mutation or claim it makes quotas ready.

### Task 1: Attribute metering independently of enforcement

**Files:** modify `src/backend/app/services/metering_service.py`,
`quota_accounting.py`, `quota_baseline.py`, `quota_locks.py` only as necessary;
tests `src/backend/tests/test_metering_attribution.py` (new) and existing metering,
baseline and transfer suites. Inspect direct interval writers in cleanup/deploy
paths; fix only demonstrated missing locking/stamping within this contract.

**Interfaces:** retain `reconcile_project`, `freeze_invoice`, `finalize_month`,
`touch_metering` signatures. Reuse `lock_metering_project(db, project_id)` and
`attribution(db, project)`. Separate identity locking/stamping from quota-only
snapshot/rate validation; ordinary unassigned legacy sources stay explicitly
unknown without breaking nonquota billing. Later transfer code can rely on new
assigned sources being stamped and source mutations being serialized.

- [ ] Write failing disabled-mode tests: open source keeps orgA after owner later
  becomes orgB; reconcile/close/freeze serialize with transfer; unknown assignment
  never receives invented identity; legacy invoice amounts remain unchanged;
  baseline preserves existing stamps.
  ```python
  assert opened.quota_attribution['organization_id'] == original_group.id
  assert old_charge.organization_id == original_group.id
  assert invoice_after.total_usd == invoice_before.total_usd
  ```
- [ ] Run the new focused tests to capture RED without app lifespan/live Redis.
- [ ] Implement lock/stamp separation and validate no reversed lock acquisition
  in callers; use focused helpers instead of expanding large functions.
  ```python
  project, _, _ = lock_metering_project(db, project.id)
  # quota_mode controls demand checks, not whether source identity is retained.
  ```
- [ ] Run focused tests and disposable PostgreSQL race checks (transfer versus
  reconcile/freeze); no reliance on SQLite for row-lock behavior.
- [ ] Run exact applicable hooks, record evidence, commit and task review.

### Task 2: Shared group transfers and authority

**Files:** `services/organization_service.py`, `api/groups.py`, `api/users.py`,
`schemas/managed_group.py`, `models/managed_group.py`, `services/group_access.py`;
tests `test_organization_transfers.py`, `test_groups_api.py`.

**Interfaces:** extend `transfer_user` with optional expected-source check or a
wrapper that checks under its user lock. Both routes call this service and own
one commit/rollback. Preserve existing response keys; add explicit enforcement
and verification state for disabled mode. Explicit false group type is rejected;
new groups always true. Do not convert live existing rows in this task.

- [ ] RED: disabled live transfer with unready capacity preserves old source,
  splits future source and keeps user caps; enabled over-cap still denied; stale
  group removal cannot unassign another group; manager cannot mutate roster.
  ```python
  assert moved_user.organization_id == destination.id
  assert previous_charge.organization_id == source.id
  assert readiness.ready is False
  assert source_intervals[-1].ended_at == successor.started_at
  ```
- [ ] Implement disabled transfer without fake admission/capacity/spend result;
  validate real ledger identities/timestamps, preserve strict unassignment rules.
  Route group member operations through same transaction/error mapping; no
  destination lock before the transfer user lock.
- [ ] GREEN existing enabled and new disabled transfer/API permission tests;
  rollback proof includes injected failure after interval split.
- [ ] Hooks, checkpoint commit and task review.

### Task 3: Truthful group reports with unavailable capacity baseline

**Files:** `services/group_metering.py`, `services/group_access.py`,
`api/metering.py`, billing schemas where needed; tests `test_group_metering.py`
and organization transfer/report authorization tests.

**Interfaces:** `scoped_members` always uses User.organization_id after cutover;
`group_report` retains its response shape where possible, with explicit known
subtotal/availability when evidence incomplete. Current owner documents remain a
separate labelled view. Historical group detail includes only attributed rows.

- [ ] RED: mid-month orgA->orgB keeps prior amounts in A and future in B, includes
  former-member attributed detail without cross-org leakage, missing native data
  yields unknown total, immutable owner invoice/statement unchanged.
  ```python
  assert report_a['total_usd'] != owner_lifetime_total
  assert incomplete_report['total_usd'] is None
  assert serialized_invoice_after == serialized_invoice_before
  ```
- [ ] Implement available-source reporting without capacity-readiness refresh;
  preserve exact money arithmetic and avoid charge/interval/document double count.
- [ ] GREEN report, access-revocation and transfer history tests; hooks/commit/review.

### Task 4: One group UI

**Files:** frontend `components/groups/GroupEditor.tsx`, `ManagerControls.tsx`,
`app/admin/groups/page.tsx`, `app/team-billing/page.tsx`,
`components/quotas/OrganizationAssignment.tsx`, relevant lib types/tests.

**Interfaces:** existing group endpoints now perform authoritative transfers;
all groups expose member/admin/quota/settings controls. Preserve the requested
information-icon popover. Read frontend AGENTS and installed Next docs.

- [ ] RED UI tests: creation has no type choice; member addition moves through
  shared endpoint and displays updated membership; errors retain prior roster.
  ```tsx
  expect(screen.queryByLabelText('Quota organization')).not.toBeInTheDocument();
  expect(screen.getByRole('tab', { name: /Quotas/ })).toBeVisible();
  ```
- [ ] Remove type column/checkbox, enable members and quota UI consistently,
  explain inactive configured limits and label owner documents separately.
- [ ] GREEN focused Vitest/typecheck/lint and desktop/mobile visual checks.
- [ ] Hooks/commit/review; do not release UI before backend/cutover ready.

### Task 5: Audited membership conversion

**Files:** new `services/group_migration.py`, `scripts/migrate_groups.py`, migration
for true group defaults and receipt model if required; tests
`test_group_migration.py`, `docs/dev/unified-groups-migration.md`.

**Interfaces:** operator CLI has explicit DB target and dry-run/apply with expected
inventory digest; durable receipt records boundary/mapping/digest. No implicit
production target, credential output or live conversion inside Alembic. Preserve
legacy association table as evidence. All-or-nothing apply and exact replay.

- [ ] RED deterministic nine-user mapping, overlap/conflicting nondefault
  rejection, stale inventory/cursor rejection, whole-transaction rollback,
  replay idempotency, historical documents/charges unchanged.
  ```python
  assert applied_again.receipt_id == applied.receipt_id
  assert legacy_roster_after == legacy_roster_before
  assert historical_charge_digest_after == historical_charge_digest_before
  ```
- [ ] Implement identity attribution backfill and boundary split before mapping;
  old unknown history uses documented Default provenance, never new employer
  guesses. Preserve actual live ledger IDs and policies, leave readiness false.
- [ ] Rehearse complete conversion in disposable PostgreSQL with adversarial
  failures, retain sanitized evidence, document writer fence/resume/rollback.
- [ ] Hooks/commit/task review; full phase review of Tasks1–5.

### Task 6: Controlled production rollout

- [ ] Verify exact revision tests and normal prepush quality gate; never bypass.
- [ ] Read deployment runbook, build immutable release and retain in-cluster backup.
- [ ] Fence/drain old app writers and dispatch; inspect active jobs before changing
  replica counts. No operator/project-pod rollout or provider mutation.
- [ ] Dry-run migration against current data; compare candidate mapping and digest.
  Stop on conflicts instead of selecting a group arbitrarily.
- [ ] Apply reviewed utility at one boundary, resume new attribution-aware writers,
  verify membership/report/permission/history invariants and app health.
- [ ] Record actual deployment/migration evidence and remaining native accounting
  blockers. Quota enforcement stays disabled.
