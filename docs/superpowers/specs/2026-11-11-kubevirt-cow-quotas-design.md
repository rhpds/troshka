# Native KubeVirt COW allocation accounting and quota integration

Date: 2026-11-11. Status: implementation design following the user's instruction
to continue with KubeVirt. No production change or enablement is implied by this
document. Native accounting/reporting is the first deliverable; native enforcement
is a later, separately verifiable milestone of the same work.

## Outcome and boundaries

Show native KubeVirt projects' allocated RBD extents, including all namespace PVCs
and retained storage, without charging virtual disk capacity as actual allocation.
Build the native lifecycle adapter needed to apply organization → owner → project
quotas to the actual workload mix: VMs, TPM, pod volumes, installer/Showroom and
Ceph-appliance storage. Reuse existing durable quota accounting and history.

The first release is read-only reporting. It MUST NOT alter quota allocations,
reservations, readiness, baseline versions, admission, enforcement settings or
billing. Complete reporting coverage is not enforcement readiness. Native growth
guards remain until native lifecycle, operator fencing, overhead and migration
tests pass. No allowlist-only implementation is acceptable.

Alternatives considered: PVC capacity is readily available but measures a
different quantity; guest filesystem statistics omit stopped volumes and backend
sharing. Synchronous RBD scans on admission block requests and fail the latency
contract. A bounded asynchronous, privileged-boundary-separated collector plus a
pure ownership graph is the selected approach.

## Global constraints

- `quotas.enabled` remains false during reporting development and rollout.
- Metric name is `rbd_allocated_extent_bytes`; UI label is `Allocated RBD storage`.
- The metric excludes replication/erasure-coding overhead and is not raw physical device consumption.
- No PVC-capacity, guest-used, missing-value-as-zero, or namespace-name-only fallback is permitted.
- Reporting never writes quota reservations, allocations, readiness, spending or billing history.
- All stored byte quantities are nonnegative signed-int64 integers; JSON UI transport preserves exact integers.
- Collection never executes in an HTTP request, admission transaction or quota-lock critical section.
- Collector mutations, general shell execution and general application pods/exec privileges are prohibited.
- Unknown ownership, incomplete inventory, stale evidence and unsupported backend semantics remain explicit.
- Stop preserves storage; release requires independently confirmed backend absence after worker/controller fencing.
- Run Python commands with `python3`; never add Co-Authored-By trailers.

## Existing evidence and integration points

The operator creates S3 goldens in `troshka-cache`, per-VM clone DataVolumes and
blank PVCs (`src/operator/helpers/kubevirt.py:614-696`). Those builders currently
omit volumeMode, while the actual inventory contains Filesystem and Block volumes.
Collect observed mode rather than assuming either. Native identity is available
from TroshkaProject `spec.projectId` (full UUID); namespace labels currently use
only its first eight characters. A namespace name/label is therefore a discovery
hint, never ownership proof.

Read-only production investigation found 124 Troshka PVCs across two RBD classes,
including TPM, pods and Ceph-appliance storage. One 300 GiB provisioned project
image had approximately 218 GiB allocated extents and an internal CSI temporary
parent whose parent maps to a golden cache PVC. That temporary parent measured
zero own allocation after a query taking minutes without fast-diff. These are
sample observations, not complete production accounting. Zero is valid only after
a successful, identity-verified complete measurement; timeout is not zero.

The current provider's `ceph df` probe is cluster-wide display data, not per-image
ownership evidence. Existing `quota_storage.py` is local filesystem/inode-specific;
`ws_pubsub._quota_vm_states` publishes only nested raw states; native destroy
returns through a separate branch before generic quota completion. Existing
operator timers/child reconcilers can outlive backend workers. All require native
integration before enforcement, not just a provider type allowlist.

## Measurement and ownership contract

`kubevirt-rbd-allocated-v1` measures logical allocated RBD extents for a unique
image including its retained snapshots, excluding shared ancestor extents that
belong to another image. Use the JSON result of per-image
`rbd du --merge-snapshots` under a tested command/version capability. Store one
aggregate per image; never add its snapshot rows a second time. Snapshot IDs and
ancestry remain separate lifecycle/ownership evidence. A parser rejects missing,
multiple unexpected or identity-mismatched aggregates. Validate this quantity on
the actual supported Ceph version with a clone/snapshot integration experiment
before presenting a complete report. No claim of byte-exact raw device usage is
made. Ceph documents both snapshot merging and slow scans without fast-diff:
[official rbd manual](https://docs.ceph.com/en/squid/man/8/rbd/).

Canonical identities:

```text
KubernetesClusterKey = kube-system Namespace UID
PoolKey = (Ceph FSID, numeric pool ID)
ImageKey = (Ceph FSID, numeric pool ID, RBD namespace, immutable image ID)
SnapshotKey = (ImageKey, immutable snapshot ID)
ProjectRoot = (full project UUID, KubernetesClusterKey, namespace UID,
               TroshkaProject UID, deployment generation or null)
VolumeBinding = (ProjectRoot, PVC UID, PV UID, CSI driver, volumeHandle, ImageKey)
```

Provider/host rows and API URLs are aliases of these identities, not pool keys.
Verify PV claimRef UID matches PVC UID, CSI mapping agrees with authoritative
image metadata/CSI journal or supported volume-handle decoder, and backend image
ID remains stable before/after measurement. A suffix/name heuristic alone cannot
establish a volume binding. Driver-specific CSI mapping is isolated and rejects
unrecognized layouts. Kubernetes cluster UID and Ceph FSID are different fields.

Discovery lists ALL PVCs in each verified project namespace, including unattached,
stopped, terminating, pod, TPM, installer, appliance and scratch PVCs, independent
of topology. List PVs, VolumeSnapshots and VolumeSnapshotContents, and persisted
retained bindings. Pending PVCs are pending/unmeasured, not allocated zero. A
namespace lacking its CR can use an earlier verified namespace-UID binding; a
new namespace with only a matching prefix is unresolved. Detect duplicate full
project roots and prefix collisions. Record owner-reference chains when present,
but absence of a VM owner reference does not omit a namespace PVC.

Walk parent image/snapshot links with cycle, depth and object-count bounds. Also
inventory child references needed to establish whether private ancestors are
shared beyond the project's descendants. A disconnected backend image associated
with a retained binding is not dropped because its PVC disappeared. Unknown
non-Troshka consumers of a private ancestor create an ownership blocker.

Attribution rules are deterministic:

1. A golden is platform-owned only through an explicitly configured trusted cache
   namespace UID plus verified PVC/PV/backend identity, or its retained verified
   cache binding. A `golden-` name or empty image metadata proves nothing.
2. Traverse the graph per project. Unique non-cache nodes reachable exclusively
   by that project are project-owned, including CSI temporary intermediates.
3. A non-cache node shared by multiple projects/unknown consumers is unresolved;
   report its measured bytes once in an unattributed pool bucket. Do not split
   arbitrarily or duplicate those bytes into each project.
4. Retained image/snapshot identity keeps its established ownership after PVC/CR
   deletion. A conflicting new consumer invalidates exclusive attribution.
5. Aggregate by canonical ImageKey. All pool/project/cache totals deduplicate
   aliases; provisioned PVC capacity is a separate sum of unique bound PVCs.

Project total is nullable if any required binding, ancestor, snapshot coverage,
measurement or exclusive ownership is unresolved. `known_allocated_bytes` may
show a clearly labeled measured subset, never a quota-authoritative total. A
platform pool total is visible only to platform admins; members see their own
project amount and a statement that shared bases are excluded, not other tenants'
volume names, graph edges, amounts or provider credentials.

## Collector and evidence schema

Create focused modules under `app/schemas/kubevirt_storage.py` and
`app/services/kubevirt_storage_{inventory,graph,collector}.py`, a constrained
read-only transport module `kubevirt_storage_transport.py`, and
`scripts/collect_kubevirt_storage.py`. Pure graph evaluation consumes normalized
objects, not Kubernetes clients or subprocess handles.

```python
class CollectionLimits(BaseModel):
    command_timeout_seconds: int = 15
    scan_timeout_seconds: int = 600
    max_objects: int = 10000
    max_parent_depth: int = 32
    max_output_bytes: int = 16 * 1024 * 1024
    max_parallel_commands: int = 1

def discover_inventory(kube_reader, retained_bindings, roots) -> NativeInventory: ...
def collect_report(inventory: NativeInventory, rbd_reader, *, limits: CollectionLimits,
                   clock) -> NativeStorageReport: ...
def attribute_graph(inventory: NativeInventory,
                    images: list[ImageObservation]) -> AttributionResult: ...
```

The limits are configurable within validated bounded ranges. Slow no-fast-diff
images can use a dedicated operator scan with a larger explicit timeout; default
service collection remains bounded. Killing a local read-only query on timeout
does not justify retry storms. No automatic enable-fast-diff, flatten, mutation,
image delete, snapshot purge, filesystem mounting or guest execution is allowed.

The read-only CLI takes `--inventory`, `--kubeconfig`, `--transport direct|toolbox`,
`--ceph-conf`, `--ceph-keyring` (direct mode), `--output`, optional
`--previous-report`, and explicit limits.
Inventory is an operator-controlled manifest of full project/cluster bindings and
trusted cache namespace UID. Kubernetes discovery validates it against live CRs.
Use direct local `ceph`/`rbd` read-only subcommands with argv arrays and a fixed
allowlist (FSID/pool metadata, info, du, snapshot/child/trash lists and required
CSI identity reads); no shell, user-supplied executable or arbitrary command.
Credentials stay in configured files and never enter JSON/stderr reports.
Kubernetes client RBAC is get/list only for required discovery objects. Storage
credentials are externally provisioned with read-only capabilities for allowed
pools; prove the identity cannot write during deployment validation, without
attempting destructive commands. Lack of readable CSI identity is a blocker.

For laptops without Ceph binaries, the standalone operator CLI can select an
existing toolbox transport. Its operator-controlled manifest pins cluster UID,
namespace `openshift-storage`, the existing `rook-ceph-tools` workload/pod UID and
container, permitted pools and the expected Ceph FSID. Resolve and verify the
current pod UID before dispatch; replacement invalidates the connection. Execute
only the same fixed read-only argv allowlist through `oc --kubeconfig=... exec`,
with remote `timeout --kill-after=2s <deadline>s <fixed-command> ...` and a slightly
larger local process-group deadline. Verify remote timeout support first; absent
support disables remote collection. Kill/reap local process descendants on
timeout and report failure; remote timeout prevents an abandoned long-running du.
Never pass `sh -c`, arbitrary executable/namespace, user command strings or raw
stderr into this interface. Existing operator credentials may authorize exec;
this does not grant exec or storage-admin credentials to the Troshka application.
Do not create a toolbox pod, key, secret or role. Direct transport remains default.

Report fields (Pydantic extra-forbid; bounded arrays/text):

```text
version: "kubevirt-rbd-allocated-v1"
metric: "rbd_allocated_extent_bytes"
report_id: UUID; collector_id: configured stable ID
started_at, completed_at: UTC ISO times
inventory_digest: SHA256 of normalized identities, never secret data
cluster_keys[], pool_keys[], project_roots[], volume_bindings[]
images[]: key, parent snapshot key|null, snapshots[], collected_at,
          allocated_bytes|null, provisioned_bytes, measurement_status,
          command_version, measurement_semantics_version
projects[]: project_id, namespace_uid, allocated_bytes|null,
            known_allocated_bytes, provisioned_bytes|null,
            measured_images, required_images, oldest_observed_at|null,
            coverage: "complete"|"partial"|"unavailable", blockers[]
platform_pools[]: pool_key, allocated_bytes|null, known_allocated_bytes
unattributed_images[]; retained_bindings[]; collection_errors[]
enforcement_eligible: false
```

Graph identity is rechecked after collection; changed ancestry/bindings invalidate
affected totals. Report coverage includes observation span and earliest source
timestamp. Running writes make this an asynchronous observation, not an atomic
cluster snapshot. For reporting, complete evidence becomes stale after a default
15 minutes; display age and preserve last-known values with their original time.
The reporting stale threshold does not change the current 60-second enforcement
freshness contract. Successful zero-byte measurements are distinct from null.

Exit codes: 0 complete report, 2 partial/unavailable report saved, 1 invalid
configuration/transport initialization with no usable report. Persist JSON with
0600 permissions via temporary-file + atomic rename. Accept no arbitrary provider
stdout or secret-bearing manifests into the report. First task needs no database.

## Persistence, API and UI milestone

Add an append-only `native_storage_reports` table with UUID, collector ID,
inventory digest, original collection timestamps, report JSON and imported_at;
unique report ID/hash prevents edited replay. Add a retained-binding table keyed
by canonical backend identity and full ownership root, without delete cascades
from project/provider rows. Imports never rewrite historical reports. Separate
operator CLI imports a locally trusted report into an explicitly supplied target
database; no public upload endpoint and no application Ceph credentials. Reject
same-ID changed payload and out-of-order replacement of latest evidence. Import
ownership must agree with current/retained DB provider/project identity; raw JSON
is not trusted merely because it says `complete`.

Expose `GET /api/v1/quotas/{scope}/{scope_id}/native-storage` with authorization
through `authorize_policy(..., lock=False)`. API only reads reporting tables and
authoritative live membership; it never scans a cluster. Return aggregate
`NativeStorageUsage` with status `complete|partial|stale|unavailable`, nullable
allocated/provisioned totals, known measured subset, observation time, coverage,
safe reason codes and `enforcement_eligible: false`. Resolve current scope
membership at request time; project shares do not authorize owner/organization
totals. Detailed backend image IDs and pool-wide cache amounts require admin.
Mixed-provider scopes return native-only covered-project counts and do not claim
that this report replaces total organization quota usage.

Add a separate `NativeStorageUsage` component beside `QuotaUsage` through
`QuotaScope`, with independent loading/error state, exact GiB formatting and
permission revocation handling. Display “Allocated RBD storage”, “Provisioned PVC
capacity”, original observation time, coverage and “Reporting only; native quota
enforcement unavailable”. Explain that replication overhead is excluded. Partial
values say “Measured subset”; stale values say “Last measured”. Never relabel the
existing `used.storage_bytes` or `baseline_ready`. A missing quota baseline must
not prevent this new report from loading.

## Native lifecycle/enforcement milestone

Introduce adapter dispatch for plan/demand, bind, provider intent, observation,
settlement, stop and confirmed absence. Preserve nested adapter behavior. Native
plan identity derives from the normalized final execution plan, not mutable editor
topology. Include all generated persistent storage and maximum concurrent compute:
VMs plus virt-launcher overhead, DHCP/gateway/exec, CDI, TPM, installer/recert,
Showroom/pods and Ceph-appliance jobs. Classify shared platform services explicitly.
Round aggregate helper CPU upward to existing whole-vCPU quota units; preserve
underlying millicore evidence. RAM is byte-exact. Do not change billing rates.

Generation authorization is persisted in TroshkaProject spec and propagated to
children. It includes operation ID, execution digest, immutable generation and
desired power/growth state. An operator fence is enforced at every growth dispatch,
including retries, timers, BMC, capture/file-pull, PVC resize, recovery, installer
and pod recreation. A local flag alone is insufficient: the generation's shared
dispatch coordinator serializes authorization checks with fence acknowledgment,
tracks accepted in-flight intents, and prevents new side effects after the fence
acknowledges. Previously accepted API requests are reconciled by UID/resourceVersion
before declaring settled. Cross-process coordination must be durable (Kubernetes
Lease/CAS plus status intents), not an in-memory mutex. API timeout remains an
ambiguous original intent, never permission to replace a CR or retry growth.

Native observations directly list VM/VMI/pods/PVCs with retained UID binding.
runStrategy or CR phase alone is not actual compute state. Stop halts VMIs and
scales/suspends all owned writing workloads while preserving PVCs and the project
slot; paused VMIs retain compute. Partial start retains full reserved compute
until per-resource confirmation can safely lower it. Native destroy feeds generic
ledger/meter finalization only after fenced controllers and confirmed backend
absence, including retained PVs/snapshots/trash. Missing provider is unavailable.

Allocation increments can charge observed growth above reservations. Decreases
require settled generation and fresh complete evidence. Pool locks use Ceph
FSID/pool ID across aliases; use pool capacity metrics with compatible units.
Native metadata/import allowances must come from tested execution profiles;
unbounded full-copy/preallocation modes stay denied. Shared-cache cold imports
require a platform-owned reservation. Pool headroom is a separate backend safety
decision and must not mix replicated raw free bytes with logical project extents.

Current 60-second freshness cannot be assumed achievable for native graph scans.
Until bounded evidence meets the enforcement freshness contract, native
enforcement remains unavailable. A different explicit native freshness policy
requires measured collection/stop latency and an operating reserve sized for
worst observed write growth; never silently reuse reporting's 15-minute age.

## Baseline, migration and completion

Baseline v3 must independently collect native resource and storage evidence under
the existing maintenance writer fence plus native controller fencing. Enumerate
actual mixed Filesystem/Block, TPM, pod, installer, Showroom and appliance storage.
Retain old allocation and billing identity; do not reinterpret allocated-v1 local
filesystem values or virtual bytes. Versioned metric metadata distinguishes
logical RBD extents from filesystem blocks. Existing unsupported snapshot/capture,
cross-project sharing or EIP resource paths remain explicit blockers until their
adapter passes tests. Never omit live unsupported resources to declare readiness.

Completion gates are separate: (A) reporting collector + deterministic graph
tests, (B) persisted authorized API/UI with real sampled evidence, (C) native
operation/operator fence and overhead lifecycle suites, (D) fenced migration
rehearsal and complete current inventory, (E) deliberately authorized production
enablement. A and B provide usable output while C–E are unfinished.
