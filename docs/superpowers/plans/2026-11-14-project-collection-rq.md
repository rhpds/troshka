# Project collection RQ implementation plan

> **For agentic workers:** Use subagent-driven-development; preserve the shared checkout and existing cleanup overlay changes. Do not commit, push, or deploy independently.

**Goal:** Run bounded project collection jobs in Troshka's existing RQ infrastructure without coupling unrelated accounting or starving lifecycle jobs.

**Architecture:** Project attempts have explicit scope and durable ordering. Shared domain observations remain separate. A dispatcher submits identifier-only jobs to collection-profile RQ workers, with cleanup priority, leases, provider limits, and no thread fallback.

**Tech Stack:** Python3, FastAPI, SQLAlchemy/Alembic, Redis/RQ, Helm.

**Spec:** docs/superpowers/specs/2026-11-14-project-collection-rq-design.md

## Global constraints

- Preserve project ownership, topology, accrued spending, shared images, and all existing cleanup safeguards.
- Preserve the two uncommitted managed_power.py cleanup changes.
- No shared-pool purge, production mutation, or Git push.
- Use the persistent dev app and immutable source overlays for rapid testing; database and worker configuration changes remain explicit.
- Keep new scheduling disabled until its runtime configuration and accounting tests pass.
- Never bypass locks via the generic enqueue helper's daemon-thread fallback.

## Task 1: Scoped accounting reports

Files: native storage model/schema/migration, kubevirt_storage_reports.py,
kubevirt_storage_usage.py, quota_native.py, quota_cleanup.py,
native_trash_cleanup.py, focused scoped-report tests.

Interface: `NativeStorageReport.project_scope: UUID | None`; a shared
`report_cursor(db, domain, project_id=None)` selects the project attempt or legacy
domain fallback. `domain_evidence(..., project_id=None)` applies scoped roots and
retained evidence. Project attempts cannot advance domain state.

- [ ] Add failing tests for independent A/B reports, failed attempt isolation,
  late completion, replay/conflict, foreign roots, legacy fallback, and stale scope.
- [ ] Run `./venv/bin/python3 -m pytest tests/test_native_project_reports.py -q`.
- [ ] Implement additive migration, scope validation, selection and all consumers.
- [ ] Run scoped report, native measurement, usage, and cleanup regression tests.

## Task 2: Queue/lease/worker integration

Files: new native_collection_jobs.py, worker entrypoint/configuration, main startup,
Helm collection worker profile, worker budget, job concurrency tests.

Interface: identifier-only `run_project_collection(domain_id, project_id)` invokes
`native_collection_runtime.run_collection(domain_id, project_id)`; domain work
passes `project_id=None`. Configuration lives under `native_collection`.

- [ ] Test Redis-down/no-thread behavior, stable job IDs, lease token release,
  provider capacity, duplicate dispatch and preservation of lifecycle job refs.
- [ ] Implement separate native_cleanup/native_collection queues with bounded
  job timeouts and owned leases. Defer when provider capacity is occupied.
- [ ] Add configurable collection worker profile and reserve lifecycle workers.
- [ ] Wire disabled-by-default scheduling and include collection DB connections
  in capacity budgeting. Test worker entrypoint and Helm rendering.

## Task 3: Protected project collection runtime

Files: native_managed_collector.py, import_kubevirt_storage.py,
native_collection_runtime.py, runtime tests.

Interface: `cycle(..., project_id=None)` preserves domain enrollment and shared
verification when unscoped; scoped jobs collect and reconcile only their project.
Runtime loads administrator-owned config by domain ID; job payloads cannot set
paths, kubeconfigs, backend identities or credentials.

- [ ] Test scope validation, filtered roots/bindings/intents, independent report
  failures, unchanged source timestamps, and no unrelated reconciliation.
- [ ] Extract/reuse CLI collection setup with protected fixed config resolution.
- [ ] Separate shared enrollment/source/lineage work from per-project measurement.
- [ ] Preserve exact COW attribution and active/trash absence rechecks.
- [ ] Test synthetic hundreds-project scheduling and bounded remote command work.

## Task 4: Review and dev acceptance

- [ ] Review scoped report consumers and concurrency/credential boundaries.
- [ ] Run SQLite/PostgreSQL accounting/cleanup suites and queue tests.
- [ ] Snapshot dev configuration; apply additive migration and scoped overlay.
- [ ] Configure collection-profile worker using protected mounted config; stop
  manual collector before enabling durable scheduling.
- [x] Verify runtime hashes, health, worker registry, preserved storage identities.
- [x] Exercise real-image API lifecycle, guest writes, overage stop, restart and
  cleanup; confirm zero private trash and preserved shared images/ownership.
- [x] Verify worker restart recovery and lifecycle capacity under collection load.
- [x] Record results and remaining limitations. Keep dev environment intact.

## Execution ledger

- 2026-11-14 readiness separation: live combined domain work took 68 seconds,
  exceeding the independent 60-second installed-identity freshness ceiling.
  Add `native_readiness` with one reserved RQ readiness worker; preserve two dev
  lifecycle workers and the existing collection worker. All share Redis, image,
  and worker entrypoint. Readiness uses identifier-only jobs, a distinct domain
  reservation/execution lease, and one separate read-only slot per provider/backend.
- Readiness runs only protected initialization observation, with a 45-second RQ
  deadline and 35-second scan deadline; it never runs enrollment/source coordination
  or platform image scans. Schedule readiness every 20-25 seconds; restore platform
  domain scheduling to 60 seconds plus jitter. Scheduling remains disabled by default.
- Platform attempts never publish or invalidate readiness. Readiness failures never
  invalidate platform storage. Existing attempt ordering and original observation
  timestamps remain unchanged. Helm and live worker DB budgets include both reserved
  native worker profiles. Live readiness duration/freshness verification remains a
  deployment acceptance requirement; no stale timestamp extension is permitted.

- Ruling: reuse current feature checkout and keep edits uncommitted for overlay
  testing — user requested fast dev overlays and later consolidation.
- Ruling: separate queue profiles share Redis/RQ and image; existing lifecycle
  replicas are preserved — non-preemptive jobs otherwise exhaust all capacity.
- 2026-11-14: Implemented scoped attempt cursors, separate shared initialization
  and cache observations, bounded identifier-only RQ dispatch, cleanup priority,
  and lease checks at publication and trash-removal boundaries.
- Integration validation: 359 passed, 2 skipped. Follow-up runtime/shared-storage/
  trash fencing validation: 38 passed, 20 PostgreSQL-only cases skipped in the
  SQLite run; a separate disposable PostgreSQL run is tracked independently.
- Applied additive schema `d92b51e7046a` to the persistent dev app after saving
  and verifying a private PostgreSQL archive. The initial Alembic revision-path
  lookup failed without schema changes; the corrected lookup then migrated.
- Applied immutable source overlay to backend, both lifecycle workers and tunnel.
  Verified 26 source files plus the previous managed-secret cleanup overlay on
  each of the four replicas. Existing lifecycle queues and replicas preserved.
  Evidence and exact rollback patches are in the private rollout directory's
  `collection-rq-overlay/`; no production changes or image rebuild performed.
- Automatic collection remains disabled pending worker transport configuration,
  final review fixes and live RQ acceptance. Shared-cache measurement reuse in
  project reports remains unwired; private observations must retain strict
  per-attempt timestamps until explicit shared provenance is implemented.
- Follow-up disposable PostgreSQL validation: 62 passed. Final review corrected
  runtime UUID scope conversion, rejected private timestamps predating an
  attempt, and shortened domain dispatch independently of the project interval.
  All three fixes are in `troshka-collection-20261114-062611-review`; runtime
  hashes and two lifecycle workers verified after rollout.
- Started one dedicated collection worker using the same backend image and
  both source overlays. A named OpenShift CLI image supplies only the CLI binary
  through an init container. Provider-token transport is mounted only in this
  worker, with verified API CA roots; no laptop administrator credentials were
  copied. Toolbox access is scoped to the pinned pod in openshift-storage.
  Verified queue registration, protected config ownership checks, cluster UID
  and Ceph FSID. One shared-observation RQ acceptance job submitted; scheduling
  still disabled pending its result. No project lifecycle work was interrupted.
- First live RQ job failed safely on a fourth, previously deleted retained cache
  image. Read-only diagnostics confirmed its active and trash absence; the
  other three caches measured normally. Added existing Kubernetes cache
  discovery checks, repeated backend absence and inventory rechecks, and
  persisted absence provenance. Platform-byte failures now preserve separately
  verified initialization. Focused tests passed (23); independent review found
  no concrete blocker. The two-file fix is mounted in
  `troshka-collection-20261114-063759-review` on all five replicas, with all 27
  source/cleanup hashes verified. A new RQ acceptance attempt is in progress.
- The fixed RQ job `336d537c0184406d9c351c14d57c74dd` completed successfully
  at 2026-11-14T06:39:37Z. Persisted initialization and platform storage attempts
  are complete: three live cache images measured, the fourth conclusively absent.
  Evidence: private `collection-rq-overlay/shared-job-verified.json`.
- Scheduling remains intentionally disabled. This first shared scan took about
  68 seconds, longer than the 60-second initialization freshness window; shorter
  enqueue cadence alone cannot solve that under load. Before enablement, separate
  frequent identity refresh from expensive shared-byte scans/reuse and complete
  per-project API acceptance. Current evidence proves the overlay and real RQ
  worker integration, not hundreds-project freshness or full lifecycle acceptance.
- Redeploy review found and fixed two scoped-report integration gaps: historical
  settled roots are now excluded before root cardinality checks; cache-only
  baseline readiness uses validated shared storage evidence, falling back to the
  legacy report only before any shared observation exists. Unsettled duplicates
  and failed/stale shared attempts still fail closed. Both regressions were
  reproduced before fixing. Independent review found no regression; 84 tests
  passed with five PostgreSQL cases skipped, followed by all five passing against
  the disposable PostgreSQL database. Private logs: native-rq-redeploy-regression
  and native-rq-redeploy-postgres-verified under /private/tmp.
- Prepared a new one-shot, receipt-backed API acceptance helper for the existing
  dev project, using new guest write filenames and new receipts. No lifecycle
  mutation submitted yet. Read-only API verification confirmed draft state,
  unchanged owner, and zero used/reserved resources.
- Applied readiness separation in dev as immutable source ConfigMap
  `troshka-collection-20261114-121113-readiness`; verified 28 source/cleanup
  hashes on six replicas. Added one `native_readiness` worker, retaining the
  collection worker and both lifecycle workers. Manual concurrent RQ acceptance:
  readiness job `2a9f34259be844d7acb950b82d80b57a` completed in 21 seconds;
  platform job `e0fac6ea87bb4e47b3b5a21e8a38fe0d` completed in 63 seconds.
- Enabled dev scheduling for the single configured domain using immutable
  backend config `troshka-rq-scheduling-20261114-121445`. Subsequent readiness
  jobs completed in 21–23 seconds while heavy collection continued independently.
  API reported accounting ready, baseline ready, enforcement active. A transient
  route 503 during rollout cleared after backend readiness; no write was retried.
- Submitted new real-image deployment through Troshka API at 12:15Z (HTTP 200).
  Operation `695f75af-ee67-4403-97f0-51b2b5b8b844` is bound with one source;
  startup allowance remains reserved while the coordinator verifies it.
  Acceptance is in progress; new receipts live in private `rq-api-acceptance/`.
- Readiness worker restart recovered automatically with subsequent 23–25 second
  observations. All six replicas retained their verified overlay hashes and the
  two lifecycle workers remained available.
- Cache import completed at 12:17:28Z. A read-only diagnostic verified its source,
  CSI identity, image ancestry, and writer absence. The scheduled coordinator
  published its cache receipt at 12:21:04Z, then failed its later shared discovery
  within the bounded scan. The following coordinator completed at 12:24:58Z;
  durable state advanced to `initialization-intent` with one clone lineage.
  The earlier `cache_verification_pending` diagnostic was stale metadata, not
  evidence of a current receipt failure. No timeout or freshness changes made.
- Fixed successful cache/clone verification leaving stale pending diagnostics.
  Clearing requires all subjects to succeed, the same operation target, no
  cancellation, a matching diagnostic, and a valid collection lease. Focused
  SQLite and PostgreSQL validation: 12 cases each passed. Full managed-image
  SQLite suite: 64 passed, 65 PostgreSQL variants skipped. Ruff passed. This
  follow-up is not deployed yet; the active acceptance scan is left uninterrupted.
- The next scoped report persisted the new source binding, and the shared scan
  completed at 12:31:03Z without a code change. API startup succeeded: project
  active, baseline ready, enforcement active, configured 2 CPU/4 GiB charged,
  all reservations zero. Initial verified COW charge was 125,829,120 bytes for
  a 42,949,672,960-byte virtual disk. Guest-agent probe and first 256 MiB write
  succeeded through the Troshka API (HTTP 200, guest exit 0).
- Applied the diagnostic-only follow-up as immutable dev overlay
  `troshka-collection-20261114-123202-diagnostic` after checking idle lifecycle
  workers. Rollout succeeded; all 28 source/cleanup hashes verified on six
  replicas, schema unchanged, both lifecycle workers and independent collection
  and readiness queues registered. Scheduling remains enabled. Awaiting a report
  whose observation begins after the first guest write before arming overage.
- Post-write scoped measurement completed at 12:35:08Z. Through the API, set the
  test limit to 1,358,954,496 bytes (fresh measured 1,224,736,768 plus 128 MiB)
  and wrote the second 256 MiB file successfully. Automatic shutdown is pending
  the next scheduled project measurement; original 3 GiB limit is saved privately.
- Live worker-budget reporting exposed missing GET permission for the two new
  worker Deployment names. Added a separate exact-name GET-only Helm Role rule;
  existing deployment patch scope is unchanged. Regression/render validation:
  16 passed, Ruff passed. Applied the identical dev Role addition with backup and
  guarded rollback. Backend status now succeeds: 2 lifecycle replicas, one
  collection and one readiness replica, 58 DB connections, 7 lifecycle-worker
  ceiling. No worker scaling was performed.
- Scheduled collection measured 1,493,172,224 bytes after the second write.
  Enforcement created `physical_storage_limit` cause
  `2e489c0b-dc3a-43f8-ac13-be2e0cdb3131`; its lifecycle RQ job issued the stop
  at 12:38:22Z. Kubernetes confirms VM stopped and no VMI, with disk retained.
  Final settlement exposed inherited retry history: a new stop episode reused
  attempt 5, delaying its follow-up observation until 12:46:22Z. The initial
  missing proof is normal asynchronous operator completion, not stop failure.
- Reset attempts only when reopening a completed/cancelled stop for a new
  episode. Same-episode retry and already-off reobservation retain backoff and
  identity. Eight new regression cases plus affected suites: 64 passed, Ruff
  passed. Applied immutable dev overlay
  `troshka-collection-20261114-124358-stop-episode`; existing issued action's
  durable deadline remains unchanged. Runtime verification is pending.
- Retry-fix runtime verification passed: 29 hashes on six replicas, expected
  schema and queues, old workers drained. The untouched durable retry fired at
  12:46:40Z and completed at 12:46:42Z. API confirmed stopped state, zero CPU/RAM,
  retained storage 1,493,172,224 bytes, and ready accounting. Restored original
  3 GiB limit and requested restart through Troshka API (both HTTP 200).
- Restart completed through the API; both 256 MiB guest files survived. Captured
  project ownership/topology and shared-cache evidence, then submitted API
  undeploy at 12:48:56Z. New cleanup stop correctly began with attempts zero.
- The first cleanup scan (12:50:29–12:51:39Z) overlapped teardown beginning at
  12:51:20Z. Its initial snapshot had no retirement authority; final checks found
  the private images removed. Import rejected the mixed snapshot with
  `retiring_root_evidence_missing`, preserving the charge. Namespace deletion
  followed at 12:51:52Z. A fresh cleanup-priority scan began at 12:53:00Z;
  final settlement and shared-cache preservation remain under verification.
- Final acceptance passed. Cleanup-priority job
  `cleanup-648d39dab0e144ecb7d5e4337aa8a78e` completed at 12:54:55Z;
  accepted settlement report `b42b2301-f18a-4ca4-ab9d-496259424047`
  confirms private images `a9c1c32966f8d6` and `a9c1c396595246` absent from
  active storage and trash. API project is draft with unchanged owner/topology,
  zero used/reserved allocations, finalized undeploy intent, and namespace absent.
  The next shared observation completed and preserved all five prior cache
  records with unchanged allocation and absence states. Full assertions and
  evidence are in private `rq-api-acceptance/cleanup-verified.json`.
- Final runtime verification: 29 source/helper hashes on each of six replicas,
  schema `d92b51e7046a`, two lifecycle workers, one collection worker and one
  readiness worker, scheduling enabled. `git diff --check` passed. Dev remains
  running; no production mutation, commit, or push performed.
- Acceptance limitations: synthetic 500-job scheduling is not a live
  hundreds-project capacity benchmark. Exact private-image active/trash absence
  is proven; this run does not claim a trash-removal command was necessary.
  Changes remain in the working tree and dev overlays; normal image packaging
  and promotion remain separate release work.
- Release validation: full backend suite completed in 368 seconds with 8,886
  passed, 473 skipped and six failures. All six were stale test expectations:
  original migration versus later native tables, three versus six queues, and
  obsolete blanket first-deploy denial messages. Corrected tests only, retaining
  exact table sets, queue failure isolation and denial before host selection.
  Reran all four affected modules: 209 passed. Operator managed-power suite:
  74 passed. Ruff on changed tests and `git diff --check` passed. The complete
  backend suite was not repeated after these test-only corrections.
- Permanent image preparation uses the existing clean-commit build workflow.
  Current branch remains `codex/managed-group-billing`, with changes uncommitted;
  no image build, integration, push or rollout performed in this release check.
- Local integration follow-up: commit hooks reformatted Python source (AST
  equivalence verified) and found collection typing issues. Declared the lease
  ContextVar type, retained explicit/exact toolbox pins through validators for
  all Mapping inputs, and compared shared/private image identities as exact
  four-field tuples. Focused collection tests: 77 passed; shared-storage tests:
  17 passed. Scoped Pyright passed and Helm lint passed. Commit/merge verification
  follows these corrections; no runtime overlay changes were made for them.
