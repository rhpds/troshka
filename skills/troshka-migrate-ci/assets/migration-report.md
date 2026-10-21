# CI migration report

This is an authoring template. Replace instructions with evidence before delivery;
do not mark a check passed without executing or observing it.

## Request and sources

Record requested mode, CI path/stage, AgnosticV revision/dirty state, framework
generation/config/revision, EE image, collection refs/resolved SHAs, Troshka and
provider collection revisions, and Showroom content/config revisions. Identify
inferred settings and inaccessible sources. Do not include secret values.

## Ownership and feasibility

State dedicated, shared tenant, or hybrid; overall candidate/partial/blocked;
reasons and mandatory blockers. Identify cluster/pool ownership, tenant allocation
and cleanup boundaries where relevant. Explain retained curriculum dependencies.

## Dependency decisions

| Dependency / consumer | Source revision:path:line | Status | Replacement / adaptation | Owner / phase | Acceptance check |
| --- | --- | --- | --- | --- | --- |

Use reusable/infra_mapping/workload_adaptation/external_required/unresolved.

## Workload and Showroom phase ownership

| Role / service and source | Build / post-restore / tenant provision / remove | Executor | Inputs / outputs | Replay and cleanup behavior |
| --- | --- | --- | --- | --- |

Describe GUID-use replacements, TLS trust paths, runtime identity/secrets,
tenant uniqueness, and cluster-scoped mutations. Include Showroom content
preservation and author decisions about changed lessons.

## Generated artifacts

| Path | Purpose / consuming interface | Source basis | Static validation | Remaining runtime bindings |
| --- | --- | --- | --- | --- |

Include standalone template versus catalog adapter differences. For tenant-only
CIs explain why no cluster template/pattern is produced. List new CI metadata,
related_files/includes, stage behavior, workload/content pins, and runtime access
bindings. Link adaptation diffs. Keep production stages non-publishable until
their required artifact and capability checks are satisfied.

## Validation evidence

| Check | Evidence class | Result | Command / observation and artifact fingerprint |
| --- | --- | --- | --- |

Results: passed/failed/unverified/not_applicable. Classes: source review, static
check, offline exercise, live runtime. Cover dev readiness, application behavior,
Showroom terminals/consoles/links, fresh credentials, TLS trust, source-project
independence, retry, and supported lifecycle. For dedicated projects include fresh
pattern restore and content identity. For tenants include two allocations, unique
ownership, privilege boundaries, and cleanup without cluster destruction.

## Promotion and next action

Record validated pattern identity and disk/topology evidence for owned
infrastructure, or pinned workload/content/allocator revisions for shared tenants.
Show test/prod/optional event differences. List unresolved capabilities and exact
next actions; do not present generated files as deployed. Point to secret-free
external state/evidence locations without exposing token-bearing URLs.
