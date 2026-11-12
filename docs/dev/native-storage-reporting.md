# Native RBD reporting (operator evidence only)

The native-storage GET reports `rbd_allocated_extent_bytes`: allocated image and
snapshot extents excluding replication. Provisioned capacity is a separate field.
Missing, unsupported, ambiguous or stale measurements have a null authoritative
total. `known_allocated_bytes` is a measured subset, never a capacity estimate.
Every response has `enforcement_eligible: false`. This workflow does not change
quota readiness, reservations, allocations, charges or `quotas.enabled`.

The local CLI is a privileged operator boundary. Anyone holding its DB credentials
is trusted to submit collector evidence. Operator-owned files and collector IDs
are not cryptographic signatures; there is no public report ingestion endpoint.
Do not build an enrollment manifest from an uploaded collector report.

## Explicit target and independent enrollment

Apply the reporting Alembic migration through the normal reviewed release process
before using the CLI. The commands below do not create or migrate a database.
Set `TROSHKA_NATIVE_REPORT_DATABASE_URL` securely to the chosen target database.
The CLI never falls back to the application database URL. Prefer the dedicated
environment variable over a credential-bearing command-line URL. Every command
also requires the target's stable UUID, independent of its database name or DSN.

```sh
# One-time reporting metadata configuration; target UUID cannot be overwritten.
src/backend/venv/bin/python3 scripts/import_kubevirt_storage.py \
  --target-id <target-uuid> configure-target

src/backend/venv/bin/python3 scripts/import_kubevirt_storage.py \
  --target-id <target-uuid> prepare --project-id <full-project-uuid> \
  --output enrollment-request.json
```

Prepare exports only the requested projects and their current owner/organization,
host and effective native provider tuples. Native placement uses
`Host.provider_id`; a contradictory `Project.provider_id` blocks enrollment.
The request is also recorded in the target DB. A fabricated request without that
independent preparation is rejected.

Create a separate pins document matching `VerificationPins` in
`app/schemas/kubevirt_storage_enrollment.py`. It contains a domain UUID, collector
allowlist, provider-to-cluster-UID aliases, exact project namespace/CR UIDs,
trusted cache namespace UIDs, and physical Ceph FSID/pool IDs. The domain must
cover every enrolled namespace/cluster alias sharing these physical pools. One
domain owns each physical pool, including pools shared by multiple Kubernetes
clusters. No second domain can enroll an overlapping pool.

Create an operator-owned backend configuration JSON array. Every object specifies
`ceph_fsid`, `pools` (the collector's `PoolConfig` schema), and one transport mode.
The configured physical FSID/pool IDs must exactly cover the pins document;
duplicate FSID configurations are rejected. Replace all example identities and
paths below with independently checked operator values. These examples contain
no credentials.

Direct mode requires both local paths to an independently provisioned read-only
Ceph identity and local Ceph binaries. Existing direct documents that omit `mode`
remain accepted. Missing paths never fall back to ambient Ceph credentials.

```json
[
  {
    "mode": "direct",
    "ceph_fsid": "00000000-0000-4000-8000-000000000001",
    "pools": [
      {"pool_id": 1, "name": "rbd-pool", "csi_cluster_id": "openshift-storage"}
    ],
    "ceph_conf": "/operator/ceph/ceph.conf",
    "ceph_keyring": "/operator/ceph/read-only.keyring"
  }
]
```

If the workstation has no local Ceph identity/binaries, toolbox mode reuses an
**existing** `rook-ceph-tools-*` pod in `openshift-storage`. The operator must
already hold access to GET the pinned pod and `kube-system` namespace and exec
into that pod with local `oc`. Use an explicit absolute kubeconfig path; there is
no default kubeconfig or in-cluster fallback. Both verification and collection
require a bounded operator-owned regular kubeconfig file without symlinks or
group/other write permissions. Only static authentication is supported (for
example, an existing token or client certificate). Any `exec` or `auth-provider`
entry, including in an unused user, is rejected before loading authentication,
creating API clients or running commands. Existing auth helpers and refreshing
provider credentials are unsupported; the workflow does not run a helper or
create replacement credentials. Python loads the same parsed configuration that
was validated. `oc` reads the explicit validated path, so the trusted operator
must keep that file unchanged throughout verification/collection. Pin the cluster's `kube-system` UID,
pod UID, namespace, container, FSID and pool name allowlist. All toolbox fields
shown below are required, including namespace and container. The FSID must match
the outer backend FSID, and the allowlist must exactly match configured pool
names. Direct credential paths are forbidden in toolbox mode.

```json
[
  {
    "mode": "toolbox",
    "ceph_fsid": "00000000-0000-4000-8000-000000000001",
    "pools": [
      {"pool_id": 1, "name": "rbd-pool", "csi_cluster_id": "openshift-storage"}
    ],
    "kubeconfig": "/operator/kubeconfigs/storage-cluster.yaml",
    "toolbox": {
      "cluster_key": "00000000-0000-4000-8000-000000000002",
      "namespace": "openshift-storage",
      "pod_name": "rook-ceph-tools-example",
      "pod_uid": "00000000-0000-4000-8000-000000000003",
      "container": "rook-ceph-tools",
      "ceph_fsid": "00000000-0000-4000-8000-000000000001",
      "allowed_pools": ["rbd-pool"]
    }
  }
]
```

Toolbox verification checks the cluster/pod/container pins before reads and
rechecks them before each fixed Ceph operation. It first checks the existing
container's `timeout --kill-after` capability; unsupported timeout behavior
blocks verification. The only Ceph operations are `ceph fsid` and
`ceph osd lspools --format json`, with numeric pool IDs checked against the
configuration and independent pins. No image measurement is needed. This
operator-only transport creates no pod, credential, RBAC or application exec
capability. The existing toolbox identity must already meet the operator's
read-only access requirements; this workflow does not provision credentials.

```sh
src/backend/venv/bin/python3 scripts/import_kubevirt_storage.py \
  --target-id <target-uuid> verify --request enrollment-request.json \
  --pins enrollment-pins.json --backend-config backend-identities.json \
  --verification-timeout-seconds 600 --command-timeout-seconds 15 \
  --output verified-enrollment.json

src/backend/venv/bin/python3 scripts/import_kubevirt_storage.py \
  --target-id <target-uuid> enroll --enrollment verified-enrollment.json
```

Verify performs bounded Kubernetes GET/list requests using the target provider's
configured API identity: kube-system UID, namespace UID, exact TroshkaProject UID
and full `spec.projectId`. An eight-character prefix never establishes ownership.
It independently checks configured Ceph FSIDs and pool IDs through read-only
transport. Verify never reads a collector report. Enrollment then locks current
user → organization → project identities, rechecks the prepared tuples and
`updated_at`, and persists the immutable enrollment digest/version and roots.
Changes while preparing/verifying require fresh preparation. Ordinary report
imports do not compare project `updated_at`; display/topology writes do not alter
immutable namespace ownership.

Verification shares one finite deadline across provider API reads, toolbox
identity reads, startup checks and Ceph commands. The default budget is 600
seconds (allowed 1–7200); the command budget defaults to 15 seconds (allowed
1–3600). Kubernetes reads also retain the collector's 15-second maximum.
Requests and commands are not retried, all Kubernetes clients close on success
or failure, and failures omit raw client errors, command stderr and credentials.
Project-root ownership always uses the independently prepared target database
request and the corresponding provider's configured API credentials, including
when the toolbox is in another cluster. The operator kubeconfig cannot substitute
for missing provider credentials. `verify` writes only the private output file;
it does not enroll roots or write resource, quota or billing records.

## Import and read behavior

After separately collecting evidence with the enrolled domain/collector identity:

```sh
src/backend/venv/bin/python3 scripts/import_kubevirt_storage.py \
  --target-id <target-uuid> import --report native-report.json \
  --enrollment-id <enrollment-uuid> --enrollment-digest <verified-digest>
```

Inputs must be bounded regular files owned by the operator, without group/other
write permission; symlinks are refused. Failures sanitize credentials and DSNs.
Import invokes no collector or cluster clients. It reconstructs normalized graph
attribution instead of trusting report aggregate amounts or ownership claims.
New bindings must belong to an enrolled namespace and complete original discovery.
Old immutable root and PVC/backend identities survive live object deletion.

Every accepted attempt remains append-only. Changed same-ID replay is rejected;
identical replay preserves original timestamps. A domain's newest **source start**
selects one coherent graph. A later partial attempt or omitted root invalidates
older complete totals. Late imports of older scans cannot revive them. Conflicting
reports with the same source start make the domain unavailable until an explicitly
later report. Unknown/conflicting roots or backend pools remain stored for admin
inspection, without ownership promotion or complete current usage.

`inventory_blockers` preserves the original inventory digest separately from
measurement/final-check errors. All errors still block complete usage. Legacy
reports without that field are accepted only when their digest can be exactly
reconstructed using `collection_errors`. Legacy partial reports whose late errors
make that impossible are rejected as unsupported legacy inventory evidence;
collect a new report. Unresolved CSI snapshot evidence participates in digest
verification and remains a completeness blocker.

`GET /api/v1/quotas/{project|user|organization}/{id}/native-storage` performs no
collection or accounting writes and works while the quota baseline is unready.
It reloads the actor's role/access, shares, current ownership and organization
membership in one read-only repeatable-read snapshot. Revocation committed before
the snapshot is honored; a concurrent transfer cannot mix versions. Separate
connections leave caller transactions untouched. An in-memory/shared single-
connection SQLite setup is deliberately refused because it cannot provide that
boundary. Source evidence older than 15 minutes is stale, regardless of import
time. Aggregate overflow beyond signed int64 leaves the authoritative amount
null with `aggregation_overflow`; the known field remains a bounded subset.

Project shares authorize project-only reads. Non-admin output includes no graph
identities, other-project breakdowns or platform-pool fields. Admin-only
`platform_bytes`, `unattributed_retained_projects` and
`unattributed_retained_known_bytes` are diagnostics across the selected domains
(including all retained orphan domains). They are **not** scope-attributed amounts
and must not be added to a scope total.

Deleted projects return 404 at project scope. Without a trusted deletion-time
owner snapshot, retained storage is admin-only unresolved inventory. User and
organization totals remain null with `retained_attribution_unavailable` whenever
such ambiguity exists; the known live subset remains available. This bounded
phase uses a conservative global blocker because an import-time historical owner
may have transferred the project before deletion. A future trusted deletion hook
can narrow this, but is not part of this reporting implementation.
