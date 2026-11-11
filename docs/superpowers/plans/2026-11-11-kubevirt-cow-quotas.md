# Native KubeVirt COW Quotas Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Use subagents only where the user or applicable instructions authorize delegation.

**Goal:** Deliver usable native KubeVirt RBD allocation reporting first, then establish tested native lifecycle and migration contracts required for quota enforcement over the real workload inventory.

**Architecture:** A constrained external read-only collector produces versioned graph evidence; a pure ownership evaluator separates project allocation, shared cache and unresolved storage. Append-only reporting persistence feeds authorized project/user/organization UI independently of quota readiness. Native enforcement is a subsequent gated phase with operator fencing, complete execution demand and confirmed backend release.

**Tech Stack:** Python 3.13, Pydantic, Kubernetes Python client, constrained Ceph/RBD CLI, SQLAlchemy/PostgreSQL/Alembic, FastAPI, Next.js/PatternFly, pytest/Vitest.

**Spec:** `docs/superpowers/specs/2026-11-11-kubevirt-cow-quotas-design.md`

## Global Constraints

- `quotas.enabled` remains false during reporting development and rollout.
- Metric name is `rbd_allocated_extent_bytes`; UI label is `Allocated RBD storage`.
- The metric excludes replication/erasure-coding overhead and is not raw physical device consumption.
- No PVC-capacity, guest-used, missing-value-as-zero, or namespace-name-only fallback is permitted.
- Reporting never writes quota reservations, allocations, readiness, spending or billing history.
- All stored byte quantities are nonnegative signed-int64 integers; JSON UI transport preserves exact integers.
- Collection never executes in an HTTP request, admission transaction or quota-lock critical section.
- Collector mutations, general shell execution and general application pods/exec privileges are prohibited.
- Unknown ownership, incomplete inventory, stale evidence and unsupported backend semantics remain explicit.
- Stop preserves storage; release requires independently confirmed backend absence after worker/controller fencing.
- Run Python commands with `python3`; never add Co-Authored-By trailers.

The user's continued implementation authorization does not require another plan
confirmation. This plan does not authorize production enablement, new credentials,
cluster mutations or automatic agent/operator deployment. No commit is performed
while writing this plan; implementation checkpoints below are reviewed changesets,
and commits/pushes follow the active session's authorization and Sonar hook rules.

## Phase boundaries and file map

Execute Tasks 1–3 as the first independently useful reporting phase, including its
API/UI and evidence replay. Tasks 4–8 are the enforcement phase: begin only when
their predecessor contracts are testable; keep unsupported native enforcement
blocked throughout. Do not call Tasks 1–3 “native quota enforcement support”.

| Files | Responsibility |
| --- | --- |
| `src/backend/app/schemas/kubevirt_storage.py` | Strict versioned identity, observation, report and API contracts |
| `src/backend/app/services/kubevirt_storage_inventory.py` | Kubernetes ownership discovery and CSI bindings |
| `src/backend/app/services/kubevirt_storage_transport.py` | Fixed read-only argv transport with process/output limits |
| `src/backend/app/services/kubevirt_storage_collector.py` | Bounded graph collection and evidence construction |
| `src/backend/app/services/kubevirt_storage_graph.py` | Pure allocation attribution/deduplication |
| `scripts/collect_kubevirt_storage.py` | External report-only CLI, explicit credentials/output |
| `src/backend/app/models/kubevirt_storage.py` | Reporting snapshots and retained verified identities |
| `src/backend/app/services/kubevirt_storage_reports.py` | Import/replay validation and authorized scope aggregation |
| `scripts/import_kubevirt_storage_report.py` | Explicit-target operator import, no collector invocation |
| `src/frontend/src/components/quotas/NativeStorageUsage.tsx` | Separate observation-only native report panel |
| `src/backend/app/services/quota_native.py` | Native demand, intents, observations and adapter entry points |
| `src/operator/helpers/quota_fence.py` | Durable native generation authorization/dispatch coordinator |
| `src/backend/app/services/quota_native_baseline.py` | Native baseline conversion without historical rewrites |

Keep changed functions below cognitive complexity 16; extract focused helpers
rather than expanding provider/deploy monoliths. Read frontend AGENTS before UI
edits. Use run-in-background support for test/build commands when available;
otherwise start asynchronous tool execution and wait for completion notifications,
never `sleep && check` loops.

### Task 1: Read-only native collector and ownership graph report

**Files:** Create the schema, inventory, transport, collector and graph modules
above plus `scripts/collect_kubevirt_storage.py`; create
`src/backend/tests/test_kubevirt_storage_{inventory,transport,graph,collector}.py`
and sanitized `src/backend/tests/fixtures/kubevirt_storage/` recordings.

**Interfaces:**

```python
# schemas/kubevirt_storage.py: frozen extra-forbid models, not positional tuples
class PoolKey(BaseModel):
    ceph_fsid: str
    pool_id: int

class ImageKey(BaseModel):
    ceph_fsid: str
    pool_id: int
    rbd_namespace: str
    image_id: str

# NativeInventory contains verified project_roots, volume_bindings,
# cache_bindings, retained_bindings and discovery blockers from the spec.
# ImageObservation/NativeStorageReport use the spec's exact fields and literals.
def discover_inventory(kube_reader, retained_bindings, roots) -> NativeInventory: ...
def collect_report(inventory: NativeInventory, rbd_reader, *,
                   limits: CollectionLimits, clock) -> NativeStorageReport: ...
def attribute_graph(inventory: NativeInventory,
                    images: list[ImageObservation]) -> AttributionResult: ...
```

`kube_reader` exposes explicit read methods for namespace/CR/PVC/PV/snapshot
discovery. `rbd_reader` exposes `cluster_identity`, `pool_identity`, `resolve_csi`,
`image_info`, `image_usage`, `image_snapshots`, `image_children`, `trash_inventory`;
no generic shell method. Bind image identity before and after every measurement.
Return result objects containing source timestamps and typed failure codes;
exceptions must not leak CLI stderr or credentials.

- [ ] Write strict contract/parser tests for negative/overflow bytes, future
  timestamps, incomplete du output, identity replacement and legitimate zero.
  Define the reusable fixture loader locally in each test module:

  ```python
  from pathlib import Path
  import json
  from app.schemas.kubevirt_storage import NativeStorageReport

  def report_fixture(name):
      path = Path(__file__).parent / "fixtures" / "kubevirt_storage" / name
      return NativeStorageReport.model_validate(json.loads(path.read_text()))

  def test_zero_is_valid_but_timeout_is_not_zero():
      complete = report_fixture("zero-private-parent.json")
      partial = report_fixture("timeout-private-parent.json")
      assert complete.projects[0].allocated_bytes is not None
      assert partial.projects[0].allocated_bytes is None
      assert partial.projects[0].coverage == "partial"
      assert partial.enforcement_eligible is False
  ```

  Fixture graph: two project children (30 and 40 bytes), their separate private
  parents (0 and 10), shared verified cache (100). Complete totals are 30 and 50,
  cache 100 once. Timeout the first parent for the partial fixture; retain known
  30, nullable total. Use synthetic UUIDs and backend IDs, no copied credentials.
- [ ] Run `src/backend/venv/bin/python3 -m pytest src/backend/tests/test_kubevirt_storage_graph.py -q`
  from repository root; expect import/contract failures before implementation.
- [ ] Implement full-UUID CR proof, namespace UID retention, PVC/PV claim UID
  checks and supported CSI identity resolver. Test collision of equal eight-byte
  prefixes; unrelated namespace PVC; changed PVC UID; pending/unbound PVC; Block,
  Filesystem, TPM, pod, mon/OSD, installer and unattached PVC inclusion. Do not
  require topology membership or `TroshkaVM` owner references.
- [ ] Implement the pure graph evaluator. Traverse children as well as ancestors
  to detect undiscovered consumers; reject cycles and excessive depth. Deduplicate
  images across providers; mark shared non-cache ancestors unresolved. Count
  merged image+snapshot allocation once. Retain detached/deleting storage using
  previous verified bindings. Exercise the 30/50/100 fixture and one shared
  private parent yielding nullable project totals/unattributed bytes once.
- [ ] Implement a fixed argv builder and bounded transport. Allowed commands are
  `ceph fsid`, read-only pool/CSI metadata queries and RBD info/du/snap/children/
  trash listing. Use no shell and reject unsupported command/identity syntax.
  Spawn a process group, concurrently drain capped stdout/stderr, terminate the
  entire group on timeout/output overflow, wait/reap children, return typed
  unavailable. Tests launch a harmless process which spawns a sleeping child and
  writes excess output; assert both are reaped and no output/secret reaches report.
  Direct transport is the default external CLI. Also implement an optional
  standalone existing-toolbox transport: operator manifest pins cluster UID,
  `openshift-storage` namespace, existing `rook-ceph-tools` pod UID/container,
  allowed pools and Ceph FSID. Verify those identities before each command. Invoke
  `oc --kubeconfig=... exec -n openshift-storage <verified-pod> -c <verified-container>
  -- timeout --kill-after=2s 15s rbd ...` as an argv array, never a shell. Local
  process-group deadline must exceed the remote deadline/kill-after by a bounded
  grace period. Verify timeout capability first; reject remote mode if missing.
  Tests assert exact allowlisted remote argv, wrong namespace/UID rejection,
  command-injection rejection and remote timeout reporting. This is operator CLI
  access only: no app pods/exec permission, credential creation or pod creation.
- [ ] Implement bounded collection with explicit limits from the spec, inventory
  recheck and per-object original timestamps. Never retry a slow du automatically.
  Require capability/version match for snapshot merging; reject an unexpected
  result row rather than summing unknown output. Missing ancestor is partial;
  measured zero ancestor is complete. No persisted quota writes/imports.
- [ ] Implement CLI arguments and 0600 atomic JSON output. Validate configured
  cache namespace UID and full project roots before reading backend volumes.
  `--previous-report` supplies retained identity only, never fresh byte values.
  Exit 0 complete, 2 saved partial/unavailable, 1 invalid/no usable report.
- [ ] Run all four new test files plus existing `test_quota_storage.py` and
  `test_kubevirt_provider_helpers.py`; prove legacy behavior unchanged and all
  reporting code paths leave quota tables/config untouched. Review this changeset
  before proceeding; Task 1 is usable independently as a JSON collector.

### Task 2: Persist reports and expose authorized native scope totals

**Files:** Create model/service/import CLI from file map; modify
`src/backend/app/models/__init__.py`, `src/backend/app/api/quotas.py` and native
schema; add an Alembic revision generated against the current repository head;
create `src/backend/tests/test_kubevirt_storage_reports.py` and
`src/backend/tests/test_kubevirt_storage_api.py`.

**Interfaces:**

```python
def import_report(db, report: NativeStorageReport, *, trusted_collector_ids: set[str],
                  now: datetime) -> str:  # returns persisted report UUID
    ...

def read_native_usage(db, actor, scope, scope_id, now: datetime) -> NativeStorageUsage:
    ...

# GET /quotas/{scope}/{scope_id}/native-storage
# NativeStorageUsage: scope, scope_id, metric, status,
# allocated_bytes|null, known_allocated_bytes, provisioned_bytes|null,
# observed_at|null, covered_projects, expected_projects, reason_codes,
# enforcement_eligible=False; platform_bytes only for authorized admins.
```

- [ ] Write persistence tests for changed replay and original timestamp retention:

  ```python
  def test_report_import_does_not_enable_accounting(db, report):
      from app.models.quota import QuotaReadiness, QuotaReservation
      from app.services.kubevirt_storage_reports import import_report
      before = [(r.id, r.ready) for r in db.query(QuotaReadiness).all()]
      import_report(db, report, trusted_collector_ids={report.collector_id},
                    now=report.completed_at)
      assert db.query(QuotaReservation).count() == 0
      assert [(r.id, r.ready) for r in db.query(QuotaReadiness).all()] == before
  ```

  `report` fixture loads Task 1 complete JSON; use existing test database/auth
  conventions. Seed additional existing allocations/history and compare unchanged.
- [ ] Run the new report tests to observe missing model/service failures.
- [ ] Add append-only snapshot and retained-binding tables without project/provider
  deletion cascades. Register model and write migration upgrade tests. Store
  original timestamps, digest and canonical backend identities; enforce same-ID
  same-content replay. Import rejects untrusted collector and mismatching current/
  retained project/namespace identity; retain old evidence on import failure.
- [ ] Implement explicit-target importer CLI. Require a target DB argument or
  dedicated environment variable, never silently use production/application DB.
  Validate a local operator-owned report and collector allowlist; no network
  ingestion endpoint, no collector subprocess, no credential provisioning.
- [ ] Implement scope query using `authorize_policy(..., lock=False)` and current
  owner/organization membership. Deduplicate all backend identities across
  providers. A report for a deleted project is visible only under permitted
  retained accounting/admin ownership rules; do not infer access from old user
  names. Mixed-provider scope counts native projects explicitly. Stale evidence
  uses original timestamps; partial project totals make scope total nullable.
- [ ] Add route authorization tests: owner/shared project viewer/admin allowed;
  other owner denied; project share cannot read owner's/org totals; manager scope
  obeys existing policy; revoked membership immediately fails; non-admin response
  contains no graph IDs, other-project amounts or pool cache totals. Verify the
  route invokes no collector or provider clients and works with baseline unready.
- [ ] Run report/API tests and `test_quota_permissions.py`,
  `test_quota_usage_api.py`, `test_quota_migration.py`; review migration and API
  output before proceeding. No production migration is part of this task.

### Task 3: Native allocation panel and reporting end-to-end verification

**Files:** Create `src/frontend/src/lib/kubevirtStorage.ts`,
`src/frontend/src/components/quotas/NativeStorageUsage.tsx` and matching tests;
modify `QuotaScope.tsx`, existing quota styles and safe supported-scope copy;
create `docs/dev/kubevirt-storage-accounting.md`.

**Interfaces:**

```typescript
import { Quantity, Scope } from "@/lib/quotas";
export interface NativeStorageUsageData {
  scope: Scope; scope_id: string; metric: "rbd_allocated_extent_bytes";
  status: "complete" | "partial" | "stale" | "unavailable";
  allocated_bytes: Quantity | null; known_allocated_bytes: Quantity;
  provisioned_bytes: Quantity | null; observed_at: string | null;
  covered_projects: number; expected_projects: number; reason_codes: string[];
  enforcement_eligible: false; platform_bytes?: Quantity | null;
}
export function fetchNativeStorage(scope: Scope, id: string): Promise<NativeStorageUsageData>;
// component props: {scope: Scope; id: string; onDenied?: () => void}
```

- [ ] Read frontend AGENTS and write component/API transport tests. Use exact
  integer parser and GiB formatter from `quotas.ts`; add >2^53 bytes test. Assert
  partial report says “Measured subset”, complete zero displays zero, null total
  says unavailable and stale value says “Last measured”. Assert report loading
  cannot change baseline/enforcement labels or quota edit state.
- [ ] Run `npm test -- --run src/components/quotas/NativeStorageUsage.test.tsx`
  from `src/frontend`, adapting only to the existing package's exact test script
  after reading package.json; expect missing component failures.
- [ ] Implement independent fetch/error/loading and cancellation guards. Place
  the native component outside `QuotaScope`'s usage-loaded-only branch so a missing
  quota baseline/error does not hide reporting. Clear data on scope change and
  403/404, preserve request ordering, and stop updating unmounted views. No policy
  progress bar based on report-only bytes. Show “Allocated RBD storage”, separate
  provisioned PVC capacity, UTC source time, native project coverage and
  “Reporting only; native quota enforcement unavailable”.
- [ ] Add project/user/org route fixture tests and UI integration test using a
  real temporary SQLite database importer and FastAPI response, plus the frontend
  fixture of that response. Verify owner transfer moves current aggregate without
  rewriting original reports, and shared cache does not multiply in org totals.
- [ ] Write the runbook: external read-only credential prerequisites, explicit
  inventory/collector/import commands, meaning of zero/null/partial/stale, slow
  no-fastdiff handling, retention and no enforcement enablement. Include safe
  argv-only examples and original timestamp preservation. No secret creation.
- [ ] Run frontend targeted tests and typecheck, backend reporting suites and
  existing quota component tests. Replay sanitized sampled child→private-parent
  zero→golden evidence end-to-end. If separately authorized read-only access is
  available, collect a real complete/partial report with explicit limits and
  compare every bound PVC; otherwise retain the live acceptance gate as unrun.
  Deliver Tasks 1–3 as working observation-only output; keep enforcement off.

### Task 4: Native execution demand and adapter boundary

**Files:** Create `quota_native.py`,
`src/backend/tests/test_quota_native_plan.py`; modify focused interfaces in
`quota_operations.py`, `quota_resources.py`, `placement.py`, provider
`kubevirt.py`, operator topology helpers and resource templates.

**Interfaces:** `build_native_plan(db, project, host) -> NativeExecutionPlan`;
`NativeExecutionPlan` includes immutable digest, generation, full compute
inventory, per-pool storage initialization demand, helper classification and
unsupported-path reasons. `native_resources(plan) -> Resources` rounds aggregate
helper millicores up to whole CPUs while recording underlying millicores.

- [ ] Write tests comparing generated native resources to planned demand for
  basic VMs, TPM, pod volumes, installer, Showroom and Ceph appliance topology:

  ```python
  def test_all_generated_workloads_have_demand(native_plan):
      assert set(native_plan.generated_resource_ids) == set(native_plan.demand_ids)
      assert native_plan.cpu >= native_plan.guest_cpu
      assert not native_plan.unclassified_resources
  ```

  `native_plan` fixture builds the existing complex native topology through the
  same render helpers used by deployment; not a handwritten list of expected IDs.
- [ ] Run failing native-plan tests, then extract pure final-plan rendering before
  admission. Include warm/cold cache imports, transient maximum concurrency and
  full-copy guest paths; unsupported profiles fail with named resource reasons.
  Do not reuse nested initialization allowance. Bind plan to retained topology;
  reject changes between reservation and dispatch.
- [ ] Introduce adapter selection that defaults native to unavailable until later
  readiness gates. Preserve nested semantics/tests. Resolve canonical pool aliases
  and admission unit conversion; no pool operation under inconsistent units.
- [ ] Run native-plan and existing quota entry-point/admission/resource tests.
  Review an explicit coverage inventory of every native allocation mutation,
  including recovery, BMC, captures/file-pull, reconfigure and background jobs.

### Task 5: Durable operator growth fence and native dispatch intents

**Files:** Create `src/operator/helpers/quota_fence.py` and tests; modify
`src/operator/crds/{troshkaproject,troshkavm}.yaml`, operator project/VM/network/
container/ceph handlers, native provider and `quota_native.py`.

**Interfaces:** `NativeAuthorization` contains generation, operation_id,
execution_digest, desired_growth_state and epoch. Operator coordinator
`begin_intent(resource_key, authorization) -> intent_id`,
`finish_intent(intent_id, observed_uid, resource_version)` and
`acknowledge_fence(epoch) -> FenceStatus` uses durable lease/CAS, not process-local
locks. `FenceStatus` contains observed epoch and unresolved accepted intents.

- [ ] Write a race test where a paused handler holds an accepted intent while a
  fence arrives. Assert fence is not settled until its Kubernetes request is
  resolved, and another handler cannot begin a growth intent after fence ack.
  Restart the coordinator in the same test from persisted Lease/status objects;
  unresolved intent remains, never automatically redispatched.
- [ ] Implement the shared durable coordinator; guard every mutation listed in
  Task 4 including delayed retries, timers, BMC and child recreation. Use expected
  UID/resourceVersion preconditions, immutable operation keys and bounded leases.
  A crash/expired lease does not prove the previous HTTP call failed: reconcile
  original intent before fencing completion or permitting replacement work.
- [ ] Wire backend cancellation into operator fence and wait for observed epoch
  plus settled intents. Prevent existing stale-CR replacement logic from erasing
  ambiguous generation. Preserve operation IDs across deployment monitor handoff.
- [ ] Run operator/native recovery suites with 409, timeout-after-create, old CR,
  duplicate delivery and operator restart cases. Review the coverage inventory;
  any unguarded writer prevents completion of this milestone.

### Task 6: Native observations, shutdown and confirmed cleanup

**Files:** Modify `quota_native.py`, `quota_operations.py`, `quota_storage.py`,
`quota_cleanup.py`, `quota_enforcement.py`, `ws_pubsub.py` and native lifecycle
branches in `deploy_service.py`; add `test_quota_native_lifecycle.py` and
`test_quota_native_enforcement.py`.

**Interfaces:** `observe_native(project_id, generation) -> NativeObservation`;
`stop_native(project_id, fence_epoch) -> NativeStopResult`;
`verify_native_absence(project_id, generation) -> AbsenceEvidence | None`.
NativeObservation carries direct VM/VMI/pod UID states, storage report references,
original times and operator settlement. These types live in the native schema.

- [ ] Write tests for all-VMs-off but live installer/Showroom writer; paused VMI;
  partial VM starts; old CR status; terminating PVC; deleted namespace with retained
  PV/snapshot/trash; missing provider. All retain capacity/evidence appropriately.
- [ ] Implement native direct observations and current membership locks following
  existing operation→identity order. Bind compute to admitted plan, use per-resource
  confirmed changes, preserve full pending demand when uncertain. Native storage
  measurements may increase usage; reductions require settled generation.
- [ ] Fence and stop VMIs plus owned writers/helpers. Confirm actual absence of
  running compute, not desired Halted state; paused VMIs remain compute. Retain
  slot/storage. Retry durable actions without auto-restart or deleting data.
- [ ] Route native destroy through generic ledger/meter finalization before project
  deletion. Independently prove retained backend image/snapshot/trash absence;
  never treat namespace absence or unavailable credentials as deletion evidence.
- [ ] Run focused lifecycle/enforcement/concurrency suites. Measure bounded native
  collection/stop latency; keep native enforcement unavailable unless it satisfies
  the enforcement freshness/reserve contract. Reporting's 15-minute status is not
  a quota admission freshness policy.

### Task 7: Complete workload coverage and native baseline migration

**Files:** Create `quota_native_baseline.py`; modify `quota_baseline.py`,
`quota_accounting.py`, `scripts/backfill_quotas.py`, baseline docs and native
integration tests; extend policy/storage semantic metadata with an additive
migration if required by finalized adapter contracts.

**Interfaces:** `validate_native_baseline(entry, inventory, fence) -> NativeBaseline`;
`import_native_baseline(db, baseline) -> str`. Baseline version is explicit v3;
native metric is allocated RBD extents, never mislabeled allocated-v1 filesystem.

- [ ] Write baseline tests with the full complex inventory, existing quotas off,
  old reservations/history and retained storage. Unknown shared parent, installer
  writer, EIP, capture/file-pull or full-copy mode remains a named readiness blocker
  until its demand/dispatch/cleanup path is covered, never silently excluded.
- [ ] Implement native maintenance evidence and original UID/generation settlement;
  preserve ledger operation IDs and money history. Native reports supply storage
  evidence but cannot themselves set readiness. Validate every retained project,
  owner/org assignment, pool alias and auxiliary workload against independent
  observations collected after all writers are fenced.
- [ ] Rehearse import on disposable PostgreSQL and verify rollback preserves old
  allocations and invoices. Test two-session pool/membership races, report replay,
  deletion/redeploy generations and owner transfer. Require all-phase completion
  before marking native capability supported in runtime admission and UI copy.

### Task 8: Evidence-based operational acceptance

**Files:** Extend `docs/dev/kubevirt-storage-accounting.md`,
`docs/dev/quotas.md`, `docs/dev/quota-allocation-paths.md`; add a restricted
acceptance report artifact under the rollout evidence directory.

- [ ] Run a separately authorized disposable native storage experiment: large
  thin disk, two golden clones, independent writes, retained snapshot/overwrite,
  stop, PVC deletion with retained backend, reclaim completion and provider alias.
  Compare unique image+snapshot allocation, cache and private intermediates at
  each step; record Ceph/CSI/CDI versions and measurement duration.
- [ ] Run full complex environment deploy/start/stop/destroy with installer,
  Showroom, Ceph pods, TPM and copies only after their adapter tests pass. Inject
  collector loss and controller restart; prove no growth after acknowledged fence,
  storage retained on shutdown and no unconfirmed release.
- [ ] Produce a rollout report separating reporting coverage, lifecycle coverage,
  native baseline readiness and enforcement state. Any failed gate remains open
  with original identities and evidence. No automatic production enablement,
  backfill, credential change or deployment follows a successful reporting test.

## Plan self-review

Tasks 1–3 cover all-namespace discovery, unique ancestry ownership, exact metric,
bounded transport, reporting-only persistence, project/user/org authorization and
usable UI. Tasks 4–6 cover demand, every mutation surface, operator settlement and
native lifecycle/release. Tasks 7–8 cover migration and actual complex inventory.
Interfaces preserve metric names and separate report status from quota readiness.
No full enforcement claim is permitted while any later-phase gate remains open.
