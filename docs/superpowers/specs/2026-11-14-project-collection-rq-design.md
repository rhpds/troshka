# Project collection in the existing RQ system

## Approved direction

Use Troshka's existing Redis, RQ, backend image, and worker management for
project-level native storage collection. Do not create a collector process per
project or introduce another queue system. Bound provider load, prioritize
cleanup, and preserve capacity for deploy/start/stop jobs. Preserve project
ownership, topology, images, patterns, and existing billing history.

This is an architectural change to the current domain-wide observation contract,
not just moving the CLI invocation into a queue. This document specifies the
implementation; it does not claim that project collection is already deployed.

## Current constraints verified in source

- `native_managed_collector.cycle` collects and reconciles a whole domain.
- `NativeCollectionDomain.latest_report_id` selects the latest attempt for all
  projects in that domain, including failed attempts.
- Report import normalizes against every enrolled root. A single-project report
  currently makes other roots missing; submitting it unchanged is incorrect.
- `quota_native`, cleanup settlement, trash removal, and usage reporting consume
  the domain report pointer. All must agree on scoped report selection.
- `enqueue_job` has a daemon-thread fallback and updates `job:project:<id>`.
  Collection must not use either behavior: it must not bypass distributed limits
  or overwrite a deployment's active-job tracking.
- Existing workers consume `project_lifecycle`, `host_lifecycle`, and `default`.
  RQ jobs are non-preemptive. Queue ordering alone cannot reserve capacity once
  all workers are running scans.
- The current acceptance collector uses operator-owned local configuration and
  kubeconfig files. Those laptop paths are not a deployable worker configuration.

## Queue and worker arrangement

Add `native_cleanup` and `native_collection` queues to the same Redis/RQ system.
Collection-capable workers consume cleanup first, then routine collection.
Lifecycle workers retain their current queues and do not consume storage scans.
Use the same backend image and worker entrypoint, with an explicit queue profile.
There is no second worker implementation or separate Redis service.

Initially reserve one collection worker and preserve the existing lifecycle
worker capacity. Represent both profiles in the existing worker capacity and
database-connection budget, rather than adding replicas outside those controls.
This may require a separate Deployment for the collection profile, but it is
part of the same RQ worker system. Only collection-profile pods receive the
protected collector configuration and storage transport credentials.

A lightweight periodic dispatcher enqueues due project IDs and domain IDs.
Multiple API replicas may run the dispatcher; an atomic Redis scheduling lease
and stable job identities prevent duplicate scheduling. No HTTP request waits
for collection. When Redis is unavailable, dispatch stops and accounting ages
naturally; it does not start an unbounded local thread.

Routine collection initially targets a 60-second interval with deterministic
jitter. This is a scheduling target, not a promised observation latency. Keep the
existing evidence freshness ceiling until load tests justify a change. Cleanup
work is due immediately and runs ahead of routine jobs. Avoid duplicate pending
jobs for the same project; re-evaluate cleanup priority when a job starts.

## Distributed limits and recovery

Use a project execution lease and provider-wide bounded execution slots. Start
with two slots per provider, configurable by administrators. A one-worker dev
deployment naturally uses at most one slot. Use the physical backend identity
for shared backend work so provider aliases cannot bypass the storage limit.

Workers that cannot obtain a slot defer the job; they do not wait while holding
an RQ worker. Leases carry unique owner tokens, expire after the bounded job
deadline plus a termination margin, and release only by token comparison. Late
or superseded jobs cannot publish or settle cleanup. Network subprocesses retain
their existing deadlines. Recovery after worker loss must preserve old charges
until a new observation proves release is safe.

Queue payloads contain identifiers only. Resolve current ownership, enrollment,
provider identity, and protected transport configuration when execution begins.
Never enqueue credentials or authorization envelopes supplied by a project user.

## Project-scoped report contract

Introduce an explicit project scope on native reports and durable latest-attempt
state keyed by domain and enrolled project-root identity. Keep the existing
domain report path for shared inventory and compatibility. A project report may
advance only its project's attempt pointer; it cannot become the domain report.

Project imports validate the exact enrolled root, deployment generation, bound
volumes, retained disks, and relevant retiring intent. Reject another project's
bindings or cleanup proof. Shared cache and ancestry evidence must reference an
authorized shared observation and preserve its original timestamps.

Track failed attempts per project as well as successful ones. A failed scan must
invalidate that project's readiness rather than quietly continue using an older
successful report. Unrelated projects retain their own latest evidence. Same-time
conflicts, replay checks, stale evidence, changed enrollment, and replacement UIDs
remain fail-closed. Never relabel copied old measurements with the new job time.

Usage projections, quota measurement, settlement, and scoped trash authorization
must all use the same report-selection helper. User and organization totals sum
project measurements and remain explicitly incomplete if any required project
is stale or unavailable. Shared platform bytes must not be counted once per
project. Ownership transfers continue to use current database membership.

## Shared storage evidence

A coordinated domain job discovers shared caches, backend identities, and pool
inventory. Project jobs collect their own disks and the ancestry required for
attribution. Cache immutable identities and reuse fresh shared observations;
do not cache private disk usage or disappearance proofs as if freshly measured.

Mutable ancestry must be rechecked where required for attribution. A job cannot
assume a cached parent/child relationship still holds across a clone or deletion.
Shared observations have their own freshness and identity checks. If shared
evidence is unavailable, affected projects retain charges and cannot claim a
fresh complete measurement. Avoid traversing every sibling clone merely because
projects share a base image; preserve exact ownership checks for private images.

New-root enrollment, image-source verification, and clone-lineage publication
remain coordinated domain operations. They must complete before project jobs
can publish measurements against that enrollment version.

## Cleanup and accounting

Cleanup runs independently of whether quota enforcement is enabled. It continues
to use durable intent records and exact original resource identities. Give
cleanup priority within the collection worker profile, without letting it
consume lifecycle-only workers.

Do not release retained storage charges on an accepted Kubernetes DELETE, a
missing PVC, or a successful trash-removal command. Release only after fresh
evidence confirms the project's private images are absent from both active
storage and trash. Preserve accrued spending and close meters using observation
timestamps. Shared-pool trash is never purged.

Separate cleanup reconciliation from routine measurement so a slow deletion does
not extend a normal project's collection job. Keep cleanup receipts sufficient
to resume after a worker restart without repeating an ambiguous destructive
operation. After retirement, remove the project from routine scheduling only
when settlement is durable.

## Configuration, rollout, and rollback

Store collector enrollment/configuration as administrator-managed mounted
configuration and transport credentials as Secrets. Validate the reporting
target, provider, backend FSID, toolbox identity, and pool allowlist before
enabling dispatch. Do not copy the laptop's admin kubeconfig into general
lifecycle workers or expose transport credentials in job metadata/logs.

Use additive migrations for project attempt state and optional report scope.
Preserve existing reports and domain-wide readers while the new dispatcher is
disabled. A project without a scoped attempt may use the current valid domain
report. Once it has a scoped attempt, a failed newer attempt cannot be bypassed
through that compatibility fallback. A deliberate, complete domain observation
can advance project state only through the same ordering and identity checks.

Enable in the persistent ocpvdev01 dev app first. Stop the temporary manual
collector before enabling the durable dispatcher. Verify no duplicate authority
or overlapping collection. No production rollout is part of this design step.

Rollback disables dispatch and drains bounded jobs before resuming the existing
domain collector. Preserve additive tables and all cleanup/accounting evidence.
Publish a new complete domain observation before relying on domain freshness;
do not reset attempt pointers to resurrect old evidence. Incorporate temporary
source overlays into the eventual normal image and remove them after runtime
hash verification.

## Required verification

1. Two projects in one domain publish independently; failure or late completion
   in one cannot replace or invalidate the other's report.
2. Forged scope, foreign binding, obsolete enrollment, replacement root UID,
   conflicting attempt time, and stale shared evidence are rejected.
3. Concurrent dispatchers produce one pending job per project. Redis failure
   never starts a thread. Collection does not modify lifecycle job references.
4. Project/provider leases respect concurrency, worker death, lease expiry,
   ownership-token release, and late completion fencing.
5. Existing lifecycle worker capacity remains available while collection is busy;
   cleanup is selected before queued routine collection.
6. CPU/RAM, COW physical storage, retained storage after stop, accrued spending,
   user/org aggregation, and transfers retain their accounting semantics.
7. Cleanup with quotas enabled and disabled preserves shared images, confirms
   active/trash absence, and releases charges only after complete settlement.
8. Exercise hundreds of synthetic projects with multiple disks and shared
   ancestors. Measure jobs queued, duplicate jobs, command counts, database lock
   time, oldest observation age, and lifecycle queue wait under collection load.
   Synthetic results do not substitute for live Ceph performance evidence.
9. In dev, repeat a real-image lifecycle through the Troshka API: deploy, guest
   writes, measured growth, overage stop, restart, and cleanup. Verify scheduler
   recovery across a worker restart and preserved project ownership/topology.

## Implementation sequence

1. Add scoped report schema/state and one report-selection helper with regression
   tests; keep scheduling disabled.
2. Update import, usage, quota measurement, cleanup, and trash consumers together.
3. Extract the protected collection runtime from the operator CLI into reusable
   services; add project scope and coordinated shared observations.
4. Add RQ jobs, dispatch, leases, queue profiles, configuration, and worker-budget
   integration with focused concurrency/recovery tests.
5. Run accounting/cleanup regression suites and synthetic scaling checks.
6. Roll out to dev, validate the mounted configuration, enable dispatch, and run
   live API acceptance. Report remaining capacity limits before production.
