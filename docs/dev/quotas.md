# Quotas — baseline and controlled enablement

## Entry point and supported scope

Use this procedure before deliberately enabling organization → owner → project
quotas, or when admission reports `Accounting baseline has not been verified`.
The default is `quotas.enabled: false`. That disables the new hierarchy and
resource evaluator; **existing positive canonical `Project.budget_usd` limits
still use legacy metering enforcement**. New hierarchy zero/unlimited semantics
apply when enabled. No script here changes configuration or restarts anything.

The initial adapter covers basic nested VMs on **one existing shared-billing
troshkad host** with an AWS/EC2, GCP, Azure or OCP Virt backing provider. Guest
CPU/RAM, allocated filesystem blocks owned by the project and one environment slot count once.
This physical-storage adapter requires **local-filesystem qcow2 disks**; a shared
billing host is not a shared filesystem. Shared filesystem pools are unready until
a cluster-wide pool identity/reservation adapter exists. Raw/materializing disks
and cold base-image downloads are denied. Existing unsupported resources cannot
be omitted to establish readiness.
Backing-host platform capacity is not a second guest charge. Stopped environments
retain storage and the slot. See [the allocation inventory](quota-allocation-paths.md)
for the maintained supported/denied boundaries and their tests.

Native/operator KubeVirt, BMC/deferred power, multi-host placement, dedicated
billing, containers/pods, installers/Showroom/runners, live reconfiguration,
individual VM start/reboot/resume, live migration, snapshot/capture buffers,
unresolved image virtual sizes, legacy Disk rows and retained EIPs are not covered
by this baseline. Those growth surfaces are explicitly denied with enforcement
enabled. Existing unsupported live resources **block readiness**; denial of future
growth does not account for already allocated infrastructure.

## Physical storage contract and operating limits

`storage_bytes` is the sum of `st_blocks * 512` from every regular file beneath
the exact `/var/lib/troshka/vms/<full-project-UUID>` directory: active disks, seeds,
retained overlays and orphan files count, including while compute is stopped.
Sparse file length and qcow2 virtual size are not physical usage. Immutable image,
pattern and snapshot **cache** files are platform storage, inventoried once per
physical host/device pool, never copied into each owner's quota. This does not
enable capture operations or establish ownership for unsupported retained captures.
Provisioned disk capacity is reported separately. Existing disk GiB-hour pricing,
decimal charge calculations and historical invoices are unchanged.

The authenticated agent's read-only `POST /storage/inventory` endpoint accepts
`project_id` and the current `generation_id`. It performs descriptor-relative,
no-symlink enumeration, rejects hardlinks, special files, duplicate inode identities,
changed directory inventories and nested mounts, and limits enumeration to 20,000
directory-entry visits (two passes) and two seconds. Backend transport has a five
second timeout and one attempt. These are best-effort bounds: a blocked filesystem
syscall cannot be interrupted by the Python scan deadline. No shell or qemu command
is executed for this measurement. A missing writable root, inaccessible agent,
incomplete inventory or changed device identity is unavailable, never zero.
Expected deployed disks must remain present; missing/open-unlinked disks require
stop and operator reconciliation rather than an inferred release.

Admission reserves **64 MiB per attached disk plus 16 MiB per VM** for initial COW
metadata/seeds, not the full virtual disk capacity. This is an initial allowance,
not a hard upper bound on allocation. Unsupported copy/preallocation/raw options
are denied; observed overshoot is fully charged even if it exceeds the allowance.
The pool additionally retains `max(storage_pool_reserve_bytes,
capacity * storage_pool_reserve_percent / 100)`, default **1 GiB or 5%**, whichever
is larger. Positive values are required. Independent organizations serialize
pending initial reservations on the same host/device pool after taking identity
locks. Physical free space includes platform cache consumption and all other
filesystem users; shared base totals are informational and do not substitute for
free-space checks.

Cold image downloads cannot run in an enabled quota deployment. A platform
administrator must populate and verify approved immutable base caches as a
separately authorized platform maintenance task, check pool headroom, and then
retry the deployment. Do not disable quota enforcement or bypass project growth
guards to warm a cache. A cache miss/corruption during deployment remains an error.

Fresh observations retain request-start time and the exact host, project and
deployment-generation identity. Storage freshness is at most **60 seconds**.
All current-generation operation locks precede live user → organization → project
locks. A single deployment row owns storage; subsequent start operations never
duplicate it. Pending provider work may increase measured usage but cannot release
storage or its unobserved reservation. Reductions require a measurement after all
generation workers/calls settle. Ordinary compute confirmation still cannot grant
unreserved compute or arbitrary storage growth. Transfers preserve ledger identity
and history while reassigning the current allocation to its authoritative owner.

The durable enforcement poll runs every **30 seconds**, and also reconciles after
normal provider-state polling. At or above any project/user/organization physical
cap, further growth is blocked and compute in that affected scope is stopped using
the same durable fence, mutex and retry machinery as spending enforcement, with
distinct `physical_storage_limit` or `physical_pool_headroom` causes. Low pool
headroom stops measured environments on that pool. Stored data is never deleted
and compute never restarts automatically. A stale observation creates no new
exhaustion claim but blocks admission; an existing proven exhaustion survives
telemetry loss until fresh recovery evidence or a policy change clears it.

**Lowering a physical cap to or below current measured use triggers shutdown.**
This differs from CPU/RAM/project-count reductions, which only block additional
growth. The editor states this consequence. Freeing files or raising/removing the
cap clears the cause, but never automatically restarts compute.

This is **best-effort polling, not a filesystem hard quota**. Guest writes can
overshoot between observations or while shutdown is delayed. Scanning is serial
across hosts/projects in a poll, so large inventories, timeouts and slow providers
increase detection latency beyond 30 seconds. Choose pool reserve and deployment
scale accordingly. No claim of a strict byte cap or bounded shutdown latency is made.

## Production rollout phases and handoff

Deploy the schema and application with hierarchy enforcement **off** first. The
backend image includes `/opt/app-root/src/scripts/backfill_quotas.py`; it is never
executed by the container entrypoint. Run it with `python3`, from a dedicated
operator/migration process using the exact release image and its production
configuration. The repository equivalent is `src/backend/venv/bin/python3
scripts/backfill_quotas.py`. Every mode requires an explicit target DSN through
`QUOTA_BASELINE_DATABASE_URL` or `--database-url`; prefer the environment to avoid
credentials in process arguments. It does not fall back to the application DB.

| Phase | Required result | Action on failure |
| --- | --- | --- |
| Backup and schema expansion | Verified backup and additive Alembic revision `c935e6fa0421` | Keep old application running; investigate migration error before promotion |
| Disabled deployment preflight | `--preflight` exits 0 with `deployment_ready: true` | Resolve schema/config mismatch; preserve JSON report |
| Disabled application rollout | API and worker image digests match release; all processes report enforcement off; smoke checks pass | Revert application images, retaining expanded schema |
| Baseline maintenance | Writers fenced, original jobs reconciled, inventory and independent fresh evidence recorded | Keep enforcement off; preserve infrastructure and job identities |
| Rehearsal and apply | Dry-run and apply both report `ready` and `accounting_ready`; final `--check-ready` exits 0 | Reconcile blockers without deleting history or allocations |
| Deliberate enablement | Separate approval and fresh provider/accounting checks for every API/worker | Keep growth fenced; investigate readiness or observation failures |

The new API reads `User.organization_id`, so schema expansion must finish before
the new application is promoted. Use the release image's Alembic migration
command against the verified target. `alembic/env.py` loads the URL from the
application configuration: changing only `alembic.ini` or the baseline CLI's DSN
does **not** configure Alembic. Verify the application configuration target in the
migration process without printing credentials. A revision mismatch is a hard
preflight failure, even if selected quota tables happen to exist.

Capture operator output on durable restricted storage outside disposable pods.
Create a new directory for each attempt; retain previous attempts and exit codes:

```bash
umask 077
mkdir "$QUOTA_BASELINE_DIRECTORY"
python3 /opt/app-root/src/scripts/backfill_quotas.py --preflight \
  > "$QUOTA_BASELINE_DIRECTORY/preflight.json"
preflight_status=$?
printf '%s\n' "$preflight_status" > "$QUOTA_BASELINE_DIRECTORY/preflight.exit"
```

When running through `oc exec` or an isolated Job, redirect output to the operator's
durable directory and retain the remote process exit status. The example above
assumes that directory is already durable in the process running the command.
Do not save credentials, full provider records or controller stdout in the handoff.

`--preflight` checks schema/revision before importing application models. Once the
schema matches, it includes desired inventory, the baseline gate and retained
operations that lack physical-storage metadata. It is read-only, contacts no
providers, and never takes a successful deployment check as permission to enable:
its top-level `ready` remains false. Exit 0 means only that the schema matches this
release and enforcement is disabled in **this process**. Verify configuration in
every API/worker separately. Unsupported inventory entries, an unready baseline,
legacy allocations or absent provider evidence still block enablement. Ordinary
production writes may change inventory between reads; only the fenced baseline
workflow establishes a cutover boundary.

The durable handoff must identify the target cluster/namespace and database name
(no DSN), image digests, backup location/restore verification, before/after Alembic
revisions, rollout and smoke-test results, enforcement settings per workload,
report paths and exit codes, blockers with original project/operation IDs, and the
next operator action. Record “not performed” for provider evidence, baseline apply
or enablement that has not occurred. Never fill missing evidence with assumptions.

For rollback, revert application images while retaining additive schema and quota
history. Do not run `alembic downgrade`: it drops ledger/history tables and removes
organization assignments. Keep enforcement off during this initial rollout. After
an enabled cutover, returning to disabled/old writers invalidates the reviewed
baseline; use an incident maintenance boundary and repeat reconciliation before
any later enablement. A failed or interrupted apply requires inspection of retained
reports/readiness and the original operations; do not blindly rerun a mutation.

## Allocated-byte baseline upgrade

Readiness now requires `operator-baseline-v2-allocated`. Existing v1 markers and
virtual-size evidence cannot enable physical quota admission. Keep the full
maintenance workflow below; do not relabel or rewrite existing virtual ledger
values as physical bytes. Each live project entry additionally requires `storage`
containing the complete `allocated-v1` agent inventory, `host_id`, and original
ISO `observed_at`, with `generation_id: baseline:<project-UUID>`. Collect it after
the recorded worker fence and no later than the entry's provider `observed_at`.
Keep the existing `resources.disks` virtual-size proof for provider command
validation and provisioned billing; every expected local disk must also appear in
the physical file inventory. Original unresolved in-flight records remain retained
and unready, never converted into confirmed physical allocations. This change
does not run a database migration or enable the feature.

An existing v1 allocation is not automatically upgraded by replaying the importer.
`--preflight` reports retained operations missing `allocated-v1` metadata under
`retained_operation_blockers`. A changed live manifest for an already imported
baseline operation is rejected with `Baseline evidence changed; use generation-aware
reconciliation`. Preserve that allocation, reservation, original generation and
billing history. Obtain fresh physical inventory for the original generation under
maintenance and review a generation-aware reconciliation before attempting v2
readiness. There is no automatic v1 conversion or destructive reset flag. Do not
delete the old operation, reuse an observation ID with edited evidence, or change
only `baseline_version` to bypass this gate. On a first rollout with no previous
quota allocations, the normal v2 import below creates the baseline directly.

## Prerequisites and maintenance boundary

Use an independently authorized rollout window, a tested PostgreSQL backup/restore
procedure, the migrated schema (`c935e6fa0421` at this revision), backend virtual
environment, read-only host/provider access, and explicit database/Redis connection
settings supplied through your secret-management environment. Do not paste DSNs,
tokens, credentials, provider stdout, full job results or VM XML into a manifest
or shared report. Keep detailed evidence privately; reference it by ticket/digest.

**Quiesce every writer before collecting ready evidence:** block API mutations,
stop all API replicas/background pollers, lifecycle/RQ workers, schedulers and
external provisioning automation. Account for in-memory fallback workers too.
Resolve/drain or explicitly fence their original provider jobs. Keep read-only
agent/libvirt observation available. Stopping an API or losing its RQ worker is
not proof that a previously submitted agent job or child process finished.

The importer first takes sorted advisory locks for the existing operation/project
inventory (a five-second lock timeout), then PostgreSQL `EXCLUSIVE ... NOWAIT`
locks on all mapped tables and verifies the operation/project set did not change.
It never waits for an external operation advisory while holding table locks. It
then prelocks the full user → organization → project identity tiers before invoking
shared accounting/confirmation services. An active conflicting writer or row-lock
reader aborts import before changes; ordinary read-only SELECT remains available.
Database writes cannot race the import. These locks do **not** fence
provider processes or protect the period after commit. A maintenance attestation
is therefore mandatory. **Keep every writer stopped through apply, preflight and
configuration change; restart API/workers only with the deliberately enabled new
hierarchy.** A disabled restart between apply and enablement invalidates this
boundary. If any writer resumes, repeat collection/review/import. SQLite is only
a disposable rehearsal target, never the production concurrency boundary.

## Procedure

1. **Back up and rehearse restoration.** Set `QUOTA_BASELINE_DIRECTORY` to a
   private operator directory, `QUOTA_BASELINE_PG_SERVICE` to your configured
   libpq service and `QUOTA_BASELINE_DATABASE_URL` to the separately supplied
   SQLAlchemy DSN. The importer has no application-database fallback.

   ```bash
   umask 077
   mkdir -p "$QUOTA_BASELINE_DIRECTORY"
   pg_dump --dbname="service=$QUOTA_BASELINE_PG_SERVICE" --format=custom \
     --file="$QUOTA_BASELINE_DIRECTORY/pre-quota.dump"
   pg_restore --list "$QUOTA_BASELINE_DIRECTORY/pre-quota.dump"
   ```

   Preserve existing invoices/statements, metering/rate snapshots, project budgets,
   users, reporting groups, quota policies/ledger/actions and queue/job identities.
   Restore to a disposable database for the first rehearsal. A table of contents
   is not a restore test.

2. **Collect the desired identity inventory without writes.** From the repository
   root, with all writers still stopped:

   ```bash
   src/backend/venv/bin/python3 scripts/backfill_quotas.py --inventory \
     > "$QUOTA_BASELINE_DIRECTORY/inventory.json"
   ```

   This gives the source inventory SHA-256, project/host IDs, desired resource
   digests and allowed VM names/disk paths. These are expected identities, **not
   observations**. The digest covers all non-quota source tables; it excludes
   only the metering cursor/attribution columns maintained by the importer.
   No credentials/source values are returned by the digest. Unrelated source
   changes also invalidate it: this is intentionally an offline procedure.

3. **Obtain independent read-only provider evidence.** On each identified host,
   use approved SSH access and direct queries, for example:

   ```bash
   sudo virsh list --all --name
   sudo virsh domstate '<domain from allowed_resources.vms>'
   sudo virsh dumpxml '<domain from allowed_resources.vms>'
   sudo qemu-img info --force-share --output=json '<actual attached disk path>'
   sudo find '/var/lib/troshka/vms/<project UUID>' \
     '/var/lib/troshka/shared/vms/<project UUID>' -maxdepth 1 -type f -print
   date --utc --iso-8601=ns
   ```

   Compare libvirt vCPU/current-memory values and **actual attachment paths** to
   the inventory. Convert memory to MiB for `ram_mb`; convert qemu `virtual-size`
   bytes to GiB for the disk map. File length, sparse bytes used and image object
   size are not virtual capacity. Include exactly the actual existing disk path,
   choosing the verified local or shared path, not both alternatives. Verify all
   VM/domain and disk identities for that project, including both directories;
   unexplained overlays/resources, missing domains, changed capacities, multiple
   consumers or ambiguous mapping require manual reconciliation. Resolve one
   `LibraryItemDisk.virtual_size_bytes` for each image-backed disk; a deployed
   request smaller than the verified image needs explicit metadata reconciliation,
   not an assumed smaller baseline.

   Map each domain to its full canvas VM node ID using the deployed topology and
   `app.services.deploy_topology._vm_domain_name(project_id, node_id)`, the same
   function used by `quota_operations.resource_map`. Record states by that node
   ID. Capture an original UTC collection timestamp after direct inspection.
   Copying an API cache timestamp or replacing an old timestamp with today's time
   does not produce fresh evidence. The separate importer cannot read API-local
   WebSocket memory while quotas are disabled.

   Inspect agent health and each original provider ticket using authenticated,
   pinned **GET** requests, without starting an agent job. This example runs from
   `src/backend`, uses the explicit DSN, and prints only allowed evidence fields:

   ```python
   import json, os
   from datetime import UTC, datetime
   from sqlalchemy import create_engine
   from sqlalchemy.orm import Session
   from app.models.host import Host
   from app.services.troshkad_client import troshkad_request

   engine = create_engine(os.environ["QUOTA_BASELINE_DATABASE_URL"])
   with Session(engine) as db:
       host = db.get(Host, os.environ["QUOTA_BASELINE_HOST_ID"])
       health = troshkad_request(host, "GET", "/health", timeout=2, retries=1)
       # retries=1 is one attempt in this client; it performs no retry.
       tickets = []
       for ticket in filter(None, os.environ.get("QUOTA_BASELINE_JOB_IDS", "").split(",")):
           job = troshkad_request(host, "GET", f"/jobs/{ticket}", timeout=2, retries=1)
           tickets.append({"id": ticket, "status": job["status"],
                           "completed_at": job.get("completed_at")})
       print(json.dumps({"host_id": host.id, "status": health["status"],
                         "running_jobs": health["running_jobs"], "tickets": tickets,
                         "jobs_checked_at": datetime.now(UTC).isoformat()}))
   engine.dispose()
   ```

   The current agent registers jobs as running before creating their worker thread.
   Zero running jobs is useful only after dispatch has been fenced and in conjunction
   with original ticket/process evidence. A restarted agent, missing/lost ticket,
   unknown detached process, partial failure or ambiguous worker remains a blocker;
   an elapsed duration or project state is never terminal evidence.

   Inspect RQ queues and started/deferred/scheduled registries read-only, including
   stale identities; use `registry.get_job_ids(cleanup=False)` explicitly. Never use
   `get_job_info()` as baseline proof: it can clear zombie tracking. Do not delete
   job references or clean registries to make an inventory look empty. Record any
   queued/unknown work under `inflight` below. Preserve original operation IDs.
   `jobs: []` means a reviewed empty inventory of unfinished/ambiguous work, not
   “the operator did not look up any jobs.”

4. **Prepare a reviewed manifest and dry run.** The JSON example is illustrative;
   replace every identity and timestamp with actual collected evidence. A manifest
   is an explicitly trusted operator transport, not live provider verification by
   the importer. Never copy expected capacities into it without checking them.

   ```json
   {
     "version": 1,
     "inventory_digest": "SHA-256 from inventory.json",
     "maintenance": {
       "writers_stopped": true,
       "history_complete": true,
       "since": "2026-11-11T11:59:00+00:00",
       "evidence": "Maintenance ticket; API/workers/schedulers/external writers fenced; retained billing sources reviewed"
     },
     "projects": {
       "PROJECT_UUID": {
         "host_id": "HOST_UUID",
         "source": "operator-readonly-virsh-and-agent-health",
         "observation_id": "unique immutable collection identity or content digest",
         "observed_at": "2026-11-11T12:00:00+00:00",
         "worker_fenced_at": "2026-11-11T11:59:58+00:00",
         "jobs_checked_at": "2026-11-11T11:59:59+00:00",
         "fence_evidence": "Private evidence reference: workers terminal/fenced; original provider tickets/processes reconciled",
         "jobs": [],
         "states": {"FULL_CANVAS_VM_NODE_ID": "running"},
         "resources": {
           "vms": {"ACTUAL_DOMAIN_NAME": {"cpu": 2, "ram_mb": 4096}},
           "disks": {"/var/lib/troshka/vms/PROJECT_UUID/ACTUAL_DISK.qcow2": 10}
         },
         "storage": {
           "version": "allocated-v1", "complete": true,
           "project_id": "PROJECT_UUID", "generation_id": "baseline:PROJECT_UUID",
           "host_id": "HOST_UUID", "observed_at": "2026-11-11T11:59:59+00:00",
           "writable_pool": "ACTUAL_ST_DEV",
           "files": [{"path": "/var/lib/troshka/vms/PROJECT_UUID/ACTUAL_DISK.qcow2",
                      "device": "ACTUAL_ST_DEV", "inode": "ACTUAL_ST_INO", "allocated_bytes": 4096}],
           "bases": [],
           "pools": [{"id": "ACTUAL_ST_DEV", "capacity_bytes": 107374182400,
                      "free_bytes": 96636764160, "platform_allocated_bytes": 0}]
         }
       }
     },
     "inflight": []
   }
   ```

   Required top-level fields are `version` (integer 1), `inventory_digest`
   (matching string), `maintenance` (the four fields above), `projects` (object
   keyed by project UUID) and `inflight` (array). Every non-draft/live project needs
   every documented project field. Use strings with explicit timezone offsets for
   times and integral capacities. `since <= worker_fenced_at <= jobs_checked_at <=
   observed_at <= now`; observation age must be at most `max(30, metering.poll_seconds)`.
   All mapped VM states must be confirmed on (`running`/`paused`) or confirmed off
   (`shut_off`, `shut off`, `shutoff`, `stopped`, `off`, `hibernated`). Mixed/unknown
   projects require reconciliation. Project `active`/`stopped` state alone is not
   evidence. Actual open metering resources must agree with these observations;
   rates must resolve from existing configuration, never the manifest.

   Each unresolved `inflight` entry has exactly `operation_id`, `project_id`,
   `requested` (all four exact resource dimensions: `cpu`, `ram_bytes`,
   `storage_bytes`, `projects`) and nonempty `provider_jobs` (objects containing
   `id`, `status`, optional `host_id`). Import preserves it as a reconciling
   reservation with `worker_finished=false`; **it always blocks readiness**.
   No guessed vector, fabricated zero, terminal status or replacement job is added.

   ```bash
   src/backend/venv/bin/python3 scripts/backfill_quotas.py \
     --evidence "$QUOTA_BASELINE_DIRECTORY/evidence.json" \
     > "$QUOTA_BASELINE_DIRECTORY/dry-run.json"
   ```

   Default dry run rehearses the real import in one transaction and rolls back
   all rows, metering cursors/attribution, allocations and readiness markers. It
   takes locks temporarily. Exit 0 means the rehearsal reports ready; exit 2 means
   blockers/failure. `--inventory` and `--check-ready` are read-only. Without a
   manifest, useful history can still be reviewed/imported but readiness stays
   false. Recollect original live evidence if review exceeds its freshness window.

5. **Review historical coverage and blockers.** Detailed intervals win only when
   their complete disjoint coverage explains retained summaries. Exact decimal
   charges split at UTC month boundaries. An invoice/statement already fully
   represented by detail/another document is checked, not added. Legacy totals
   compare at their persisted six-decimal precision. Disjoint single-UTC-month
   project summaries can import through the shared `import_legacy_charge` service.
   Partial overlaps, unexplained totals/gaps, overlapping same-resource intervals,
   broken cursors, foreign currencies, unknown owner identities or multi-month
   summaries without detailed month evidence require manual resolution. Never
   distribute an arbitrary residual over months or treat a failed import as zero.

   Deleted-project invoices/statements retain project/owner identity without live
   project foreign keys. Existing reliable organization stamps remain intact.
   Unknown historical organization goes to migration default organization
   `dc591874-9d97-4bf5-ab31-581bb95cc645`, with migration provenance explicitly
   stating **not evidence of historical employer**. Today's reporting memberships
   or current organization never determine unknown historical attribution.
   Overlapping reporting groups do not create multiple quota parents. At a ready
   cutover, open sources split using the shared transfer/accounting interface:
   past unknown history remains default, future charges use current authoritative
   membership. Allocations use current authoritative membership too.

   Canonical project budgets and existing policies remain unchanged. Legacy
   `User.quota_overrides` are preserved and reported as blockers for explicit
   manual mapping; the importer neither erases nor silently translates them.
   New users remain unassigned; drafts do not allocate. Do not repurpose reporting
   joins to assign organizations.

6. **Apply, observe/reconcile, then deliberately enable.** Continue maintenance
   and obtain a fresh matching manifest after the review. Apply to the intended
   authorized target only:

   ```bash
   src/backend/venv/bin/python3 scripts/backfill_quotas.py --apply \
     --evidence "$QUOTA_BASELINE_DIRECTORY/evidence.json" \
     > "$QUOTA_BASELINE_DIRECTORY/apply.json"
   src/backend/venv/bin/python3 scripts/backfill_quotas.py --check-ready
   ```

   Apply can commit reliable partial history with `ready=false`; that does not
   authorize enablement. Review both `ready` and `accounting_ready`. Replaying the
   same evidence does not duplicate charges/allocations. Changed evidence for an
   imported live generation requires generation-aware reconciliation. The preflight
   rejects source-inventory drift while disabled. Inspect confirmed allocations,
   original observation times, resource/digest/image maps, generation IDs, zero
   compute for confirmed stopped projects and retained storage/slots. Observe
   providers read-only again; changes require reconciliation and a new review.

   Only after a ready preflight, explicitly configure `quotas.enabled: true` for
   every API/worker process before restarting them. Keep mutations blocked until
   their trusted provider polling/accounting is current and usage APIs agree.
   This is a separate authorized operational action. Admission/spending reject an
   unready baseline. There is no script flag to enable, dispatch, repair providers
   or automatically restart workloads.

## Normal operation and recovery

Organization/user spending uses UTC calendar months, without rollover. Project
spending is lifetime, across stops, destroy/redeploy and monthly resets. Limits
overlap; a charge is not added three times. Missing limits mean unlimited, zero
prohibits consumption, and user limits are not reserved parent slices. Lowering
CPU/RAM/project-count caps blocks growth without stopping existing environments.
Lowering physical storage caps to or below measured usage triggers shutdown as
described above. At 80%/90% of spending,
warnings deduplicate by scope, policy revision and period. At 100%, growth blocks
and durable compute shutdown is queued; metering latency can cause overshoot.

Forced budget shutdown overrides a project's pause/hibernate preference for that
stop, while preserving its saved preference. Storage and other retained billable
infrastructure continue charging. Confirmed shutdown releases CPU/RAM, not storage
or environment slots. Cleanup remains available despite cap/accounting problems.
Monthly resets or raised allowances never auto-restart compute; every project
restart requires a new admission and any lifetime cap still applies.

“Budget exceeded—shutdown pending” persists until fresh provider evidence proves
shutdown after all relevant worker fences. Failed stops retain attempt/error/next
retry state and retry with bounded backoff (30–1800 seconds). Review the existing
action/operation before retrying or repairing provider access. Do not delete its
reservation, relabel project state, free capacity after a timeout, or create a
replacement for lost/claimed work. A never-claimed pending operation may recover
under its original ID; ambiguous claimed provider calls require reconciliation.
Stop/destroy must preserve ledger/history until actual shutdown/absence is proved.

Accounting outages block growth and do not create new missing-data-only shutdowns.
A previously persisted exact monotonic exhaustion proof can continue a failed
stop only with the same membership, policy revision, exact cap and UTC period.
Transfers, policy changes and monthly reset invalidate that proof. Restore trusted
observations/rates; never turn unavailable accounting into a zero balance.

The API publishes trusted original provider snapshots to a shared Redis transport
for worker reconciliation. Its dedicated client uses 250 ms connect/read timeouts
and zero retry. The 120-second transport TTL, replay, retrieval time or another
process cannot refresh the original timestamp/identity. Missing/stale transport
retains capacity and pending shutdown state. Tests use isolated fake Redis; they
do not establish live multi-process/provider transport behavior. Validate that
behavior during a separately authorized environment rehearsal before rollout.

The enabled periodic evaluator starts immediately and repeats every 30 seconds.
It deliberately takes a globally ordered full identity inventory; this can contend
with platform-wide writes during a pass. Investigate lock waits and cycle duration
before scaling; do not bypass locks or introduce batches without preserving the
operation → user → organization → project → source order. Provider mutations run
outside database row transactions, under a separate per-project execution mutex.

Transfers are explicit administrator actions: past charges retain original
organization, current resources and future sources move at one serialized boundary,
and current-month user/lifetime project totals do not reset. Destination resources
and spending apply immediately. Optional cost center can be filled later; “Cost
center not assigned” does not block deployment and changing it does not rewrite
organization history.

## Troubleshooting and references

Read `blockers[].code`, identity and message in the report. Missing identity/rate,
unsupported provider/path, lost worker, stale observation, inventory change or
partial historical coverage stays unready until independently resolved. The tool
does not mutate a provider. Unexpected failures expose the exception class without
DSN/input values; inspect private database/operator diagnostics. A failed dry run
leaves no changes. Restore/rehearse or rerun a corrected import instead of manually
setting the baseline marker to true. Do not enable to test whether a blocker matters.

See [the approved design](../superpowers/specs/2026-11-11-organization-quotas-design.md),
[allocation paths](quota-allocation-paths.md), [deployment pipeline](deploy-pipeline.md)
and [subsystem/queue references](subsystems.md). Baseline, tests and documentation
are local implementation evidence, not production validation or rollout approval.
