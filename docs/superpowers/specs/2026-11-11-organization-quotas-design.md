# Organization, user, and project quotas

Date: 2026-11-11
Status: Design captured from approved brainstorming; awaiting written-spec review.

## Purpose

Give Troshka organizations shared resource allowances and spending quotas, with
delegated administration of user limits and owner-controlled project guardrails.
Every operation must satisfy all applicable levels. Calculated charges provide
timely spending enforcement and a consistent foundation for future chargeback.

This specification defines behavior and component boundaries. It does not enable
quotas, migrate production, or implement the feature.

## Hierarchy and permissions

The accounting hierarchy is organization → user → project. Multiple organizations
are supported. Each user belongs to at most one organization, and organization
membership is required before deploying. Unassigned users may create drafts.
Projects inherit their owner's organization; there is no per-project organization
selector. Shared-project actions consume the owner's allowances, not the actor's.

| Scope | Limit administrator | Usage included |
| --- | --- | --- |
| Organization | Platform administrator | All member users' projects |
| User | Organization/group administrator | All projects owned by the user |
| Project | Project owner | Resources and lifetime charges for that project |

Platform administrators manage organization membership and transfers. Group admins
manage their own users' limits and see their organization's usage; they cannot
change organization ceilings or grant themselves broader authority. Collaborators
cannot change project limits. Existing access to unrelated projects is not granted
by quota administration.

Administrative resource operations have no implicit quota bypass. A platform admin
can explicitly raise limits, with an audit record. Limit changes, membership
transfers, and organization administrator changes record actor, time, scope, and
before/after values.

## Organizations and cost centers

Each organization has a stable identity, name, administrators, limits, and an
optional manually entered cost-center identifier. SSO is not assumed to supply
cost-center information. Missing identifiers do not prevent deployment or quota
enforcement. Reports display “Cost center not assigned” and retain organization
attribution so the identifier can be supplied later.

Existing managed groups provide a starting point for organization administration.
Their current many-to-many reporting memberships must not implicitly determine
quota membership. Use a single authoritative organization association on the user;
existing overlapping reporting assignments cannot create multiple quota parents.
Implementation planning will choose how to reuse the group records and screens
while preserving existing reporting access and enforcing this association.

## Resource quotas

Supported initial dimensions are vCPU count, RAM in GiB, allocated storage in GiB,
and deployed-project count. Persist quantities in exact integral units, converting
to GiB for display. A missing limit means unlimited at that scope; zero prohibits
consumption. Neither value overrides limits at other scopes.

| Dimension | Counted usage | Release condition |
| --- | --- | --- |
| CPU and RAM | Deploying/running compute plus pending reservations | Confirmed compute shutdown or removal |
| Storage | Allocated capacity plus pending reservations, including stopped environments | Confirmed storage release |
| Deployed projects | Provisioning and deployed environments, including stopped environments | Confirmed environment destruction |

Drafts consume no infrastructure quota. A project-level deployed-project cap is
effectively an enable/disable guardrail because a project contains one environment;
user and organization caps control the number of environments.

Resource quantities represent provisioned/requested capacity, not observed CPU
utilization or bytes written. For nested environments, count the provisioned
workload resources once, without also charging their backing capacity as the same
resource allocation. Provider adapters must explicitly map supported resource types
to these dimensions; unsupported accounting must not silently count as zero.

User caps are not reserved slices of the organization pool. For example, five users
may each have a 40-vCPU cap within a shared 100-vCPU organization cap. The total
cannot exceed 100. Project and user allowances can exceed currently available
parent capacity; admission still checks every level independently.

Lowering a resource limit below existing usage leaves environments running and
blocks increases in the exceeded dimension. Stops, deletions, and resource-reducing
operations remain permitted. Restart requires a fresh capacity check.

## Spending quotas

| Scope | Period | Administrator |
| --- | --- | --- |
| Organization | UTC calendar month | Platform administrator |
| User | UTC calendar month | Group administrator |
| Project | Entire project lifetime | Project owner |

Use Troshka's calculated charges and configured rates for enforcement and displays.
Do not depend on delayed cloud-provider invoices. A charge counts once in the
project total and rolls up to user and organization totals; these are overlapping
limits, not three separate charges. Use decimal monetary arithmetic.

Monthly allowances reset without rollover. Project lifetime spend survives stops,
restarts, environment destruction/redeployment within the same project, and monthly
boundaries. Creating a new project does not erase user or organization monthly spend.

Warn at 80% and 90% of each finite spending quota. Deduplicate notifications per
scope, threshold, and accounting period/policy revision. Zero is immediately
exhausted; unlimited scopes do not generate threshold notifications.

At 100%, block deployments, starts, and resource increases in the affected scope,
and queue stops for its compute. Project exhaustion affects that project; user
exhaustion affects that owner's projects; organization exhaustion affects all
projects charged to the organization. Lowering a spending quota to or below
accumulated spend applies the same enforcement.

Preserve data and allow cleanup. Retained storage and other billable infrastructure
can continue accruing charges. Metering and shutdown latency can cause overshoot:
the quota is an enforcement threshold, not a guaranteed exact-dollar ceiling.

Track shutdown pending, completion, and failures durably. Retry failures and expose
“Budget exceeded—shutdown pending” while compute remains active. Repeated polls
must not create duplicate stop operations. Workers recheck the applicable exhausted
limits before issuing a queued stop so a restored allowance does not cause an
obsolete pending action. Already issued stops need not be reversed.

A monthly reset or authorized limit increase can restore eligibility, but never
automatically restarts compute. Every restart must still pass all resource and
spending checks, including any exhausted lifetime project quota.

## Admission and reservation architecture

Use a shared backend enforcement service for all allocation paths, including API,
worker, collaborator, provider, and administrative operations. Frontend checks are
informational; they are not the enforcement boundary.

1. Resolve project owner and organization, authorize the requested action, and
   compute its incremental resource requirements from the desired transition.
2. Obtain authoritative usage and current calculated spend. If spending cannot be
   calculated reliably, report accounting as unavailable and block new growth.
   Do not treat missing/stale metering as zero or stop existing workloads solely
   because accounting is unavailable. Cleanup remains available.
3. In a database transaction, serialize admission against the affected organization,
   user, and project in a consistent lock order. Check usage plus reservations plus
   the requested increment against each limit.
4. Create a durable reservation keyed to an idempotent operation identity before
   dispatching infrastructure work. Handle dispatch failures without leaving an
   untracked allocation or silently losing the reservation.
5. Revalidate pending work before allocation, including changed spending limits and
   organization membership. Convert reservations to observed allocations without
   double counting. Partial failures retain accounting for resources that exist.
6. Release reservations or allocations only when non-creation, shutdown, or cleanup
   is confirmed. Reconcile interrupted jobs against provider/agent state; elapsed
   time alone is not proof that resources disappeared.

PostgreSQL provides the authoritative concurrency boundary. Redis remains useful
for jobs and notifications but is not the sole quota ledger or lock mechanism.
Reconciliation repairs drift and identifies stale reservations without freeing
capacity that may still be consumed. Concurrent policy changes and admission must
use the same serialization rules.

## Durable accounting and organization transfers

Retain charge records or equivalent durable aggregates independently of live
project rows. Deleting a project cannot refund monthly spending. Preserve the
project identity/name, owner identity, organization identity, metering period,
quantity, and rate needed to explain charges. Historical identity must survive
later membership changes and project deletion.

The existing metering intervals, retained project invoices, and monthly statements
are integration points. Current-membership group reports alone are not sufficient
for historical organization accounting. Preserve existing rate snapshots and
avoid counting both detailed charges and retained summaries for the same period.

An organization transfer is an explicit platform-admin operation:

- Past charges remain attributed to the original organization.
- Current allocations and future charges move to the destination at one recorded
  effective boundary. Close/split open metering intervals at that boundary.
- The user's current-month spend and project lifetime spend do not reset.
- Check destination resource limits, including pending reservations, atomically
  before transfer. Reject a transfer that would exceed them.
- Serialize transfer against allocation and in-flight reservations so no work can
  charge the old organization after the boundary accidentally.
- Apply the destination's spending enforcement immediately; an already exhausted
  destination does not grant running projects an exemption.
- Existing explicit user limits carry across; the destination organization cap
  always applies and its admins can subsequently adjust user limits.

Changing or filling a cost-center identifier does not rewrite organization identity
or charge amounts. Actual financial export/reconciliation is outside this release.

## User experience and API behavior

Provide organization, user, and project usage views with resource quantities shown
as used + reserved / limit. Spending shows spent / allowance, warning/enforcement
state, and the next reset for monthly quotas. Project spending is labeled lifetime.
Include an accounting “as of” time and an explicit unavailable state.

Limit editors follow the permission hierarchy. Lowering resource limits below
usage explains that growth will be blocked. Lowering spending limits below spend
explains that compute shutdown will be triggered. A missing organization blocks
deployment with an assignment message rather than a generic quota failure.

Backend denials include structured scope, dimension, requested increment, used,
reserved, limit, and reason information, filtered by authorization. The UI should
identify the binding constraint, for example: “Cannot start: your organization has
8 GiB RAM available; this project needs 16 GiB.” Ordinary users need not see other
members' projects or individual spending to understand a shared cap.

## Migration and rollout

Create a default organization with unlimited resource/spending limits and no
cost-center identifier. Assign existing users and projects to it, preserving
existing project guardrails and historical calculated totals. Unlimited new limits
must not remove an existing explicit project spending cap.

Existing reporting-group memberships do not automatically override the default
assignment. Administrators establish actual organizations and perform explicit
transfers. New users remain unassigned until a platform admin assigns them and
cannot deploy in the meantime.

Backfill historical charges to the default organization where no reliable prior
organization attribution exists; document this as migration attribution, not
evidence of a historical employer. Deleted-project charges must remain included.
Capture existing live allocations and in-flight work before enforcement is enabled.

Deliver in three reviewable stages:

1. Foundation: organizations, authoritative membership, optional cost center,
   delegated policy storage, auditing, and default-organization migration.
2. Enforcement: atomic resource reservations, durable calculated-spending rollups,
   all allocation entry points, shutdown retries, transfers, and reconciliation.
3. User experience: usage dashboards, editors, warnings, accounting status, and
   actionable denial messages.

Keep enforcement disabled until allocation coverage, baseline accounting, and
reconciliation have been validated. Enabling enforcement and production migration
are separate operational actions from writing this design.

## Validation and acceptance criteria

- PostgreSQL concurrency tests prove simultaneous deployments cannot exceed a
  shared organization/user/project cap; SQLite alone is insufficient evidence.
- Retries and duplicate job dispatch do not double-reserve or double-release usage.
- Partial provisioning failures, lost workers, shutdown failures, and reconciliation
  retain usage until resources are confirmed absent/stopped.
- Stops release CPU/RAM, retain storage/project slots, and require fresh quota for
  restart. Drafts consume nothing; unsupported resource mappings do not undercount.
- Zero, unlimited, exact-boundary, and over-limit changes behave as specified.
- Exhaustion at each spending scope blocks growth and stops precisely that scope.
  Polls are idempotent; failed stops retry and remain visible.
- UTC month boundaries, intervals spanning months, resets without rollover, and
  persistent lifetime project spend are tested with controlled time.
- Deleted projects retain monthly charges; detail/invoice/statement aggregation
  neither loses nor duplicates charges. Missing accounting never appears as zero.
- Transfers preserve historical organization attribution, user monthly spend, and
  project lifetime spend; concurrent work cannot escape either organization's cap.
- Group admins cannot alter other organizations, raise organization ceilings, or
  bypass quotas; collaborators cannot change owner limits or charge themselves.
- Migration places existing users in the unlimited default organization, preserves
  existing project caps, and establishes live allocations without surprise stops
  caused by newly introduced limits.
- API and UI expose the same binding limit, units, calculated totals, reset dates,
  missing cost-center state, and shutdown/accounting status.

## Out of scope

Multiple quota organizations per user, project-selected organizations, reserved
user slices, allowance rollover, automatic restart, provider-invoice enforcement,
cost-center discovery through SSO, and financial-system chargeback export are not
part of the initial implementation.
