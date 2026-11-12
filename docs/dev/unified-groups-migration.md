# Unified Groups: fresh accounting conversion

This is a fenced database conversion, performed separately from schema upgrade.
It assigns legacy single-group members to their authoritative group and starts a
new accounting era. It imports no historical costs, invoices or statements.
Existing assets, owners, personal libraries, shares, storage paths and live ledger
operation identities remain intact. Enforcement stays disabled; capacity and
full monetary coverage remain unverified. This document describes the operator
contract; it is not authorization to perform a production conversion.

## Before the maintenance window

Deploy the reviewed code and additive schema revision `8fbde773a642`. Alembic only
changes the default of `managed_groups.is_organization` to true; it does not
convert existing rows or assign users. Do not start an application just to run
this CLI: application lifespan starts background work. Use the backend Python
environment, with its dependencies, the reviewed script and matching model code.
The backend image ships the script at
`/opt/app-root/src/scripts/migrate_groups.py`, beside `/opt/app-root/src/app`.
Invoke it explicitly with `python3` in an operator process; the normal image
entrypoint remains the application. The script also supports a repository checkout.

Prepare a database backup **inside the cluster**, mode 0600, with existing private
credential injection. Verify its restore path and retention. Do not export the
production database to a workstation, print connection URLs, enable SQL echo,
or include credentials/full records in evidence. Capture current workload replica
counts, images, deployment configuration and nonsecret job/monitor identities for
resume and rollback. Preserve any RQ tracking during a monitor handoff; consult
[production hotpatching](production-hotpatching.md) if overlays are involved.

## Establish the writer and external-operation fence

Set `quotas.enabled=false` in the deployed configuration and keep it false through
verification/resume. Stop incoming mutations, backend lifespan pollers (metering,
timers, auto-stop/delete, health/monitor work), RQ consumers and any other scripts
or reconcilers that write these tables. Idle RQ queues alone are insufficient:
worker startup can resume detached monitors. Prove active/detached deployment,
destroy, workload and OCP monitors have finished or are safely fenced, including
provider-side jobs and callbacks. A project with an OCP error can still be a
resume candidate; establish terminal-versus-live evidence before continuing.
Never clear tracking, force a finalizer, cancel provider work or mark a job done
merely to make this preflight pass.

An existing tunnel can remain only after checking its actual code path performs
authentication touches/read-only routing and cannot dispatch work or mutate asset
or membership state. Do not restart/roll tunnel pods during conversion: startup
can run schema initialization. `api_keys.last_used_at` alone is omitted from
digests because authentication updates it; key ownership, project binding, scopes,
expiry, activation and all other columns are included. Table locks can briefly
block authentication; a conflicting writer causes NOWAIT failure and a safe
retry after the fence is rechecked. Unverified indirect tunnel writes are a
reason to stop and resolve the fence, not to weaken inventory coverage.

The CLI attestation does not establish this fence. It rejects persisted pending,
dispatched or reconciling quota reservations, reconciling allocations,
pending/issued/failed quota actions, pending/running workload runs, and transitional
project states. Persisted state cannot prove that remote or detached work is idle.
Review all blockers before resuming; do not edit ambiguous state to bypass them.

## Exact operator CLI contract

Run in the target environment with an explicitly injected URL in an existing
environment variable such as `GROUP_MIGRATION_DATABASE_URL`. The CLI accepts the
**variable name**, never a URL argument, and has no application-target fallback.
It requires PostgreSQL; SQLite is available only through isolated service tests.
Do not use shell tracing or echo the variable. From a repository root, use:

```sh
python3 scripts/migrate_groups.py \
  --database-url-env GROUP_MIGRATION_DATABASE_URL \
  --dry-run
```

Inside the released backend image, substitute
`python3 /opt/app-root/src/scripts/migrate_groups.py` for the command prefix;
the flags and explicit environment-variable contract are identical.

This rehearses the complete transaction and rolls it back. Save sanitized JSON
output privately. It contains the inventory digest, proposed UTC accounting start,
mapping digest/count, successor count, and per-table preservation counts/digests.
No raw asset rows, topology, credentials or user/email mapping are printed.
Re-inventory after the fence; never assume the earlier observed production roster
still matches. The synthetic rehearsal's nine users/two groups are not an apply
precondition. Overlapping legacy membership or conflicting nondefault membership
is rejected; unrepresented users keep their authoritative assignment. The
schema-defined foundation Default organization must exist with
`quota_baseline.DEFAULT_ORGANIZATION_ID` and `is_organization=true`. The foundation
seed is named `Default organization` with normalized name `quota-default-<UUID>`;
an arbitrary group named Default is not accepted as that identity. Candidate
group/user mappings and roster counts are always discovered, never hardcoded.

Apply requires all three of the reviewed digest, explicit start and fence flag:

```sh
python3 scripts/migrate_groups.py \
  --database-url-env GROUP_MIGRATION_DATABASE_URL \
  --apply \
  --expected-digest '<inventory_digest from the reviewed dry-run>' \
  --accounting-start '<accounting_started_at from the reviewed dry-run>' \
  --writers-fenced
```

The start must have a timezone, cannot be in the future, and cannot precede source
timestamps, accrual cursors or existing charge coverage. Dry-run defaults to the invocation's current UTC
time; apply has no default boundary. Old open intervals close at this boundary
without generating old charges. New successors copy only persisted observed
quantities, snapshotted rates, resource/host/provider identities and receive the
new owner's group stamp. Desired topology supplies no quantities or physical-byte
proof. Unknown physical accounting stays unknown. Live allocation/reservation IDs,
operation IDs, observation identities, dispatch metadata and policy caps survive;
current ledger group attribution follows the owner. Released historical operations
are retained. No provider or Redis call occurs.

The service holds all known application tables in an EXCLUSIVE NOWAIT transaction
for the conversion. A stale digest, ambiguity, conflicting lock, asset fingerprint
mismatch, failed flush or lost transaction rolls back every change. A failure
prints a generic scrubbed diagnostic and exits 1; argparse contract errors exit 2.
Success exits 0. Do not infer success from partial output or retry with a newly
guessed digest. Reinspect the fence and repeat a dry-run when inventory changes.

## Receipt, verification and resume

The durable receipt is `quota_readiness.id='unified-groups-v1'`, with the immutable
accounting start, original inventory/mapping digests, UUID-only mapping and
per-table sanitized preservation evidence. It has `ready=false` and is separate
from `id='baseline'`; normal readiness reads select the baseline explicitly.
The conversion sets the baseline false too. Repeating apply with the identical
original digest and start returns the same receipt without moving membership,
opening sources again, or touching subsequent post-start accounting. A conflicting
replay is rejected. Receipt replay confirms the original conversion, not the
health or absence of subsequent writes. A new post-conversion inventory can be
inspected through `group_migration.inventory` in a dedicated read-only session;
compare its `preservation` with the receipt only while still fenced.

Verify owner project/pattern/image visibility and shared pattern/image access,
including disabled user/role flags and personal-library lookup. ProjectShare rows
are preserved; current project GET/list handlers enforce owner/admin access and
do not grant access from ProjectShare. Conversion intentionally preserves that
existing behavior. Inspect all required fingerprints: projects/project_shares,
patterns/pattern_disks/pattern_shares, libraries/library_items/library_item_disks/
library_shares/image_caches, and actual project VM/disk/network/provider/storage
relationships. Other persisted tables are also fingerprinted. Only expected
membership/group-kind and current-ledger organization/timestamp columns may change;
metering and readiness have their explicit conversion contract.

Group reports expose `accounting_started_at` at the root, retain UTC calendar-month
labels, and clip usage to this start. Pre-start sources and charges cannot accrue
or enter new totals. Ordinary post-start transfers split attribution as usual;
owner billing's existing historical documents remain unchanged.

Resume only after receipt and preservation/access verification. Restore the saved
workload counts/configuration in a controlled order; retain job/monitor tracking
and verify any resumed detached work explicitly. Keep quotas disabled and capacity
unverified. No enforcement enablement is part of this procedure.

## Baseline limitation and rollback

The old `backfill_quotas.py` import/run path is intentionally blocked after this
receipt exists: its `_history` routine reconstructs Default attribution and
imports invoices/statements, contrary to this fresh-start contract. Direct legacy
summary import is blocked too. This is not a permanent prohibition on a future
capacity baseline: a separately reviewed fresh-era-only baseline must validate
physical allocated bytes and current operations without importing pre-start
costs, and honor the saved start before enforcement may be enabled. Do not delete
the receipt to run the old importer. This task does not verify such a baseline.

Before commit, rollback is automatic. After commit but before any writer resumes,
restore the verified in-cluster backup while all writers/external work remain
fenced, with matching code/schema/configuration; verify the restore and access
before resuming. There is no destructive reverse-mapping command. Once any writer
resumes, restoring a whole backup can discard newly created assets or operations:
stop, preserve current evidence and design a forward repair instead. Downgrading
the default-only Alembic revision does not reverse the conversion and must not be
treated as data rollback.

## Disposable rehearsal

The supplied runner targets only the isolated local database
`troshka_unified_groups_20261111` and uses disposable fixture-specific schemas:

```sh
src/backend/venv/bin/python3 \
  /private/tmp/troshka-quota-rollout-20261111/run-group-postgres-tests.py \
  tests/test_group_migration.py -q --tb=no --disable-warnings
```

The tests cover deterministic nine-user mapping, conflicts/staleness, pending
work/actions, dry-run and whole-transaction rollback, exact replay, asset/storage
fingerprints and actual access handlers, old-cost exclusion, post-start transfer
and frozen invoice arithmetic. They do not start application lifespan or call a
provider/Redis. The parent operator owns database cleanup. Use abbreviated/no
tracebacks: database connection exceptions may include DSN parameters.
