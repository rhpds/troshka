# Native deployment origin implementation plan

> **For agentic workers:** Execute task by task with independent review. The user authorized completing all code before pushing on November 17. No push or deployment is part of this implementation stage.

**Goal:** Make new quota-OFF native deployments eligible for the existing guarded deletion workflow using durable creation evidence, without inventing historical ownership.

**Architecture:** Retain creation intent and exact namespace/CR identities independently of quota admission. Fence project activity and retain storage liabilities; delete VMs and verify VMIs gone, then use normal namespace teardown and independent storage settlement. Keep generic topology builders and producers. The spec's superseding namespace cleanup contract overrides older stop/seal/per-resource-drain requirements below.

**Tech Stack:** Existing Python/FastAPI, SQLAlchemy/PostgreSQL, Kubernetes client and Kopf operator.

**Spec:** `docs/superpowers/specs/2026-11-17-native-deployment-origin-design.md`.

Execution order after dependency review: 1, 3, 2, 6, 4, 5. Task 6 is the
snapshot-specific portion of Task 2 (also called Task 2b in review notes).
Task 2 establishes the ordinary claim handshake and safe temporary snapshot
holds; those holds do not constitute finished capture/file-pull support.

Task 4 is reviewed in three serial slices: 4a writer and cancellation
prerequisites, 4b support recovery and root-never-submitted namespace cleanup,
then 4c rootful namespace cleanup and typed cleanup authority. All three are required;
passing a prerequisite review is not completed deletion support. The user
approved namespace-scoped, LIST-only inventory access for managed project
namespaces so the early-failure path can detect arbitrary resource types.
No cluster-wide inventory binding or new mutation grant is included.

## Constraints and simplifications

- No historical origin backfill, fake administrator attestation, quota activation, or inferred backend/cache trust.
- Preserve generic VM/CDI, network, container, BMC, EIP, Showroom and S3 behavior. Do not substitute `native_managed_deploy.final_payload` or its pending initialization profile.
- An origin token proves a specific durable creation attempt, not namespace-wide storage exclusivity.
- Reduce the spec's proposed event framework: use one immutable intent and immutable typed receipts for namespace binding, root binding and custody checkpoints. Unique intent/receipt-kind constraints and a locked active-attempt pointer suffice; a generic sequenced hash-chain event engine is unnecessary. Custody checkpoints retain their explicit predecessor and digest.
- Reuse `NativeRetainedBinding`, lineage/ancestry records and binding-retirement evidence for storage liabilities. Do not create another disk ledger.
- Reuse the existing cleanup-only `QuotaReservation` adapter; this is compatibility identity, not admission or observed zero compute. Keep `compute_accounting="unavailable"` and no allocation creation.
- Freeze deployment lifecycle mode in the intent. Later quota/capture toggles cannot reinterpret an existing attempt.
- All commands below run from `src/backend` with `./venv/bin/python3`. PostgreSQL runs must be serialized with other agents.

## Task 1: Durable creation boundary and exact recovery

**Files:** create `src/backend/app/models/native_deployment_origin.py`, `src/backend/app/schemas/native_deployment_origin.py`, `src/backend/app/services/native_deployment_origin.py`, a new Alembic revision; register models in `app/models/__init__.py`. Modify `app/services/providers/kubevirt.py` and `app/services/deploy_service.py`.

**Records:** immutable intent UUID, project/owner/organization/host/provider IDs, attempt token, mode, cluster UID, namespace/name, namespace mode, final generic CR-spec digest and sanitized support-intent digests. Namespace receipt pins UID/token/cluster; root receipt additionally pins namespace UID, CR UID/generation/spec digest. Keep records after project deletion. Use PostgreSQL UUID columns consistent with existing retained models. A project active-attempt pointer is mutable only under canonical locks.

**Proposed internal interface:** `prepare(db, project, provider, descriptor) -> OriginIntent`; `bind_namespace(db, intent_id, observed) -> NamespaceReceipt`; `bind_root(db, intent_id, observed) -> RootReceipt`; `recover(api, intent, receipts) -> observed`. Observed values are exact provider readbacks, not callback assertions.

- [ ] Add `tests/test_native_deployment_origin.py` first: provider POST cannot run before intent commit; same token retry is idempotent; different token/cluster/namespace UID/CR UID/spec fails; unavailable tracking cannot establish idle; receipt survives project deletion. Run that module and observe failures before implementation.
- [ ] Split `_ensure_project_s3_secrets` into configuration resolution and writes. Resolve MTU/S3/Showroom inputs and call existing `_build_troshka_project_cr` before project-namespace writes. Store hashes, not S3 credentials, private keys or presigned URLs in origin payloads. Recover an already-created CR by its exact spec digest; do not persist a second plaintext launch bundle.
- [ ] At `KubeVirtDriver.deploy_project`, commit intent before `_ensure_project_namespace`. Stamp the exact token on namespace and CR. Namespace POST/readback must bind before RBAC or Secret writes. Preserve existing external-namespace provisioning but record no namespace-delete authority. Shared `_ensure_operator_crds` and cache setup are provider infrastructure, never project-owned origin assets.
- [ ] Replace name-only recovery for origin-participating attempts in `_kubevirt_create_or_resume_cr`: the `Deploying` early return, `_replace_stale_kubevirt_cr`, and `AlreadyExists` exception handler all require exact origin recovery. Historical attempts retain their explicit recovery hold. Timeout followed by a matching object can bind; absence permits retry of the same intent; mismatch holds. A bound UID cannot be replaced by a retry.
- [ ] Use short transactions around provider calls: lock user, organization, project in `quota_locks.lock_rows` order, re-read identity, persist intent, commit; after I/O reacquire and compare active intent/cancellation fence before binding. Do not hold DB locks across remote calls. Cleanup and late completion must serialize on the same project lock.
- [ ] Test crash injection before/after namespace POST, namespace bind, grants/Secrets, CR POST and root bind; cancellation and late responses; mode toggle after intent; generated credentials absent from durable origin serialization. Run SQLite and PostgreSQL variants before moving on.

## Task 2: Preserve generic producer liabilities before replacement

**Files:** modify `src/operator/handlers/vm.py`, `src/operator/handlers/project.py`, `src/operator/helpers/managed_power.py`; add a narrowly scoped origin checkpoint service/API only where existing authenticated callback plumbing cannot carry it. Inspect the actual callback authentication path before selecting an endpoint; do not trust annotations or unauthenticated operator reports as proof.

**Boundary:** `_finalize_kubevirt_deploy` is too late to establish all storage liabilities. In `vm.py`, `_recreate_failed_clone_datavolume`, `_try_delete_datavolume`, `_delete_datavolume`, and old-CDROM cleanup can remove original PVC identities first. `project.py` also removes import/helper PVCs. Enumerate these exact deletion sites before implementation; a success-only finalizer hook is insufficient.

- [ ] Add `tests/test_native_origin_producer_fences.py`: a clone retry or disk replacement with an uncheckpointed old PVC must hold; a forged callback must not publish custody; checkpoint preserves the old binding if later topology drops it; teardown already carrying a valid cleanup intent continues through existing fences.
- [ ] Implement an origin-aware pre-removal checkpoint request. The backend independently reads exact PVC/PV/CSI/backend identities and persists existing retained bindings/ancestry before acknowledging removal. An operator-supplied PVC name only selects a read target. Unavailable backend evidence holds removal; never records fabricated zero usage. A stable request token makes retry idempotent.
- [ ] Reuse `helpers.managed_power.producer` and `checked_await` for cancellation/cleanup fencing, without pretending generic deployment used managed initialization. Inventory all generic storage-removal paths and ensure they require either the checkpoint acknowledgment or an existing cleanup intent.
- [ ] Capture a checkpoint after `_finalize_kubevirt_deploy` finishes routes/EIPs and records the resulting topology, and on failed deployments with a bound root. Capture alone does not make cleanup eligible until complete storage evidence exists.

This task is the principal release dependency. If a storage object can appear and disappear without an observable retained identity, automatic cleanup must hold that attempt. Do not claim full generic failure-path deletion until this test is satisfied.

## Task 3: Reuse verification and explicitly enroll the new root

**Files:** new `app/services/native_origin_custody.py`; focused extraction from `native_online_enrollment.py`; integration with `kubevirt_storage_enrollment.py`, `native_ancestry_custody.py`, `native_binding_retirement_collection.py` and `native_collection_runtime.py` only where authority resolution currently assumes administrator custody.

**Proposed interface:** `prepare(db, intent_id) -> OriginCustodyRequest`; `verify(request, kube_readers, backend) -> VerifiedOriginCustody`; `apply(db, verified) -> OriginCustodyReceipt`; `retained(db, project) -> validated receipt or None`. Request pins the immutable root receipt and current enrollment predecessor; receipt pins exact namespace objects, bindings, ancestry and existing backend/domain identities.

- [ ] Add `tests/test_native_origin_custody.py`: absent domain or auto-admission authorization holds; wrong target/pool/cache/predecessor fails; generic support fixtures pass only existing exact shapes; foreign ownerless objects, journal remaps, missing old bindings and surviving private intermediates fail.
- [ ] Extract reusable read-only verification from `native_online_enrollment._verify_namespace`, `_bindings` and final identity brackets. Do not construct `PreparedCustody` with a synthetic administrator. Reuse `helpers/native_custody.py` support validators and exact identity evidence; any new generic support shape requires its own producer-matched fixture.
- [ ] Use `prepare_native_enrollment`, `verify_native_enrollment`, `validate_domain_extension` and `enroll_native_roots` with an existing independently approved domain. Add a protected domain policy permitting origin-bound root additions; leave it disabled absent explicit configuration. Never infer approval from a matching provider or fabricate cache authorization/root-transition review digests.
- [ ] Publish under target/domain serialization followed by canonical actor/project locks, as existing enrollment does. Verify the exact predecessor after locking. Preserve old roots and all retained storage liabilities. Reuse cutoff-watermark invalidation so old in-flight reports cannot restore obsolete authority.
- [ ] Extend ancestry and retirement authority resolution with a typed origin source, rather than putting origin rows in `NativeCustodyReceipt`. Collector/importer must resolve that source from protected DB records on every scan/read. Shared cache identities remain independently trusted and never project-retired.
- [ ] PostgreSQL regressions: concurrent origin publication versus enrollment extension, two checkpoint workers, cleanup versus publication, owner/provider changes while waiting for locks, and delayed old report import.

## Task 4: Adapt origin custody into the existing cleanup workflow

**Files:** `app/services/native_online_enrollment.py` (extract shared cleanup-anchor construction), new `app/services/native_cleanup_authority.py`, `app/services/quota_cleanup.py`, and `app/services/native_managed_power.py` only as required by the new authority discriminator.

**Proposed interface:** `resolve(db, project) -> CleanupAuthority`, with explicit source kind/ID/digest, exact root/domain/provider/host, namespace mode, object identities and expected bindings. Both administrator custody and origin custody must validate independently before returning this common value.

- [ ] Add `tests/test_native_origin_cleanup.py`: no reservations plus valid origin creates only a cleanup anchor; no origin or substituted origin receipt fails; changing editable topology cannot change pinned ownership; absent/stale collection queues observation only; stop/fence/seal/settlement remain mandatory; no shared cache removal or quota admission occurs.
- [ ] Extract construction from `native_online_enrollment.cleanup_anchor`, preserving `cleanup_only`, `compute_accounting="unavailable"`, current attribution and `native_storage_identity`. Use `native-origin:<receipt-id>` operation identity and an explicit origin source field; retain legacy `native_custody_id` behavior unchanged.
- [ ] Route `quota_cleanup.prepare_native`, `_namespace_authority`, and expected-binding validation through the typed resolver. Keep `_measurement_domain`, `_measurement_binding`, complete graph checks, `request_native_stop`, `seal_native_inventory`, and `_settle_native` intact. `native_managed_power.retained_context` already carries `cleanup_custody` for cleanup-only rows; reuse that operator contract.
- [ ] Support failed projects only after an exact root-bound custody checkpoint, queue/provider-work fencing and absence of active producer operations. Do not merely broaden the current legacy `active/stopped` state check.
- [ ] Treat namespace-only and truly storage-free failure as a separate explicit branch: current `prepare_native` requires nonempty `owned` and `bindings`, and retained power context requires a real root. Do not invent a root or empty disk report. Add a namespace-only intent with exact birth UID, complete allowed-object inventory and no-storage proof, then reuse namespace sealing/teardown verification. External namespaces remain ineligible for namespace deletion.

## Task 5: Release validation and documentation

- [ ] Run focused origin modules with `./venv/bin/python3 -m pytest tests/test_native_deployment_origin.py tests/test_native_origin_producer_fences.py tests/test_native_origin_custody.py tests/test_native_origin_cleanup.py -q`, then existing generic provider/deploy, custody, collector and cleanup suites. Repeat lock-sensitive tests on PostgreSQL.
- [ ] Run isolated-cluster E2E with quotas OFF: generic mixed topology, successful deploy/delete; failed clone retry; crash/lost POST; failure before root creation; injected foreign object; retained snapshot; quota toggle mid-attempt. Preserve exact before/after PVC/PV/CSI/backend/private-image evidence and shared-cache survival. Missing namespace alone is not success.
- [ ] Document independent capture and explicit domain-admission settings, historical project holds and partial-failure limitations. Update current-month `CHANGELOG.md` only for implemented behavior; do not claim deployment.

## Task 6: New-origin capture and file-pull snapshot custody

This is Task 2b, split after the producer-path audit showed that CSI snapshots
can allocate separate backing images and existing custody only supports cache
clone ancestry. Complete this before Task 4 and final validation. The reviewed
integration contract is `.superpowers/sdd/2026-11-17-native-deployment-origin/task-2-snapshot-checkpoint-review.md`.

- [ ] Retain a producer intent before snapshot POST; add exact root ownership and operation/object-role metadata to new origin-mode snapshot, restored/scratch PVC, Job and Pod creation. Retain returned UIDs; 409/restart recovery must match original retained identities and specification. Never adopt historical name matches. Snapshot readiness timeout must not be treated as success.
- [ ] Add typed pending snapshot debt and verified snapshot authority, distinct from existing PVC bindings. Independently verify original source PVC/PV/CSI identity, dynamic VolumeSnapshot/VolumeSnapshotContent UID chain, driver/class/pool/profile, and backend immutable backing image/snapshot identities. Preserve the original source and its ancestry.
- [ ] Implement a narrow read-only CSI snapshot journal resolver for an explicit pinned/tested layout/profile. Unsupported drivers, versions, layouts and ambiguous responses hold. Use bounded allowlisted reads, not arbitrary OMAP access or mutating repair. Do not claim deployment compatibility from upstream source alone.
- [ ] Extend origin scoped ownership, snapshot-restore ancestry, collector seeding, graph validation and report-import protected source resolution for exact retained snapshot authority. Cover supported linked and flattened layouts without coercing project sources into CacheBinding or snapshot images into VolumeBinding. Preserve legacy serialization and cache/private/sibling boundaries.
- [ ] Integrate Task 2 requests/acknowledgments for snapshot/helper cleanup. Stop exact Jobs/Pods, retain restored/scratch bindings, conditionally delete original resources through normal controllers, and preserve CSI finalizers. Independently prove original snapshot/content and retained private backend identities absent before retiring snapshot debt. Never select the original source or unrelated siblings for deletion.
- [ ] Test successful new-origin capture/file pull and resolved snapshot-only failures; restart/lost responses; wrong/replaced UIDs or handles; unsupported/Retain/static snapshots; journal remaps; linked/flattened ancestry; source/sibling survival; pending requests; and outstanding debt preventing full cleanup. Retain unresolved requests when no terminal provisioning fence exists—empty scans are not terminal proof.
- [ ] Keep non-origin generic behavior unchanged. No live writes, configuration enablement, commits, push or deployment in this implementation stage. Record tested profile limitations and run focused backend/operator/transport tests plus relevant PostgreSQL races when new publication state warrants them.

## Decisions required before runtime work

1. **Automatic domain admission:** recommend explicit per-domain origin admission, default off. This is necessary to avoid a human attestation per new deployment; it must not authorize adding pools/caches or changing existing roots.
2. **Capture rollout:** recommend capture enabled for new generic attempts after end-to-end validation, with persisted mode authoritative across toggles. Existing deployments remain on reviewed legacy custody.
3. **Producer checkpoint outage:** recommend hold destructive clone retry/replacement until the old liability is recorded. This changes failure/retry behavior and is required for safe accounting of vanished storage.
4. **Release claim:** capture plus successful-deployment cleanup is a bounded first milestone, but full new-project deletion also requires namespace-only and partial-import paths. Current architecture can support this without a ledger rewrite; it is more than a one-file fix and remains a release blocker for an unconditional quota-OFF deletion claim.

No runtime code, production state or quota setting was changed while preparing this plan.
