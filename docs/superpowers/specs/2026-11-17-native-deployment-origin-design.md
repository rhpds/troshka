# Native deployment origin independent of quota enforcement

Date: 2026-11-17. Status: implementation authorized by the user; local work and review in progress. No push or production change in this stage.

## Decision and release boundary

### Superseding namespace cleanup contract

The user's November 17 clarification supersedes the detailed drain sequence below wherever it conflicts: Troshka manages dedicated project namespaces, together with normal Kubernetes/CSI controllers. External administrator manipulation is not the operating assumption. Retain original namespace authority, fence new project activity, and persist known storage identities and unresolved liabilities. Then delete KubeVirt VMs normally and verify both VM and VMI sets are empty before requesting normal namespace deletion. Preserve controller finalizers and report concrete blockers.

Use namespace deletion for remaining ordinary namespaced resources. Do not require a per-helper/PVC/snapshot drain, managed Stop/off proof, an ops-Pod evidence finalizer, or a per-container exec nonce as prerequisites. Troshka's own finalizers must recognize this cleanup authority even if the project root disappears first. Keep concrete external obligations such as DNS and SCC membership where applicable.

Concrete RBAC ordering exception: after VM/VMI absence, normally delete Troshka's own project/VM/network/Ceph custom resources and wait for their finalizers while their existing namespaced RoleBinding remains. Use orphan propagation to leave ordinary child resources for namespace teardown. Operator cluster permissions allow observing these CRs but their mutation/finalizer rights come from the RoleBinding; deleting the namespace first could remove those rights too early. This narrow prerequisite replaces neither namespace GC nor storage settlement, and requires no broader permission or protective RBAC finalizer.

Namespace disappearance and storage settlement are separate stages. Verify exact private image, snapshot, PV/VSC and backend trash retirement using retained identities, while preserving shared/cache resources. Unknown allocations remain visible pending liabilities; neither namespace absence nor a failed read means zero usage. Ordinary snapshot-only removal keeps its source-survival checks; intentional whole-project deletion requires a distinct cleanup-bound verification path. Block redeployment and completion until applicable obligations settle. No historical origin fabrication or shared/external namespace deletion is authorized.

Validation must include the originally reported existing projects as well as disposable success and failure cases. Tests of new-origin deployments alone do not establish that legacy reported failures are resolved. Namespace inventory is useful for storage coverage and blocker diagnosis; full content allowlisting is not itself required to establish deletion authority over an already verified dedicated managed namespace. Reassess the earlier permission/inventory implementation against actual needs before release.

Record origin at the generic native provider's actual creation boundary, independently of whether quotas are enabled. Keep origin separate from quota reservations and from administrator-attested legacy custody. Use verified origin, independently enrolled storage identities, and fresh complete storage observations together when preparing cleanup.

Capturing origin is a bounded repair. Making every newly created generic deployment immediately deletable is a larger lifecycle integration: origin capture alone cannot prove disk ownership, private clone ancestry, or backend absence. A release claiming to fix deletion for new quota-off deployments must include the bridge and failure-path tests below. Shipping capture alone must explicitly retain cleanup holds where storage evidence is unavailable. This is not a reason to disable the existing safety checks or to block unrelated completed recovery fixes.

The existing generic deployment features remain available. Do not route quota-off deployment through the narrower initialized managed-deployment payload/profile merely to obtain its provenance. Disk imports, multiple networks, containers, BMC, EIPs, Showroom, S3 credentials, and backend-generated topology fields continue through the current generic builders and finalization routines. Unsupported cleanup evidence remains an explicit hold, never an implicit unsupported deployment or broad namespace-shape exemption.

## Current code and missing evidence

- `app/services/providers/kubevirt.py::KubeVirtDriver.deploy_project` dispatches on `quotas_enabled()`. The generic branch creates namespace grants and S3 Secrets, builds the CR, and discards the CR POST response.
- `_ensure_project_namespace` treats `AlreadyExists` as success and returns no original UID or creation result. Labels and deterministic names are not creation authority.
- `app/services/deploy_service.py::_kubevirt_create_or_resume_cr` resumes an existing Deploying CR or replaces a stale CR by name. Neither path establishes fresh origin.
- `_finalize_kubevirt_deploy` adds routes/EIPs and records the resulting topology. Reaching Running is not independent ownership evidence, and resources may already exist when deployment fails.
- Operator producer callbacks run from the CR. `helpers/managed_power.py::producer` and `checked_await` implement managed fences; the generic path must not pretend that it used managed initialization or admission. Operator status is not a trusted substitute for a backend-retained creation intent.
- `native_managed_deploy.create_pending`, `establish_namespace`, and `bind` already demonstrate the necessary write ordering: durable intent before POST, namespace UID retained before grants/CR, then exact cluster/root readback. Reuse the principles and small pure validators, not their reservation-dependent execution contract.
- `quota_cleanup.prepare_native` requires namespace authority plus retained storage identities, current collection evidence, and settlement. `native_online_enrollment.cleanup_anchor` currently bridges administrator custody into a cleanup-only ledger row; it does not provide a birth-provenance API.

Paths above are relative to `src/backend/`, except operator helpers under `src/operator/`.

## Minimal protected records

Add a dedicated `native_deployment_origin` model/schema/service. Do not overload `NativeCustodyReceipt`, whose meaning is explicit legacy attestation.

1. **Intent:** UUID primary key; project ID; deployment attempt UUID; immutable canonical payload and digest. Payload includes owner/organization/host/provider identities, independently read cluster UID, namespace/name, managed versus external namespace mode, a random operation token, canonical intended CR spec digest, and builder/schema version. Store secret-bearing launch material only through existing protected credential handling; origin records need digests and references, not plaintext keys or presigned URLs.
2. **Creation receipts:** immutable typed namespace and root receipts linked uniquely to the intent. Bind namespace UID, root UID/generation and exact desired/readback digests in the relevant receipt. A unique active project-attempt pointer tracks cancellation and nonce-owned remote I/O. Original identity facts are never overwritten; a generic event-chain engine is unnecessary.
3. **Custody checkpoint:** append-only reference to origin receipt, actual current root generation, verified namespace object/storage inventory, backend and enrollment identities, and verification digest. This is machine-derived origin custody, with a distinct authority discriminator; it is not `CustodyAttestation(administrator=...)`.

These records survive project deletion, matching the retained evidence models' lack of cascading deletion. Confirm UUID database types and migration constraints with PostgreSQL tests. A mutable cursor may track recovery progress, but cannot replace the immutable facts.

Suggested service entrypoints: `prepare_origin(db, project_id, provider_id, launch_descriptor)`, `bind_namespace(db, intent_id, observation)`, `bind_root(db, intent_id, observation)`, `recover_origin(intent, api)`, `prepare_origin_custody(db, intent_id)`, `verify_origin_custody(prepared, readers)`, and `apply_origin_custody(db, verified)`. These are proposed interfaces, not existing APIs.

## Creation and crash recovery

Freeze the final generic CR descriptor after topology preparation and before namespace/provider side effects. `_build_troshka_project_cr` and S3 configuration preparation currently straddle side effects: separate descriptor construction from Secret creation rather than digesting a different preliminary topology. Record actual post-builder spec and intended support-object identities without retaining secret values in an operator-facing origin report.

| Boundary | Durable state and recovery rule |
| --- | --- |
| Before any POST | Commit intent and fresh random token. Verify selected cluster and absence of the intended namespace/root. A missing receipt for an old deployment is never backfilled as origin. |
| Namespace POST | Put intent token and project/attempt identity in namespace annotations. Managed ownership requires a new namespace. External namespaces are recorded as external, without namespace-delete authority. |
| Namespace response received | Validate response UID against fresh GET, token, cluster and nonterminating state; append namespace-bound event before grants, Secrets or CR creation. |
| Lost namespace response | Read the exact intended namespace. Recover only when exact durable intent token/project/attempt and cluster match; otherwise hold. `AlreadyExists` alone cannot establish ownership. If absent, retry the same intent according to bounded recovery, never issue a new token to adopt an existing object. |
| CR POST | Stamp the same intent/attempt reference on the generic CR. Require the retained namespace UID before POST. Persist the complete expected spec digest before submission. |
| Lost CR response | Recover only exact token, namespace UID, CR name, project ID and expected spec in the pinned cluster. Bind returned UID/generation once; a different UID after binding is a replacement and requires a new explicitly authorized lifecycle, not a retry. |
| Producers ran before binding commit | Root readback may still recover exact original token/spec, but child presence is not proof of ownership. Capture children through normal namespace custody and storage verification. Do not require the generic operator to have obeyed the managed pending profile. |
| Failure before CR exists | Namespace-bound evidence covers the original namespace and known support writes. Cleanup still seals/rechecks namespace contents; an injected foreign object blocks it. No fictitious root UID or storage zero is manufactured. |
| Worker cancellation/restart | Resume only the original intent under job/attempt checks. Preserve events after failure. Cancellation and cleanup fence further writes; a delayed POST response cannot advance a cancelled/replaced attempt. |

Remove name-only resume/replacement behavior for attempts participating in origin capture: resume exact bound UID/token; require ordinary guarded cleanup before a fresh attempt replaces an old root. Existing historical deployments continue to require the explicit custody recovery workflow.

## Locks and write authority

Use the existing ordering: reporting-target/domain serialization when publishing enrollment, operation/attempt lock, sorted user locks, sorted organization locks, then sorted project locks. Re-read scalar identities after locks; never acquire an earlier tier after a later tier. The exact transaction boundaries must match `quota_locks.lock_rows` and enrollment's `_validate_manifest` ordering rather than nesting them in reverse order.

Persist intent/state transitions in short transactions before network calls. Provider calls carry the immutable intent ID; completion transactions reacquire the same ordered locks and compare active attempt, previous event digest, owner/provider identity and cancellation fence. A network timeout does not release identity for another attempt. Read-only recovery does not mutate job tracking or accept unavailable tracking as idle. Use canonical project locks to serialize cleanup with provenance publication and later support writes.

## Bridge to existing domains and cleanup

Origin proves who created the root; it does not independently select a Ceph pool or prove current storage exclusivity.

1. Require an existing, independently configured reporting target/domain for the selected provider/cluster and backend pools. Never derive trusted collector credentials, pool IDs, cache roots or CSI identity mappings solely from the new project's objects.
2. Extend that domain using its exact predecessor manifest and existing extension invariants. Automatic addition requires a separately configured, explicit permission to admit origin-bound roots to that domain. Without that permission, retain origin and require the existing reviewed enrollment workflow. Preserve existing roots/cache/provider constraints and collector cursor semantics.
3. Verify generic namespace custody against actual operator/backend producer behavior, exact root identity, and frozen support manifests. Root ownerReferences are useful evidence, not blanket authorization for every object in a namespace. Ownerless backend-created objects require exact recorded creation or existing narrow custody validators.
4. Verify PVC/PV claim references, CSI journal identities, backend image IDs, complete graph edges and any private intermediate/source snapshots through existing readers. Preserve all observed liabilities across checkpoints. A disk absent from the latest topology is not thereby retired; use independent per-binding absence evidence where required.
5. Publish a distinct origin-custody receipt only after those checks and fresh final identity brackets. Generalize the narrow cleanup authority resolver to accept either validated administrator custody or validated origin custody. Reuse the existing cleanup-only anchor, stop/fence, sealed deletion inventory and backend settlement pipeline. Do not create quota admission, allocation, historical CPU usage, or fake zero storage.

Capture a checkpoint after deployment finalization and before supported storage-removal/replacement operations; retain failed/partial deployment evidence as well. A single post-Running snapshot cannot account for storage created and removed during a failed attempt. Where the generic producers create transient storage not covered by existing CSI/ancestry verification, that producer needs an explicit provenance hook or cleanup must hold. This dependency is part of the release boundary, not an acceptable silent gap.

## Feature flags and quota toggles

Origin capture is an independent rollout setting, not an alias for quota enforcement. Existing quota-managed attempts retain their original managed provenance. New generic attempts admitted with capture enabled must complete capture or fail closed; disabling capture afterward cannot erase their records or switch their recovery to name-only behavior. A quota toggle mid-attempt must not change provider dispatch mode: freeze lifecycle mode in the attempt and validate it at each entrypoint. Domain auto-admission is a separate explicit opt-in; cleanup never treats either switch as evidence.

## Verification and E2E plan

- Unit tests assert transaction-before-provider-call ordering, exact tokens and digests, duplicate events, mismatched cluster/provider/namespace/root UID, external namespace denial, CR spec mismatch, and no secret material in origin serialization.
- Fault-injection tests interrupt before/after every POST and DB commit; exercise lost responses, AlreadyExists, timeout followed by late success, cancellation, retry and process restart. Recover exact original objects or retain a hold.
- PostgreSQL tests cover concurrent deploy/delete, two recovery workers, membership/provider changes while waiting on locks, immutable event retention, and enrollment publication races. SQLite tests do not establish lock correctness.
- Toggle tests change quota enforcement and capture flags between intent, namespace bind, CR bind, finalization and cleanup. No phase changes authority or creates retrospective reservation history.
- Bridge tests reject absent/unapproved domains, altered predecessors, stale/incomplete collections, foreign ownerless objects, CSI mismatches, reappearing disks, surviving private intermediates and shared cache edges. Retain all previous liabilities.
- Generic topology regression fixtures cover VM disks/CDI imports, multiple networks, containers, BMC, EIPs/routes, Showroom and S3 Secrets; no conversion to the restricted managed initialization topology.
- An isolated-cluster E2E with quotas OFF creates a fresh supported generic deployment, records immutable origin, verifies storage, stops and deletes via existing guarded cleanup, and proves original PVC/PV/CSI/backend/private storage absence. Repeat with early import failure, namespace-only failure, worker crash, external namespace, injected foreign object and retained snapshot. Shared cache data must remain present. Require before/after evidence, not merely an HTTP success or missing project row.

No historical ownership is inferred, no automatic legacy attestation is issued, and this document authorizes no production changes.
