# Project deletion release gate

Scope clarification: the user directed this release back to project deletion.
On November 18 the user authorized installing the candidate and permanently
deleting projects with definitive prior failed deletion requests. The retained
incident evidence identifies mytest, httpserver, surendra, bastion, Innovation Hub,
ocp multinode and Bastion Builder. A current production read confirmed all seven
still exist and report stopped; the candidate successor schema is not installed.
Unrelated projects remain outside deletion scope. Acceptance is normal deletion
with VM/VMI, namespace and private-storage removal and shared-storage survival;
broader workload permutations and capture qualification are deferred. Existing
runtime ownership checks remain enforced. This authorization is not evidence
that any of the seven has been deleted or that production has been upgraded.
Source 8507ceca completed the normal backend-test fallback with 10,002 passed
and 1,152 skipped; Sonar analysis did not run because SONAR_TOKEN was absent.
The first release source 55d45044 also passed the normal pre-push tests (10,002
passed, 1,152 skipped) and was pushed as v2026.11.18-project-deletion. Backend CI
then rejected 37 optional-value type errors with Pyright 1.1.414; local checks
used 1.1.408. The rollout orchestrator was stopped before deployment. The r2
correction adds type-only narrowing after existing guards and an equivalent
explicit pod null check; it does not relax runtime cleanup requirements.
Successful capture/export/restore qualification is deferred and is not a release
gate. Completed capture fixes remain documented; failed test attempts remain
preserved. Deletion's own ownership, VM/VMI, namespace, private-storage and
shared-survival checks remain required. Assess any demonstrated capture-related
deletion dependency explicitly rather than expanding capture work automatically.

Status on November 17, 2026: **release held**. The candidate source supports
bounded cleanup paths; source review and local tests do not establish successful
deployment or deletion of the seven original projects. Task5a covers local
validation. The consolidated runtime fix passed independent re-review. Task5b
disposable end-to-end qualification remains required before a full release with
`--fresh-accounting`.

November 18 accounting recovery: the user approved bounded accounting recovery
for the two existing failed captures, preserving their data and failed records
and running no new captures. Source `53311803` passed the normal pre-push test
fallback: **10,000 passed, 1,150 skipped**; Sonar analysis was skipped because its
token was absent. The correction shares the configured scan deadline with
snapshot metadata reads and ignores only controller retry bookkeeping during
final root comparison. Writer state, outcomes and resource identities remain
protected. Both read-only snapshot verifications passed. Both accounting receipts
were published and independently confirmed, preserving both failed Pattern
records. The second required fresh verification after an execution changed state;
the safety check held before publication, and the bounded retry passed after
both executions reported complete. Subsequent fresh dev accounting **failed**:
both publications contain proposed enrollment pins that exclude the settled,
deleted mixed fixture, but snapshot reconciliation did not publish that enrollment
transition. Exact protected-anchor validation correctly rejects those pins.
The snapshot proofs and failed Pattern records remain preserved; publication
alone is not successful recovery. No anchor guard was weakened. The user approved
the bounded correction and existing verified enrollment workflow. The successor
`2bf74c06-274d-45d1-b5c9-92737bcf8af7` was independently verified and published;
both unchanged receipts passed retained-authority checks before commit. Fresh
normal-worker accounting failed again: both project reports were accepted but
had partial coverage, including a timeout measuring each retained snapshot image.
The shared scan also failed validation. A separate read-only diagnostic returned
unsupported or unavailable shared-image measurements and could not confirm one
source's expected snapshot and child sets. Incomplete reads do not establish
missing storage or authorize graph repair. Enrollment authority now resolves;
fresh complete accounting remains unverified. No storage mutations, limit changes,
new captures or original-project deletes were performed. Further accounting
diagnosis/remediation awaits scope review. The forward fix preserves current
enrolled pins and prepares only the snapshot project's identity, including when
an unrelated historical project has been deleted. Production remains held until
fresh accounting passes. These source fixes have not yet been installed in
running services.

Earlier November 18 qualification: committed source `b904f060` passed all normal
commit hooks and the normal pre-push test fallback: **9,980 passed, 1,149 skipped**.
Sonar analysis did not run because its token was absent. Candidate v17 is installed
in dev at backend digest
`2f41de4260968c601805311bfb1fe2cbc6200249684eabdc6663435926c6d63a`.
All five deployment rollouts passed; all six backend pods verified 306 application
Python files, the exact image digest, protected files and effective 900/870/15
budgets. Operator, frontend, configuration and paused Argo settings were preserved.

The final mixed fixture `7a97003e-05be-42bf-bcd7-9d019c7798ff` passed the expected
pre-resource failure, normal Undeploy recovery to draft, and successful retry with
a ready VM/VMI, network helper and container. One normal Delete finalized cleanup
`3ec8b287-5483-4632-a8a2-07fabdde8ae7`. Independent observation proved VM/VMI absence
while the namespace was still Active and not terminating; namespace, PV and VSC
absence followed. Both private images are absent from active storage and trash,
and the shared cache survives. Accepted report
`4b72b6b8-23d0-4854-b65b-48f7f8670c3e` has complete coverage and zero allocation.
The authenticated API returns 404. The temporary test key was revoked with HTTP
204 and its local secret removed. Readiness evidence covers controllers and Pods,
not guest application connectivity.

The preceding disposable Delete also finalized, on a later v16 scan before the
v17 rollout, using report `6053af32-de79-44bf-a6d4-de90505d150b`; do not attribute
that success to v17. All seven original reported projects were refreshed through
their enrolled provider transports and retain their namespace and VM/root UIDs.
No original project DELETE, production rollout, release tag or push was performed.
Full release still requires resolution of the separately reported failed-capture
accounting gate. The user subsequently approved the bounded recovery above.

Earlier November 18 checkpoint: candidate v16 was installed in dev at backend digest
`18e5aaea212eea9c1879cf59d7fd84481d3b65465b90c473d29dba368019afb9`.
All five backend deployment rollouts completed, and all six backend pods passed
source, digest and protected-runtime verification. The committed source
`f30aa97229c9f708228a32f0f3fd75d0de842259` passed the normal pre-push test fallback:
9,942 passed, 1,145 skipped. No Sonar analysis ran because its token was absent.
Fresh report `60913c0c-1d3d-4ae4-8286-d0a0d8b78f8b` still leaves the disposable
Delete pending: one other-project descendant's usage measurement timed out,
preventing complete isolation proof. A separate bounded usage read timed out as
well. Both private images remain independently absent from active storage and
trash; the shared source survives. This is not completed Delete qualification.

The subsequent dependency-verification correction passed independent scoped
review, 152 focused tests, eight SQLite settlement cases and eight isolated
PostgreSQL settlement cases. It preserves unknown allocation after a usage
timeout and requires a separate fresh final dependency verification. Its subsequent
installation and live qualification are recorded above.

The full dev health gate also fails on unresolved snapshot accounting for the
two preserved failed capture tests. The subsequently approved bounded recovery
is recorded above; no accounting gate exception is permitted. Production
remains unchanged. A read-only refresh at 07:24:45 UTC confirmed all seven original
project namespaces active and not terminating, with their root and VM objects
present. At that checkpoint the final mixed recovery/delete fixture, temporary-key
revocation and full dev/production release remained pending.

Latest retained dev qualification, November 18 at 06:22:30 UTC: the v15 normal
Delete of the redeployed disposable project reached VM/VMI and namespace absence.
Independent two-round storage observations at 06:17:13 UTC prove both retained
private images absent from the active directory and trash, while the exact shared
cache survives. The approved dev-only collection budget increase was verified
in all five replacement application pods: 900-second job timeout, 870-second scan
budget and unchanged 15-second command limit. Durable teardown evidence records
the guarded namespace DELETE at 06:14:24 UTC; an observer gap prevents claiming a
complete independent watch-event chronology. Report
`d0c591af-1d85-4415-87f2-a6f8606ab523`, completed at 06:21:01 UTC, still has partial
coverage and `origin_snapshot_authority_unverified`: current snapshot/child edges
and changed ancestry on preserved other-project sources fall outside the installed
isolation proof. No dependency on this attempt's private images was demonstrated,
but the additional edges' dependency boundary remains unproved. The project remains
`cleanup_pending`, with no durable settlement or finalization. The approved narrow
current-ancestry correction is in progress and is not yet qualified. Retained
evidence is in
`/private/tmp/troshka-native-delete-acceptance-20261117/v7-deletion-only-v15/`:
`durable-teardown-evidence.json`, `storage-after-20261118T061725Z.json`, and
`fresh-accounting-blocker.md`. These observations do not complete normal Delete
qualification or any remaining release gate. The seven original projects and
production were unchanged by this dev attempt, according to the retained execution
record; this documentation update performs no fresh live verification.

Earlier dev qualification: backend v13 and operator v14b are installed and healthy, with database head
`28bc37e61a90`; production is unchanged. The LIST-response, storage-retention,
bounded post-verification observation and inventory-comparison corrections are
installed together. The comparison correction passed independent review and
143 tests (110 skips). The disposable fixture retained storage coverage and
reached verified VM/VMI absence. CDI then blocked orphaning a dependent DataVolume.
The narrow namespace sequencing correction passed independent review and 140
affected tests (131 skips), plus 21 final focused tests (21 skips). It is installed;
the original pending disposable namespace disappeared through normal Kubernetes
deletion on November 18 around 04:32 UTC. Stored completion evidence links the
exact prior Troshka finalizer to the observed orphan-only state. A fresh report
completed at 04:35:17 UTC, and the independent retirement verifier passed for
both private images and the shared ancestor. An unresolved snapshot producer
belonging to an older failed test initially prevented application finalization.
The user approved a narrow accounting correction:
only independently verified terminal retirement may exclude that debt, after
fresh exact protected source ancestry proves no private storage dependency.
Shared cache survival remains required; unknown, stale or conflicting evidence
continues to block. The correction passed independent review and 28 final focused
tests (5 PostgreSQL variants skipped). Installed v13 qualified the original
pending cleanup: report `3881bf1e-003f-4b64-ab03-63e2f93b0447`, completed
November 18 at 05:04:50 UTC, has complete coverage, zero private allocation and
no blockers or collection errors. Cleanup was confirmed finalized at 05:05:27
UTC; private absence and shared survival were verified through the supported
settlement path, with no forced record or finalizer changes. This is one passing
disposable Undeploy case, not verification of every remaining scenario or the
seven original user projects. A separate
snapshot capture reached error/zero bytes while blocked by this cleanup's missing
root; its failed attempt and still-held operator request are preserved.

The user subsequently approved a narrow capture-resume correction and continuation
of both original dev captures. Update and restart handling now resume only retained
matching origin operations; terminal phases resume recorded cleanup only. Existing
signed acknowledgement, submission and UID safeguards remain unchanged. Independent
re-review passed, with 794 affected and 44 final focused tests passing. Operator
v14b was installed after offline and runtime verification of all 131 packaged files,
permissions and non-root access. Both original operations created exactly one ready
CSI snapshot by November 18 at 05:32 UTC. Restore authorization remains held; no
restore/export or complete snapshot qualification is claimed. Both original API
pattern records remain in error and are preserved. Production remains unchanged.

Read-only reproduction at November 18 05:46 UTC used the actual configured
210-second scan and 15-second command limits. It failed after 109.95 seconds:
the snapshot observation adapter's separate 55-second Kubernetes inventory
deadline had expired while backend snapshot resolution ran, so the next producer
LIST raised `managed_observation_timeout`. This is separate from capture dispatch
resumption. Aligning that adapter's read-only deadline with the existing scan
deadline requires scope review; no such correction is included yet. The earlier
60-second private diagnostic timeout was not evidence of the actual failure;
a fresh source-only read matched the original source identity.

The user approved the bounded retention correction: keep the deletion request
asynchronous, fence producers, independently verify and retain storage identities,
then atomically adopt that evidence into the original pending request and require
a successor operator acknowledgement before destruction. Missing or incomplete
coverage must hold before VM/CR/namespace deletion. Independent review and the
original disposable cleanup qualification passed. Remaining scenarios below
still require evidence. Unrelated findings are retained
in [the follow-up list](project-deletion-followups.md).

## Current candidate behavior

Generic native attempts can retain original namespace/root receipts independently
of quota enforcement. `native_origin.capture_enabled` defaults off; the persisted
attempt mode remains authoritative across later toggles. Automatic origin
admission additionally requires explicit `origin_admission: true` on the protected
collection domain profile. Admission does not add pools/caches or authorize
existing roots. Enable capture/admission only after the disposable qualification
below. Existing deployments continue to require their reviewed original custody
or an explicitly applied administrator custody successor.

Whole-project cleanup commits cancellation and known liabilities, fences producer
dispatch and waits for the qualified operator acknowledgement. It issues normal
conditional VM deletion and verifies both VM and VMI absence. Troshka's own CRs
are deleted with Orphan propagation and their finalizers finish while the managed
namespace RoleBinding still exists. Namespace deletion requires own-CR absence
or exact persisted terminating CR UIDs with only the Kubernetes `orphan`
finalizer. The latter path also requires immutable inventory proving Troshka's
finalizer was registered on each UID, its retained DELETE request, and immutable
completion observations committed before namespace DELETE. Missing or changed
proof, unknown finalizers or replacement CRs hold cleanup. The live namespace UID,
producer acknowledgement, local writer fence, VM/VMI absence and complete storage
liabilities remain required before deleting the original UID-bound namespace.
Remaining namespace-local helpers, claims and
snapshots go through controller teardown; controller finalizers remain intact.
A missing root does not prevent own-CR finalization, and a stopped VM is not proof
of VM/VMI absence.

Namespace absence is recorded separately from storage completion. Settlement
requires independent fresh PVC/PV/snapshot-content observations and complete
backend directory/trash exclusion of every retained private source, snapshot,
restore and intermediate image, plus shared/cache and applicable sibling survival.
Ordinary temporary-snapshot retirement still requires source survival.
DNS/EIP obligations retry before finalization. Delete and Undeploy preserve
retained cleanup/evidence history; neither namespace absence nor accepted DELETE
means zero storage.

Rootful cleanup lists only required PVC/snapshot and participating Troshka CR
evidence; unrelated discovered resources do not require new legacy namespace
grants. Denied, incomplete or changed required inventory still holds. A qualified
singleton operator replacement can publish a bounded successor cleanup marker
after retained and live prior executions are complete. The original marker and
successor evidence remain immutable; overlap, unfinished history and unqualified
runtime still hold. A lost successor PATCH resumes from its retained transition.

The independent checkpoint executor's exact worker threads do not prevent the
deployment invocation from recording its exit. Untracked participating child
threads still block that record. Successful verified Secret creation creates no
remote-execution debt. Verified support Pods, exact returned exec calls and
successful file-pull results retain their original operation digest and Pod/root
and namespace identities until whole-project namespace and independent storage
settlement. Return alone never establishes remote quiescence. Cleanup rechecks
late operation results under its existing locks while preserving the original
request evidence. Ambiguous dispatch, changed admission and historical untracked
remote effects remain unresolved, including after namespace absence.

A root never submitted uses its separate support-receipt/namespace authority.
A root whose POST was submitted but whose original UID was never retained is
unknown debt, even if it is later absent. Lost POST outcomes, unknown storage,
source loss, incomplete historical custody and interrupted remote execution are
not reconstructed from familiar names or empty lists. Clone retry/replacement
holds if the prior liability cannot be retained. Bounded histories are explicit:
snapshot facts and operator process-incarnation history each cap at 1,000 entries;
capacity exhaustion holds rather than evicting evidence. Read-only inventory has
two helper slots and real caller deadlines; blocked DNS can occupy those slots.
This does not change the separate synchronous namespace DELETE transport.

## Pinned snapshot profile contract

Schema acceptance does not enable a profile or qualify a live installation.
The upstream pair retains exactly its previous serialized values:
`ceph-csi-v3.13.0-rbd-snapshot-v1` with `external-snapshotter-v8.0.1`.
The only added pair is `odf-4.20-a3db60e1-rbd-snapshot-v1` with
`openshift-snapshotter-fd82be21387d`; mixed pairs and arbitrary aliases reject.
The downstream contract is pinned to the saved development preflight:

| Component | Exact build identity |
| --- | --- |
| Ceph-CSI | release-4.20, commit `a3db60e1aa8c1dbaf081afb28c2c470f6f46774c` |
| Ceph-CSI image | `sha256:63a42d4aa5707d0cea37cf84f907628ba43a5b8efda2a1a4f4c33b6abd5ac2fa` |
| Snapshotter observed build | commit `fd82be21387d750cd8912cf8aa5127e0ade38f73` |
| Snapshotter image | `sha256:71aff0fde53ca89e14955e15bee9d88004079fe619a55d2d29382fa905086a34` |

The snapshotter label is an observed downstream build identifier; exact source
provenance remains unresolved and it is not relabeled as upstream v8.0.1.
The same narrow resolver still requires the exact full snapshot UID request,
allowlisted journal fields, same image/journal pool, default csi-snap naming,
distinct source/backing IDs and a stable numeric user-namespace snapshot ID,
with repeated journal/source reads. No alternate prefix, cross-pool layout,
Retain policy, CephFS/NFS or general ODF4.20 qualification is implied.

The saved class is `ocs-storagecluster-rbdplugin-snapclass`, driver
`openshift-storage.rbd.csi.ceph.com`, Delete policy and no name-prefix override.
Those are prior read-only observations, not a fresh fixture. Recheck the exact
digests/class/flags at disposable fixture start. Source-defined collector RBAC
adds only GET on VolumeSnapshotClass; the deployed protected kubeconfig principal
and actual access still require verification. Do not reuse a managed namespace's
LIST-only inventory grant for operator-namespace qualification.

## Original incident cases

Read-only application refresh at **2026-11-17 22:26 UTC** found all seven project
records present, reporting stopped, with a custody receipt and no stored
deployment error. The query used a read-only transaction in the running production
application. Receipt presence does not establish current cleanup authority;
stopped does not establish VM/VMI absence. No deletion was attempted.

| Project | Candidate custody path and required transition | Current readiness |
| --- | --- | --- |
| mytest | Existing five-root custody; retain exact original scope | Hold: fresh complete storage/ownership preflight and disposable qualification |
| httpserver | Existing five-root custody; retain exact original scope | Hold: fresh complete storage/ownership preflight and disposable qualification |
| surendra | Accounting-only receipt requires reviewed cleanup-authority successor | Hold: successor not applied; fresh preflight required |
| bastion | Accounting-only receipt requires reviewed cleanup-authority successor | Hold: successor not applied; fresh preflight required |
| Innovation Hub | Existing changed custody requires reviewed ownership reconciliation | Hold: successor not applied; fresh preflight required |
| ocp multinode | Three original replaced-disk retirement receipts, then reviewed custody reconciliation in five-root scope | Hold: retirement receipts/successor not applied; fresh storage collection required |
| Bastion Builder | Generation 1→2, added resources and accounting-only receipt require reviewed authority transition | Hold: transition not applied; fresh preflight required |

The candidate has append-only prepare/review/apply recovery interfaces for these
paths, preserving original receipts and exact predecessor identities. A passing
new-origin fixture cannot close these existing-custody cases. Prior online checks
and absence diagnostics did not persist authority or protected retirements.
RHBS and the separate `ocp_multinode` project remain outside the selected cleanup
scopes; unrelated shared-cache consumers must survive. The full chronological
[diagnostic record](project-deletion-diagnostics-2026-11-17.md) preserves the
audit facts, timestamps, earlier failed runs and superseded test totals.

## Final local validation

The final Task5a command record is retained in the implementation handoff report.
One full nonlive backend run recorded **9,653 passed / 964 skipped / 20 failed**,
with **14 live/tier2 cases deselected**. Its twenty failures were resolved through
explicit fixture corrections and one bounded accounting fix: unmatched discovered
snapshot evidence blocks its exact pinned project, while unknown roots and
conflicting protected facts retain global holds. Affected local reruns passed
**15 generic/session tests**, **4 timer tests**, and **39 graph/snapshot/legacy
tests** (45 unavailable-PG/SQLite-only-contract skips). The final affected
PostgreSQL graph/import/API/snapshot/namespace set passed **304 tests / 5 skips**;
the skips are SQLite variants of PostgreSQL-only locking contracts. The full
10,000-test suite was not repeated after these bounded corrections.

Other completed checks are **602 passed / 26 skipped** in the disposable PostgreSQL
origin/custody/snapshot/namespace/accounting set; **160 passed** in release/RBAC
scripts; **117 passed** across frontend deletion and both project-page suites;
and a passing frontend typecheck. The full operator run exposed thirteen fixture
assumptions about namespace reads and seven sandbox-denied loopback cases. Exact
synthetic namespace fixtures and the real startup predicate restored **766 passed**
in affected modules; the permitted loopback module passed **11 tests**. These
counts overlap the initial full runs and must not be added to them.

The PostgreSQL set executed all ten pending migration bodies from installed
predecessor ab841d2e6109 through f17ae65bd394 via Alembic Operations.context,
downgraded and upgraded again, and checked real immutable evidence triggers,
uniqueness and retention after Project deletion. The isolated predecessor uses
model-created scaffolding; this is not a copy of a live database. Source inspection
finds only new retained tables/indexes/function/triggers in this slice, with no
ALTER of preexisting tables; its only foreign keys target the new origin-intent
table. Production/development migrations were not run.

Full frontend ESLint remains failing globally (**480 errors / 32 warnings**).
Running the actual repository ESLint config on HEAD and candidate contents through
stdin found **zero new normalized diagnostics** across all six changed/new UI
paths (42 errors / 6 warnings in either version). This comparison does not claim
the entire frontend is lint-clean. No unrelated UI refactor was attempted.

Earlier totals in the diagnostic record are historical and are not
final-candidate evidence. Every pytest process receives a unique explicit SQLite
URL before application imports. Real PostgreSQL checks use only the established
exclusive disposable harness and per-test schemas. No tests target development
or production databases.

## Remaining release gates

- Installed candidate verification after the reviewed deployment-wiring fixes:
  the CRD schema now passes a development API-server dry run, and existing
  operators must receive the singleton Recreate upgrade. These checks do not
  replace live checkpoint admission or deletion verification.
- Disposable installed-controller qualification with quotas OFF: generic mixed
  topology deploy/delete, failed clone retry, ambiguous/lost POST, failure before
  root creation, unrelated namespace-local object, retained snapshot, and quota
  toggle during an attempt. API and all workers must share the candidate revision;
  record immutable image digests and configuration/profile identity separately.
- Whole-project retirement through VM/VMI absence and the own-CR Orphan prerequisite.
  Retain before/after PVC/PV/CSI/backend/private-image identities and complete
  directory/trash observations, and prove source/shared/cache/sibling survival as
  applicable. Missing namespace, CSI success and schema fixtures are insufficient.
  Unknown retained snapshot custody must remain safely pending; successful
  snapshot export/restore qualification is deferred to the separate capture work.
- Exercise interrupted/ambiguous DELETE retries and finalizers after the root
  disappears, plus actual DNS/EIP and SCC obligations. No forced controller
  finalizers, manual OMAP repair or manufactured empty evidence.
- Qualify installed namespace/Pod/NAD ordering, KubeVirt/CDI/CSI behavior and both
  advertised OpenShift RoleBinding views. Verify exact support provenance/UID
  receipts, complete discovery, namespace-scoped LIST-only inventory with exact
  bind bootstrap, and post-namespace backend read access. Slow/saturated inventory
  must remain pending.
- Recheck each original incident case against the final candidate and explicitly
  review/apply only its needed ownership/retirement transitions. Diagnostics do
  not authorize deleting remaining user data.
- Pass the normal pre-push hook and release prerequisites. With an exported
  `SONAR_TOKEN` and reachable service, the exact pushed revision must pass Sonar;
  without a token, the supported backend-test fallback applies. Neither hook
  path has been established by this source correction. Full release needs
  a clean committed checkout and `--fresh-accounting`; `scripts/deploy-full.sh`
  atomically pushes HEAD/main and a release tag, so it is not a local build command.
  Append a release-tag mapping only when actually releasing.

No builds, commits, pushes, deployments, live configuration enablement or original
project deletion occurred during Task5a. Local validation does not complete Task5.
