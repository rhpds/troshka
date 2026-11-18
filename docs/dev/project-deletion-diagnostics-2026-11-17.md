# Historical project-deletion diagnostics

Archived intact before Task5a consolidation on November 17, 2026. The checks and
counts below are chronological historical evidence, not final candidate proof.
See [current release gates](project-deletion-release-gate.md).

# Project deletion release gate

Status on November 17, 2026: full deployment is on hold. The shared fixes in the
working tree do not resolve every known production deletion blocker.

The current implementation is being consolidated around the user-approved
sequence: fence project activity and retain storage identities, delete VMs and
verify VMIs are gone, delete the namespace normally, then independently verify
storage retirement and shared-resource survival. This is not yet validated
end to end. The seven reported cases below remain release checks; successful
new-project tests alone do not close those reports. Diagnostic results below
describe their audit-time state and must be refreshed before consequential action.

Read-only application refresh at **2026-11-17 22:26 UTC**: all seven project
records still exist, each reports `stopped`, each has a custody receipt, and none
currently has a stored deployment error. This query used a read-only database
transaction against the running production application. Receipt presence does
not establish cleanup authority, and `stopped` does not establish VM/VMI absence.
No deletion was attempted; the ownership and storage checks below remain open.

The working tree now includes reviewed append-only custody successors and the
explicit-target prepare/review/apply CLI. Scoped deletion-triggered observation
refresh can run while automatic collection remains disabled, without running
trash cleanup or allocation reconciliation. These changes are not deployed.

The retained backend logs identify seven projects with rejected DELETE requests.
Older failures may be outside log retention. The following checks used candidate
code in an isolated process, a read-only database transaction, and GET-only
cluster requests. They did not change or delete production resources.

| Project | Candidate live ownership check | Remaining blockers |
| --- | --- | --- |
| mytest | Full online diagnostic passes in original five-root scope | Fresh storage evidence |
| httpserver | Full online diagnostic passes in original five-root scope | Fresh storage evidence |
| surendra | Full online diagnostic passes without RHBS | Accounting-only receipt; explicit cleanup authority; fresh storage evidence |
| bastion | Full online diagnostic passes without RHBS | Accounting-only receipt; explicit cleanup authority; fresh storage evidence |
| Innovation Hub | Full online diagnostic passes without RHBS | Reviewed ownership reconciliation; fresh storage evidence |
| ocp multinode | Full online diagnostic passes with three retirement inputs | Protected original disk retirement receipts; reviewed ownership reconciliation; fresh storage evidence |
| Bastion Builder | Full online diagnostic passes without RHBS | Root generation changed from 1 to 2; added resources; accounting-only receipt; reviewed authority transition; fresh storage evidence |

All original project CR UIDs still match. Matching the root UID does not prove
ownership of added resources or settlement of replaced disks. At audit time,
all seven accepted storage observations were stale (completed November 16,
22:26–22:35 UTC).

The three fresh resource-shape checks above use current generation and resources
as diagnostic inputs. They do not grant cleanup authority or prove storage
settlement. Candidate handling now recognizes the producer's placeholder external
IP service and validates configured route/service forwards against retained gateway
topology when the gateway Deployment is absent. A present gateway must still match.

A separate read-only immutable-identity audit found three original ocp multinode
bindings absent from PVC/PV/CSI inventory and from complete Ceph active/trash
inventory. Both recorded private intermediates were also absent. The other twelve
original bindings remained present. Both obsolete clone source snapshots were
also absent. This diagnostic result is not a protected
retirement receipt and does not authorize removing any stored liability.

The candidate's complete per-binding absence verifier subsequently passed against
those three original bindings and five private image identities in 44.45 seconds,
using existing collector permissions. It checked both complete namespace/PV/
snapshot inventories, both backend inventories, obsolete source snapshots, and
the unchanged root. Candidate code ran only in memory in an isolated read-only
process. No retirement requests, receipts, or successor records were persisted.
Full online verification and end-to-end cleanup remain separate release gates.

The retirement path must also re-resolve current CSI journal/image mappings
after absence verification. Kubernetes descriptor equality alone cannot detect
a backend journal remap. Review rejected an ordering shortcut that omitted that
final resolution; the full check was restored and a journal-only mutation test
added. A fixed sequential remote CSI batch did not establish a viable shortcut:
the complete 52-binding sample hit its 120-second bound. The candidate workflow
is instead separating protected retirement-receipt admission from later custody
metadata reconciliation. This requires invalidating old observation cursors and
freshly proving retirement absence in collection before accounting or cleanup.
That change has passed focused SQLite/PostgreSQL validation and independent
review; live application and end-to-end cleanup remain unverified. The standalone
retirement admission and scan limits, final online-verification freshness, and
cleanup observation guards remain required; no timestamps are relabeled.

Full online candidate verification subsequently stopped at `Custody source has
unverified edges` for the four-project scope containing Bastion Builder,
Innovation Hub, bastion, and surendra. The original three-project ocp multinode
receipt also failed that check; a later receipt includes mytest and httpserver
as well, so its five-project scope must be used for the next diagnostic run.
These failures are not permission to ignore shared-source edges. Exact excess
snapshots/children are being traced before any recovery authority is applied.

The two excess intermediate images in the Bastion Builder domain resolve through
their child CSI volumes to RHBS (`e629c16e-d3fe-429e-9cea-f1bf56e9bba6`), an active
project outside the four recorded custody roots. Neither image is proven orphaned.
RHBS remains untouched. The initial combined enrollment approach was removed:
unrelated consumers of an independently verified shared cache should not need
enrollment to unblock another project's deletion. Candidate verification is being
narrowed to exact children of each selected source snapshot, preserving strict
private-disk edges and explicit absence of retired snapshots. Unknown children
of a selected snapshot must still block cleanup. Historical observations without
the required snapshot-specific evidence must be refreshed before cleanup.

The complete read-only online verifier passed for those four projects plus RHBS
as accounting-only support: five roots and 27 bindings in 350.88 seconds. This
does not persist authority or establish completed project deletion. RHBS was
created during this audit, exposing a recurring new-deployment provenance gap
while quota enforcement is disabled; the durable prevention path needs review.

After the snapshot-specific verification change, complete online verification
passed for the original four projects alone: 24 bindings in 300.76 seconds.
RHBS was excluded from the candidate scope and was not changed. The run verified
current resource/CSI/ancestry identities with the final identity bracket; it did
not apply custody reconciliation or execute cleanup. Independent code review
found no actionable issue in the selected-snapshot boundary.

The five-root ocp domain diagnostic also found two excess source edges. Both map
to the separate project `ocp_multinode` (`8c62d6bc-1550-47b4-bff9-e7968b4da286`),
created earlier on November 17. A six-root read-only diagnostic includes it as
accounting-only support while retaining the three original replaced-disk
retirement inputs for `ocp multinode`.

That earlier six-root diagnostic passed full online verification: 70 bindings
and three retirements in 1,367.27 seconds. Retirement evidence was then 338.96
seconds old, so this did not satisfy the original successor-apply freshness
check. The revised five-root diagnostic excluded the unrelated underscore-name
project and passed: 55 bindings, three retirements, and both final CSI and root
identity checks in 1,114.68 seconds. Retirement evidence retained its original
timestamp and was 248.47 seconds old at completion. The candidate separation
permits later metadata reconciliation of already admitted retirement receipts,
but a subsequent complete storage scan remains required before cleanup. This
diagnostic did not create those protected receipts, reconcile stored ownership,
or execute deletion. Neither run grants or applies cleanup authority.

Frozen local validation after the recovery changes: 760 backend tests passed
(two skipped), including SQLite/PostgreSQL paths and both migration roundtrips;
1,985 operator tests passed. All changed Python files pass Ruff, and the diff has
no whitespace errors. These results do not claim production deployment or completed
deletion of any reported project.

A later integration run passed 789 tests, skipped two, and exposed one fixture
timestamp failure in the PostgreSQL variant. Further source-snapshot isolation
and retirement-ordering changes require a new frozen integration run; these
earlier totals are not final candidate validation.

Subsequent integration passed 847 tests and skipped two, with seven failures
traced to fixture timestamps crossing the PostgreSQL server clock. The fixtures
now explicitly flush state changes before restoring their fixed test timestamp;
production future-timestamp rejection remains unchanged. The affected enrollment,
cleanup, and shared-source suites then passed 398 tests with two skipped.
The retirement separation, retirement collection, restored CSI recheck, and
shared-source suites passed 157 tests before the final cursor-race changes.

Final cursor validation passed 309 tests with two skipped across reconciliation,
cutoff ordering, and cleanup suites using the SQLite/PostgreSQL harness. Another
139 tests passed for storage reports, collection jobs/runtime, and shared-source
boundaries. Reconciliation preserves a cutoff so an older in-flight report cannot
replace the refresh requirement. Combined enrollment transitions retain
preexisting conflict flags and ordering watermarks, reload cached ORM rows after
locking, and align sibling cursors only with their exact current manifest
identity. Independent review found no remaining actionable issue in these
changes. These results do not replace disposable end-to-end cleanup tests.

## Required before release

- Validate single, bulk, and detail-page rejection handling, including preservation
  of the canvas and selections after rejected deletion.
- Validate cleanup after canvas edits and repeated helper rollout restarts,
  while rejecting owner/provider changes and non-restart configuration changes.
- Complete and review the recovery workflow for changed custody and promotion
  from accounting-only authority. Existing `preserve-existing` enrollment does
  not perform either operation. Preserve original receipts and storage liabilities,
  pin reviewed predecessor/successor evidence, and reject concurrent transitions
  or retargeting of an issued cleanup intent.
- Verify changed/missing support resources and old disk settlement independently;
  a replacement volume is not evidence that its predecessor was removed.
- Obtain fresh complete storage observations and recheck every affected project's
  complete deletion preflight against the release candidate.
- Verify unrelated shared-cache consumers do not block cleanup while extra
  children of a project's selected source snapshot still do.
- Resolve and test deletion provenance for new native deployments when quota
  enforcement is disabled; existing-project recovery alone does not cover them.
- Exercise cleanup through final storage settlement on representative disposable
  environments, including partial teardown and changed resource scenarios.

Passing unit tests, a successful ownership check, or an accepted DELETE response
alone is not end-to-end deletion verification. Verification of reported projects
does not itself authorize deleting their remaining resources.
