# One group for membership, reporting and quotas

The user's correction is authoritative: a group is an employer organization, and
each user belongs to at most one. There must be no separate reporting membership
that appears to move a user but leaves their quota organization unchanged. This
implements the original approved single-organization model; it does not require
another model-selection approval.

## Invariants

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

## Membership and permissions

Reuse stable ManagedGroup IDs and User.organization_id. New groups are always
organizations. Retain is_organization as a true-only compatibility field, rejecting
explicit false. Old ManagedGroupMember rows become migration evidence, not a live
membership source. Keep manager permissions separate from employer membership.

Both the Groups member endpoints and Users assignment endpoint call the same
transaction-neutral transfer service. Additions move the user; removals check the
expected source while holding the user lock, preventing a stale request from
removing a later assignment. Preserve user -> sorted groups -> sorted projects
lock ordering. Unassignment remains forbidden with retained infrastructure.

## Metering and transfers

Attribution and serialization must work independently of enforcement. All source
writers, including legacy reconcile, freeze/delete and finalization, participate
in membership locking. New sources retain organization identity when opened.
Do not activate provider quota validation just to obtain identity locks.

Disabled transfers close existing open intervals under their old identity and
open successors at one UTC boundary under the destination, retaining observed
quantities/rates. They move only existing live allocation/reservation rows and
preserve operation identities. They do not fabricate a baseline or check finite
limits against an incomplete zero-filled ledger. Responses explicitly say limits
are inactive and accounting/capacity may be unavailable. Enabled transfers retain
readiness, destination capacity and budget checks. Any ambiguous source/cursor or
inconsistent ledger identity rolls back the whole transfer.

## Reporting and history

Current membership always comes from users. Organization-attributed usage comes
from immutable charge/source identities, including former members. Available
monetary evidence can be shown while capacity readiness is unavailable, with
explicit incomplete/null totals where coverage is missing. Detailed history is
scoped to the requested group's attribution, never the owner's entire history.

Keep owner invoices/statements unchanged and label their current-member document
view as owner billing history. They may span multiple organizations; do not add
them to group charge totals or relabel whole documents as employer charges.
Preexisting Default-attributed history stays Default. Legacy unstamped history
uses the established migration provenance rule, not the current reporting roster
as proof of historical employer. Unknown/deleted-owner history stays unresolved.

## Migration and deployment

Use an explicit dry-run/apply utility with deterministic inventory digest,
idempotency receipt, one UTC boundary and all-or-nothing transaction. A user with
one legacy group and Default organization maps to that group; matching existing
membership stays; users without legacy memberships stay where they are. Multiple
groups or conflicting nondefault assignment block conversion for operator
resolution. Preserve group IDs, managers, policies and legacy roster evidence.

The observed production candidate has nine users in two nonoverlapping groups,
all currently Default. Recheck before apply; this is evidence, not a hardcoded map.

Deploy attribution-aware writers before membership changes. Drain/fence old API,
worker and metering writers during the conversion boundary; DB locks alone cannot
fence old processes or already dispatched work. Preserve/backfill source identities
before membership changes, split open sources, move actual ledger rows, audit and
store receipt atomically. The full quota baseline's conditional cutover cannot be
reused as this identity migration. Later baseline work must honor saved stamps.
Resume only new writers and verify membership, permissions, unchanged historical
charges/documents and truthful reports. Post-cutover rollback uses compensating
transfers at a new boundary, never a historical dump restore over new activity.

## User interface

Groups has Members, Administrators, Quotas and Settings for every group. Remove
the type column and quota-organization checkbox. Adding a member explains that
they move from their current group; use the shared transfer endpoint. Retain the
requested information-icon popover for detailed transfer semantics, updating its
copy to distinguish enabled limits from configured inactive limits. Optional cost
center remains metadata. Reporting, user administration and quota selectors use
the same group names and membership.

## Verification

Cover disabled transfers with active projects, historical attribution, rollback,
stale removal, limit preservation, permission revocation and enabled over-cap
denial. Use disposable PostgreSQL to verify membership/metering lock races.
Rehearse migration with nine users/two groups plus overlap/drift/cursor failures.
Validate owner documents and pre-boundary charge amounts/identities unchanged,
source split arithmetic unchanged, no invented capacity, and native unknowns
remain unknown. UI tests cover creation and moving a member through Groups.
