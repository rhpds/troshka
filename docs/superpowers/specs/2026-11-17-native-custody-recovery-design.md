# Native custody recovery

Full deployment remains blocked by the project deletion release gate.

## Evidence and authority

Keep original custody receipts immutable. Append successor receipts through a
bounded, digest-checked chain with one successor per predecessor. Each protected
reconciliation request pins its predecessors and the selected cleanup-authority
projects. A review document identifies the exact before/after evidence, authority,
administrator reason, and digest. Applying requires that digest and fresh independent
verification under canonical identity locks. Existing cleanup reservations or
allocations prohibit reconciliation; issued cleanup intents must never retarget.

Reuse online verification for live namespace ownership, supported runtime shape,
provider identity, and twice-read storage ancestry. The first recovery increment
requires unchanged root and immutable storage/ancestry identities. It permits
reviewed support-resource changes and explicit promotion from accounting-only
authority. Changed roots and replaced disks remain blocked pending their separate
transition and historical-storage verification; this increment is not release-ready
by itself.

## Storage freshness

Automatic collection is currently disabled. Deletion requires fresh measurements
before it creates a cleanup intent, while the disabled scheduler only refreshes
projects already carrying such an intent. Resolve this deadlock with an authorized,
project-scoped observation refresh after preflight rolls back. The refresh may
publish measurement evidence but must not run trash cleanup or allocation
reconciliation. Return an explicit retryable response; do not automatically delete.

## Operator workflow

Add prepare-reconciliation, review-reconciliation, and apply-reconciliation to the
existing explicit-target CLI. Reuse verify-online with protected input/output files.
Preparation grants no authority. Review grants no authority. Application changes
only protected database evidence after explicit authority selection and exact
review matching. Do not add a general force-delete bypass.

## Validation

Test replay, changed predecessor, tampering, stale verification, implicit authority
promotion, existing cleanup anchors, owner/provider drift, root drift, storage loss,
and changed ancestry. Exercise real verification and successful cleanup after an
authorized successor. Test observation refresh with automatic collection disabled
and prove it does not invoke destructive runtime stages. Run PostgreSQL cases in
an isolated local database and keep production verification read-only.
