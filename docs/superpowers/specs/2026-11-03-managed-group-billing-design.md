# Managed groups and team billing

## Outcome

Admins create Troshka-managed groups and assign existing users as members and
managers. A group supports multiple managers; users may belong to or manage
multiple groups. Managers can inspect member usage, costs, project invoices, and
monthly statements, and download billing data without gaining environment control.

The user approved this design for implementation on November 3, 2026, then
authorized group managers to add/remove other managers with last-manager protection.

## Approach

Use database-backed groups independent of OpenShift login groups. OpenShift group
mapping would couple reporting to cluster access and require an additional mapping
for managers. A global manager role would not express which users a manager may
see. Group-specific assignments provide the required boundary while leaving SSO
role resolution intact.

## Data and administration

Add ManagedGroup, ManagedGroupMember, and ManagedGroupManager models and an
additive Alembic migration. Groups have UUID IDs, a unique normalized name,
description, and creation timestamp. Association tables have UUID foreign keys,
unique group/user pairs, and cascading deletion of associations. Register models
in app/models/__init__.py. No existing user roles or billing records are migrated.

An admin-only Groups page provides create, rename, description edit, membership
assignment, manager assignment, and deletion with confirmation. Assignments use
existing user records, including invited users. Membership and management are
independent: managers' own costs enter a group report only if they are members.
Deleting a group removes assignments but preserves users and billing records.
Groups with no members or managers are valid during setup.

Current group managers can add/remove managers within their own group, but cannot
remove the last manager (including themselves). Site admins follow this same
last-manager rule; they can delete an unneeded group instead. Serialize assignment
changes by locking the parent group row before checking authorization/counts.
Managers add another existing user by exact email, without directory browsing.
Member assignment and group metadata/deletion remain site-admin-only. User deletion
must also reject removal of a sole manager to preserve this invariant.

## Authorization and history

Centralize billing authorization in a small service. An admin can see all billing;
a user can see their own billing; a manager can additionally see billing owned by
current members of groups they currently manage. Ordinary membership alone does
not grant access to other members' data. Operators gain no implicit team access.

Apply the same policy to list, detail, project-metering, and export requests,
including the project-metering alias. Group-specific requests require management
of that exact group or admin access. Validate requested owner filters against
that group's membership. Use database filtering before loading financial records;
never rely on frontend filtering for access control. Unknown or unauthorized group
IDs return 404. Existing billing detail denial behavior can remain 403.

Current membership grants access to the member's available billing history,
including records predating their assignment. Removing a member or manager, or
deleting the group, revokes access on subsequent requests. Already downloaded
files cannot be revoked. Reports are labeled "Current members"; this is a view
of member costs, not historical departmental chargeback. Historical membership
accounting is outside this first version.

Do not modify project listing, project control, console, credential, rate-editing,
statement-finalization, or user-administration permissions. Manager reporting
returns only IDs, display names, emails, project names/states, and metering fields.
Ownerless historical records remain admin-only.

## Reporting and accounting

Provide a dedicated Team Usage & Billing page with a group selector, member
selector, and billing-month selector. Admins can select any group; managers see
only groups they manage. Return minimal member identities from the scoped groups
API rather than opening the admin-only users endpoint.

Show active project spend separately from monthly usage and finalized statements.
Monthly usage includes resource quantities (vCPU-hours, GiB-hours, and IP-hours)
and USD subtotals per user, project, and resource kind. Sum quantity-hours across
resources; do not present summed wall-clock hours as resource consumption.

For a finalized owner/month, use the stored statement as the authoritative total.
For a provisional month, combine stored draft contributions for destroyed projects
with period-clipped intervals for surviving projects, replacing contributions by
project ID to prevent duplicates. Reuse period_usage and existing rate snapshots;
do not reprice historical usage. Use UTC calendar-month boundaries and one shared
as-of timestamp per report. Distinguish provisional and final totals, and show
missing/unavailable data explicitly rather than treating it as confirmed zero.

Provide separate project-invoice and monthly-statement lists with detail views.
Never add project-invoice totals to statement totals: those documents overlap.
Likewise, lifetime active spend is not added to monthly spend. Group totals count
each member once. An all-managed-groups view, if implemented, must deduplicate
owners across overlapping groups; the initial UI selects one group at a time.

Reports read persisted metering intervals and show an as-of timestamp; they do not
trigger infrastructure operations or finalize statements. Existing metering polls
continue to reconcile usage. Batch queries across scoped owners/projects rather
than issuing one HTTP request per project.

## API and frontend boundaries

Add /groups routes for admin CRUD and assignment management, plus authenticated
discovery/detail of managed groups. Add group-scoped metering report routes under
/metering/groups/{group_id}, with validated owner and year/month parameters.
Extend existing invoice, statement, and project-metering access through the shared
billing policy. Keep helper modules small and functions below complexity limits.

Add admin group management and the team billing page using existing PatternFly,
fetch, and React state conventions. Extract reusable billing breakdown/export
helpers from the existing metering UI only where needed. Preserve personal billing
navigation and make the team scope explicit. Clear cached details when group or
user selection changes; ignore stale responses from earlier selections. Handle
loading, empty groups, expired access, request failures, and disabled metering.
Group administration remains available when metering is disabled; reports follow
the existing disabled-metering API behavior.

Downloads include CSV summaries and detailed invoice/statement CSVs with owner,
group, period, currency, resource unit, quantity-hours, rate, and cost fields.
Use the same authorized scope as displayed reports. Quote CSV cells and neutralize
spreadsheet formulas in user-controlled text. Do not introduce a new PDF invoice
format or externally sourced cloud-provider invoices in this version.

## Validation and rollout

Backend tests cover multiple managers, overlapping groups, manager/member
independence, self-access, admin access, ordinary users and operators, forged
group/owner/document IDs, ownerless records, and immediate assignment revocation.
Exercise both project-metering routes and verify environment-control permissions
remain unchanged. Test admin-only mutations and duplicate/invalid assignments.

Accounting tests cover UTC month boundaries, open intervals, destroyed-project
draft contributions, final-statement precedence, quantity-hour aggregation, and
no duplication between live intervals, drafts, invoices, and final statements.
Frontend tests cover group selection, scope changes, failures, filters, downloads,
and escaping of CSV formula-like text. Check the migration upgrade/downgrade on a
disposable database, then run relevant backend/frontend tests and required quality
checks. Do not alter the development database merely to validate the migration.

The migration initially creates empty groups, preserving current access. Release
the schema and application together using the normal deployment process. This
design does not authorize production deployment or creation of real assignments.
