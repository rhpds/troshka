# Organization Quotas Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Execute inline unless delegation is explicitly selected.

**Goal:** Enforce delegated organization, user, and project resource and calculated-spending quotas without losing historical charges or disrupting existing installations on migration.

**Architecture:** Reuse managed-group identities for organizations but add an authoritative single organization association to users, independent of reporting membership. PostgreSQL owns policies, reservations, durable charges, and enforcement actions; workers reconcile infrastructure and execute idempotent shutdowns. Existing metering and deployment services call small quota modules rather than duplicating checks in routes.

**Tech Stack:** Existing FastAPI, SQLAlchemy, Alembic, PostgreSQL, RQ, Next.js, PatternFly, pytest, and Vitest stack. Use the repository's installed versions; do not upgrade dependencies for this feature.

**Spec:** `docs/superpowers/specs/2026-11-11-organization-quotas-design.md` (approved for planning on 2026-11-11).

## Global Constraints

- Each user belongs to at most one organization, and organization membership is required before deploying.
- A missing limit means unlimited at that scope; zero prohibits consumption.
- User caps are not reserved slices of the organization pool.
- Monthly allowances reset without rollover.
- Administrative resource operations have no implicit quota bypass.
- Missing identifiers do not prevent deployment or quota enforcement.
- Redis remains useful for jobs and notifications but is not the sole quota ledger or lock mechanism.
- Use `python3`, absolute paths for git staging, and no Co-Authored-By trailers.
- Use background-capable execution for long tests/builds and wait for completion notifications; never sleep-poll. If the available executor lacks that flag, use its asynchronous task/session mechanism.
- Run focused tests for each task, meaningful PostgreSQL concurrency tests before enforcement, and existing required checks before pushing. Never bypass the Sonar pre-push hook.
- Keep function cognitive complexity at or below 15. Do not introduce unrelated refactors.
- Do not enable production enforcement, deploy, or push as part of writing this plan.

## File and interface map

New backend modules, each with one responsibility:

| File under `src/backend/app/` | Responsibility |
| --- | --- |
| `models/quota.py` | Policy, allocation, reservation, charge, audit, enforcement-action persistence |
| `schemas/quota.py` | Typed limits, resource vectors, usage/denial response shapes |
| `services/quota_policy.py` | Authorization, policy reads/updates, audit |
| `services/quota_resources.py` | Resource vectors from desired and observed workloads |
| `services/quota_admission.py` | Locking, checking, idempotent reservations |
| `services/quota_accounting.py` | Durable decimal charges, monthly/lifetime spend |
| `services/quota_enforcement.py` | Thresholds, durable stop requests, retry/reconciliation |
| `services/organization_service.py` | Assignment and atomic transfers |
| `api/quotas.py` | Authorized policy and usage endpoints |
| `scripts/backfill_quotas.py` | Idempotent baseline import and readiness report |

Existing integration points: `models/{user,managed_group,project,metering}.py`,
`models/__init__.py`, `api/{groups,users,projects,disks,metering,group_metering}.py`,
`services/{group_access,group_metering,metering_service,metering_math,deploy_service}.py`,
`core/redis.py`, `main.py`, and `config/config.yaml`. Frontend integration uses
`src/frontend/src/app/admin/{groups,users}/page.tsx`, `app/team-billing/page.tsx`,
`app/projects/[id]/page.tsx`, and new components under `components/quotas/`.

Implement these shared signatures; all types are defined here for cross-task consistency:

```python
# schemas/quota.py: Pydantic types; monetary fields use Decimal, IDs use str.
class Resources(BaseModel):
    cpu: int = 0
    ram_bytes: int = 0
    storage_bytes: int = 0
    projects: int = 0

class Limits(BaseModel):
    cpu: int | None = None
    ram_bytes: int | None = None
    storage_bytes: int | None = None
    projects: int | None = None
    spending_usd: Decimal | None = None

# Validate all quantities as nonnegative and forbid unknown keys.
# Scope is Literal['organization', 'user', 'project'].
# QuotaDenied carries scope, scope_id, dimension, used, reserved, requested,
# limit, and reason. AccountingUnavailable is a distinct service exception.

# quota_policy.py
def get_limits(db: Session, scope: Scope, scope_id: str) -> Limits: ...
def set_limits(db: Session, actor: User, scope: Scope, scope_id: str,
               limits: Limits, now: datetime) -> None: ...

# quota_resources.py
def requested_resources(topology: dict, operation: str) -> Resources: ...
# Reject unsupported operations/resource mappings explicitly.

# quota_admission.py
def reserve(db: Session, project_id: str, operation_id: str,
            increment: Resources, now: datetime) -> str: ...
def confirm(db: Session, operation_id: str, observed: Resources,
            observation_id: str, now: datetime) -> None: ...
# observed is the operation's absolute observed allocation, not a delta.
# Caller owns the transaction. These functions flush but never commit.

# quota_accounting.py
def spend(db: Session, scope: Scope, scope_id: str, now: datetime) -> Decimal: ...
def accrue(db: Session, project_id: str, through: datetime) -> None: ...

# quota_enforcement.py
def evaluate(db: Session, now: datetime) -> None: ...
def reconcile_action(db: Session, action_id: str, now: datetime) -> None: ...

# organization_service.py
def transfer_user(db: Session, actor: User, user_id: str,
                  organization_id: str, now: datetime) -> None: ...
```

The ellipses above denote interface signatures, not implementation instructions.
Each task below defines behavior and tests. Generate Alembic revisions at execution
time against the actual head; do not invent a revision parent in advance.

## Task 1: Organizations and typed quota persistence

**Files:** Modify `models/user.py`, `models/managed_group.py`, `models/__init__.py`;
create `models/quota.py`, `schemas/quota.py`, an Alembic revision in
`src/backend/alembic/versions/`, and `tests/test_quota_models.py`.

**Consumes:** Existing managed-group/user identities. **Produces:** `Resources`,
`Limits`, `Scope`, `QuotaDenied`, `AccountingUnavailable`, and database models.

- [ ] Write model/schema tests including the following assertions, invalid negative
  values, unknown keys, decimal precision, and unique operation identities:

```python
def test_zero_and_unlimited_are_distinct():
    assert Limits().cpu is None
    assert Limits(cpu=0).cpu == 0
    assert Limits(spending_usd=Decimal('0')).spending_usd == Decimal('0')
```

- [ ] Run `./venv/bin/python3 -m pytest tests/test_quota_models.py -q` from
  `src/backend`; verify failures concern missing quota behavior.
- [ ] Add nullable `User.organization_id` referencing managed groups with
  `UUID(as_uuid=False)`; add nullable cost-center ID and organization designation
  to managed groups. Do not infer organization membership from reporting joins.
- [ ] Implement `Limits` and `Resources` with nonnegative validators and
  `extra='forbid'`. Use BigInteger byte/count columns and Numeric monetary columns.
  Policies are unique on `(scope, scope_id)` and include a revision counter.
- [ ] Add reservation/allocation rows keyed by unique operation identity, recording
  project/owner/org, requested and observed vectors, state, and observation ID.
  Add durable charge segments with unique source interval/start/end, stable
  project/owner/org identity snapshots, quantity, rate, and decimal amount; no
  cascading deletion from live projects. Add audit and durable action tables.
- [ ] Add `quotas.enabled: false` and readiness state. Generate the migration,
  seed an unlimited default organization, and assign existing users to it.
  Preserve `Project.budget_usd`; do not discard pre-existing guardrails or silently
  reinterpret arbitrary legacy `quota_overrides` keys.
- [ ] Run model tests and `tests/test_managed_groups_migration.py`; verify upgrade
  on a disposable PostgreSQL database populated with overlapping reporting groups.
  Commit only these changes using absolute staging paths.

## Task 2: Delegated policies and organization authorization

**Files:** Create `services/quota_policy.py`, `api/quotas.py`,
`tests/test_quota_permissions.py`; modify `api/users.py`, `api/groups.py`,
`services/group_access.py`, `main.py`.

**Consumes:** Task 1 models/types. **Produces:** `get_limits`, `set_limits`,
authorized GET/PATCH `/quotas/{scope}/{scope_id}` endpoints.

- [ ] Add API tests proving a group admin can edit a user's policy only when
  `User.organization_id` identifies an organization they administer. A legacy
  reporting membership alone must return 403/404. Test owners, collaborators,
  platform admins, and cross-organization requests independently.

```python
def test_reporting_membership_does_not_grant_quota_authority(client, quota_case):
    # Fixture creates a reporting manager but gives the target a different org.
    quota_case.login_reporting_manager(client)
    response = client.patch(f'/api/quotas/user/{quota_case.target_id}',
                            json={'cpu': 4})
    assert response.status_code in (403, 404)
```

- [ ] Create the `quota_case` fixture in this test module using existing group API
  fixtures, with separate reporting and organization memberships and explicit login.
- [ ] Run the test file and observe unauthorized writes fail before implementation.
- [ ] Implement authorization before reads/writes; merge PATCH fields by
  `model_fields_set` so omitted means unchanged and explicit null means unlimited.
  Store revision and audit atomically. Delegate no manager-roster or membership
  authority through these endpoints. Prevent the old generic `quota_overrides`
  update from becoming an alternate policy-write path.
- [ ] Register router; preserve existing reporting APIs. Lowering resource caps
  does not enqueue stops; spending changes request reevaluation in Task 7.
- [ ] Run quota permissions plus `test_groups_api.py` and `test_users_api.py`;
  commit the independently working policy API.

## Task 3: Durable spending accounting

**Files:** Create `services/quota_accounting.py`, `tests/test_quota_accounting.py`;
modify `services/metering_service.py`, `services/metering_math.py`,
`models/metering.py`, and the metering API serialization paths.

**Consumes:** Durable charge schema. **Produces:** `accrue` and `spend`.

- [ ] Build fixtures with an interval spanning 23:00 Jan 31 to 01:00 Feb 1 UTC,
  quantity 2 and rate $3/hour. Assert $6 attributed to each month and $12 lifetime;
  rerun accrual and assert totals unchanged. Delete the live project and assert
  user/org spend remains. Assert an accounting error raises
  `AccountingUnavailable`, rather than returning Decimal('0').
- [ ] Run `./venv/bin/python3 -m pytest tests/test_quota_accounting.py -q`.
- [ ] Implement decimal interval integration, splitting at month and transfer
  boundaries. Retain a durable accrued-through cursor per source interval. Under
  row locking, append only the uncovered range and advance the cursor atomically.

```python
amount = quantity * unit_rate * Decimal(str((end - start).total_seconds())) / Decimal(3600)
# Keep full Numeric precision in storage; round only display currency.
```

- [ ] Implement `spend`: project sums lifetime segments; user/org sum segments
  intersecting the current UTC month. Include open-interval accrual through `now`
  once, and return known zero only when baseline and observations are authoritative.
- [ ] Preserve rate snapshots. Give imported legacy summaries a disjoint source
  namespace and coverage range so intervals, invoices, and statements cannot be
  summed twice. Preserve nonquota billing behavior until readiness is established.
- [ ] Run new tests plus `test_metering_math.py`, `test_metering_service.py`, and
  `test_group_metering.py`; commit durable accounting.

## Task 4: Atomic resource admission

**Files:** Create `services/quota_resources.py`, `services/quota_admission.py`,
`tests/test_quota_admission.py`, `tests/test_quota_concurrency.py`.

**Consumes:** Policy/accounting services. **Produces:** `requested_resources`,
`reserve`, `confirm` and a single lock helper used by policy updates/transfers.

- [ ] Add pure resource-vector tests for draft, start, stop, disk growth, and
  destroy; assert stopped environments retain storage and one deployed slot.
  Explicitly reject unsupported mappings. Count nested workload allocations once.
- [ ] Add a real two-session PostgreSQL test: organization cap 10 CPUs, two users
  concurrently request 6 each. Use a thread barrier, separate sessions and bounded
  futures; exactly one succeeds and total allocation plus reservations is 6.
  Repeat with same operation ID: only one reservation exists.
- [ ] Run tests before implementation. PostgreSQL concurrency tests must fail or
  explicitly skip without a configured disposable database; never report a skip as
  proof of concurrency safety.
- [ ] Implement consistent locking: lock affected user rows sorted by ID, then
  affected organization rows sorted by ID, then project rows sorted by ID. This
  matches current user-first group-assignment operations. After locking, refresh
  organization membership and restart the transaction if the resolved scope changed.
  An org-only policy update locks the org and never subsequently waits on users;
  enforcement enumerates projects outside its policy transaction.
- [ ] Under locks, accrue/check spend and check each positive dimension delta:

```python
if limit is not None and used + reserved + requested > limit:
    raise QuotaDenied(scope=scope, scope_id=scope_id, dimension=dimension,
                      used=used, reserved=reserved, requested=requested,
                      limit=limit, reason='resource_limit')
```

- [ ] Insert reservation before queue dispatch. Same operation ID with different
  project/vector is a conflict. Implement observation-based replacement of reserved
  usage with actual allocation, without adding both. Do not free on a timeout.
- [ ] Run tests, including concurrent policy reduction and admission; commit.

## Task 5: Cover allocation entry points and job recovery

**Files:** Modify `api/projects.py`, `api/disks.py`, `services/deploy_service.py`,
`core/redis.py`; create `tests/test_quota_entry_points.py` and
`docs/dev/quota-allocation-paths.md`.

**Consumes:** `reserve`, `confirm`. **Produces:** Server-side admission for every
growth path and durable operation IDs passed into infrastructure jobs.

- [ ] Inventory all provider allocation/start/resize/storage calls using `rg` in
  `src/backend/app`. Record each producer and authoritative execution boundary in
  `docs/dev/quota-allocation-paths.md`, including direct VM operations, imports,
  clones, snapshots, restore/redeploy, containers, external callbacks and any
  independently allocated storage. Classify draft metadata changes separately.
- [ ] Add parameterized integration tests per recorded growth path; with zero cap
  assert the provider mock is never called. Verify collaborator/admin actions charge
  the owner and unassigned users cannot deploy. Allow stops and cleanup.

```python
# Apply to each actual endpoint/service fixture from the path inventory.
assert response.status_code == 409
provider_allocate.assert_not_called()
assert response.json()['detail']['reason'] == 'resource_limit'
```

- [ ] Run failing tests, then call admission at shared execution boundaries. Reserve
  before dispatch; use durable pending operation rows and retryable dispatch so a
  commit/queue failure cannot lose a job or allocate without a reservation.
- [ ] Recheck spending and membership before provider work. Carry operation IDs
  through retries and thread fallback. Observe partial success and confirmed stop
  outcomes; retain uncertain allocations for reconciliation. Edits to a running
  topology must not bypass deployed-resource accounting.
- [ ] Run new tests plus existing deployment, VM operation, and disk contract tests;
  confirm every inventory row has an enforcement test before commit.

## Task 6: Organization assignment and transfers

**Files:** Create `services/organization_service.py`,
`tests/test_organization_transfers.py`; modify `api/users.py`, `api/groups.py` and
`services/group_metering.py`.

**Consumes:** Shared lock helper, `accrue`, admission state. **Produces:**
`transfer_user` and platform-only organization assignment endpoint.

- [ ] Test transfer with live compute and pending reservations: source keeps past
  charges, destination receives current resources/future charges, user monthly and
  project lifetime totals remain unchanged. Test a full destination rejects without
  any partial membership/metering changes.
- [ ] Run tests; implement platform-only transfer in one transaction using Task 4
  lock ordering. Accrue to the boundary, split intervals, move reservations and
  allocations, then update membership. Prevent concurrent workers using stale orgs.
- [ ] Carry user limits forward and evaluate destination spending immediately.
  Prevent organization deletion while users/active allocations remain; retain
  historical identities even after an organization is retired. Unassignment is
  allowed only when no live allocation or pending work remains.
- [ ] Test simultaneous transfers/admission in PostgreSQL, including opposite
  direction transfers. Verify existing reporting memberships do not alter charges.
- [ ] Commit after transfer, group reporting and permission suites pass.

## Task 7: Budget shutdown and reconciliation

**Files:** Create `services/quota_enforcement.py`,
`tests/test_quota_enforcement.py`; modify `services/metering_service.py`,
`services/project_timer.py`, and `main.py` startup wiring.

**Consumes:** `spend`, policies, durable actions. **Produces:** `evaluate`,
`reconcile_action`, threshold events and visible enforcement state.

- [ ] Add controlled-clock tests for 80/90/100%, zero/unlimited, UTC reset,
  lifetime exhaustion, policy reductions, failed stops, and restoration of allowance
  before queued execution. Assert repeated evaluation emits one action per project
  and cause and never restarts compute after reset.
- [ ] Run tests; implement durable warning keys `(scope, id, period, revision,
  threshold)` and project stop intents with all currently applicable causes.
  Reevaluate causes immediately before dispatch. Claim actions atomically to avoid
  competing workers. Track attempts, errors, next retry and confirmed completion.
- [ ] Use existing project stop orchestration; do not mark completed merely because
  enqueue succeeded. Reconcile provider observations to release CPU/RAM only.
  Never delete data as a spending action. Exhaustion during provisioning cancels
  remaining growth and stops any compute that materializes.
- [ ] Replace duplicate legacy project-budget stop decisions with the shared
  evaluator when quotas are enabled; keep legacy behavior when disabled.
  Accounting outages block growth and show unavailable status, without triggering
  shutdown purely from missing data.
- [ ] Run enforcement, timer and metering regression suites; commit.

## Task 8: Usage APIs and delegated UI

**Files:** Extend `api/quotas.py`; create `tests/test_quota_usage_api.py`,
`src/frontend/src/lib/quotas.ts`, `components/quotas/QuotaUsage.tsx`,
`components/quotas/QuotaEditor.tsx`, and colocated `.test.tsx` files; modify the
admin groups/users, team billing, and project detail pages listed above.

**Consumes:** Authorized policies, allocations, reservations, spend and actions.
**Produces:** GET `/quotas/{scope}/{scope_id}/usage`, reusable UI components.

- [ ] Write API tests for authorized aggregate visibility without other users'
  project details; decimal totals serialized as strings; exact integer byte fields;
  nullable reset dates for lifetime spending; explicit unavailable accounting.
- [ ] Define a response with `limits`, `used`, `reserved`, `spent_usd`, `as_of`,
  `reset_at`, `accounting_status`, `enforcement_status`, `can_edit`, and
  `cost_center_id`. Validate serialized output against the schema.
- [ ] Add React tests for zero versus unlimited, used+reserved, lifetime versus
  monthly, missing cost center, disabled unauthorized editors, and pending shutdown:

```tsx
expect(screen.getByText('Cost center not assigned')).toBeVisible();
expect(screen.getByText('Budget exceeded—shutdown pending')).toBeVisible();
expect(screen.getByLabelText('CPU limit')).toBeDisabled();
```

- [ ] Implement raw-fetch clients with typed response handling and PatternFly
  components. Display currency rounded for humans while retaining decimal strings
  in edits. Keep organization terminology distinct from legacy reporting groups.
  Project editor warns about lifetime spending reduction; resource reductions
  explain growth blocking. Surface structured admission errors in operation UI.
- [ ] Run backend API tests and `npm test -- --run` for new component tests and
  affected page tests from `src/frontend`; run repository frontend lint/type checks.
  Commit after meaningful permission/display tests pass.

## Task 9: Baseline import, readiness gate, and rollout documentation

**Files:** Create `scripts/backfill_quotas.py`, `tests/test_quota_backfill.py`,
`docs/dev/quotas.md`; finalize configuration and migration tests.

**Consumes:** All previous services. **Produces:** Idempotent dry-run/apply import,
readiness report, and documented enablement procedure.

- [ ] Test a mixed baseline with running/stopped/draft/deleted projects, overlapping
  reporting groups, old invoices/statements, open metering, and existing project
  spending caps. Rerunning import must not change totals or duplicate allocations.
- [ ] Implement dry-run by default; explicit `--apply` writes historical segments
  with migration provenance and imports confirmed allocations/in-flight operations.
  Preserve unknown legacy quota data and report it for manual mapping rather than
  erasing it. No historical attribution inferred from today's reporting groups.
- [ ] Readiness requires complete resource-path coverage, reconciled allocations,
  trustworthy open intervals and disjoint historical coverage. Unknown/missing
  rates or unsupported providers produce actionable failures, not zero spend.
  Enablement must reject an unready baseline.
- [ ] Document backup, dry run, review, apply, observe/reconcile, and deliberate
  enforcement enablement. Explain UTC resets, retained-storage costs, default org,
  transfer behavior, missing cost center, and how to recover failed shutdowns.
- [ ] Run focused backend suites and the PostgreSQL race suite once against the
  final revision, plus frontend affected tests. Record commands and outcomes in
  the delivery PR; do not claim production validation from local tests.
- [ ] Commit and prepare reviewable foundation/enforcement/UI changes. Follow the
  normal review and Sonar gates; production rollout is a separate authorized action.

## Plan self-review and delivery gates

- Task 1 covers hierarchy, optional cost center, unlimited defaults, units/schema.
- Task 2 covers delegation, auditing and legacy write bypasses.
- Tasks 3 and 6 cover decimal charges, deletion, resets and historical attribution.
- Tasks 4 and 5 cover concurrency, reservations and all allocation paths.
- Task 7 covers warnings, stop retries, outages and reset-without-restart.
- Task 8 covers visibility, actionable errors and all three administration levels.
- Task 9 covers migration, readiness and operational validation.
- All tasks use the shared interfaces above. Unknown provider paths are an explicit
  Task 5 inventory deliverable and block enablement until covered; they are not
  permission to ship a partial enforcement boundary.
- Implementation is intentionally not performed by this planning document.
